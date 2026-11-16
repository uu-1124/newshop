import json
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from test_runtime import settings_for

from app.application.expiry import filter_event
from app.application.governance import POLICY_KEY, Governance, PublicPolicy
from app.bootstrap import Services
from app.domain.capabilities import JobLease, JobType
from app.domain.identity import DomainError, Principal
from app.infrastructure.postgres.database import encoded, execute, one, rows


def operator():
    return Principal(
        1900000000 + uuid4().int % 100000000,
        "retention operator",
        "STAFF",
        frozenset(
            {
                "agent:settings:read",
                "agent:settings:write",
                "agent:retention:preview",
                "agent:retention:execute",
                "agent:job:read",
                "agent:job:retry",
                "agent:conversation:read",
                "agent:conversation:history:all",
                "agent:conversation:note",
            }
        ),
    )


async def run_job(svc, jid):
    job = await svc.jobs.claim("retention-test", UUID(str(jid)))
    assert job
    result = await svc.job_handlers.dispatch(
        JobLease(
            job["id"], JobType(job["job_type"]), job["lease_owner"], job["fencing_token"], job["parameters"]
        )
    )
    result = await svc.jobs.finish(job, result=result)
    assert result["state"] == "SUCCEEDED", result
    return result


def service(config):
    return Services(
        settings_for(config).model_copy(update={"governance_enabled": True, "retention_enabled": True})
    )


