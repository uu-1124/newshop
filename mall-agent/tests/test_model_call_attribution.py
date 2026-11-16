"""Real isolated PG accounting with synthetic adapter IO; no supplier calls."""

import asyncio
from uuid import UUID, uuid4

import httpx
import pytest
from test_runtime import settings_for

from app.bootstrap import Services
from app.domain.identity import DomainError, Principal
from app.infrastructure.aliyun_models import AliyunModels
from app.infrastructure.model_call_context import ModelCallContext, current_model_call, model_call_scope
from app.infrastructure.model_quota import model_actor
from app.infrastructure.postgres.database import execute, one, rows


async def active_run(svc, member):
    conversation = await svc.conversations.create(
        member, str(uuid4()), {"title": "[TEST ONLY] attribution", "entry_context": {"kind": "GENERAL"}}
    )
    sent = await svc.conversations.send(
        member,
        "Bearer SYNTHETIC_ONLY",
        conversation["id"],
        str(uuid4()),
        {"client_message_id": str(uuid4()), "text": "仅测试调用记账关联，不执行业务", "attachment_ids": []},
    )
    rid = UUID(sent["run"]["id"])
    async with svc.db.transaction() as conn:
        await execute(
            conn,
            "UPDATE ag_run SET state='RUNNING',lease_owner='attribution-test',lease_until=now()+interval '5 minutes' WHERE id=:id",
            id=rid,
        )
    return ModelCallContext(run_id=rid, lease_owner="attribution-test", fencing_token=1)


async def active_job(svc, actor):
    jid = uuid4()
    async with svc.db.transaction() as conn:
        await execute(
            conn,
            "INSERT INTO ag_job(id,job_type,dedupe_key,parameters,state,created_by,lease_owner,lease_until) VALUES(:id,'MODEL_COMPATIBILITY',:key,'{}','RUNNING',:actor,'attribution-job',now()+interval '5 minutes')",
            id=jid,
            key=str(jid),
            actor=actor,
        )
    return ModelCallContext(job_id=jid, lease_owner="attribution-job", fencing_token=1)


async def end_test_job(svc, context):
    async with svc.db.transaction() as conn:
        await execute(
            conn,
            "UPDATE ag_job SET state='FAILED',lease_owner=NULL,lease_until=NULL WHERE id=:id AND state IN ('QUEUED','RUNNING')",
            id=context.job_id,
        )


@pytest.mark.anyio
async def test_auxiliary_adapter_reserves_a_durable_run_link_before_io_and_keeps_actor_after_cancellation(
    integration_config,
):
    svc = Services(settings_for(integration_config))
    buyer = Principal(2600000000 + uuid4().int % 10000000, "attribution buyer")
    await svc.models.setup()
    context = await active_run(svc, buyer)
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def transport(request):
        async with svc.db.transaction() as conn:
            record = await one(
                conn,
                "SELECT c.*,m.actor_kind,m.actor_id FROM ag_model_call c JOIN ag_model_meter m ON m.call_id=c.id WHERE c.run_id=:rid AND c.capability='EMBEDDING'",
                rid=context.run_id,
            )
            assert record and record["status"] == "UNKNOWN" and record["error_code"] == "ADAPTER_IN_FLIGHT"
            assert (
                record["job_id"] is None
                and record["actor_kind"] == "MEMBER"
                and record["actor_id"] == buyer.id
            )
            seen.append(record["id"])
        entered.set()
        await release.wait()
        return httpx.Response(
            200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}], "usage": {"total_tokens": 2}}
        )

    adapter = AliyunModels(
        svc.models.settings.model_copy(update={"embedding_dimension": 2}),
        svc.models,
        httpx.MockTransport(transport),
    )
    task = None
    try:
        with model_call_scope(context):
            task = asyncio.create_task(adapter.embed(["合成关联测试"]))
        await asyncio.wait_for(entered.wait(), 30)
        assert current_model_call.get() is None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with svc.db.transaction() as conn:
            record = await one(conn, "SELECT * FROM ag_model_call WHERE id=:id", id=seen[0])
            assert record["run_id"] == context.run_id and record["status"] == "CANCELLED"
            assert record["error_code"] == "MODEL_CANCELLED_BILLING_UNKNOWN"
            meter = await one(conn, "SELECT usage_state FROM ag_model_meter WHERE call_id=:id", id=seen[0])
            assert meter["usage_state"] == "UNKNOWN"
    finally:
        release.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await adapter.close()
        await svc.close()


