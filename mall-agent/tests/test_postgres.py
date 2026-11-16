import asyncio
from uuid import uuid4

import pytest

from app.application.conversations import Conversations
from app.application.runs import Runs
from app.domain.identity import DomainError, Principal
from app.infrastructure.cursors import Cursors
from app.infrastructure.model_ledger import ModelLedger
from app.infrastructure.postgres.database import Database, append_events, execute, one
from app.infrastructure.runtime_auth import RuntimeAuth
from app.main import create_app
from app.settings import Settings


@pytest.mark.anyio
async def test_persistent_idempotency_isolation_and_event_order(integration_config):
    cfg = integration_config
    settings = Settings(
        database_url=cfg["databaseUrl"],
        bridge_secret=cfg["bridgeSecret"],
        broker_secret=cfg["brokerSecret"],
        cursor_secret=cfg["cursorSecret"],
    )
    db = Database(cfg["databaseUrl"])
    svc = Conversations(db, RuntimeAuth(180), Cursors(cfg["cursorSecret"]))
    # Synthetic IDs exist only in the isolated agent DB, never in mall MySQL.
    member = Principal(900000001, "integration-test")
    other = Principal(900000002, "other-test")
    key = uuid4()
    body = {"title": "integration-test", "entry_context": {"kind": "GENERAL"}}
    try:
        a, b = await asyncio.gather(svc.create(member, key, body), svc.create(member, key, body))
        assert a["id"] == b["id"]
        cid = a["id"]
        with pytest.raises(DomainError) as forbidden:
            await svc.snapshot(other, cid)
        assert forbidden.value.status == 404
        with pytest.raises(DomainError) as conflict:
            await svc.create(member, key, body | {"title": "changed"})
        assert conflict.value.code == "IDEMPOTENCY_CONFLICT"

        message_key = uuid4()
        payload = {"client_message_id": str(uuid4()), "text": "并发消息😀", "attachment_ids": []}
        first, replay = await asyncio.gather(
            svc.send(member, "test-token-not-a-real-session", cid, message_key, payload),
            svc.send(member, "test-token-not-a-real-session", cid, message_key, payload),
        )
        assert first["message"]["id"] == replay["message"]["id"]
        assert first["run"]["id"] == replay["run"]["id"]
        with pytest.raises(DomainError) as reused:
            await svc.send(member, "test", cid, uuid4(), payload | {"text": "different"})
        assert reused.value.code == "MESSAGE_ID_REUSED"
        runs = Runs(db, settings)
        await runs.stop(member, first["run"]["id"], uuid4(), {"expected_version": 1})
        assert (await runs.get(member, first["run"]["id"]))["state"] == "STOPPED"

        # A new repository/connection can read the accepted message after a simulated API restart.
        second_db = Database(cfg["databaseUrl"])
        fresh = Conversations(second_db, RuntimeAuth(180), Cursors(cfg["cursorSecret"]))
        snapshot = await fresh.snapshot(member, cid)
        assert snapshot["recent_messages"][0]["text"] == payload["text"]
        await second_db.engine.dispose()
        async with db.transaction() as conn:
            assert not (await one(conn, "SELECT has_schema_privilege(current_user,'public','CREATE') AS p"))[
                "p"
            ]
            # No bearer strings in application data, event JSON, or checkpoint state.
            result = await one(
                conn, "SELECT to_jsonb(r)::text AS value FROM ag_run r WHERE id=:id", id=first["run"]["id"]
            )
            assert "test-token" not in result["value"]
    finally:
        await db.engine.dispose()


@pytest.mark.anyio
async def test_event_clock_blocks_later_commit(integration_config):
    db = Database(integration_config["databaseUrl"])
    svc = Conversations(db, RuntimeAuth(180), Cursors("test"))
    member = Principal(900000003, "clock-test")
    body = {"title": "clock-test", "entry_context": {"kind": "GENERAL"}}
    left = await svc.create(member, uuid4(), body)
    right = await svc.create(member, uuid4(), body)
    allocated = asyncio.Event()
    release = asyncio.Event()

    async def first():
        async with db.transaction() as conn:
            conv = await one(conn, "SELECT * FROM ag_conversation WHERE id=:id FOR UPDATE", id=left["id"])
            cursor = await append_events(conn, conv, [("conversation.updated", {"conversation": left})])
            allocated.set()
            await release.wait()  # deliberate test-only pause while clock is held
            return cursor

    async def second():
        await allocated.wait()
        async with db.transaction() as conn:
            conv = await one(conn, "SELECT * FROM ag_conversation WHERE id=:id FOR UPDATE", id=right["id"])
            return await append_events(conn, conv, [("conversation.updated", {"conversation": right})])

    try:
        a = asyncio.create_task(first())
        b = asyncio.create_task(second())
        await allocated.wait()
        await asyncio.sleep(0.2)
        assert not b.done()
        release.set()
        ca, cb = await asyncio.gather(a, b)
        assert int(cb) == int(ca) + 1
    finally:
        release.set()
        await db.engine.dispose()


