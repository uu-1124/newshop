import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.agents.live_workflow import build_live_workflow
from app.application.extensions import Extensions
from app.application.jobs import JobHandlers, Jobs
from app.application.outbox import Outbox
from app.bootstrap import Services
from app.domain.capabilities import JobType
from app.domain.identity import DomainError, Principal
from app.infrastructure.dialog_provider import DialogEvent
from app.infrastructure.postgres.database import Database, execute, one
from app.infrastructure.wakeups import wakeup_id
from app.main import create_app
from app.settings import Settings
from app.workers.jobs import execute_job


def settings_for(cfg, **extra):
    return Settings(
        _env_file=None,
        database_url=cfg["databaseUrl"],
        bridge_secret=cfg["bridgeSecret"],
        broker_secret=cfg["brokerSecret"],
        cursor_secret=cfg["cursorSecret"],
        job_retry_base_seconds=0.1,
        **extra,
    )


async def due(db, job):
    async with db.transaction() as conn:
        await execute(conn, "UPDATE ag_job SET next_attempt_at=now() WHERE id=:id", id=job["id"])


@pytest.mark.anyio
async def test_jobs_concurrent_dedupe_claim_backoff_and_terminal_budget(integration_config):
    settings = settings_for(integration_config)
    db = Database(integration_config["databaseUrl"])
    registry = JobHandlers()

    async def failing(lease):
        raise RuntimeError("private upstream credentials must not be stored")

    registry.register(JobType.RUNTIME_CHECK, failing)
    jobs = Jobs(db, settings, registry)
    key = str(uuid4())
    try:
        a, b = await asyncio.gather(
            jobs.enqueue(JobType.RUNTIME_CHECK, key, {}), jobs.enqueue(JobType.RUNTIME_CHECK, key, {})
        )
        assert a["id"] == b["id"]
        with pytest.raises(DomainError, match="IDEMPOTENCY_CONFLICT"):
            await jobs.enqueue(JobType.RUNTIME_CHECK, key, {"different": True})
        claims = await asyncio.gather(jobs.claim("left", a["id"]), jobs.claim("right", a["id"]))
        assert sum(claim is not None for claim in claims) == 1
        claim = next(claim for claim in claims if claim)
        retried = await execute_job(jobs, registry, claim)
        assert retried["state"] == "QUEUED" and retried["attempts"] == 1
        assert retried["next_attempt_at"] > retried["updated_at"]
        async with db.transaction() as conn:
            wake = await one(
                conn,
                "SELECT * FROM ag_job_wakeup WHERE job_id=:id AND job_version=:v",
                id=a["id"],
                v=retried["version"],
            )
            assert wake["next_attempt_at"] == retried["next_attempt_at"]
        for expected in [2, 3]:
            await due(db, a)
            claim = await jobs.claim("retry-worker", a["id"])
            assert claim["attempts"] == expected
            retried = await execute_job(jobs, registry, claim)
        assert retried["state"] == "FAILED"
        assert retried["error_code"] == "JOB_EXECUTION_FAILED"
        assert "private" not in str(retried["error_message"])
        assert await jobs.claim("duplicate", a["id"]) is None
    finally:
        await db.engine.dispose()


@pytest.mark.anyio
async def test_jobs_crash_recovery_fences_stale_results_and_manual_retry(integration_config):
    settings = settings_for(integration_config)
    db = Database(integration_config["databaseUrl"])
    registry = JobHandlers()

    async def success(lease):
        return {"done": True}

    registry.register(JobType.RUNTIME_CHECK, success)
    jobs = Jobs(db, settings, registry)
    staff = Principal(900000100, "job-staff", "STAFF", frozenset({"agent:job:read", "agent:job:retry"}))
    try:
        job = await jobs.enqueue(JobType.RUNTIME_CHECK, str(uuid4()), {}, staff.id, max_attempts=1)
        old = await jobs.claim("crashed", job["id"])
        async with db.transaction() as conn:
            await execute(
                conn, "UPDATE ag_job SET lease_until=now()-interval '1 second' WHERE id=:id", id=job["id"]
            )
        assert not await jobs.heartbeat(old)
        with pytest.raises(DomainError, match="LEASE_LOST"):
            await jobs.finish(old, {"stale": True})
        assert await jobs.recover_expired() == 1
        failed = await jobs.get(staff, job["id"])
        assert failed["state"] == "FAILED"
        outsider = Principal(staff.id + 1, "outsider", "STAFF", staff.permissions)
        with pytest.raises(DomainError, match="NOT_FOUND"):
            await jobs.get(outsider, job["id"])
        with pytest.raises(DomainError, match="FORBIDDEN"):
            await jobs.retry(
                Principal(staff.id, "reader", "STAFF", frozenset({"agent:job:read"})),
                job["id"],
                failed["version"],
                uuid4(),
            )
        with pytest.raises(DomainError, match="VERSION_CONFLICT"):
            await jobs.retry(staff, job["id"], 0, uuid4())
        key = uuid4()
        retried = await jobs.retry(staff, job["id"], failed["version"], key)
        replay = await jobs.retry(staff, job["id"], failed["version"], key)
        assert retried["version"] == replay["version"]
        with pytest.raises(DomainError, match="IDEMPOTENCY_CONFLICT"):
            await jobs.retry(staff, job["id"], retried["version"], key)
        current = await jobs.claim("recovered", job["id"])
        assert current["fencing_token"] > old["fencing_token"]
        with pytest.raises(DomainError, match="LEASE_LOST"):
            await jobs.finish(old, {"stale": True})
        assert (await execute_job(jobs, registry, current))["state"] == "SUCCEEDED"
        assert await jobs.claim("duplicate", job["id"]) is None
    finally:
        await db.engine.dispose()


