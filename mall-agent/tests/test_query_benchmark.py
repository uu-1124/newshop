import asyncio
import json
from collections import Counter
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest

from app.infrastructure.performance_timing import RequestTiming
from scripts import benchmark_queries as benchmark


def definitions():
    routes = [
        {
            "name": "member_list",
            "actor_key": "member0",
            "path": "/mall-agent/v1/member/conversations?limit=20",
            "expected_operation": "listConversations",
            "weight": 1,
        }
    ]
    streams = []
    for index in range(10):
        cid = f"00000000-0000-4000-8000-{index:012d}"
        streams.append(
            {
                "name": f"member{index}",
                "actor_key": f"member{index}",
                "path": f"/mall-agent/v1/member/conversations/{cid}/events",
                "expected_owner_cid": cid,
            }
        )
    streams.extend(
        {"name": f"staff{index}", "actor_key": f"staff{index}", "path": "/mall-agent/v1/admin/events"}
        for index in range(3)
    )
    return {
        "schema_version": 1,
        "base_url": "http://127.0.0.1:8201",
        "rate_per_second": 1.5,
        "samples_per_route": 1000,
        "warmup_seconds": 120,
        "max_in_flight": 32,
        "timeout_seconds": 30,
        "max_start_lag_seconds": 1,
        "routes": routes,
        "streams": streams,
    }


def timing(run_id, request_id, operation="listConversations"):
    db = {key + suffix: 0 for key in benchmark.DB_TIMINGS for suffix in ("_ms", "_count")}
    db.update(
        pool_queue_ms=None,
        physical_connect_incomplete_count=0,
        pool_queue_supported=False,
        pool_queue_reason="not_observed",
    )
    return {
        "schema_version": 1,
        "run_id": run_id,
        "request_id": request_id,
        "operation": operation,
        "status_code": 200,
        "total_ms": 3,
        "handler_ms": 2,
        "metrics_write_ms": 1,
        "java_bridge_ms": 0,
        "db": db,
        "streaming": False,
        "scope": "response_headers_ready",
        "db_includes_metrics": True,
        "sql_includes_implicit_begin": True,
        "checkout_residual_includes_setup": True,
        "pool_queue_in_checkout_residual": True,
    }


class Clock:
    def __init__(self):
        self.value = 0.0

    def now(self):
        return self.value

    async def sleep(self, seconds):
        await asyncio.sleep(0)
        self.value += seconds
        await asyncio.sleep(0)


def test_plan_requires_bounded_read_queries_thirteen_distinct_authenticated_streams():
    plan = definitions()
    routes, streams = benchmark.validate_plan(plan)
    assert len(routes) == 1 and len(streams) == 13
    for change in [
        {"base_url": "https://private-token@example.com"},
        {"base_url": "http://113.249.103.102"},
        {"samples_per_route": 999},
        {"rate_per_second": float("nan")},
        {"streams": plan["streams"][:-1]},
        {"routes": [plan["routes"][0] | {"path": "/mall-agent/v1/member/messages/1/speech"}]},
        {"routes": [plan["routes"][0] | {"authorization": "must-not-be-public"}]},
        {"routes": [plan["routes"][0] | {"path": "/mall-agent/v1/member/conversations?limit=20&limit=20"}]},
    ]:
        with pytest.raises(benchmark.PlanError):
            benchmark.validate_plan(plan | change)


def test_weighted_schedule_preserves_ratio_and_minimum_and_uses_nearest_rank():
    route = benchmark.Route(
        "history", "member0", "/mall-agent/v1/member/conversations", "listConversations", 3
    )
    cycle, count = benchmark.route_sequence(
        [route, benchmark.Route("orders", "member0", "/mall-agent/v1/member/orders", "listOrders", 2)], 1000
    )
    assert Counter(cycle[index % len(cycle)].name for index in range(count)) == {
        "history": 1500,
        "orders": 1000,
    }
    assert benchmark.percentile(list(range(1, 21)), 0.95) == 19
    assert benchmark.percentile([], 0.95) is None


@pytest.mark.asyncio
async def test_slow_responses_do_not_change_arrival_times_and_finish_after_deadline_is_drained():
    clock, release = Clock(), asyncio.Event()
    route = benchmark.Route("history", "member0", "/mall-agent/v1/member/conversations", "listConversations")
    started, completed = [], []

    async def execute(arrival):
        started.append(arrival.started)
        if len(started) == 3:
            release.set()
        await release.wait()
        if not completed:
            clock.value += 1
        completed.append(arrival.sequence)

    drops = []
    summary = await benchmark.run_arrivals(
        (route,),
        3,
        10,
        3,
        1,
        execute,
        lambda item, reason: drops.append(reason),
        clock=clock.now,
        sleep=clock.sleep,
    )
    assert started == pytest.approx([0, 0.1, 0.2])
    assert sorted(completed) == [0, 1, 2]
    assert summary["completed"] == 3 and summary["drain_seconds"] > 0.8
    assert summary["offered_seconds"] == 0.3 and not drops


