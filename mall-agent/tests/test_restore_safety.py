from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row


def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "deployment"))
    from backup_archive import BackupError
    from restore_safety import apply_deletions, freeze_runtime

    return BackupError, apply_deletions, freeze_runtime


def connect(config):
    url = config["migrationUrl"].replace("postgresql+psycopg://", "postgresql://")
    assert url.endswith("/mall_agent_architecture_test")
    return psycopg.connect(url, row_factory=dict_row)


def empty_journal():
    return {
        "tombstones": [],
        "deleted_conversations": [],
        "deleted_messages": [],
        "deleted_attachments": [],
        "protected_refs": [],
    }


def test_newer_erasure_overlays_an_older_visible_snapshot_and_never_reexecutes_business(
    integration_config, monkeypatch
):
    _, apply_deletions, freeze_runtime = modules(monkeypatch)
    cid, mid, rid, action, attachment, reference = [uuid4() for _ in range(6)]
    now = datetime.now(UTC)
    with connect(integration_config) as conn:
        try:
            conn.execute(
                "INSERT INTO ag_conversation(id,member_id,title,next_message_seq) VALUES(%s,912300001,'restore-only fixture',2)",
                (cid,),
            )
            conn.execute(
                "INSERT INTO ag_message(id,conversation_id,seq,sender_kind,sender_id,status,text_content,retention_until) VALUES(%s,%s,1,'MEMBER',912300001,'COMPLETED','must not revive',now()+interval '90 days')",
                (mid, cid),
            )
            conn.execute(
                "INSERT INTO ag_run(id,conversation_id,trigger_message_id,state,conversation_version,memory_epoch,control_epoch,checkpoint_thread_id,lease_owner,lease_until) VALUES(%s,%s,%s,'RUNNING',1,1,1,%s,'old-worker',now()+interval '1 minute')",
                (rid, cid, mid, str(rid)),
            )
            conn.execute(
                "UPDATE ag_generation_slot SET run_id=%s,lease_until=now()+interval '1 minute' WHERE slot_no=(SELECT min(slot_no) FROM ag_generation_slot)",
                (rid,),
            )
            conn.execute(
                """INSERT INTO ag_action(id,conversation_id,member_id,action_type,state,conversation_version,control_epoch,
                         parameters,parameters_hash,operation_no,expires_at,result) VALUES(%s,%s,912300001,'ADD_CART','SUCCEEDED',1,1,
                         '{}'::jsonb,%s,%s,now()+interval '5 minutes','{"business_result":"already committed"}'::jsonb)""",
                (action, cid, "0" * 64, action),
            )
            conn.execute(
                "INSERT INTO ag_context_summary(id,conversation_id,memory_epoch,through_seq,summary,retention_until) VALUES(%s,%s,1,1,'private old memory',now()+interval '1 day')",
                (uuid4(), cid),
            )
            conn.execute(
                """INSERT INTO ag_attachment(id,owner_kind,owner_id,purpose,object_key,original_name,mime_type,byte_size,sha256,state,retention_until)
                         VALUES(%s,'MEMBER',912300001,'CHAT_IMAGE',%s,'fixture.jpg','image/jpeg',10,%s,'READY',now()+interval '1 day')""",
                (attachment, "chat/" + str(attachment), "0" * 64),
            )
            conn.execute(
                "INSERT INTO ag_attachment_ref(id,attachment_id,ref_kind,message_id,protected) VALUES(%s,%s,'MESSAGE',%s,true)",
                (reference, attachment, mid),
            )
            protected = dict(
                conn.execute("SELECT * FROM ag_attachment_ref WHERE id=%s", (reference,)).fetchone()
            )
            original = conn.execute(
                "SELECT state,result,operation_no FROM ag_action WHERE id=%s", (action,)
            ).fetchone()
            journal = empty_journal()
            journal["deleted_conversations"] = [
                {"id": cid, "memory_epoch": 2, "control_epoch": 2, "deleted_at": now}
            ]
            journal["tombstones"] = [
                {
                    "resource_type": "CONVERSATION",
                    "resource_id": str(cid),
                    "memory_epoch": 2,
                    "erased_at": now,
                    "created_at": now,
                }
            ]
            journal["protected_refs"] = [protected]
            overlay = apply_deletions(conn, journal)
            freeze_runtime(conn)
            message = conn.execute(
                "SELECT text_content,deleted_at FROM ag_message WHERE id=%s", (mid,)
            ).fetchone()
            assert message["text_content"] == "" and message["deleted_at"]
            assert (
                conn.execute(
                    "SELECT count(*) n FROM ag_context_summary WHERE conversation_id=%s", (cid,)
                ).fetchone()["n"]
                == 0
            )
            assert (
                conn.execute("SELECT state FROM ag_run WHERE id=%s", (rid,)).fetchone()["state"]
                == "INTERRUPTED"
            )
            assert (
                conn.execute("SELECT count(*) n FROM ag_generation_slot WHERE run_id=%s", (rid,)).fetchone()[
                    "n"
                ]
                == 0
            )
            assert (
                conn.execute(
                    "SELECT state,result,operation_no FROM ag_action WHERE id=%s", (action,)
                ).fetchone()
                == original
            )
            assert conn.execute(
                "SELECT protected,released_at FROM ag_attachment_ref WHERE id=%s", (reference,)
            ).fetchone() == {"protected": True, "released_at": None}
            assert overlay["protected_refs_checked"] == 1
        finally:
            conn.rollback()