@pytest.mark.anyio
async def test_job_timeout_cancels_handler_and_permanent_failure_does_not_retry(integration_config):
    settings = settings_for(integration_config, job_timeout_seconds=1)
    db = Database(integration_config["databaseUrl"])
    registry = JobHandlers()
    cancelled = asyncio.Event()

    async def slow(lease):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def permanent(lease):
        raise DomainError("VALIDATION_ERROR", "invalid", 422)

    registry.register(JobType.RUNTIME_CHECK, slow)
    registry.register(JobType.KB_PARSE, permanent)
    jobs = Jobs(db, settings, registry)
    try:
        for kind, code in [(JobType.RUNTIME_CHECK, "JOB_TIMEOUT"), (JobType.KB_PARSE, "VALIDATION_ERROR")]:
            job = await jobs.enqueue(kind, str(uuid4()), {})
            result = await execute_job(jobs, registry, await jobs.claim("timeout-worker", job["id"]))
            assert result["error_code"] == code
            assert result["state"] == ("QUEUED" if kind == JobType.RUNTIME_CHECK else "FAILED")
        assert cancelled.is_set()
        # Finish the deliberately timed-out test job so unrelated claims cannot pick it up.
        async with db.transaction() as conn:
            await execute(conn, "UPDATE ag_job SET state='FAILED' WHERE id=:id", id=result["id"])
            await execute(
                conn,
                "UPDATE ag_job SET state='FAILED' WHERE lease_owner IS NULL AND error_code='JOB_TIMEOUT'",
            )
    finally:
        await db.engine.dispose()