@pytest.mark.asyncio
async def test_concurrency_exhaustion_records_drops_instead_of_a_hidden_request_queue():
    clock, release = Clock(), asyncio.Event()
    route = benchmark.Route("history", "member0", "/mall-agent/v1/member/conversations", "listConversations")
    dispatched, dropped = [], []

    async def execute(arrival):
        dispatched.append(arrival.sequence)
        await release.wait()

    def on_drop(arrival, reason):
        dropped.append((arrival.sequence, reason))
        if arrival.sequence == 2:
            release.set()

    summary = await benchmark.run_arrivals(
        (route,), 3, 10, 1, 1, execute, on_drop, clock=clock.now, sleep=clock.sleep
    )
    assert dispatched == [0]
    assert dropped == [(1, "in_flight_limit"), (2, "in_flight_limit")]
    assert summary["dispatched"] == summary["completed"] == 1 and summary["dropped"] == 2


@pytest.mark.asyncio
async def test_failed_task_stops_future_arrivals_and_still_returns_a_failure_count():
    clock, stop = Clock(), asyncio.Event()
    route = benchmark.Route("history", "member0", "/mall-agent/v1/member/conversations", "listConversations")

    async def execute(arrival):
        raise RuntimeError("not copied to evidence")

    summary = await benchmark.run_arrivals(
        (route,), 5, 10, 2, 1, execute, lambda *args: None, clock=clock.now, sleep=clock.sleep, stop=stop
    )
    assert summary["aborted"] and stop.is_set()
    assert summary["task_errors"] == {"RuntimeError": 1}
    assert summary["dispatched"] == summary["completed"] == 1


def test_phase_header_is_correlated_whitelisted_and_never_fills_unknown_queue_with_zero():
    run_id, request_id = str(uuid4()), str(uuid4())
    value = timing(run_id, request_id)
    parsed, state = benchmark.parse_performance_timing(
        json.dumps(value), run_id, request_id, "listConversations", 200
    )
    assert state == "observed" and parsed["db"]["pool_queue_ms"] is None
    assert benchmark.parse_performance_timing(None, run_id, request_id, "listConversations", 200) == (
        None,
        "unobserved",
    )
    for bad in [
        value | {"request_id": str(uuid4())},
        value | {"private": "secret"},
        value | {"total_ms": float("nan")},
        value | {"streaming": True},
        value | {"db": value["db"] | {"sql_parameters": "secret"}},
        value | {"db": value["db"] | {"pool_queue_ms": 1}},
    ]:
        assert benchmark.parse_performance_timing(
            json.dumps(bad), run_id, request_id, "listConversations", 200
        ) == (None, "invalid")
    supported = value | {
        "db": value["db"] | {"pool_queue_ms": 0.2, "pool_queue_supported": True, "pool_queue_reason": None}
    }
    assert (
        benchmark.parse_performance_timing(
            json.dumps(supported), run_id, request_id, "listConversations", 200
        )[1]
        == "observed"
    )


def test_bridge_diagnostics_keep_upstream_status_and_reject_unbounded_or_private_fields():
    run_id, request_id = str(uuid4()), str(uuid4())
    call = {
        "bridge_request_id": str(uuid4()),
        "operation": "MEMBER_ORDERS",
        "upstream_http_status": 500,
        "upstream_code": "OTHER",
        "error_type": "NONE",
        "elapsed_ms": 3010.5,
    }
    value = timing(run_id, request_id) | {
        "java_bridge_calls": [call], "java_bridge_calls_overflow": 0,
    }
    parsed, state = benchmark.parse_performance_timing(
        json.dumps(value), run_id, request_id, "listConversations", 200
    )
    assert state == "observed" and parsed["java_bridge_calls"][0]["upstream_http_status"] == 500
    timeout = call | {"upstream_http_status": None, "upstream_code": "NONE", "error_type": "READ_TIMEOUT"}
    assert benchmark.parse_performance_timing(
        json.dumps(value | {"java_bridge_calls": [timeout]}), run_id, request_id, "listConversations", 200
    )[1] == "observed"
    bad_calls = [
        call | {"path": "/private-account"},
        call | {"bridge_request_id": "private-token"},
        call | {"operation": "/private-query"},
        call | {"upstream_http_status": True},
        call | {"upstream_code": True},
        call | {"upstream_code": {"private": "body"}},
        call | {"upstream_code": "private-message"},
        call | {"error_type": "private-error-message"},
        call | {"elapsed_ms": float("nan")},
    ]
    invalid = [value | {"java_bridge_calls": [item]} for item in bad_calls]
    invalid.extend([
        value | {"java_bridge_calls": [call] * 5},
        value | {"java_bridge_calls_overflow": True},
        value | {"java_bridge_calls_overflow": -1},
        timing(run_id, request_id) | {"java_bridge_calls": []},
        timing(run_id, request_id) | {"java_bridge_calls_overflow": 0},
    ])
    for bad in invalid:
        assert benchmark.parse_performance_timing(
            json.dumps(bad), run_id, request_id, "listConversations", 200
        ) == (None, "invalid")


