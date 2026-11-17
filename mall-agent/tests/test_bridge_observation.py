import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr

from app.domain.identity import DomainError
from app.infrastructure import java_bridge
from app.infrastructure.java_bridge import JavaBridge
from app.infrastructure.performance_timing import finish_request, start_request

PRIVATE = "private-token-url-query-response-exception"
ORDERS = "/mall-portal/agent-bridge/v1/orders"


@asynccontextmanager
async def observed_bridge(handler, enabled=True):
    seen = []

    def transport(request):
        seen.append(request)
        return handler(request)

    bridge = JavaBridge(
        SimpleNamespace(
            gateway_url="http://unused.test",
            bridge_secret=SecretStr("bridge-observation-test-only-secret"),
            staff_bridge_secret=SecretStr("staff-observation-test-only-secret"),
        )
    )
    await bridge.client.aclose()
    bridge.client = httpx.AsyncClient(
        base_url="http://unused.test", transport=httpx.MockTransport(transport), trust_env=False
    )
    timing, token = start_request(enabled, str(uuid4()), str(uuid4()))
    try:
        yield bridge, timing, seen
    finally:
        finish_request(timing, token)
        await bridge.client.aclose()


def header(timing, status=503):
    raw = timing.header("listMemberOrders", status)
    assert raw is not None and raw.isascii() and len(raw) <= 4096
    assert PRIVATE not in raw and "bridge-observation-test-only-secret" not in raw
    return json.loads(raw)


@pytest.mark.anyio
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    "upstream_status,upstream_code,expected_status,expected_code,expected_message",
    [
        (500, 500, 503, "JAVA_BRIDGE_REJECTED", "商城桥接请求未通过校验"),
        (503, "BRIDGE_NONCE_UNAVAILABLE", 503, "JAVA_BRIDGE_REJECTED", "商城桥接请求未通过校验"),
        (200, 500, 503, "JAVA_BRIDGE_REJECTED", "商城桥接请求未通过校验"),
        (200, 401, 401, "AUTH_EXPIRED", "请重新登录商城"),
        (200, 403, 403, "FORBIDDEN", "当前身份没有相关权限"),
        (403, "BRIDGE_SIGNATURE_INVALID", 403, "FORBIDDEN", "当前身份没有相关权限"),
        (400, PRIVATE, 400, PRIVATE, "商品不存在、已下架或筛选条件无效"),
    ],
)
async def test_actual_upstream_outcome_is_observed_without_changing_existing_domain_error(
    enabled, upstream_status, upstream_code, expected_status, expected_code, expected_message
):
    def reply(request):
        return httpx.Response(
            upstream_status, json={"code": upstream_code, "message": PRIVATE, "data": PRIVATE}
        )

    async with observed_bridge(reply, enabled) as (bridge, timing, seen):
        with pytest.raises(DomainError) as raised:
            await bridge.get(ORDERS, "Bearer " + PRIVATE, {"limit": 5})
        assert (raised.value.status, raised.value.code, raised.value.message) == (
            expected_status,
            expected_code,
            expected_message,
        )
        assert len(seen) == 1
        assert seen[0].url.raw_path == b"/mall-portal/agent-bridge/v1/orders?limit=5"
        assert seen[0].headers["Authorization"] == "Bearer " + PRIVATE
        if not enabled:
            assert timing is None and "X-Agent-Parent-Request-ID" not in seen[0].headers
            return
        value = header(timing, expected_status)
        assert value["status_code"] == expected_status
        call = value["java_bridge_calls"][0]
        assert call["upstream_http_status"] == upstream_status
        assert call["upstream_code"] == ("OTHER" if upstream_code == PRIVATE else upstream_code)
        assert call["bridge_request_id"] == seen[0].headers["X-Request-ID"]
        assert str(UUID(call["bridge_request_id"])) == call["bridge_request_id"]
        assert call["operation"] == "MEMBER_ORDERS" and call["error_type"] == "NONE"
        assert call["elapsed_ms"] >= 0 and value["java_bridge_calls_overflow"] == 0


