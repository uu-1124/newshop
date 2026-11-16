from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from test_runtime import settings_for

from app.application.backups import put_state
from app.application.governance import Governance
from app.application.observability import (
    DEPENDENCIES,
    STATE_KEY,
    alerts,
    fresh,
    readiness,
    runtime_snapshot,
    validate_snapshot,
)
from app.bootstrap import Services
from app.infrastructure.dependency_probes import measured, signed_bridge
from app.infrastructure.postgres.database import Database, execute, one
from app.main import create_app


def snapshot():
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "duration_ms": 100,
        "dependencies": {name: {"state": "UP", "latency_ms": 1} for name in DEPENDENCIES},
        "cloud": {
            "memory_total_bytes": 10000,
            "memory_available_bytes": 5000,
            "disk_total_bytes": 10000,
            "disk_free_bytes": 5000,
            "swap_used_bytes": 0,
            "containers_running": 8,
            "containers_expected": 8,
            "containers_unhealthy": 0,
            "containers_oom": 0,
            "container_restarts": 0,
        },
        "postgres": {
            **{
                name: 0
                for name in [
                    "connections",
                    "lock_waiters",
                    "longest_transaction_seconds",
                    "active_slots",
                    "queued_runs",
                    "oldest_queued_seconds",
                    "queued_jobs",
                    "oldest_job_seconds",
                    "job_wakeups_pending",
                    "event_notifications_pending",
                    "event_clock",
                    "event_replay_floor",
                    "event_clock_violations",
                    "pool_wait_p95_ms",
                    "pool_saturated_workers",
                    "sse_cursor_expired_5m",
                    "sse_invalid_cursor_5m",
                    "first_text_samples",
                ]
            },
            "max_connections": 100,
        },
        "java_outbox": {"pending_due": 0, "overdue_60_seconds": 0, "oldest_due_seconds": 0, "retrying": 0},
    }


def test_operational_alerts_use_actual_thresholds_and_never_trust_stale_good_samples():
    value = snapshot()
    good = validate_snapshot(value)
    assert fresh(good) and not alerts(good)
    value["cloud"].update(
        memory_available_bytes=1400,
        disk_free_bytes=1900,
        containers_unhealthy=1,
        containers_oom=1,
        containers_running=7,
    )
    value["postgres"].update(
        connections=85,
        lock_waiters=2,
        longest_transaction_seconds=31,
        queued_runs=4,
        oldest_queued_seconds=61,
        pool_wait_p95_ms=1001,
        pool_saturated_workers=1,
        sse_cursor_expired_5m=3,
        event_clock_violations=1,
    )
    value["java_outbox"].update(overdue_60_seconds=1, retrying=2)
    codes = {item["code"] for item in alerts(validate_snapshot(value))}
    assert {
        "CLOUD_MEMORY_PRESSURE",
        "CLOUD_DISK_PRESSURE",
        "CONTAINER_UNHEALTHY",
        "CONTAINER_OOM_RECORDED",
        "POSTGRES_CONNECTION_PRESSURE",
        "POSTGRES_LOCK_WAIT",
        "POSTGRES_LONG_TRANSACTION",
        "POSTGRES_POOL_WAIT",
        "GENERATION_QUEUE_OVER_60_SECONDS",
        "SSE_REPLAY_EXPIRED_BURST",
        "JAVA_OUTBOX_OVERDUE",
        "JAVA_OUTBOX_RETRYING",
    } <= codes
    value["checked_at"] = (datetime.now(UTC) - timedelta(seconds=91)).isoformat()
    assert {x["code"] for x in alerts(validate_snapshot(value))} == {"OBSERVABILITY_STALE"}
    value["dependencies"]["arbitrary_host"] = {"state": "UP", "latency_ms": 0}
    with pytest.raises(ValueError):
        validate_snapshot(value)


def test_readiness_enforces_configured_critical_services_but_keeps_notification_degradation_separate():
    settings = SimpleNamespace(
        staff_bridge_secret=SecretStr("configured"), attachments_enabled=True, knowledge_enabled=True
    )
    value = snapshot()
    value["dependencies"]["redis"]["state"] = "DOWN"
    assert readiness(validate_snapshot(value), settings)[0]
    value["dependencies"]["private_objects"]["state"] = "DOWN"
    assert not readiness(validate_snapshot(value), settings)[0]
    assert not readiness(None, settings)[0]