@pytest.mark.parametrize(
    ("queue_supported", "queue_reason"),
    [(None, None), (True, None), (False, "sqlalchemy_version_unsupported")],
)
@pytest.mark.parametrize("incomplete", [0, 1])
def test_actual_request_timing_header_remains_compatible_without_database_access(
    queue_supported, queue_reason, incomplete
):
    run_id, request_id = str(uuid4()), str(uuid4())
    actual = RequestTiming(run_id, request_id)
    actual.queue_supported = queue_supported
    actual.queue_reason = queue_reason
    actual.incomplete_connects = incomplete
    actual.add("handler_ms", 0.002)
    actual.add("java_bridge_ms", 0.001)
    actual.add("metrics_write_ms", 0.001)
    actual.add("acquire_ms", 0.001, database=True)
    if queue_supported:
        actual.add("pool_queue_ms", 0.0002, database=True)
    raw = actual.header("listConversations", 200)
    parsed, state = benchmark.parse_performance_timing(raw, run_id, request_id, "listConversations", 200)
    assert state == "observed"
    assert parsed["java_bridge_ms"] == 1
    assert parsed["pool_queue_in_checkout_residual"] is True
    if queue_supported:
        assert parsed["db"]["pool_queue_ms"] == 0.2
        assert parsed["db"]["pool_queue_count"] == 1
    else:
        assert parsed["db"]["pool_queue_ms"] is None
        assert parsed["db"]["pool_queue_reason"] in benchmark.QUEUE_REASONS
    if incomplete:
        assert parsed["db"]["physical_connect_ms"] is None
        assert parsed["db"]["checkout_residual_ms"] is None


def test_actual_bridge_header_round_trips_the_bounded_optional_extension():
    run_id, request_id = str(uuid4()), str(uuid4())
    actual = RequestTiming(run_id, request_id)
    for _ in range(5):
        actual.record_java_bridge_call(
            bridge_request_id=str(uuid4()),
            operation="MEMBER_ORDERS",
            upstream_http_status=503,
            upstream_code="BRIDGE_NONCE_UNAVAILABLE",
            error_type="NONE",
            elapsed_ms=3100.25,
        )
    raw = actual.header("listConversations", 503)
    parsed, state = benchmark.parse_performance_timing(raw, run_id, request_id, "listConversations", 503)
    assert state == "observed" and len(parsed["java_bridge_calls"]) == 4
    assert parsed["java_bridge_calls_overflow"] == 1
    assert parsed["java_bridge_calls"][0]["upstream_http_status"] == 503
    assert parsed["java_bridge_calls"][0]["upstream_code"] == "BRIDGE_NONCE_UNAVAILABLE"


def test_bridge_producer_bounds_match_the_runner_and_never_discard_the_existing_header():
    run_id, request_id = str(uuid4()), str(uuid4())
    actual = RequestTiming(run_id, request_id)
    metadata = {
        "bridge_request_id": str(uuid4()), "operation": "MEMBER_ORDERS",
        "upstream_http_status": None, "upstream_code": "NONE", "error_type": "READ_TIMEOUT",
    }
    actual.record_java_bridge_call(**metadata, elapsed_ms=3600000.001)
    assert actual.java_bridge_calls == []
    for _ in range(4):
        actual.record_java_bridge_call(**metadata, elapsed_ms=3600000)
    actual.java_bridge_calls_overflow = 3600000
    actual.record_java_bridge_call(**metadata, elapsed_ms=1)
    raw = actual.header("listConversations", 503)
    parsed, state = benchmark.parse_performance_timing(raw, run_id, request_id, "listConversations", 503)
    assert state == "observed" and parsed["java_bridge_calls_overflow"] == 3600000
    assert all(item["elapsed_ms"] == 3600000 for item in parsed["java_bridge_calls"])


