"""Restore-only safety overlay. Business operation ledgers are never replayed."""

from uuid import UUID

from backup_archive import BackupError
from psycopg.types.json import Jsonb


def has_media_requests(conn):
    # Older supported snapshots predate migration 0011.
    return conn.execute("SELECT to_regclass('ag_media_request') present").fetchone()["present"] is not None


DELETED_MEDIA_RESULT = """SELECT count(*) n FROM ag_media_request r WHERE r.result IS NOT NULL AND (
    EXISTS(SELECT 1 FROM ag_message m JOIN ag_conversation c ON c.id=m.conversation_id
        WHERE m.id=r.message_id AND (m.deleted_at IS NOT NULL OR c.deleted_at IS NOT NULL))
    OR EXISTS(SELECT 1 FROM ag_attachment a WHERE a.state IN ('DELETING','DELETED')
        AND (a.id=r.attachment_id OR a.id::text=r.result->'attachment'->>'id')))"""


def apply_deletions(conn, journal):
    import json

    def payload(rows):
        return Jsonb(json.loads(json.dumps(rows, default=str)))

    deleted_conversations = {UUID(str(row["id"])): row for row in journal["deleted_conversations"]}
    message_map = {UUID(str(row["id"])): row for row in journal["deleted_messages"]}
    contexts, notes = set(), set()
    for marker in journal["tombstones"]:
        resource = marker["resource_type"]
        if resource not in {"CONVERSATION", "CONTEXT", "MESSAGE", "NOTE", "ATTACHMENT"}:
            raise BackupError("Unreviewed deletion marker type; restore stays closed")
        if resource == "CONTEXT":
            contexts.add(UUID(str(marker["resource_id"])))
        elif resource == "NOTE":
            notes.add(UUID(str(marker["resource_id"])))
    # Recheck protection before erasing anything. Newer references without their
    # original business records require reconciliation, never silent omission.
    protected_ids = set()
    for reference in journal["protected_refs"]:
        protected_ids.add(UUID(str(reference["attachment_id"])))
        existing = conn.execute("SELECT * FROM ag_attachment_ref WHERE id=%s", (reference["id"],)).fetchone()
        if not existing or any(
            str(existing[key]) != str(reference[key])
            for key in ("attachment_id", "action_id", "return_apply_id", "kb_version_id", "message_id")
        ):
            raise BackupError("Newer protected business reference requires original-ledger reconciliation")
    if any(UUID(str(row["id"])) in protected_ids for row in journal["deleted_attachments"]):
        raise BackupError("Conflicting deletion and protected evidence; restore stays closed")
    conn.execute(
        """INSERT INTO ag_erasure_tombstone(resource_type,resource_id,memory_epoch,erased_at,created_at)
        SELECT resource_type,resource_id,memory_epoch,erased_at,created_at FROM jsonb_to_recordset(%s)
        AS j(resource_type text,resource_id text,memory_epoch bigint,erased_at timestamptz,created_at timestamptz)
        ON CONFLICT(resource_type,resource_id) DO UPDATE
        SET memory_epoch=GREATEST(ag_erasure_tombstone.memory_epoch,EXCLUDED.memory_epoch),
            erased_at=GREATEST(ag_erasure_tombstone.erased_at,EXCLUDED.erased_at)""",
        (payload(journal["tombstones"]),),
    )
    cids = list(deleted_conversations)
    conn.execute(
        """UPDATE ag_conversation c SET deleted_at=COALESCE(c.deleted_at,j.deleted_at),title='已删除会话',
        memory_epoch=GREATEST(c.memory_epoch,j.memory_epoch),control_epoch=GREATEST(c.control_epoch,j.control_epoch),version=c.version+1
        FROM jsonb_to_recordset(%s) AS j(id uuid,memory_epoch bigint,control_epoch bigint,deleted_at timestamptz) WHERE c.id=j.id""",
        (payload(journal["deleted_conversations"]),),
    )
    conn.execute("UPDATE ag_staff_note SET body='已删除' WHERE conversation_id=ANY(%s)", (cids,))
    conn.execute("UPDATE ag_handoff SET reason=NULL WHERE conversation_id=ANY(%s)", (cids,))
    for row in conn.execute(
        "SELECT id,conversation_id FROM ag_message WHERE conversation_id=ANY(%s) OR id=ANY(%s)",
        (cids, list(message_map)),
    ):
        if row["conversation_id"] in deleted_conversations:
            message_map[row["id"]] = {
                "id": row["id"],
                "conversation_id": row["conversation_id"],
                "deleted_at": deleted_conversations[row["conversation_id"]]["deleted_at"],
            }
    mids = list(message_map)
    affected = (
        contexts
        | set(deleted_conversations)
        | {UUID(str(row["conversation_id"])) for row in message_map.values()}
    )
    conn.execute("DELETE FROM ag_kb_citation WHERE message_id=ANY(%s)", (mids,))
    conn.execute("DELETE FROM ag_feedback WHERE message_id=ANY(%s)", (mids,))
    conn.execute(
        "UPDATE ag_attachment_ref SET released_at=COALESCE(released_at,now()) WHERE message_id=ANY(%s) AND NOT protected",
        (mids,),
    )
    conn.execute(
        """UPDATE ag_message m SET text_content='',blocks='[]'::jsonb,sender_display_name=NULL,
        deleted_at=COALESCE(m.deleted_at,j.deleted_at) FROM jsonb_to_recordset(%s) AS j(id uuid,deleted_at timestamptz)
        WHERE m.id=j.id""",
        (payload(list(message_map.values())),),
    )
    conn.execute("UPDATE ag_staff_note SET body='已到期' WHERE id=ANY(%s)", (list(notes),))
    affected_ids = list(affected)
    conn.execute("DELETE FROM ag_context_summary WHERE conversation_id=ANY(%s)", (affected_ids,))
    conn.execute("DELETE FROM ag_event_outbox WHERE conversation_id=ANY(%s)", (affected_ids,))
    conn.execute(
        "DELETE FROM ag_image_observation WHERE run_id IN(SELECT id FROM ag_run WHERE conversation_id=ANY(%s))",
        (affected_ids,),
    )
    for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
        conn.execute(
            f"DELETE FROM langgraph_checkpoint.{table} WHERE thread_id IN(SELECT checkpoint_thread_id FROM ag_run WHERE conversation_id=ANY(%s))",
            (affected_ids,),
        )
    conn.execute(
        "UPDATE ag_conversation SET memory_epoch=memory_epoch+1,control_epoch=control_epoch+1,version=version+1 WHERE id=ANY(%s)",
        (affected_ids,),
    )
    conn.execute(
        "UPDATE ag_action SET state='STALE',version=version+1 WHERE conversation_id=ANY(%s) AND state='PENDING_CONFIRMATION'",
        (affected_ids,),
    )
    strings = [str(cid) for cid in affected_ids]
    conn.execute(
        """UPDATE ag_request_dedupe SET response_ref=%s WHERE scope LIKE ANY(%s) OR response_ref->>'conversation_id'=ANY(%s)
        OR response_ref->'message'->>'conversation_id'=ANY(%s) OR response_ref->>'id'=ANY(%s)""",
        (Jsonb({"resource_deleted": True}), ["%:" + cid for cid in strings], strings, strings, strings),
    )
    conn.execute(
        """UPDATE ag_request_dedupe SET response_ref=%s WHERE scope LIKE 'submitFeedback:%%'
        AND (response_ref->>'message_id'=ANY(%s) OR scope=ANY(%s))""",
        (
            Jsonb({"resource_deleted": True}),
            [str(mid) for mid in mids],
            [f"submitFeedback:{mid}" for mid in mids],
        ),
    )
    conn.execute(
        """UPDATE ag_attachment_ref r SET protected=true,released_at=j.released_at
        FROM jsonb_to_recordset(%s) AS j(id uuid,released_at timestamptz) WHERE r.id=j.id""",
        (payload(journal["protected_refs"]),),
    )
    conn.execute(
        "UPDATE ag_attachment SET state='DELETED',storage_deleted_at=COALESCE(storage_deleted_at,now()),original_name='已删除附件' WHERE id=ANY(%s)",
        ([UUID(str(row["id"])) for row in journal["deleted_attachments"]],),
    )
    if has_media_requests(conn):
        conn.execute(
            """UPDATE ag_media_request r SET result=NULL WHERE r.result IS NOT NULL AND (
            EXISTS(SELECT 1 FROM ag_message m JOIN ag_conversation c ON c.id=m.conversation_id
                WHERE m.id=r.message_id AND (m.deleted_at IS NOT NULL OR c.deleted_at IS NOT NULL))
            OR EXISTS(SELECT 1 FROM ag_attachment a WHERE a.state IN ('DELETING','DELETED')
                AND (a.id=r.attachment_id OR a.id::text=r.result->'attachment'->>'id')))"""
        )
    return {
        "affected_conversations": len(affected),
        "erased_messages": len(message_map),
        "protected_refs_checked": len(journal["protected_refs"]),
        "delete_object_keys": [row["object_key"] for row in journal["deleted_attachments"]],
    }


