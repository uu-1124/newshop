"""Real isolated PG interleavings for attachment protection and expired context."""

import asyncio
import threading
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from test_actions import settings
from test_handoffs import actor, claim, online, request
from test_order_lifecycle import LifecycleBridge
from test_retention import run_job

from app.agents.graph import build_graph
from app.application.governance import POLICY_KEY, PublicPolicy
from app.bootstrap import Services
from app.domain.capabilities import JobLease, JobType
from app.domain.identity import DomainError, Principal
from app.infrastructure.postgres.database import encoded, execute, one
from app.infrastructure.providers import FixtureDialogProvider


async def setup(config):
    svc = Services(
        settings(config).model_copy(
            update={"orders_enabled": True, "retention_enabled": True, "governance_enabled": True}
        )
    )
    member = Principal(2300000000 + uuid4().int % 100000000, "retention race buyer")
    bridge = LifecycleBridge(member)
    svc.extensions.actions.require().bridge = bridge
    conv = await svc.conversations.create(
        member,
        str(uuid4()),
        {"title": "[TEST ONLY] retention interleaving", "entry_context": {"kind": "GENERAL"}},
    )
    async with svc.db.transaction() as conn:
        await execute(
            conn,
            "INSERT INTO ag_setting(key,value,version,updated_by) VALUES(:key,CAST(:value AS jsonb),1,0) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,version=ag_setting.version+1",
            key=POLICY_KEY,
            value=encoded(PublicPolicy().model_dump(mode="json")),
        )
    return svc, member, UUID(conv["id"]), bridge


async def attachment(svc, member, days=2):
    aid = UUID("00000000-" + str(uuid4())[9:])
    async with svc.db.transaction() as conn:
        await execute(
            conn,
            "INSERT INTO ag_attachment(id,owner_kind,owner_id,purpose,object_key,original_name,mime_type,byte_size,sha256,state,created_at,retention_until) VALUES(:id,'MEMBER',:owner,'CHAT_IMAGE',:key,'proof.jpg','image/jpeg',10,:sha,'READY',now()-make_interval(days=>:days),now()+interval '90 days')",
            id=aid,
            owner=member.id,
            key=f"test-retention-race/{aid}",
            sha="c" * 64,
            days=days,
        )
    return aid


async def prepare_return(svc, member, cid, aid):
    current = await svc.conversations.snapshot(member, cid)
    return await svc.extensions.actions.require().prepare(
        member,
        "synthetic-only",
        cid,
        str(uuid4()),
        {
            "expected_conversation_version": current["conversation"]["version"],
            "parameters": {
                "action_type": "CREATE_RETURN",
                "order_id": "1",
                "order_item_id": "2",
                "quantity": 1,
                "reason_code": "1",
                "description": "合成清理并发凭证",
                "attachment_ids": [str(aid)],
            },
        },
    )


async def shorten(svc):
    policy = PublicPolicy(image_retention_days=1)
    async with svc.db.transaction() as conn:
        await execute(
            conn,
            "UPDATE ag_setting SET value=CAST(:value AS jsonb),version=version+1 WHERE key=:key",
            value=encoded(policy.model_dump(mode="json")),
            key=POLICY_KEY,
        )
    return policy


async def cleanup(svc, member):
    async with svc.db.transaction() as conn:
        await execute(
            conn,
            "UPDATE ag_action SET state='CANCELLED' WHERE member_id=:member AND state='PENDING_CONFIRMATION'",
            member=member.id,
        )
        await execute(
            conn,
            "UPDATE ag_generation_slot SET run_id=NULL,lease_until=NULL WHERE run_id IN (SELECT id FROM ag_run WHERE conversation_id IN (SELECT id FROM ag_conversation WHERE member_id=:member))",
            member=member.id,
        )
        await execute(
            conn,
            "UPDATE ag_run SET state='INTERRUPTED',finished_at=now(),lease_owner=NULL,lease_until=NULL WHERE conversation_id IN (SELECT id FROM ag_conversation WHERE member_id=:member) AND state IN ('QUEUED','RUNNING','STOP_REQUESTED')",
            member=member.id,
        )
        await execute(conn, "DELETE FROM ag_setting WHERE key=:key", key=POLICY_KEY)
    await svc.close()