@pytest.mark.anyio
async def test_retention_preview_apply_expiry_reads_execution_late_protection_and_idempotency(
    integration_config,
):
    svc = service(integration_config)
    staff = operator()
    member = Principal(2000000000 + uuid4().int % 100000000, "retention member")
    retained = svc.extensions.retention.require()
    try:
        async with svc.db.transaction() as conn:
            await execute(
                conn,
                "DELETE FROM ag_setting WHERE key=:key OR key LIKE 'agent-policy-history:%'",
                key=POLICY_KEY,
            )
            # Previous test sessions retain historical fixtures in this dedicated
            # test database. Remove their overdue visibility before seeding this case.
            await execute(
                conn,
                "UPDATE ag_message SET deleted_at=now(),text_content='',blocks='[]'::jsonb WHERE deleted_at IS NULL AND (created_at<now()-interval '1 day' OR retention_until<=now())",
            )
        conv = await svc.conversations.create(
            member, str(uuid4()), {"entry_context": {"kind": "GENERAL"}, "title": "[TEST ONLY] retention"}
        )
        cid = UUID(conv["id"])
        oldbody = {"client_message_id": str(uuid4()), "text": "EXPIRED_TEST_SECRET", "attachment_ids": []}
        oldkey = str(uuid4())
        old = await svc.conversations.send(member, "synthetic-only", cid, oldkey, oldbody)
        mid = UUID(old["message"]["id"])
        async with svc.db.transaction() as conn:
            await execute(
                conn, "UPDATE ag_message SET created_at=now()-interval '2 days' WHERE id=:id", id=mid
            )
            await execute(
                conn,
                "UPDATE ag_run SET state='INTERRUPTED',finished_at=now() WHERE id=CAST(:id AS uuid)",
                id=old["run"]["id"],
            )
            await execute(
                conn,
                "INSERT INTO ag_staff_profile(admin_id,display_name) VALUES(:id,'test operator')",
                id=staff.id,
            )
            await execute(
                conn,
                """INSERT INTO ag_staff_note(id,conversation_id,admin_id,client_note_id,body,created_at,retention_until)
                VALUES(:id,:cid,:actor,:client,'EXPIRED_NOTE',now()-interval '2 days',now()+interval '90 days')""",
                id=uuid4(),
                cid=cid,
                actor=staff.id,
                client=uuid4(),
            )
            await execute(
                conn,
                """INSERT INTO ag_context_summary(id,conversation_id,memory_epoch,through_seq,summary,retention_until)
                VALUES(:id,:cid,1,1,'OLD_MEMORY',now()+interval '90 days')""",
                id=uuid4(),
                cid=cid,
            )
            files = [uuid4(), uuid4()]
            for aid in files:
                await execute(
                    conn,
                    """INSERT INTO ag_attachment(id,owner_kind,owner_id,purpose,object_key,original_name,mime_type,byte_size,sha256,state,created_at,retention_until)
                    VALUES(:id,'MEMBER',:owner,'CHAT_IMAGE',:key,'test.jpg','image/jpeg',10,:hash,'READY',now()-interval '2 days',now()+interval '90 days')""",
                    id=aid,
                    owner=member.id,
                    key=f"test-retention/{aid}",
                    hash="a" * 64,
                )
        fresh = await svc.conversations.send(
            member,
            "synthetic-only",
            cid,
            str(uuid4()),
            {"client_message_id": str(uuid4()), "text": "FRESH_TEXT", "attachment_ids": []},
        )
        policy = PublicPolicy(message_retention_days=1, image_retention_days=1).model_dump(mode="json")
        preview = await retained.preview(staff, str(uuid4()), policy)
        ready = await run_job(svc, preview["id"])
        assert ready["result"]["messages"] >= 1 and ready["result"]["attachments"] >= 2
        assert "EXPIRED_TEST_SECRET" not in encoded(ready["result"])
        with pytest.raises(DomainError) as unapplied:
            await retained.execute(staff, str(uuid4()), UUID(preview["id"]), 1)
        assert unapplied.value.code == "SETTINGS_CHANGED"
        updated = await Governance(svc.db).update(staff, str(uuid4()), policy, 1, UUID(preview["id"]))
        # All readers reject expiry immediately, before physical deletion.
        snapshot = await svc.conversations.snapshot(member, cid)
        assert [m["text"] for m in snapshot["recent_messages"]] == ["FRESH_TEXT"]
        listed = await svc.conversations.list(member, 20, None, "")
        assert next(c for c in listed["items"] if c["id"] == str(cid))["last_message_preview"] == "FRESH_TEXT"
        async with svc.db.transaction() as conn:
            event = await one(
                conn, "SELECT * FROM ag_event_outbox WHERE payload->>'message_id'=:mid", mid=str(mid)
            )
            assert event and await filter_event(conn, event) is None
            # A new reference after preview must protect the object at execution.
            await execute(
                conn,
                "INSERT INTO ag_attachment_ref(id,attachment_id,ref_kind,message_id,protected) VALUES(:id,:aid,'MESSAGE',:mid,true)",
                id=uuid4(),
                aid=files[0],
                mid=mid,
            )
        key = str(uuid4())
        execution = await retained.execute(staff, key, UUID(preview["id"]), updated["version"])
        assert json.loads(
            encoded(await retained.execute(staff, key, UUID(preview["id"]), updated["version"]))
        ) == json.loads(encoded(execution))
        done = await run_job(svc, execution["id"])
        assert done["result"]["messages"] >= 1 and done["result"]["attachments_scheduled"] >= 1
        async with svc.db.transaction() as conn:
            erased = await one(conn, "SELECT * FROM ag_message WHERE id=:id", id=mid)
            assert erased["deleted_at"] and erased["text_content"] == "" and erased["blocks"] == []
            assert (await one(conn, "SELECT state FROM ag_attachment WHERE id=:id", id=files[0]))[
                "state"
            ] == "READY"
            assert (await one(conn, "SELECT state FROM ag_attachment WHERE id=:id", id=files[1]))[
                "state"
            ] == "DELETING"
            assert not await one(conn, "SELECT id FROM ag_context_summary WHERE conversation_id=:id", id=cid)
            assert (
                await one(conn, "SELECT state FROM ag_run WHERE id=CAST(:id AS uuid)", id=fresh["run"]["id"])
            )["state"] == "INTERRUPTED"
            cleanup = await one(
                conn,
                "SELECT id FROM ag_job WHERE parameters->>'kind'='CONTEXT_EXPIRY' AND parameters->>'conversation_id'=:id ORDER BY created_at DESC LIMIT 1",
                id=str(cid),
            )
            marker = await one(
                conn,
                "SELECT memory_epoch FROM ag_erasure_tombstone WHERE resource_type='CONTEXT' AND resource_id=:id",
                id=str(cid),
            )
            assert marker["memory_epoch"] > 1
        await run_job(svc, cleanup["id"])
        with pytest.raises(DomainError) as replay:
            await svc.conversations.send(member, "synthetic-only", cid, oldkey, oldbody)
        assert replay.value.code == "MESSAGE_EXPIRED"
        assert [m["text"] for m in (await svc.conversations.snapshot(member, cid))["recent_messages"]] == [
            "FRESH_TEXT"
        ]
        await Governance(svc.db).update(
            staff, str(uuid4()), policy | {"max_input_characters": 4}, updated["version"]
        )
        with pytest.raises(DomainError) as too_long:
            await svc.conversations.send(
                member,
                "synthetic-only",
                cid,
                str(uuid4()),
                {"client_message_id": str(uuid4()), "text": "TOO_LONG", "attachment_ids": []},
            )
        assert too_long.value.code == "INPUT_TOO_LONG"
        new = await svc.conversations.send(
            member,
            "synthetic-only",
            cid,
            str(uuid4()),
            {"client_message_id": str(uuid4()), "text": "OK", "attachment_ids": []},
        )
        async with svc.db.transaction() as conn:
            lifetime = await one(
                conn,
                "SELECT extract(epoch FROM(retention_until-created_at)) seconds FROM ag_message WHERE id=CAST(:id AS uuid)",
                id=new["message"]["id"],
            )
            assert lifetime["seconds"] == 86400
    finally:
        async with svc.db.transaction() as conn:
            await execute(
                conn,
                "UPDATE ag_run SET state='INTERRUPTED',lease_owner=NULL,lease_until=NULL,finished_at=now() WHERE conversation_id IN(SELECT id FROM ag_conversation WHERE member_id=:member) AND state IN('QUEUED','RUNNING','STOP_REQUESTED')",
                member=member.id,
            )
            await execute(
                conn,
                "DELETE FROM ag_setting WHERE key=:key OR key LIKE 'agent-policy-history:%'",
                key=POLICY_KEY,
            )
        await svc.close()