def freeze_runtime(conn):
    runs = conn.execute("""UPDATE ag_run SET state='INTERRUPTED',error_code='ISOLATED_RESTORE',lease_owner=NULL,
        lease_until=NULL,fencing_token=fencing_token+1,finished_at=now(),version=version+1
        WHERE state IN ('QUEUED','RUNNING','STOP_REQUESTED') RETURNING id""").fetchall()
    conn.execute("UPDATE ag_generation_slot SET run_id=NULL,lease_until=NULL")
    conn.execute("UPDATE ag_message SET status='INTERRUPTED' WHERE status='GENERATING'")
    conn.execute(
        "UPDATE ag_job SET state='FAILED',error_code='ISOLATED_RESTORE',error_message='恢复环境禁止重放后台任务',lease_owner=NULL,lease_until=NULL,fencing_token=fencing_token+1 WHERE state IN ('QUEUED','RUNNING')"
    )
    conn.execute("DELETE FROM ag_worker_heartbeat")
    if has_media_requests(conn):
        conn.execute(
            """UPDATE ag_media_request SET state='UNKNOWN',error_code='ISOLATED_RESTORE',
            fencing_token=fencing_token+1,lease_until=clock_timestamp() WHERE state='RUNNING'"""
        )
    # No API/worker/consumer is started. Pending action/dispatch/job records remain
    # forensic state, never an instruction to execute or call a supplier.
    return {
        "interrupted_runs": len(runs),
        "generation_slots_reclaimed": True,
        "applications_started": 0,
        "business_execute_replayed": 0,
        "paid_model_calls": 0,
    }