@pytest.mark.asyncio
async def test_request_evidence_excludes_auth_body_trace_info_and_raw_bad_header(tmp_path):
    run_id = str(uuid4())
    secret = "never-retain-this-account-or-token"
    route = benchmark.Route("history", "member0", "/mall-agent/v1/member/conversations", "listConversations")

    async def handler(request):
        raw = timing(run_id, request.headers["X-Request-ID"])
        return httpx.Response(
            200,
            json={"code": "OK", "data": {"private": secret}},
            headers={"X-Performance-Timing": json.dumps(raw), "Set-Cookie": secret},
        )

    evidence = benchmark.Evidence(tmp_path / "requests.jsonl")
    stop = asyncio.Event()
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + secret},
        transport=httpx.MockTransport(handler),
    ) as client:
        now = benchmark.time.monotonic()
        await benchmark.query_request(
            client, route, benchmark.Arrival(0, route, now, now, False), run_id, "B", evidence, stop
        )
    evidence.close()
    assert not stop.is_set() and evidence.records[0]["outcome"] == "success"
    assert secret not in evidence.path.read_text(encoding="utf-8")
    trace = benchmark.Trace()
    await trace("connection.connect_tcp.started", {"hostname": secret, "headers": {"Authorization": secret}})
    await trace("connection.connect_tcp.complete", {"return_value": secret})
    assert trace.result()["connect_tcp_ms"] is not None
    assert trace.result()["dns_ms"] is None
    assert secret not in json.dumps(trace.result())


@pytest.mark.asyncio
async def test_timeout_is_retained_and_a_missing_b_header_is_a_failure(tmp_path):
    route = benchmark.Route("history", "member0", "/mall-agent/v1/member/conversations", "listConversations")
    evidence = benchmark.Evidence(tmp_path / "requests.jsonl")

    async def timeout(request):
        raise httpx.ReadTimeout("sensitive exception text", request=request)

    async def no_header(request):
        return httpx.Response(200, json={"code": "OK"})

    for handler, error in [(timeout, "timeout"), (no_header, "required_timing_unavailable")]:
        stop = asyncio.Event()
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1", transport=httpx.MockTransport(handler)
        ) as client:
            now = benchmark.time.monotonic()
            await benchmark.query_request(
                client, route, benchmark.Arrival(0, route, now, now, False), str(uuid4()), "B", evidence, stop
            )
        assert stop.is_set() and evidence.records[-1]["error"] == error
    evidence.close()
    assert "sensitive exception text" not in evidence.path.read_text(encoding="utf-8")
    summary = benchmark.summarize_routes(evidence.records, [route], 10, {"history": 2})["history"]
    assert summary["completed"] == summary["errors"] == 2 and summary["timeouts"] == 1
    assert summary["latency_ms"]["samples"] == 2 and summary["success_latency_ms"]["samples"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "expected_code", "expected_error"),
    [
        (503, {"code": "JAVA_BRIDGE_REJECTED"}, "JAVA_BRIDGE_REJECTED", "http_status"),
        (403, {"code": "FORBIDDEN"}, "FORBIDDEN", "http_status"),
        (500, {"code": 500}, 500, "http_status"),
        (503, {"code": "private-account-secret"}, "OTHER", "http_status"),
        (503, ["private-account-secret"], "NON_OBJECT", "http_status"),
        (200, {"code": "FORBIDDEN"}, "FORBIDDEN", "api_envelope"),
    ],
)
async def test_failed_request_retains_only_safe_outer_code_and_still_stops(
    tmp_path, status, body, expected_code, expected_error
):
    run_id = str(uuid4())
    secret = "private-account-secret"
    route = benchmark.Route("history", "member0", "/mall-agent/v1/member/conversations", "listConversations")

    def handler(request):
        header = timing(run_id, request.headers["X-Request-ID"]) | {"status_code": status}
        envelope = body | {"message": secret, "data": {"token": secret}} if isinstance(body, dict) else body
        return httpx.Response(
            status, json=envelope, headers={"X-Performance-Timing": json.dumps(header)}
        )

    evidence = benchmark.Evidence(tmp_path / "requests.jsonl")
    stop = asyncio.Event()
    try:
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1", transport=httpx.MockTransport(handler)
        ) as client:
            now = benchmark.time.monotonic()
            await benchmark.query_request(
                client, route, benchmark.Arrival(0, route, now, now, False), run_id, "B", evidence, stop
            )
    finally:
        evidence.close()
    record = evidence.records[0]
    assert stop.is_set() and record["outcome"] == "error" and record["status_code"] == status
    assert record["error"] == expected_error and record["api_error_code"] == expected_code
    assert record["timing_state"] == "observed"
    assert secret not in evidence.path.read_text(encoding="utf-8")