@pytest.mark.anyio
async def test_expired_by_current_policy_cannot_become_a_new_return_proof(integration_config):
    svc, member, cid, bridge = await setup(integration_config)
    try:
        aid = await attachment(svc, member)
        await shorten(svc)
        with pytest.raises(DomainError, match="ATTACHMENT_UNAVAILABLE"):
            await prepare_return(svc, member, cid, aid)
        async with svc.db.transaction() as conn:
            assert not await one(conn, "SELECT id FROM ag_attachment_ref WHERE attachment_id=:id", id=aid)
            assert not await one(conn, "SELECT id FROM ag_action WHERE member_id=:id", id=member.id)
            assert (await one(conn, "SELECT state FROM ag_attachment WHERE id=:id", id=aid))[
                "state"
            ] == "READY"
        assert not bridge.calls
    finally:
        await cleanup(svc, member)


@pytest.mark.anyio
async def test_delete_object_io_cannot_acquire_new_proof_and_retries_after_lost_job_lease(
    integration_config, monkeypatch
):
    from app.application import conversation_cleanup

    svc, member, cid, bridge = await setup(integration_config)
    entered, release = threading.Event(), threading.Event()
    task = None

    class Objects:
        calls = 0
        exists = True

        def delete(self, key):
            assert key.startswith("test-retention-race/")
            self.calls += 1
            self.exists = False
            entered.set()
            assert release.wait(15)

        def close(self):
            pass

    objects = Objects()
    monkeypatch.setattr(conversation_cleanup, "PrivateObjects", lambda settings: objects)
    svc.conversations.settings.attachments_enabled = True
    try:
        aid = await attachment(svc, member)
        policy = await shorten(svc)
        async with svc.db.transaction() as conn:
            await svc.extensions.retention.require().sweep_attachments(conn, policy, datetime.now(UTC))
            job = await one(
                conn,
                "SELECT id FROM ag_job WHERE job_type='ATTACHMENT_DELETE' AND parameters->>'attachment_id'=:id",
                id=str(aid),
            )
        claimed = await svc.jobs.claim("delete-object-first", job["id"])
        assert claimed
        lease = JobLease(
            claimed["id"],
            JobType.ATTACHMENT_DELETE,
            claimed["lease_owner"],
            claimed["fencing_token"],
            claimed["parameters"],
        )
        task = asyncio.create_task(svc.job_handlers.dispatch(lease))
        assert await asyncio.to_thread(entered.wait, 15)
        with pytest.raises(DomainError, match="ATTACHMENT_UNAVAILABLE"):
            await prepare_return(svc, member, cid, aid)
        async with svc.db.transaction() as conn:
            assert not await one(conn, "SELECT id FROM ag_attachment_ref WHERE attachment_id=:id", id=aid)
            await execute(
                conn, "UPDATE ag_job SET lease_until=now()-interval '1 second' WHERE id=:id", id=job["id"]
            )
        release.set()
        with pytest.raises(DomainError, match="LEASE_LOST"):
            await task
        async with svc.db.transaction() as conn:
            assert (await one(conn, "SELECT state FROM ag_attachment WHERE id=:id", id=aid))[
                "state"
            ] == "DELETING"
        await svc.jobs.recover_expired()
        async with svc.db.transaction() as conn:
            await execute(conn, "UPDATE ag_job SET next_attempt_at=now() WHERE id=:id", id=job["id"])
        await run_job(svc, job["id"])
        async with svc.db.transaction() as conn:
            erased = await one(conn, "SELECT * FROM ag_attachment WHERE id=:id", id=aid)
            assert erased["state"] == "DELETED" and erased["storage_deleted_at"]
        assert objects.calls == 2 and not objects.exists and not bridge.calls
    finally:
        release.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await cleanup(svc, member)