@pytest.mark.anyio
async def test_concurrent_run_job_and_standalone_recorders_cannot_cross_attribute_or_rewrite_completed_calls(
    integration_config,
):
    svc = Services(settings_for(integration_config))
    await svc.models.setup()
    buyer = Principal(2620000000 + uuid4().int % 10000000, "concurrent attribution")
    staff = 2640000000 + uuid4().int % 10000000
    run_context = await active_run(svc, buyer)
    job_context = await active_job(svc, staff)
    try:

        async def work(context, cap):
            with model_call_scope(context):
                record = svc.models.recorder(cap)
                call = await record({"stage": "reserve", "reservation_units": {"TOKEN": 10}})
            # Completion keeps captured source even after task-local scope is reset.
            await record(
                {"call_id": call, "status": "SUCCEEDED", "usage": {"total_tokens": 2}, "latency_ms": 5}
            )
            await record({"call_id": call, "status": "FAILED", "usage": {}, "latency_ms": 999})
            return UUID(call)

        calls = await asyncio.gather(work(run_context, "RERANK"), work(job_context, "EMBEDDING"))
        standalone = svc.models.recorder("EMBEDDING")
        other = UUID(await standalone({"stage": "reserve"}))
        async with svc.db.transaction() as conn:
            records = await rows(
                conn,
                "SELECT c.*,m.actor_kind,m.actor_id FROM ag_model_call c JOIN ag_model_meter m ON m.call_id=c.id WHERE c.id=ANY(CAST(:ids AS uuid[]))",
                ids=calls + [other],
            )
        records = {row["id"]: row for row in records}
        assert records[calls[0]]["run_id"] == run_context.run_id
        assert records[calls[0]]["job_id"] is None and records[calls[0]]["actor_id"] == buyer.id
        assert records[calls[1]]["job_id"] == job_context.job_id
        assert (
            records[calls[1]]["run_id"] is None
            and records[calls[1]]["actor_kind"] == "STAFF"
            and records[calls[1]]["actor_id"] == staff
        )
        assert all(records[c]["status"] == "SUCCEEDED" and records[c]["latency_ms"] == 5 for c in calls)
        assert (
            records[other]["run_id"] is None
            and records[other]["job_id"] is None
            and records[other]["actor_kind"] == "SYSTEM"
        )
        assert current_model_call.get() is None and model_actor.get() == ("SYSTEM", None)
        # A different recorder cannot claim an earlier call ID even inside another valid scope.
        with model_call_scope(job_context):
            wrong = svc.models.recorder("RERANK")
        with pytest.raises(DomainError, match="MODEL_CALL_CONTEXT_MISMATCH"):
            await wrong({"call_id": str(calls[0]), "status": "FAILED", "usage": {}, "latency_ms": 1})
    finally:
        await end_test_job(svc, job_context)
        await svc.close()


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["stopped", "expired", "epoch", "memory", "fence", "deleted", "message"])
async def test_invalid_run_context_is_rejected_before_any_reservation(integration_config, change):
    svc = Services(settings_for(integration_config))
    await svc.models.setup()
    context = await active_run(svc, Principal(2650000000 + uuid4().int % 10000000, "invalid context"))
    try:
        async with svc.db.transaction() as conn:
            if change in {"stopped", "expired", "fence"}:
                setter = {
                    "stopped": "state='STOPPED'",
                    "expired": "lease_until=now()-interval '1 second'",
                    "fence": "fencing_token=fencing_token+1",
                }[change]
                await execute(conn, "UPDATE ag_run SET " + setter + " WHERE id=:id", id=context.run_id)
            elif change == "message":
                await execute(
                    conn,
                    "UPDATE ag_message SET deleted_at=now() WHERE id=(SELECT trigger_message_id FROM ag_run WHERE id=:id)",
                    id=context.run_id,
                )
            else:
                setter = {
                    "epoch": "control_epoch=control_epoch+1",
                    "memory": "memory_epoch=memory_epoch+1",
                    "deleted": "deleted_at=now()",
                }[change]
                await execute(
                    conn,
                    "UPDATE ag_conversation SET "
                    + setter
                    + " WHERE id=(SELECT conversation_id FROM ag_run WHERE id=:id)",
                    id=context.run_id,
                )
        with model_call_scope(context):
            record = svc.models.recorder("EMBEDDING")
        with pytest.raises(DomainError, match="LEASE_LOST"):
            await record({"stage": "reserve"})
        async with svc.db.transaction() as conn:
            assert (
                await one(conn, "SELECT count(*) n FROM ag_model_call WHERE run_id=:id", id=context.run_id)
            )["n"] == 0
    finally:
        await svc.close()


