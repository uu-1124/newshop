import asyncio
import json
import time
from collections import deque
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.responses import StreamingResponse
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.pool import AsyncAdaptedQueuePool
from sqlalchemy.util.concurrency import await_, greenlet_spawn

from app.infrastructure import performance_timing
from app.infrastructure.performance_timing import (
    HEADER,
    RUN_HEADER,
    RequestTiming,
    TimingPool,
    current_timing,
    finish_acquisition,
    finish_request,
    install_engine_timing,
    phase,
    queue_timing_configuration,
    start_acquisition,
    start_request,
)
from app.infrastructure.postgres.database import Database
from app.main import create_app
from app.settings import Settings


class OfflineSettings(Settings):
    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        return (init_settings,)


def application(enabled=False):
    settings = OfflineSettings(
        _env_file=None,
        database_url="postgresql+psycopg://unused:unused@127.0.0.1:1/unused",
        bridge_secret="timing-only",
        broker_secret="timing-only",
        cursor_secret="timing-only",
        performance_timing_enabled=enabled,
    )
    return create_app(settings)


def start():
    return start_request(True, str(uuid4()), str(uuid4()))


def tags():
    return {RUN_HEADER: str(uuid4()), "X-Request-ID": str(uuid4())}


@pytest.mark.anyio
async def test_timing_is_opt_in_validated_ascii_and_scoped_to_a_request():
    for enabled, run_id in [(False, str(uuid4())), (True, "invalid"), (True, "x" * 5000)]:
        app = application(enabled)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            reply = await c.get("/livez", headers={RUN_HEADER: run_id})
        assert reply.status_code == 200 and HEADER not in reply.headers
    app = application(True)
    headers = tags()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        reply = await c.get("/livez", headers=headers)
        second = await c.get("/livez")
    value = json.loads(reply.headers[HEADER])
    assert value["run_id"] == headers[RUN_HEADER]
    assert value["request_id"] == headers["X-Request-ID"] == reply.headers["X-Request-ID"]
    assert value["schema_version"] == 1 and value["scope"] == "response_headers_ready"
    assert value["db"]["pool_queue_ms"] is None
    assert value["handler_ms"] <= value["total_ms"] and HEADER not in second.headers
    assert reply.headers[HEADER].isascii() and len(reply.headers[HEADER]) <= 4096
    assert current_timing() is None


def test_public_sqlalchemy_hooks_keep_sql_and_parameters_out_and_do_not_reuse_request_state():
    engine = create_engine("sqlite://")
    install_engine_timing(SimpleNamespace(sync_engine=engine))
    first, token = start()
    try:
        acquisition = start_acquisition()
        with engine.connect() as conn:
            finish_acquisition(acquisition)
            assert (
                conn.execute(text("SELECT :private_value"), {"private_value": "secret-value"}).scalar()
                == "secret-value"
            )
            with pytest.raises(OperationalError):
                conn.execute(text("SELECT absent_private_column"))
        header = first.header("timingRead", 200)
        value = json.loads(header)
        assert value["db"]["physical_connect_count"] == 1
        assert value["db"]["sql_execute_count"] == 2
        assert not first.statements
        assert "secret-value" not in header and "private_value" not in header and "absent" not in header
    finally:
        finish_request(first, token)
    second, token = start()
    try:
        acquisition = start_acquisition()
        with engine.connect() as conn:
            finish_acquisition(acquisition)
            conn.execute(text("SELECT 1"))
        value = json.loads(second.header("timingRead", 200))
        assert value["db"]["physical_connect_ms"] == 0
        assert value["db"]["sql_execute_count"] == 1
        assert value["db"]["acquire_count"] == 1
    finally:
        finish_request(second, token)
        engine.dispose()