async def old_message(svc, member, cid):
    sent = await svc.conversations.send(
        member,
        "synthetic-only",
        cid,
        str(uuid4()),
        {"client_message_id": str(uuid4()), "text": "EXPIRED_PRIVATE_CONTEXT", "attachment_ids": []},
    )
    claimed = await svc.runs.claim("retention-prepare-old")
    assert str(claimed["id"]) == sent["run"]["id"]
    await svc.runs.finish(claimed)
    async with svc.db.transaction() as conn:
        await execute(
            conn,
            "UPDATE ag_message SET created_at=now()-interval '2 days',retention_until=now()-interval '1 second' WHERE id=CAST(:id AS uuid)",
            id=sent["message"]["id"],
        )
    return sent


async def erase_old(svc, cid):
    async with svc.db.transaction() as conn:
        return await svc.extensions.retention.require().erase_messages(
            conn, cid, PublicPolicy(message_retention_days=1), datetime.now(UTC)
        )


@pytest.mark.anyio
async def test_expiry_fences_late_generation_and_purges_old_checkpoint_without_erasing_new_thread(
    integration_config,
):
    svc, member, cid, bridge = await setup(integration_config)
    try:
        await old_message(svc, member, cid)
        fresh = await svc.conversations.send(
            member,
            "synthetic-only",
            cid,
            str(uuid4()),
            {"client_message_id": str(uuid4()), "text": "FRESH_REQUEST", "attachment_ids": []},
        )
        claimed = await svc.runs.claim("retention-active-worker")
        assert str(claimed["id"]) == fresh["run"]["id"]
        assert await svc.runs.append(claimed, "已经输出的部分")
        old_thread = {"configurable": {"thread_id": claimed["checkpoint_thread_id"]}}
        async with AsyncPostgresSaver.from_conn_string(svc.conversations.settings.checkpoint_url()) as saver:
            graph = build_graph(FixtureDialogProvider(), saver)
            await graph.ainvoke(
                {"question": "EXPIRED_PRIVATE_CHECKPOINT", "products": [], "answer": ""}, old_thread
            )
            assert await erase_old(svc, cid) == 1
            assert not await svc.runs.append(claimed, "过期后不得展示")
            await svc.runs.finish(claimed)
            with pytest.raises(DomainError, match="LEASE_LOST"):
                await svc.runs.history(claimed)
            async with svc.db.transaction() as conn:
                old_run = await one(conn, "SELECT * FROM ag_run WHERE id=:id", id=claimed["id"])
                assert old_run["state"] == "INTERRUPTED" and old_run["error_code"] == "CONTEXT_EXPIRED"
                job = await one(
                    conn,
                    "SELECT id FROM ag_job WHERE parameters->>'kind'='CONTEXT_EXPIRY' AND parameters->>'conversation_id'=:id ORDER BY created_at DESC LIMIT 1",
                    id=str(cid),
                )
            new = await svc.conversations.send(
                member,
                "synthetic-only",
                cid,
                str(uuid4()),
                {"client_message_id": str(uuid4()), "text": "NEXT_VISIBLE_REQUEST", "attachment_ids": []},
            )
            current = await svc.runs.claim("retention-new-worker")
            assert str(current["id"]) == new["run"]["id"]
            assert (
                current["memory_epoch"] > claimed["memory_epoch"]
                and current["checkpoint_thread_id"] != claimed["checkpoint_thread_id"]
            )
            assert "EXPIRED_PRIVATE_CONTEXT" not in encoded(await svc.runs.history(current))
            new_thread = {"configurable": {"thread_id": current["checkpoint_thread_id"]}}
            await graph.ainvoke({"question": "FRESH_CHECKPOINT", "products": [], "answer": ""}, new_thread)
            # Simulate a delayed old checkpointer before the grace-period cleanup.
            await graph.ainvoke({"question": "LATE_OLD_CHECKPOINT", "products": [], "answer": ""}, old_thread)
            await run_job(svc, job["id"])
            assert await saver.aget_tuple(old_thread) is None
            assert await saver.aget_tuple(new_thread)
            assert await svc.runs.append(current, "新世代仍可回复")
            await svc.runs.finish(current)
        assert not bridge.calls
    finally:
        await cleanup(svc, member)