def verify_invariants(conn):
    checks = {
        "duplicate_message_seq": "SELECT count(*) n FROM(SELECT conversation_id,seq FROM ag_message GROUP BY conversation_id,seq HAVING count(*)>1) s",
        "message_seq_past_conversation": "SELECT count(*) n FROM ag_message m JOIN ag_conversation c ON c.id=m.conversation_id WHERE m.seq>=c.next_message_seq",
        "event_clock_behind": "SELECT count(*) n FROM ag_event_outbox e WHERE e.event_id>(SELECT last_event_id FROM ag_event_clock WHERE singleton_id=1)",
        "deleted_message_content": "SELECT count(*) n FROM ag_message m LEFT JOIN ag_conversation c ON c.id=m.conversation_id WHERE (m.deleted_at IS NOT NULL OR c.deleted_at IS NOT NULL) AND (m.text_content<>'' OR m.blocks<>'[]'::jsonb)",
        "deleted_context_summary": "SELECT count(*) n FROM ag_context_summary s JOIN ag_conversation c ON c.id=s.conversation_id WHERE c.deleted_at IS NOT NULL",
        "live_generation_slots": "SELECT count(*) n FROM ag_generation_slot WHERE run_id IS NOT NULL",
        "invalid_fk": "SELECT count(*) n FROM pg_constraint WHERE contype='f' AND NOT convalidated",
        "deleted_feedback_result": """SELECT count(*) n FROM ag_request_dedupe d
            JOIN ag_message m ON d.response_ref->>'message_id'=m.id::text
            JOIN ag_conversation c ON c.id=m.conversation_id
            WHERE d.scope LIKE 'submitFeedback:%' AND (m.deleted_at IS NOT NULL OR c.deleted_at IS NOT NULL)""",
    }
    if has_media_requests(conn):
        checks["deleted_media_result"] = DELETED_MEDIA_RESULT
        checks["live_media_requests"] = "SELECT count(*) n FROM ag_media_request WHERE state='RUNNING'"
    results = {name: conn.execute(query).fetchone()["n"] for name, query in checks.items()}
    if any(results.values()):
        raise BackupError(
            "Restored data invariant failed: " + ",".join(name for name, value in results.items() if value)
        )
    return results