@pytest.mark.anyio
async def test_three_global_slots_and_expired_worker_fencing(integration_config):
    cfg = integration_config
    settings = Settings(
        database_url=cfg["databaseUrl"],
        bridge_secret=cfg["bridgeSecret"],
        broker_secret=cfg["brokerSecret"],
        cursor_secret=cfg["cursorSecret"],
    )
    db = Database(cfg["databaseUrl"])
    svc = Conversations(db, RuntimeAuth(180), Cursors(cfg["cursorSecret"]))
    runs = Runs(db, settings)
    principal = Principal(900000004, "slot-test")
    queued = []
    try:
        for i in range(4):
            conv = await svc.create(
                principal, uuid4(), {"title": "slot-test", "entry_context": {"kind": "GENERAL"}}
            )
            result = await svc.send(
                principal,
                "synthetic-token",
                conv["id"],
                uuid4(),
                {"client_message_id": str(uuid4()), "text": f"slot {i}", "attachment_ids": []},
            )
            queued.append(result["run"])
        claims = [await runs.claim("integration-worker") for _ in range(3)]
        assert all(claims)
        assert await runs.claim("fourth-worker") is None
        async with db.transaction() as conn:
            occupied = await one(
                conn, "SELECT count(*) AS n FROM ag_generation_slot WHERE run_id IS NOT NULL"
            )
            assert occupied["n"] == 3
            for claimed in claims:
                await execute(
                    conn,
                    "UPDATE ag_run SET lease_until=now()-interval '1 second' WHERE id=:id",
                    id=claimed["id"],
                )
        assert not await runs.append(claims[0], "late output")
        for _ in range(3):
            assert await runs.recover_expired()
        for claimed in claims:
            assert not await runs.append(claimed, "late output")
            await runs.finish(claimed)
            assert (await runs.get(principal, claimed["id"]))["state"] == "INTERRUPTED"
        async with db.transaction() as conn:
            assert (await one(conn, "SELECT count(*) AS n FROM ag_generation_slot WHERE run_id IS NOT NULL"))[
                "n"
            ] == 0
        fourth = await runs.claim("new-worker")
        assert fourth
        await runs.finish(fourth, "TEST_COMPLETED")
    finally:
        await db.engine.dispose()


@pytest.mark.anyio
async def test_api_restart_interrupts_old_auth_context_and_fences_worker(integration_config):
    cfg = integration_config
    settings = Settings(
        database_url=cfg["databaseUrl"],
        bridge_secret=cfg["bridgeSecret"],
        broker_secret=cfg["brokerSecret"],
        cursor_secret=cfg["cursorSecret"],
    )
    db = Database(cfg["databaseUrl"])
    previous = Conversations(db, RuntimeAuth(180), Cursors(cfg["cursorSecret"]))
    runs = Runs(db, settings)
    principal = Principal(900000005, "auth-restart-test")
    old_runs = []
    try:
        for i in range(2):
            conv = await previous.create(
                principal, uuid4(), {"title": "restart-test", "entry_context": {"kind": "GENERAL"}}
            )
            result = await previous.send(
                principal,
                "synthetic-credential",
                conv["id"],
                uuid4(),
                {"client_message_id": str(uuid4()), "text": f"restart {i}", "attachment_ids": []},
            )
            old_runs.append(result["run"])
        claimed = await runs.claim("worker-before-api-restart")
        assert claimed and str(claimed["id"]) in {item["id"] for item in old_runs}
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            services = app.state.services
            for item in old_runs:
                recovered = await services.runs.get(principal, item["id"])
                assert recovered["state"] == "INTERRUPTED"
                assert recovered["error_code"] == "AUTH_CONTEXT_LOST"
            assert not await runs.append(claimed, "stale worker output")
            await runs.finish(claimed)
            async with db.transaction() as conn:
                assert (
                    await one(conn, "SELECT count(*) AS n FROM ag_generation_slot WHERE run_id IS NOT NULL")
                )["n"] == 0
            with pytest.raises(DomainError):
                services.auth.get(str(claimed["id"]))
            # New context work must survive recovery for its own API generation.
            fresh_conv = await services.conversations.create(
                principal, uuid4(), {"title": "fresh-context", "entry_context": {"kind": "GENERAL"}}
            )
            fresh = await services.conversations.send(
                principal,
                "new-synthetic-credential",
                fresh_conv["id"],
                uuid4(),
                {"client_message_id": str(uuid4()), "text": "fresh", "attachment_ids": []},
            )
            assert not await services.runs.recover_expired(services.auth.context_id)
            assert (await services.runs.get(principal, fresh["run"]["id"]))["state"] == "QUEUED"
            await services.runs.stop(principal, fresh["run"]["id"], uuid4(), {"expected_version": 1})
    finally:
        await db.engine.dispose()