@pytest.mark.anyio
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    "error_class,expected_type",
    [
        (httpx.ConnectTimeout, "CONNECT_TIMEOUT"),
        (httpx.ReadTimeout, "READ_TIMEOUT"),
        (httpx.WriteTimeout, "WRITE_TIMEOUT"),
        (httpx.PoolTimeout, "POOL_TIMEOUT"),
        (httpx.ReadError, "READ_ERROR"),
        (httpx.RemoteProtocolError, "REMOTE_PROTOCOL_ERROR"),
    ],
)
async def test_transport_failure_has_no_invented_upstream_status_or_retry(enabled, error_class, expected_type):
    def fail(request):
        raise error_class(PRIVATE, request=request)

    async with observed_bridge(fail, enabled) as (bridge, timing, seen):
        with pytest.raises(DomainError) as raised:
            await bridge.get(ORDERS, "Bearer " + PRIVATE)
        assert (raised.value.code, raised.value.status, raised.value.message) == (
            "JAVA_UNAVAILABLE",
            503,
            "商城服务暂不可用",
        )
        assert raised.value.__cause__ is None and raised.value.__suppress_context__
        assert len(seen) == 1
        if enabled:
            call = header(timing)["java_bridge_calls"][0]
            assert call["upstream_http_status"] is None
            assert call["upstream_code"] == "NONE" and call["error_type"] == expected_type


@pytest.mark.anyio
@pytest.mark.parametrize("enabled", [False, True])
async def test_invalid_json_retains_the_received_http_status_but_no_response_text(enabled):
    def reply(request):
        return httpx.Response(502, content=("not-json:" + PRIVATE).encode())

    async with observed_bridge(reply, enabled) as (bridge, timing, seen):
        with pytest.raises(DomainError) as raised:
            await bridge.get(ORDERS, "Bearer " + PRIVATE)
        assert (raised.value.code, raised.value.status) == ("JAVA_UNAVAILABLE", 503)
        assert raised.value.__cause__ is None and raised.value.__suppress_context__
        assert len(seen) == 1
        if enabled:
            call = header(timing)["java_bridge_calls"][0]
            assert call["upstream_http_status"] == 502
            assert call["upstream_code"] == "NONE" and call["error_type"] == "INVALID_JSON"


@pytest.mark.anyio
@pytest.mark.parametrize("enabled", [False, True])
async def test_non_object_json_still_propagates_the_existing_attribute_error(enabled):
    def reply(request):
        return httpx.Response(200, json=[PRIVATE])

    async with observed_bridge(reply, enabled) as (bridge, timing, seen):
        with pytest.raises(AttributeError):
            await bridge.get(ORDERS, "Bearer " + PRIVATE)
        assert len(seen) == 1
        if enabled:
            call = header(timing, 500)["java_bridge_calls"][0]
            assert call["upstream_code"] == "NON_OBJECT"
            assert call["upstream_http_status"] == 200 and call["error_type"] == "NONE"


@pytest.mark.anyio
@pytest.mark.parametrize("enabled", [False, True])
async def test_value_error_before_response_is_not_misreported_as_received_invalid_json(enabled):
    def fail(request):
        raise ValueError(PRIVATE)

    async with observed_bridge(fail, enabled) as (bridge, timing, seen):
        with pytest.raises(DomainError) as raised:
            await bridge.get(ORDERS, "Bearer " + PRIVATE)
        assert (raised.value.code, raised.value.status) == ("JAVA_UNAVAILABLE", 503)
        assert len(seen) == 1
        if enabled:
            call = header(timing)["java_bridge_calls"][0]
            assert call["upstream_http_status"] is None and call["upstream_code"] == "NONE"
            assert call["error_type"] == "HTTP_ERROR_OTHER"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "unknown_code", [PRIVATE, "private-code-内容", {"private": PRIVATE}, [PRIVATE], True, 999, 500.0]
)
async def test_unknown_envelope_codes_and_private_target_never_enter_observation(unknown_code):
    def reply(request):
        return httpx.Response(200, json={"code": unknown_code, "message": PRIVATE, "data": PRIVATE})

    async with observed_bridge(reply) as (bridge, timing, seen):
        with pytest.raises(DomainError) as raised:
            await bridge.get("/private/" + PRIVATE, "Bearer " + PRIVATE, {"private": PRIVATE})
        assert raised.value.code == "JAVA_BRIDGE_REJECTED" and raised.value.status == 503
        assert len(seen) == 1
        call = header(timing)["java_bridge_calls"][0]
        assert call["upstream_code"] == "OTHER" and call["operation"] == "OTHER"
        assert set(call) == {
            "bridge_request_id",
            "operation",
            "upstream_http_status",
            "upstream_code",
            "error_type",
            "elapsed_ms",
        }