@pytest.mark.anyio
async def test_concurrent_and_cancelled_requests_keep_separate_contexts():
    ready = asyncio.Event()
    seen = []

    async def observe():
        timing, token = start()
        try:
            await ready.wait()
            with phase("handler_ms"):
                await asyncio.sleep(0)
                assert current_timing() is timing
                seen.append(timing.request_id)
            return json.loads(timing.header("timingRead", 200))
        finally:
            finish_request(timing, token)

    tasks = [asyncio.create_task(observe()) for _ in range(3)]
    await asyncio.sleep(0)
    tasks[0].cancel()
    ready.set()
    result = await asyncio.gather(*tasks, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
    assert len(set(seen)) == 2
    assert {r["request_id"] for r in result[1:]} == set(seen)
    assert current_timing() is None


class FakeConnection:
    async def execution_options(self, **options):
        await asyncio.sleep(0.001)
        return self

    @asynccontextmanager
    async def begin(self):
        try:
            yield self
        finally:
            await asyncio.sleep(0.02)


class FakeEngine:
    @asynccontextmanager
    async def connect(self):
        await asyncio.sleep(0.01)
        try:
            yield FakeConnection()
        finally:
            await asyncio.sleep(0.015)


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [False, True])
async def test_database_exit_timings_include_completed_commit_or_rollback_and_connection_release(failure):
    db = object.__new__(Database)
    db.engine, db._waits, db._timeouts = FakeEngine(), deque(maxlen=300), 0
    timing, token = start()
    marker = RuntimeError("expected business failure")
    try:
        try:
            async with db.transaction(snapshot=True):
                if failure:
                    raise marker
        except RuntimeError as error:
            assert failure and error is marker
        values = json.loads(timing.header("timingRead", 500 if failure else 200))["db"]
        assert values["transaction_end_ms"] >= 18
        assert values["connection_release_ms"] >= 13
        assert values["acquire_ms"] >= 8
        assert values["transaction_end_count"] == values["connection_release_count"] == 1
        assert values["pool_queue_ms"] is None and values["checkout_residual_ms"] > 0
        assert values["isolation_config_count"] == 1
    finally:
        finish_request(timing, token)


@pytest.mark.anyio
async def test_metrics_are_still_persisted_before_reply_and_header_failure_does_not_change_response(
    monkeypatch,
):
    writes = []

    class MetricsConnection:
        async def execute(self, statement, params):
            await asyncio.sleep(0.02)
            writes.append(params)

    class MetricsDB:
        @asynccontextmanager
        async def connection(self, isolation=None):
            assert isolation == "AUTOCOMMIT"
            yield MetricsConnection()

    app = application(True)
    app.state.services = SimpleNamespace(
        db=MetricsDB(), conversations=SimpleNamespace(settings=SimpleNamespace(governance_enabled=True))
    )

    @app.get("/timing-test", operation_id="timingRead")
    async def endpoint():
        return {"value": "unchanged"}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        reply = await c.get("/timing-test", headers=tags())
        assert reply.status_code == 200 and len(writes) == 1
        timing = json.loads(reply.headers[HEADER])
        assert timing["metrics_write_ms"] >= 18 and timing["total_ms"] >= timing["metrics_write_ms"]
        assert timing["operation"] == "timingRead" and timing["db_includes_metrics"] is True

        def broken(*args):
            raise RuntimeError("observability unavailable")

        monkeypatch.setattr(RequestTiming, "header", broken)
        reply = await c.get("/timing-test", headers=tags())
        assert reply.status_code == 200 and reply.json() == {"value": "unchanged"}
        assert len(writes) == 2 and HEADER not in reply.headers and current_timing() is None


@pytest.mark.anyio
async def test_stream_header_is_setup_only_and_later_stream_tasks_cannot_extend_capture():
    app = application(True)
    later = []

    @app.get("/timing-stream", operation_id="timingStream")
    async def endpoint():
        async def body():
            timing = current_timing()
            assert timing is not None
            while not timing.closed:
                await asyncio.sleep(0)
            await asyncio.sleep(0.01)
            later.append(current_timing())
            yield b"data: done\n\n"

        return StreamingResponse(body(), media_type="text/event-stream")

    started = time.perf_counter()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        reply = await c.get("/timing-stream", headers=tags())
    value = json.loads(reply.headers[HEADER])
    assert reply.status_code == 200 and value["streaming"] is True
    assert value["scope"] == "response_headers_ready" and later == [None]
    assert value["total_ms"] < (time.perf_counter() - started) * 1000


class DummyDBAPI:
    def rollback(self):
        pass

    def close(self):
        pass


async def await_waiter(pool):
    # Inspect the same pinned adapter that the opt-in measurement extends.
    # Establish actual saturation before release/cancel; do not rely on sleeps
    # to assume that the second task has reached the queue.
    async with asyncio.timeout(2):
        while not pool._pool._queue._getters:
            await asyncio.sleep(0)


async def traced_checkout(pool, result):
    timing, token = start()
    acquisition = start_acquisition(queue_supported=True, queue_reason=None)
    try:
        return await greenlet_spawn(pool.connect)
    finally:
        finish_acquisition(acquisition)
        result.update(json.loads(timing.header("poolProbe", 200)))
        finish_request(timing, token)