def test_missing_newer_protection_refuses_reopen_instead_of_silently_losing_evidence(
    integration_config, monkeypatch
):
    BackupError, apply_deletions, _ = modules(monkeypatch)
    with connect(integration_config) as conn:
        journal = empty_journal()
        journal["protected_refs"] = [{"id": uuid4(), "attachment_id": uuid4()}]
        with pytest.raises(BackupError, match="original-ledger reconciliation"):
            apply_deletions(conn, journal)
        conn.rollback()


def test_restore_erases_media_and_feedback_copies_but_keeps_visible_sources(integration_config, monkeypatch):
    from psycopg.types.json import Jsonb

    _, apply_deletions, freeze_runtime = modules(monkeypatch)
    from restore_safety import verify_invariants

    deleted_cid, live_cid, deleted_mid, live_mid, recording, live_recording, speech = [
        uuid4() for _ in range(7)
    ]
    removed_asr, kept_asr, removed_tts, detached_tts, running = [uuid4() for _ in range(5)]
    now = datetime.now(UTC)
    with connect(integration_config) as conn:
        try:
            # Full invariant checks must not inspect unrelated fixtures left by other suites.
            for table in (
                "ag_conversation",
                "ag_message",
                "ag_staff_note",
                "ag_handoff",
                "ag_attachment",
                "ag_attachment_ref",
                "ag_erasure_tombstone",
                "ag_kb_citation",
                "ag_feedback",
                "ag_context_summary",
                "ag_event_outbox",
                "ag_image_observation",
                "ag_run",
                "ag_action",
                "ag_request_dedupe",
                "ag_generation_slot",
                "ag_job",
                "ag_worker_heartbeat",
                "ag_media_request",
                "ag_event_clock",
            ):
                conn.execute(
                    f"CREATE TEMP TABLE {table} (LIKE public.{table} INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING INDEXES) ON COMMIT DROP"
                )
            for cid, mid in [(deleted_cid, deleted_mid), (live_cid, live_mid)]:
                conn.execute(
                    "INSERT INTO ag_conversation(id,member_id,title,next_message_seq) VALUES(%s,912300002,'restore media fixture',2)",
                    (cid,),
                )
                conn.execute(
                    "INSERT INTO ag_message(id,conversation_id,seq,sender_kind,status,text_content,retention_until) VALUES(%s,%s,1,'ASSISTANT','COMPLETED','retained answer',now()+interval '90 days')",
                    (mid, cid),
                )
                fid = uuid4()
                conn.execute(
                    "INSERT INTO ag_feedback(id,message_id,member_id,vote,reason) VALUES(%s,%s,912300002,1,'private feedback')",
                    (fid, mid),
                )
                conn.execute(
                    "INSERT INTO ag_request_dedupe(scope,actor_kind,actor_id,idempotency_key,request_hash,response_ref,expires_at) VALUES(%s,'MEMBER',912300002,%s,%s,%s,now()+interval '1 day')",
                    (
                        f"submitFeedback:{mid}",
                        uuid4(),
                        "0" * 64,
                        Jsonb({"id": str(fid), "message_id": str(mid), "reason": "private feedback"}),
                    ),
                )
            for aid, purpose in [(recording, "RECORDING"), (live_recording, "RECORDING"), (speech, "SPEECH")]:
                conn.execute(
                    "INSERT INTO ag_attachment(id,owner_kind,owner_id,purpose,object_key,original_name,mime_type,byte_size,sha256,state,retention_until) VALUES(%s,'MEMBER',912300002,%s,%s,'fixture.wav','audio/wav',10,%s,'READY',now()+interval '7 days')",
                    (aid, purpose, "chat/" + str(aid), "0" * 64),
                )
            for rid, kind, aid, mid, state, value in [
                (removed_asr, "ASR", recording, None, "SUCCEEDED", {"text": "deleted recording transcript"}),
                (
                    kept_asr,
                    "ASR",
                    live_recording,
                    None,
                    "SUCCEEDED",
                    {"text": "visible recording transcript"},
                ),
                (removed_tts, "TTS", None, deleted_mid, "SUCCEEDED", {"attachment": {"id": str(speech)}}),
                (detached_tts, "TTS", None, live_mid, "SUCCEEDED", {"attachment": {"id": str(speech)}}),
                (running, "ASR", live_recording, None, "RUNNING", None),
            ]:
                conn.execute(
                    "INSERT INTO ag_media_request(id,member_id,kind,idempotency_key,request_hash,attachment_id,message_id,state,lease_owner,result) VALUES(%s,912300002,%s,%s,%s,%s,%s,%s,'old-media-worker',%s)",
                    (rid, kind, uuid4(), "0" * 64, aid, mid, state, Jsonb(value) if value else None),
                )
            journal = empty_journal()
            journal["deleted_conversations"] = [
                {"id": deleted_cid, "memory_epoch": 2, "control_epoch": 2, "deleted_at": now}
            ]
            journal["deleted_attachments"] = [
                {"id": aid, "object_key": "chat/" + str(aid)} for aid in (recording, speech)
            ]
            apply_deletions(conn, journal)
            freeze_runtime(conn)
            for rid in (removed_asr, removed_tts, detached_tts):
                assert (
                    conn.execute("SELECT result FROM ag_media_request WHERE id=%s", (rid,)).fetchone()[
                        "result"
                    ]
                    is None
                )
            assert conn.execute("SELECT result FROM ag_media_request WHERE id=%s", (kept_asr,)).fetchone()[
                "result"
            ] == {"text": "visible recording transcript"}
            assert conn.execute(
                "SELECT response_ref FROM ag_request_dedupe WHERE scope=%s",
                (f"submitFeedback:{deleted_mid}",),
            ).fetchone()["response_ref"] == {"resource_deleted": True}
            assert (
                conn.execute(
                    "SELECT response_ref FROM ag_request_dedupe WHERE scope=%s",
                    (f"submitFeedback:{live_mid}",),
                ).fetchone()["response_ref"]["reason"]
                == "private feedback"
            )
            frozen = conn.execute(
                "SELECT state,fencing_token,lease_until<=clock_timestamp() expired FROM ag_media_request WHERE id=%s",
                (running,),
            ).fetchone()
            assert frozen == {"state": "UNKNOWN", "fencing_token": 2, "expired": True}
            assert not any(verify_invariants(conn).values())
            # The checker must reject a restored private copy even when the main message is erased.
            conn.execute(
                "UPDATE ag_media_request SET result=%s WHERE id=%s",
                (Jsonb({"text": "must not revive"}), removed_asr),
            )
            from backup_archive import BackupError

            with pytest.raises(BackupError, match="deleted_media_result"):
                verify_invariants(conn)
        finally:
            conn.rollback()