@pytest.mark.anyio
async def test_calls_are_bounded_without_dropping_business_requests_or_reusing_request_state():
    def reply(request):
        return httpx.Response(200, json={"code": "OK", "data": {"unchanged": True}})

    async with observed_bridge(reply) as (bridge, timing, seen):
        for _ in range(7):
            assert await bridge.get(ORDERS, "Bearer " + PRIVATE) == {"unchanged": True}
        value = header(timing, 200)
        assert len(seen) == 7 and len(value["java_bridge_calls"]) == 4
        assert value["java_bridge_calls_overflow"] == 3
        assert [call["bridge_request_id"] for call in value["java_bridge_calls"]] == [
            request.headers["X-Request-ID"] for request in seen[:4]
        ]
        # Closed request timing must not acquire calls from a later task/request.
        assert await bridge.get(ORDERS, "Bearer " + PRIVATE) == {"unchanged": True}
        assert len(timing.java_bridge_calls) == 4 and timing.java_bridge_calls_overflow == 3

    async with observed_bridge(reply) as (bridge, timing, seen):
        assert await bridge.get(ORDERS, "Bearer " + PRIVATE) == {"unchanged": True}
        value = header(timing, 200)
        assert len(value["java_bridge_calls"]) == 1 and value["java_bridge_calls_overflow"] == 0


@pytest.mark.anyio
@pytest.mark.parametrize("upstream_status", [200, 503])
async def test_observation_sink_failure_is_omitted_without_changing_success_or_rejection(
    monkeypatch, upstream_status
):
    def reply(request):
        return httpx.Response(upstream_status, json={"code": "OK", "data": {"unchanged": True}})

    def unavailable(**metadata):
        raise RuntimeError(PRIVATE)

    async with observed_bridge(reply) as (bridge, timing, seen):
        monkeypatch.setattr(timing, "record_java_bridge_call", unavailable)
        if upstream_status == 200:
            assert await bridge.get(ORDERS, "Bearer " + PRIVATE) == {"unchanged": True}
        else:
            with pytest.raises(DomainError) as raised:
                await bridge.get(ORDERS, "Bearer " + PRIVATE)
            assert (raised.value.code, raised.value.status) == ("JAVA_BRIDGE_REJECTED", 503)
        assert len(seen) == 1
        value = header(timing, upstream_status)
        assert "java_bridge_calls" not in value and "java_bridge_calls_overflow" not in value


@pytest.mark.anyio
@pytest.mark.parametrize("failed_clock_read", [1, 2])
async def test_observation_clock_failure_does_not_change_success(monkeypatch, failed_clock_read):
    reads = 0

    def clock():
        nonlocal reads
        reads += 1
        if reads == failed_clock_read:
            raise RuntimeError(PRIVATE)
        return 1.0

    def reply(request):
        return httpx.Response(200, json={"code": "OK", "data": {"unchanged": True}})

    async with observed_bridge(reply) as (bridge, timing, seen):
        # Replace this module's observer clock only; the existing phase clock remains untouched.
        monkeypatch.setattr(java_bridge, "time", SimpleNamespace(perf_counter=clock))
        assert await bridge.get(ORDERS, "Bearer " + PRIVATE) == {"unchanged": True}
        assert len(seen) == 1
        value = header(timing, 200)
        assert "java_bridge_calls" not in value and "java_bridge_calls_overflow" not in value


@pytest.mark.anyio
async def test_disabled_observation_does_not_read_its_clock(monkeypatch):
    def forbidden_clock():
        pytest.fail("Disabled bridge observation read its clock")

    def reply(request):
        return httpx.Response(200, json={"code": "OK", "data": {"unchanged": True}})

    async with observed_bridge(reply, False) as (bridge, timing, seen):
        monkeypatch.setattr(java_bridge, "time", SimpleNamespace(perf_counter=forbidden_clock))
        assert await bridge.get(ORDERS, "Bearer " + PRIVATE) == {"unchanged": True}
        assert timing is None and len(seen) == 1


@pytest.mark.anyio
async def test_observation_sink_failure_keeps_transport_error_status_and_suppressed_cause(monkeypatch):
    def fail(request):
        raise httpx.ReadTimeout(PRIVATE, request=request)

    def unavailable(**metadata):
        raise RuntimeError(PRIVATE)

    async with observed_bridge(fail) as (bridge, timing, seen):
        monkeypatch.setattr(timing, "record_java_bridge_call", unavailable)
        with pytest.raises(DomainError) as raised:
            await bridge.get(ORDERS, "Bearer " + PRIVATE)
        assert (raised.value.code, raised.value.status, raised.value.message) == (
            "JAVA_UNAVAILABLE",
            503,
            "商城服务暂不可用",
        )
        assert raised.value.__cause__ is None and raised.value.__suppress_context__
        assert len(seen) == 1 and "java_bridge_calls" not in header(timing)