@pytest.mark.anyio
async def test_actual_pool_saturation_measures_queue_get_until_an_existing_connection_is_released():
    pool = TimingPool(DummyDBAPI, pool_size=1, max_overflow=0, timeout=1)
    first = await greenlet_spawn(pool.connect)
    result = {}
    task = asyncio.create_task(traced_checkout(pool, result))
    try:
        await await_waiter(pool)
        await asyncio.sleep(0.02)
        await greenlet_spawn(first.close)
        second = await task
        await greenlet_spawn(second.close)
        db = result["db"]
        assert db["pool_queue_supported"] is True and db["pool_queue_reason"] is None
        assert db["pool_queue_ms"] >= 18 and db["pool_queue_count"] == 1
        assert db["acquire_ms"] >= db["pool_queue_ms"]
        assert pool.checkedout() == 0 and pool.checkedin() == 1
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await greenlet_spawn(first.close)
        await greenlet_spawn(pool.dispose)


@pytest.mark.anyio
async def test_actual_pool_queue_timeout_is_unchanged_and_still_measured():
    pool = TimingPool(DummyDBAPI, pool_size=1, max_overflow=0, timeout=0.03)
    first = await greenlet_spawn(pool.connect)
    result = {}
    try:
        with pytest.raises(PoolTimeout):
            await traced_checkout(pool, result)
        assert result["db"]["pool_queue_ms"] >= 25
        assert result["db"]["pool_queue_count"] == 1
        assert pool.checkedout() == 1 and current_timing() is None
    finally:
        await greenlet_spawn(first.close)
        await greenlet_spawn(pool.dispose)


@pytest.mark.anyio
async def test_cancelling_actual_pool_waiter_does_not_leak_queue_entries_or_connection_capacity():
    pool = TimingPool(DummyDBAPI, pool_size=1, max_overflow=0, timeout=1)
    first = await greenlet_spawn(pool.connect)
    result = {}
    task = asyncio.create_task(traced_checkout(pool, result))
    try:
        await await_waiter(pool)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert result["db"]["pool_queue_count"] == 1
        assert not pool._pool._queue._getters and pool.checkedout() == 1
        await greenlet_spawn(first.close)
        second = await greenlet_spawn(pool.connect)
        await greenlet_spawn(second.close)
        assert pool.checkedout() == 0 and pool.checkedin() == 1
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await greenlet_spawn(first.close)
        await greenlet_spawn(pool.dispose)


@pytest.mark.anyio
async def test_slow_overflow_connection_creation_is_not_reported_as_pool_queue_time():
    def slow_creator():
        await_(asyncio.sleep(0.03))
        return DummyDBAPI()

    pool = TimingPool(slow_creator, pool_size=1, max_overflow=1, timeout=1)
    first = await greenlet_spawn(pool.connect)
    result = {}
    try:
        second = await traced_checkout(pool, result)
        assert result["db"]["acquire_ms"] >= 25
        assert result["db"]["pool_queue_ms"] == 0 and result["db"]["pool_queue_count"] == 0
        await greenlet_spawn(second.close)
    finally:
        await greenlet_spawn(first.close)
        await greenlet_spawn(pool.dispose)


@pytest.mark.anyio
async def test_queue_extension_is_off_by_default_and_unknown_versions_do_not_forge_zero_wait(monkeypatch):
    url = "postgresql+psycopg://unused:unused@127.0.0.1:1/unused"
    original, observed = Database(url), Database(url, performance_timing_enabled=True)
    try:
        assert type(original.engine.pool) is AsyncAdaptedQueuePool
        assert type(observed.engine.pool) is TimingPool
        monkeypatch.setattr(performance_timing, "sqlalchemy_version", "unvalidated")
        options, reason = queue_timing_configuration(True)
        assert options == {} and reason == "sqlalchemy_version_unsupported"
        timing, token = start()
        try:
            acquisition = start_acquisition(queue_supported=False, queue_reason=reason)
            finish_acquisition(acquisition)
            db = json.loads(timing.header("poolProbe", 200))["db"]
            assert db["pool_queue_ms"] is None and db["pool_queue_supported"] is False
            assert db["pool_queue_reason"] == reason
        finally:
            finish_request(timing, token)
    finally:
        await original.engine.dispose()
        await observed.engine.dispose()