@pytest.mark.anyio
async def test_scheduler_creates_one_current_policy_preview_and_execution_per_day(integration_config):
    svc = service(integration_config)
    retained = svc.extensions.retention.require()
    try:
        await retained.schedule()
        await retained.schedule()
        async with svc.db.transaction() as conn:
            plans = await rows(
                conn,
                "SELECT * FROM ag_retention_plan WHERE scheduled AND created_at>=date_trunc('day',now())",
            )
            assert len(plans) == 1
        await run_job(svc, plans[0]["preview_job_id"])
        await retained.schedule()
        await retained.schedule()
        async with svc.db.transaction() as conn:
            plan = await one(conn, "SELECT * FROM ag_retention_plan WHERE id=:id", id=plans[0]["id"])
            assert plan["execution_job_id"]
            assert (
                await one(
                    conn,
                    "SELECT count(*) n FROM ag_job WHERE dedupe_key=:key",
                    key=f"retention-execute:{plan['id']}",
                )
            )["n"] == 1
            await execute(
                conn, "UPDATE ag_job SET state='CANCELLED' WHERE id=:id", id=plan["execution_job_id"]
            )
    finally:
        await svc.close()


@pytest.mark.anyio
async def test_retention_permissions_stale_settings_expiry_and_lost_job_fence(integration_config):
    svc = service(integration_config)
    staff = operator()
    retained = svc.extensions.retention.require()
    try:
        async with svc.db.transaction() as conn:
            await execute(
                conn,
                "DELETE FROM ag_setting WHERE key=:key OR key LIKE 'agent-policy-history:%'",
                key=POLICY_KEY,
            )
        with pytest.raises(DomainError) as denied:
            await retained.preview(
                replace(staff, permissions=frozenset()), str(uuid4()), PublicPolicy().model_dump(mode="json")
            )
        assert denied.value.status == 403
        preview = await retained.preview(staff, str(uuid4()), PublicPolicy().model_dump(mode="json"))
        with pytest.raises(DomainError) as fenced:
            await retained.preview_job(
                JobLease(
                    UUID(preview["id"]),
                    JobType.RETENTION_PREVIEW,
                    "wrong-owner",
                    999,
                    {
                        "kind": "RETENTION_POLICY",
                        "plan_id": str(uuid4()),
                        "settings_sha256": "a" * 64,
                        "settings_version": 1,
                    },
                )
            )
        assert fenced.value.code == "LEASE_LOST"
        await run_job(svc, preview["id"])
        await Governance(svc.db).update(
            staff, str(uuid4()), PublicPolicy(max_input_characters=9000).model_dump(mode="json"), 1
        )
        with pytest.raises(DomainError) as changed:
            await retained.execute(staff, str(uuid4()), UUID(preview["id"]), 2)
        assert changed.value.code == "SETTINGS_CHANGED"
        async with svc.db.transaction() as conn:
            await execute(
                conn,
                "UPDATE ag_retention_plan SET expires_at=now()-interval '1 minute' WHERE preview_job_id=CAST(:id AS uuid)",
                id=preview["id"],
            )
        with pytest.raises(DomainError) as expired:
            await retained.execute(staff, str(uuid4()), UUID(preview["id"]), 2)
        assert expired.value.code == "RETENTION_PREVIEW_EXPIRED"
    finally:
        async with svc.db.transaction() as conn:
            await execute(
                conn,
                "DELETE FROM ag_setting WHERE key=:key OR key LIKE 'agent-policy-history:%'",
                key=POLICY_KEY,
            )
        await svc.close()