@pytest.mark.anyio
@pytest.mark.parametrize("first", ["reply", "expiry"])
async def test_staff_reply_and_expiry_serialize_preserving_human_assignment_and_fresh_reply(
    integration_config, monkeypatch, first
):
    from app.application import handoffs as handoff_module
    from app.application import retention as retention_module

    svc, member, cid, bridge = await setup(integration_config)
    started, release = asyncio.Event(), asyncio.Event()
    first_task = second_task = None
    try:
        await old_message(svc, member, cid)
        staff = actor(True)
        handoffs = svc.extensions.handoffs.require()
        await online(handoffs, staff)
        handoff = await request(svc, member, {"id": str(cid)})
        await claim(handoffs, staff, handoff)
        reply_body = {
            "client_message_id": str(uuid4()),
            "text": "FRESH_STAFF_REPLY",
            "attachment_ids": [],
            "cards": [],
        }

        async def reply():
            return await handoffs.send(staff, cid, str(uuid4()), reply_body)

        if first == "reply":
            original = handoff_module.one

            async def paused(conn, sql, **parameters):
                result = await original(conn, sql, **parameters)
                if sql.startswith("SELECT * FROM ag_conversation") and parameters.get("id") == cid:
                    started.set()
                    await release.wait()
                return result

            monkeypatch.setattr(handoff_module, "one", paused)
            first_task = asyncio.create_task(reply())
            await asyncio.wait_for(started.wait(), 15)
            second_task = asyncio.create_task(erase_old(svc, cid))
        else:
            original = retention_module.one

            async def paused(conn, sql, **parameters):
                result = await original(conn, sql, **parameters)
                if sql.startswith("SELECT * FROM ag_conversation") and parameters.get("id") == cid:
                    started.set()
                    await release.wait()
                return result

            monkeypatch.setattr(retention_module, "one", paused)
            first_task = asyncio.create_task(erase_old(svc, cid))
            await asyncio.wait_for(started.wait(), 15)
            second_task = asyncio.create_task(reply())
        for _ in range(50):
            async with svc.db.transaction() as conn:
                blocked = await one(
                    conn,
                    "SELECT count(*) n FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE '%ag_conversation%'",
                )
            if blocked["n"]:
                break
            await asyncio.sleep(0.05)
        assert blocked["n"] and not second_task.done()
        release.set()
        await asyncio.gather(first_task, second_task)
        snapshot = await svc.conversations.snapshot(member, cid)
        assert snapshot["conversation"]["state"] == "HUMAN_ACTIVE" and snapshot["conversation"][
            "assigned_staff"
        ]["id"] == str(staff.id)
        assert "FRESH_STAFF_REPLY" in encoded(snapshot) and "EXPIRED_PRIVATE_CONTEXT" not in encoded(snapshot)
        assert not snapshot["active_run"] and not bridge.calls
    finally:
        release.set()
        for task in (first_task, second_task):
            if task:
                await asyncio.gather(task, return_exceptions=True)
        await cleanup(svc, member)