@pytest.mark.anyio
async def test_staff_api_checks_fresh_permissions_and_cannot_trust_identity_headers(integration_config):
    settings = settings_for(integration_config, staff_bridge_secret="unit-staff-secret")
    svc = Services(settings)
    current = {"kind": "STAFF", "permissions": ["agent:job:read"]}
    calls = []

    def identity(request):
        calls.append(request)
        assert request.url.path == "/mall-admin/agent-bridge/v1/identity"
        assert request.headers["x-agent-signature"].startswith("v1:")
        if request.headers["authorization"] == "Bearer disabled":
            return httpx.Response(401, json={"code": "AUTH_EXPIRED"})
        return httpx.Response(
            200, json={"code": "OK", "data": {"id": "900000101", "display_name": "unit-staff", **current}}
        )

    await svc.bridge.client.aclose()
    svc.bridge.client = httpx.AsyncClient(transport=httpx.MockTransport(identity), base_url="http://java")
    app = create_app(settings)
    app.state.services = svc
    headers = {"Authorization": "Bearer unit-token", "X-Actor-Id": "1", "X-Permissions": "agent:job:retry"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/mall-agent/v1/admin/me")).status_code == 401
            me = await client.get("/mall-agent/v1/admin/me", headers=headers)
            assert me.json()["data"]["id"] == "900000101"
            assert me.json()["data"]["permissions"] == ["agent:job:read"]
            page = await client.get("/mall-agent/v1/admin/jobs", headers=headers)
            assert page.status_code == 200
            current["permissions"] = []
            assert (await client.get("/mall-agent/v1/admin/jobs", headers=headers)).status_code == 403
            current["kind"] = "MEMBER"
            assert (await client.get("/mall-agent/v1/admin/me", headers=headers)).status_code == 401
            assert (
                await client.get("/mall-agent/v1/admin/me", headers={"Authorization": "Bearer disabled"})
            ).status_code == 401
            assert len({call.headers["x-agent-nonce"] for call in calls}) == len(calls)
    finally:
        await svc.close()


@pytest.mark.anyio
async def test_outbox_failure_keeps_pending_then_republishes_without_payload(integration_config):
    settings = settings_for(integration_config)
    db = Database(integration_config["databaseUrl"])
    registry = JobHandlers()
    registry.register(JobType.RUNTIME_CHECK, lambda _: None)
    jobs = Jobs(db, settings, registry)

    class Adapter:
        failing = True
        sent = []

        def require(self):
            return self

        async def publish(self, *ids):
            if self.failing:
                raise OSError("transport offline")
            self.sent.append(ids)

    adapter = Adapter()
    outbox = Outbox(db, SimpleNamespace(redis=adapter, rabbitmq=adapter))
    try:
        job = await jobs.enqueue(JobType.RUNTIME_CHECK, str(uuid4()), {"private": "not-a-notification"})
        await outbox.tick()
        async with db.transaction() as conn:
            wake = await one(conn, "SELECT * FROM ag_job_wakeup WHERE job_id=:id", id=job["id"])
            assert wake["published_at"] is None
            await execute(conn, "UPDATE ag_job_wakeup SET next_attempt_at=now() WHERE published_at IS NULL")
            await execute(conn, "UPDATE ag_event_outbox SET next_publish_at=now() WHERE published_at IS NULL")
        adapter.failing = False
        # The shared isolated DB may contain more than one publisher batch
        # from prior tests. Drain bounded batches before checking this marker.
        async with db.transaction() as conn:
            pending = await one(conn, "SELECT COUNT(*) AS n FROM ag_job_wakeup WHERE published_at IS NULL")
        for _ in range(pending["n"] // 25 + 2):
            await outbox.tick()
            async with db.transaction() as conn:
                published = await one(
                    conn, "SELECT published_at FROM ag_job_wakeup WHERE job_id=:id", id=job["id"]
                )
            if published["published_at"]:
                break
        async with db.transaction() as conn:
            assert (await one(conn, "SELECT published_at FROM ag_job_wakeup WHERE job_id=:id", id=job["id"]))[
                "published_at"
            ]
        assert any(str(ids[0]) == str(job["id"]) for ids in adapter.sent)
        assert "not-a-notification" not in str(adapter.sent)
        await due(db, job)
        await jobs.finish(await jobs.claim("test-cleanup", job["id"]), {})
    finally:
        await db.engine.dispose()


def test_wakeup_rejects_payload_injection_and_invalid_protocol():
    valid = {"schema_version": 1, "job_id": str(uuid4()), "job_type": "RUNTIME_CHECK"}
    assert str(wakeup_id(json.dumps(valid).encode())) == valid["job_id"]
    for value in [
        valid | {"parameters": {}},
        valid | {"job_type": "EXECUTE_PAYMENT"},
        valid | {"job_id": "bad"},
        None,
    ]:
        with pytest.raises((TypeError, ValueError)):
            wakeup_id(json.dumps(value).encode())


@pytest.mark.anyio
async def test_live_workflow_routes_to_guarded_real_tool_boundary_before_output(integration_config):
    settings = settings_for(integration_config)
    emitted, searches, guards = [], [], []

    class Provider:
        turns = 0

        async def stream_turn(self, inputs, instructions, tools, record, schema=None):
            if schema:
                assert not tools
                yield DialogEvent("text", '{"intent":"GUIDE"}')
            else:
                self.turns += 1
                if self.turns == 1:
                    yield DialogEvent(
                        "completed",
                        [
                            {
                                "type": "function_call",
                                "call_id": "real-1",
                                "name": "search_products",
                                "arguments": '{"query":"手机"}',
                            }
                        ],
                    )
                else:
                    assert json.loads(inputs[-1]["output"])["items"][0]["id"] == "26"
                    assert not emitted
                    yield DialogEvent("text", "查询到商品26，价格3788元。")
                    yield DialogEvent("completed", [])

    async def guard(state):
        guards.append(state["steps_used"])

    async def assemble(state):
        return {"history": [], "image_observations": []}

    async def search(query):
        searches.append(query)
        return {"items": [{"id": "26", "price_from": "3788.00"}]}

    async def emit(text):
        emitted.append(text)

    graph, context = build_live_workflow(Provider(), None, settings)
    result = await graph.ainvoke(
        {"question": "查手机", "steps_used": 0},
        context=context(
            guard=guard,
            assemble=assemble,
            search=search,
            emit=emit,
            record=None,
            route_record=None,
            extensions=Extensions(),
        ),
    )
    assert searches == ["手机", "手机"] and "".join(emitted) == result["answer"]
    assert guards[:7] == list(range(7))


@pytest.mark.anyio
async def test_live_workflow_unimplemented_intents_and_stop_do_not_emit(integration_config):
    class Provider:
        intent = "ORDER"

        async def stream_turn(self, *args):
            yield DialogEvent("text", json.dumps({"intent": self.intent}))

    async def noop(state):
        return {}

    async def forbidden(*args):
        pytest.fail("Closed or stopped workflow must not call tools or emit output")

    provider = Provider()
    graph, factory = build_live_workflow(provider, None, settings_for(integration_config))
    context = factory(
        guard=noop,
        assemble=noop,
        search=forbidden,
        emit=forbidden,
        record=None,
        route_record=None,
        extensions=Extensions(),
    )
    with pytest.raises(DomainError, match="FEATURE_NOT_ACTIVE"):
        await graph.ainvoke({"question": "取消订单", "steps_used": 0}, context=context)

    async def stopped(state):
        raise DomainError("LEASE_LOST", "stopped")

    context.guard = stopped
    with pytest.raises(DomainError, match="LEASE_LOST"):
        await graph.ainvoke({"question": "查询", "steps_used": 0}, context=context)