@pytest.mark.anyio
async def test_signed_bridge_probe_requires_signature_validation_and_unauthenticated_identity_rejection():
    settings = SimpleNamespace(
        bridge_secret=SecretStr("service-key"), staff_bridge_secret=SecretStr("staff-key")
    )

    def accepted(request):
        assert request.url.path == "/agent-bridge/v1/identity"
        assert "authorization" not in request.headers
        assert request.headers["x-agent-signature"].startswith("v1:")
        return httpx.Response(401, json={"code": "AUTH_EXPIRED"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(accepted)) as client:
        result = await measured(lambda: signed_bridge(client, settings))
        assert result.state == "UP"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(403, json={"code": "BRIDGE_SIGNATURE_INVALID"})
        )
    ) as client:
        result = await measured(lambda: signed_bridge(client, settings))
        assert result.state == "DOWN" and "key" not in result.model_dump_json()


@pytest.mark.anyio
async def test_pool_metrics_observe_transactions_without_breaking_reuse_or_rollback(integration_config):
    db = Database(integration_config["databaseUrl"])
    try:
        async with db.transaction() as conn:
            assert (await one(conn, "SELECT 1 n"))["n"] == 1
        with pytest.raises(RuntimeError):
            async with db.transaction(snapshot=True) as conn:
                assert (await one(conn, "SELECT current_setting('transaction_isolation') value"))[
                    "value"
                ] == "repeatable read"
                raise RuntimeError("test rollback")
        assert db.pool_metrics()["samples"] == 2 and db.pool_metrics()["checked_out"] == 0
        async with db.transaction() as conn:
            await put_state(conn, STATE_KEY, snapshot())
            read = await runtime_snapshot(conn)
            assert read.dependencies["postgres"].state == "UP"
    finally:
        await db.engine.dispose()


def test_remote_outbox_observation_is_fixed_and_read_only():
    source = (Path(__file__).resolve().parents[2] / "deployment/observe-remote.py").read_text(
        encoding="utf-8"
    )
    assert "START TRANSACTION READ ONLY" in source and "JSON_OBJECT" in source
    assert "os.environ" not in source and "input(" not in source


@pytest.mark.anyio
async def test_readyz_refuses_expired_or_failed_critical_probe_and_preserves_livez(integration_config):
    settings = settings_for(integration_config, observability_enabled=True)
    svc = Services(settings)
    app = create_app(settings)
    app.state.services = svc
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            async with svc.db.transaction() as conn:
                await put_state(conn, STATE_KEY, snapshot())
            assert (await client.get("/readyz")).status_code == 200
            value = snapshot()
            value["dependencies"]["portal_bridge"]["state"] = "DOWN"
            async with svc.db.transaction() as conn:
                await put_state(conn, STATE_KEY, value)
            response = await client.get("/readyz")
            assert response.status_code == 503 and response.json()["code"] == "DEPENDENCY_NOT_READY"
            assert (await client.get("/livez")).status_code == 200
            value = snapshot()
            value["checked_at"] = (datetime.now(UTC) - timedelta(seconds=91)).isoformat()
            async with svc.db.transaction() as conn:
                await put_state(conn, STATE_KEY, value)
            assert (await client.get("/readyz")).status_code == 503
    finally:
        await svc.close()


@pytest.mark.anyio
async def test_unknown_age_survives_recent_reconciliation_and_repeated_job_failure_alerts(integration_config):
    db = Database(integration_config["databaseUrl"])
    cid, aid = uuid4(), uuid4()
    try:
        with pytest.raises(RuntimeError, match="rollback fixture"):
            async with db.transaction() as conn:
                await execute(
                    conn,
                    "INSERT INTO ag_conversation(id,member_id,title) VALUES(:id,913000001,'[TEST ONLY] alert age')",
                    id=cid,
                )
                await execute(
                    conn,
                    """INSERT INTO ag_action(id,conversation_id,member_id,action_type,state,conversation_version,control_epoch,
                    parameters,parameters_hash,operation_no,expires_at,confirmed_at,created_at,updated_at)
                    VALUES(:id,:cid,913000001,'ADD_CART','UNKNOWN',1,1,'{}'::jsonb,:hash,:id,now()-interval '1 hour',now()-interval '2 hours',now()-interval '2 hours',now())""",
                    id=aid,
                    cid=cid,
                    hash="0" * 64,
                )
                for _ in range(3):
                    await execute(
                        conn,
                        "INSERT INTO ag_job(id,job_type,dedupe_key,parameters,state) VALUES(:id,'BACKUP_VERIFY',:key,'{}'::jsonb,'FAILED')",
                        id=uuid4(),
                        key="[TEST ONLY] " + str(uuid4()),
                    )
                actual = await Governance(db).alerts_in(conn)
                codes = {value["code"] for value in actual}
                assert {
                    "ACTION_UNKNOWN_OVER_10_MIN",
                    "ACTION_UNKNOWN_OVER_1_HOUR",
                    "JOB_TYPE_REPEATED_FAILURE",
                } <= codes
                raise RuntimeError("rollback fixture")
    finally:
        await db.engine.dispose()