@pytest.mark.anyio
async def test_sweep_waiting_for_proof_transaction_rechecks_committed_reference_after_attachment_lock(
    integration_config, monkeypatch
):
    from app.application import actions as actions_module

    svc, member, cid, bridge = await setup(integration_config)
    protected, commit = asyncio.Event(), asyncio.Event()
    prepare_task = sweep_task = None
    try:
        aid = await attachment(svc, member)
        original_execute = actions_module.execute

        async def pause_after_reference(conn, sql, **parameters):
            result = await original_execute(conn, sql, **parameters)
            if sql.startswith("INSERT INTO ag_attachment_ref") and parameters.get("attachment") == str(aid):
                protected.set()
                await commit.wait()
            return result

        monkeypatch.setattr(actions_module, "execute", pause_after_reference)
        prepare_task = asyncio.create_task(prepare_return(svc, member, cid, aid))
        await asyncio.wait_for(protected.wait(), 15)
        # The valid proof's insert is uncommitted while a stricter policy becomes active.
        policy = await shorten(svc)

        async def sweep():
            async with svc.db.transaction() as conn:
                return await svc.extensions.retention.require().sweep_attachments(
                    conn, policy, datetime.now(UTC), skipped
                )

        skipped = {}
        sweep_task = asyncio.create_task(sweep())
        for _ in range(50):
            async with svc.db.transaction() as conn:
                blocked = await one(
                    conn,
                    "SELECT count(*) n FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE :pattern",
                    pattern="%ag_attachment a WHERE state%",
                )
            if blocked["n"]:
                break
            await asyncio.sleep(0.05)
        assert blocked["n"] and not sweep_task.done(), "The sweep must actually wait on the proof transaction"
        commit.set()
        result = await prepare_task
        await sweep_task
        assert skipped["protected_attachments"] >= 1
        async with svc.db.transaction() as conn:
            item = await one(conn, "SELECT state FROM ag_attachment WHERE id=:id", id=aid)
            assert item["state"] == "READY"
            proof = await one(
                conn,
                "SELECT protected,released_at FROM ag_attachment_ref WHERE attachment_id=:id AND action_id=CAST(:action AS uuid)",
                id=aid,
                action=result["id"],
            )
            assert proof["protected"] and proof["released_at"] is None
            assert not await one(
                conn,
                "SELECT id FROM ag_job WHERE job_type='ATTACHMENT_DELETE' AND parameters->>'attachment_id'=:id",
                id=str(aid),
            )
            assert not await one(
                conn,
                "SELECT resource_id FROM ag_erasure_tombstone WHERE resource_type='ATTACHMENT' AND resource_id=:id",
                id=str(aid),
            )
        assert not bridge.calls
    finally:
        commit.set()
        for task in (prepare_task, sweep_task):
            if task:
                await asyncio.gather(task, return_exceptions=True)
        await cleanup(svc, member)


@pytest.mark.anyio
async def test_proof_prepare_waiting_on_attachment_rechecks_policy_that_committed_while_waiting(
    integration_config,
):
    svc, member, cid, bridge = await setup(integration_config)
    task = None
    try:
        aid = await attachment(svc, member)
        async with svc.db.transaction() as holder:
            await one(holder, "SELECT id FROM ag_attachment WHERE id=:id FOR UPDATE", id=aid)
            task = asyncio.create_task(prepare_return(svc, member, cid, aid))
            for _ in range(50):
                async with svc.db.transaction() as conn:
                    wait = await one(
                        conn,
                        "SELECT count(*) n FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE '%ag_attachment%'",
                    )
                if wait["n"]:
                    break
                await asyncio.sleep(0.05)
            assert wait["n"] and not task.done()
            await shorten(svc)
        with pytest.raises(DomainError, match="ATTACHMENT_UNAVAILABLE"):
            await task
        async with svc.db.transaction() as conn:
            assert not await one(conn, "SELECT id FROM ag_attachment_ref WHERE attachment_id=:id", id=aid)
            assert not await one(conn, "SELECT id FROM ag_action WHERE member_id=:id", id=member.id)
        assert not bridge.calls
    finally:
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await cleanup(svc, member)