@pytest.mark.anyio
async def test_paid_calls_reserve_daily_quota_and_unknown_usage_is_not_zero(integration_config):
    cfg = integration_config
    settings = Settings(
        database_url=cfg["databaseUrl"],
        bridge_secret=cfg["bridgeSecret"],
        broker_secret=cfg["brokerSecret"],
        cursor_secret=cfg["cursorSecret"],
        provider="openai_responses",
        dialog_api_key="unit-secret-must-not-persist",
    )
    db = Database(cfg["databaseUrl"])
    try:
        ledger = ModelLedger(db, settings)
        await ledger.setup()
        original = dict(ledger.profiles)
        await ledger.setup()
        assert ledger.profiles == original
        async with db.transaction() as conn:
            used = await one(
                conn,
                """SELECT count(*) AS n FROM ag_quota_reservation
                WHERE bucket_key='model-requests:DIALOG'
                AND period_start=date_trunc('day',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
                AND state<>'RELEASED'""",
            )
        settings.model_daily_requests = used["n"] + 2
        record = ledger.recorder("DIALOG")
        completed = await record({"stage": "reserve"})
        await record(
            {
                "call_id": completed,
                "status": "SUCCEEDED",
                "provider_request_id": "unit-provider",
                "usage": {"input_tokens": 12, "output_tokens": 3},
                "latency_ms": 10,
            }
        )
        unknown = await record({"stage": "reserve"})
        await record(
            {
                "call_id": unknown,
                "status": "CANCELLED",
                "usage": None,
                "latency_ms": 10,
                "error_code": "MODEL_CANCELLED_BILLING_UNKNOWN",
            }
        )
        with pytest.raises(DomainError) as limited:
            await record({"stage": "reserve"})
        assert limited.value.code == "MODEL_QUOTA_EXCEEDED"
        async with db.transaction() as conn:
            call = await one(conn, "SELECT * FROM ag_model_call WHERE id=:id", id=unknown)
            reservation = await one(conn, "SELECT * FROM ag_quota_reservation WHERE call_id=:id", id=unknown)
            assert call["input_tokens"] is None and call["output_tokens"] is None
            assert reservation["state"] == "UNKNOWN" and reservation["settled_units"] is None
            snapshot = await one(
                conn,
                "SELECT row_to_json(v)::text AS data FROM ag_model_profile_version v WHERE profile_id=:id AND version_no=:v",
                id=ledger.profiles["DIALOG"]["id"],
                v=ledger.profiles["DIALOG"]["version"],
            )
            assert "unit-secret" not in snapshot["data"]
    finally:
        await db.engine.dispose()


@pytest.mark.anyio
async def test_cancelled_live_runs_keep_slots_reserved_until_provider_timeout(integration_config):
    cfg = integration_config
    settings = Settings(
        database_url=cfg["databaseUrl"],
        bridge_secret=cfg["bridgeSecret"],
        broker_secret=cfg["brokerSecret"],
        cursor_secret=cfg["cursorSecret"],
        provider="openai_responses",
        dialog_api_key="unit-key",
    )
    db = Database(cfg["databaseUrl"])
    svc = Conversations(db, RuntimeAuth(180), Cursors(cfg["cursorSecret"]))
    runs = Runs(db, settings)
    principal = Principal(900000006, "cancel-slot-test")
    try:
        for i in range(4):
            conv = await svc.create(
                principal, uuid4(), {"title": "cancel-slot-test", "entry_context": {"kind": "GENERAL"}}
            )
            await svc.send(
                principal,
                "synthetic-token",
                conv["id"],
                uuid4(),
                {"client_message_id": str(uuid4()), "text": f"slot {i}", "attachment_ids": []},
            )
        claims = [await runs.claim("unit-live-worker") for _ in range(3)]
        for claimed in claims:
            assert await runs.heartbeat(claimed)
            await runs.finish(claimed, "MODEL_CANCELLED_BILLING_UNKNOWN")
        assert await runs.claim("fourth-worker") is None
        async with db.transaction() as conn:
            held = await one(
                conn,
                "SELECT count(*) AS n FROM ag_generation_slot WHERE run_id IS NULL AND lease_until>now()",
            )
            assert held["n"] == 3
            # Advance the test boundary without sleeping through a paid-provider timeout.
            await execute(conn, "UPDATE ag_generation_slot SET lease_until=now()-interval '1 second'")
        fourth = await runs.claim("after-timeout-worker")
        assert fourth
        await runs.finish(fourth)
    finally:
        await db.engine.dispose()