@pytest.mark.anyio
async def test_lengthening_policy_does_not_resurrect_previously_expired_messages(integration_config):
    svc = service(integration_config)
    staff = operator()
    member = Principal(2200000000 + uuid4().int % 100000000, "no resurrection")
    retained = svc.extensions.retention.require()
    try:
        async with svc.db.transaction() as conn:
            await execute(
                conn,
                "DELETE FROM ag_setting WHERE key=:key OR key LIKE 'agent-policy-history:%'",
                key=POLICY_KEY,
            )
        conv = await svc.conversations.create(
            member,
            str(uuid4()),
            {"entry_context": {"kind": "GENERAL"}, "title": "[TEST ONLY] no resurrection"},
        )
        sent = await svc.conversations.send(
            member,
            "synthetic-only",
            conv["id"],
            str(uuid4()),
            {"client_message_id": str(uuid4()), "text": "must stay expired", "attachment_ids": []},
        )
        async with svc.db.transaction() as conn:
            await execute(
                conn,
                "UPDATE ag_message SET created_at=now()-interval '2 days' WHERE id=CAST(:id AS uuid)",
                id=sent["message"]["id"],
            )
            await execute(
                conn,
                "UPDATE ag_run SET state='INTERRUPTED',finished_at=now() WHERE id=CAST(:id AS uuid)",
                id=sent["run"]["id"],
            )
        shorter = PublicPolicy(message_retention_days=1).model_dump(mode="json")
        preview = await retained.preview(staff, str(uuid4()), shorter)
        await run_job(svc, preview["id"])
        current = await Governance(svc.db).update(staff, str(uuid4()), shorter, 1, UUID(preview["id"]))
        assert not (await svc.conversations.snapshot(member, conv["id"]))["recent_messages"]
        await Governance(svc.db).update(
            staff, str(uuid4()), PublicPolicy().model_dump(mode="json"), current["version"]
        )
        assert not (await svc.conversations.snapshot(member, conv["id"]))["recent_messages"]
    finally:
        async with svc.db.transaction() as conn:
            await execute(
                conn,
                "DELETE FROM ag_setting WHERE key=:key OR key LIKE 'agent-policy-history:%'",
                key=POLICY_KEY,
            )
        await svc.close()