@pytest.mark.anyio
async def test_job_worker_binds_adapter_calls_and_clears_scope_on_return(integration_config):
    from app.application.jobs import JobHandlers, Jobs
    from app.domain.capabilities import JobType
    from app.workers.jobs import execute_job

    svc = Services(settings_for(integration_config))
    await svc.models.setup()
    context = await active_job(svc, 2660000000 + uuid4().int % 10000000)
    calls = []
    registry = JobHandlers()

    async def handler(lease):
        assert current_model_call.get() == context
        record = svc.models.recorder("EMBEDDING")
        call = await record({"stage": "reserve"})
        calls.append(call)
        await record(
            {
                "call_id": call,
                "status": "FAILED",
                "usage": {},
                "latency_ms": 2,
                "error_code": "MODEL_UNAVAILABLE",
            }
        )
        return {"synthetic": True}

    registry.register(JobType.MODEL_COMPATIBILITY, handler)
    jobs = Jobs(svc.db, settings_for(integration_config), registry)
    try:
        async with svc.db.transaction() as conn:
            job = await one(conn, "SELECT * FROM ag_job WHERE id=:id", id=context.job_id)
        result = await execute_job(jobs, registry, job)
        assert result["state"] == "SUCCEEDED"
        assert current_model_call.get() is None and model_actor.get() == ("SYSTEM", None)
        async with svc.db.transaction() as conn:
            call = await one(conn, "SELECT * FROM ag_model_call WHERE id=:id", id=calls[0])
            assert call["job_id"] == context.job_id and call["run_id"] is None
            assert call["status"] == "FAILED" and call["error_code"] == "MODEL_UNAVAILABLE"
    finally:
        await svc.close()


@pytest.mark.anyio
async def test_unknown_usage_batch_has_source_and_actor_without_per_call_queries(integration_config):
    from sqlalchemy import event
    from test_governance import operator

    from app.application.governance import Governance

    svc = Services(settings_for(integration_config))
    await svc.models.setup()
    context = await active_run(svc, Principal(2670000000 + uuid4().int % 10000000, "usage page"))
    count = [0]

    def counted(*args):
        count[0] += 1

    try:
        with model_call_scope(context):
            record = svc.models.recorder("RERANK")
            call = await record({"stage": "reserve", "reservation_units": {"TOKEN": 10}})
        event.listen(svc.db.engine.sync_engine, "before_cursor_execute", counted)
        page = await Governance(svc.db).unknown_usage(operator(), limit=100)
        event.remove(svc.db.engine.sync_engine, "before_cursor_execute", counted)
        assert count[0] == 2
        item = next(item for item in page["items"] if item["call_id"] == call)
        assert item["run_id"] == str(context.run_id) and item["job_id"] is None
        assert item["actor_kind"] == "MEMBER" and item["error_code"] == "ADAPTER_IN_FLIGHT"
        assert {r["unit"] for r in item["reservations"]} == {"REQUEST", "TOKEN"}
        assert "secret" not in str(item).lower()
    finally:
        if event.contains(svc.db.engine.sync_engine, "before_cursor_execute", counted):
            event.remove(svc.db.engine.sync_engine, "before_cursor_execute", counted)
        await svc.close()