def test_outer_error_observation_handles_invalid_or_oversize_json_without_exposing_it():
    assert benchmark.safe_api_error_code(httpx.Response(503, content=b"private-invalid-json")) == "INVALID_JSON"
    assert benchmark.safe_api_error_code(
        httpx.Response(503, content=b"x" * 2097153)
    ) == "RESPONSE_TOO_LARGE"

    class Unavailable:
        content = b""

        def json(self):
            raise RuntimeError("private-diagnostic-failure")

    assert benchmark.safe_api_error_code(Unavailable()) == "UNAVAILABLE"


@pytest.mark.asyncio
async def test_stream_monitor_detects_duplicate_events_without_retaining_payload(tmp_path, monkeypatch):
    secret = "private-chat-text-never-retain"
    cid = str(uuid4())
    stream = benchmark.Stream("member0", "member0", f"/mall-agent/v1/member/conversations/{cid}/events", cid)
    raw = f'id: 1\ndata: {{"conversation_id":"{cid}","text":"{secret}"}}\n\nid: 1\ndata: {{}}\n\n'.encode()

    class Bytes(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield raw

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Bytes())

    original = httpx.AsyncClient
    monkeypatch.setattr(
        benchmark.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    evidence = benchmark.Evidence(tmp_path / "streams.jsonl", anchor_utc=datetime(2026, 11, 16, tzinfo=UTC))
    stop = asyncio.Event()
    monitor = benchmark.StreamMonitor(
        "http://127.0.0.1", [stream], {"member0": "Bearer " + secret}, evidence, stop, 2
    )
    await monitor.consume(stream)
    monitor.sample()
    evidence.close()
    assert stop.is_set() and monitor.states["member0"]["duplicate_events"] == 1
    assert monitor.states["member0"]["connections"] == 1
    assert secret not in evidence.path.read_text(encoding="utf-8")


def test_private_auth_validation_does_not_echo_values(tmp_path):
    path = tmp_path / "auth.json"
    secret = "no-credential-in-exception"
    path.write_text(
        json.dumps({"schema_version": 1, "actors": {"member0": {"authorization": secret}}}), encoding="utf-8"
    )
    with pytest.raises(benchmark.PlanError) as error:
        benchmark.load_authorizations(path, {"member0"})
    assert secret not in str(error.value)


@pytest.mark.asyncio
async def test_sse_uses_the_same_routing_tag_and_records_heartbeat_liveness_without_private_text(
    tmp_path, monkeypatch
):
    run_id = str(uuid4())
    cid = str(uuid4())
    stream = benchmark.Stream(
        "member0", "member0", f"/mall-agent/v1/member/conversations/{cid}/events?after=12", cid
    )
    sent = []
    read_timeouts = []

    class HeartbeatThenTimeout(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b": heartbeat\n\n"
            raise httpx.ReadTimeout("private transport detail must not be retained")

    def reply(request):
        sent.append(request)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=HeartbeatThenTimeout()
        )

    original = httpx.AsyncClient

    def client(**kwargs):
        read_timeouts.append(kwargs["timeout"].read)
        return original(transport=httpx.MockTransport(reply), **kwargs)

    monkeypatch.setattr(benchmark.httpx, "AsyncClient", client)
    evidence = benchmark.Evidence(tmp_path / "streams.jsonl")
    stop = asyncio.Event()
    monitor = benchmark.StreamMonitor(
        "http://127.0.0.1",
        [stream],
        {"member0": "Bearer test-private-token"},
        evidence,
        stop,
        30,
        run_id=run_id,
    )
    await monitor.consume(stream)
    monitor.sample()
    evidence.close()
    state = monitor.states[stream.name]
    assert sent[0].headers["X-Performance-Run"] == run_id
    assert str(UUID(sent[0].headers["X-Request-ID"])) == state["request_id"]
    assert read_timeouts == [90]
    assert state["lines_received"] == 2 and state["last_line_at"] is not None
    assert stop.is_set() and state["error"] == "stream_transport_or_protocol_error"
    output = json.dumps(monitor.summary()) + evidence.path.read_text(encoding="utf-8")
    assert "private transport detail" not in output and "test-private-token" not in output