@pytest.mark.anyio
async def test_source_lost_while_reserving_rolls_back_call_meter_and_quota_together(
    integration_config, monkeypatch
):
    import app.infrastructure.model_ledger as ledger_module

    svc = Services(settings_for(integration_config))
    await svc.models.setup()
    context = await active_run(svc, Principal(2680000000 + uuid4().int % 10000000, "reservation race"))
    entered, release = asyncio.Event(), asyncio.Event()
    call_ids = []
    original = ledger_module.reserve

    async def paused(*args, **kwargs):
        call = await original(*args, **kwargs)
        call_ids.append(call)
        entered.set()
        await release.wait()
        return call

    monkeypatch.setattr(ledger_module, "reserve", paused)
    task = None
    try:
        with model_call_scope(context):
            record = svc.models.recorder("EMBEDDING")
            task = asyncio.create_task(record({"stage": "reserve"}))
        await asyncio.wait_for(entered.wait(), 30)
        async with svc.db.transaction() as conn:
            await execute(conn, "UPDATE ag_run SET state='STOPPED' WHERE id=:id", id=context.run_id)
        release.set()
        with pytest.raises(DomainError, match="LEASE_LOST"):
            await task
        async with svc.db.transaction() as conn:
            for table, key in (
                ("ag_model_call", "id"),
                ("ag_model_meter", "call_id"),
                ("ag_quota_reservation", "call_id"),
            ):
                assert (await one(conn, f"SELECT count(*) n FROM {table} WHERE {key}=:id", id=call_ids[0]))[
                    "n"
                ] == 0
    finally:
        release.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await svc.close()


@pytest.mark.anyio
async def test_actual_subprocess_exit_keeps_linked_reservation_without_inventing_success(integration_config):
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    svc = Services(settings_for(integration_config))
    context = await active_job(svc, 2690000000 + uuid4().int % 10000000)
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "tests/model_attribution_crash_child.py", str(context.job_id)],
            cwd=Path(__file__).resolve().parents[1],
            env={
                **os.environ,
                "ATTRIBUTION_TEST_DATABASE": integration_config["databaseUrl"],
                "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
            },
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert result.returncode == 73, "Synthetic crash child failed before the requested exit point"
        call_id = json.loads(result.stdout.strip())["call_id"]
        async with svc.db.transaction() as conn:
            row = await one(conn, "SELECT * FROM ag_model_call WHERE id=:id", id=call_id)
            meter = await one(conn, "SELECT * FROM ag_model_meter WHERE call_id=:id", id=call_id)
            quotas = await rows(
                conn, "SELECT state,settled_units FROM ag_quota_reservation WHERE call_id=:id", id=call_id
            )
        assert row["job_id"] == context.job_id and row["run_id"] is None
        assert row["status"] == "UNKNOWN" and row["error_code"] == "ADAPTER_IN_FLIGHT"
        assert meter["actor_kind"] == "STAFF" and meter["usage_state"] == "PENDING"
        assert quotas and all(q["state"] == "RESERVED" and q["settled_units"] is None for q in quotas)
    finally:
        await end_test_job(svc, context)
        await svc.close()


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["expired", "fence", "finished"])
async def test_invalid_job_context_and_conflicting_explicit_run_do_not_create_billing(
    integration_config, change
):
    svc = Services(settings_for(integration_config))
    await svc.models.setup()
    context = await active_job(svc, None)
    try:
        async with svc.db.transaction() as conn:
            setter = {
                "expired": "lease_until=now()-interval '1 second'",
                "fence": "fencing_token=fencing_token+1",
                "finished": "state='SUCCEEDED'",
            }[change]
            await execute(conn, "UPDATE ag_job SET " + setter + " WHERE id=:id", id=context.job_id)
        with model_call_scope(context):
            record = svc.models.recorder("EMBEDDING")
            with pytest.raises(DomainError, match="MODEL_CALL_CONTEXT_MISMATCH"):
                svc.models.recorder("EMBEDDING", uuid4())
        with pytest.raises(DomainError, match="LEASE_LOST"):
            await record({"stage": "reserve"})
        async with svc.db.transaction() as conn:
            assert (
                await one(conn, "SELECT count(*) n FROM ag_model_call WHERE job_id=:id", id=context.job_id)
            )["n"] == 0
    finally:
        await end_test_job(svc, context)
        await svc.close()
