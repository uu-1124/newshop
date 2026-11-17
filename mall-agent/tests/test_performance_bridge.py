import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from app.domain.identity import DomainError
from app.infrastructure.java_bridge import JavaBridge
from app.infrastructure.performance_timing import finish_request, start_request


@pytest.mark.anyio
@pytest.mark.parametrize("status", [200, 401, 403, 503])
async def test_bridge_observation_keeps_authentication_rejection_and_success_semantics(status):
    seen = []

    def reply(request):
        seen.append(request)
        return httpx.Response(
            status,
            json={
                "code": "OK" if status == 200 else status,
                "data": {"kind": "MEMBER", "id": "1", "display_name": "demo"},
            },
        )

    bridge = JavaBridge(
        SimpleNamespace(
            gateway_url="http://test",
            bridge_secret=SecretStr("bridge-test"),
            staff_bridge_secret=SecretStr("staff-test"),
        )
    )
    await bridge.client.aclose()
    bridge.client = httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(reply))
    timing, token = start_request(True, str(uuid4()), str(uuid4()))
    try:
        if status == 200:
            value = await bridge.identity("Bearer private-demo-token")
            assert value.id == 1
        else:
            with pytest.raises(DomainError) as error:
                await bridge.identity("Bearer private-demo-token")
            assert error.value.status == status
        header = timing.header("memberMe", status)
        observed = json.loads(header)
        assert observed["java_bridge_ms"] >= 0
        assert seen[0].headers["Authorization"] == "Bearer private-demo-token"
        assert "X-Agent-Signature" in seen[0].headers
        assert seen[0].headers["X-Agent-Parent-Request-ID"] == timing.request_id
        assert seen[0].headers["X-Request-ID"] != timing.request_id
        assert "private-demo-token" not in header and "bridge-test" not in header
    finally:
        finish_request(timing, token)
        await bridge.client.aclose()


@pytest.mark.anyio
async def test_disabled_bridge_observation_does_not_add_parent_header():
    seen = []

    def reply(request):
        seen.append(request)
        return httpx.Response(200, json={"code": "OK", "data": {"value": "unchanged"}})

    bridge = JavaBridge(
        SimpleNamespace(
            gateway_url="http://test",
            bridge_secret=SecretStr("bridge-test"),
            staff_bridge_secret=SecretStr("staff-test"),
        )
    )
    await bridge.client.aclose()
    bridge.client = httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(reply))
    try:
        assert await bridge.get("/unchanged", "Bearer private-demo-token") == {"value": "unchanged"}
        assert "X-Agent-Parent-Request-ID" not in seen[0].headers
        assert seen[0].headers["Authorization"] == "Bearer private-demo-token"
    finally:
        await bridge.client.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("staff", [False, True], ids=["member", "staff"])
@pytest.mark.parametrize("enabled", [False, True], ids=["observation_off", "observation_on"])
@pytest.mark.parametrize(
    ("status", "upstream_code", "outer_code"),
    [
        (403, "BRIDGE_SIGNATURE_INVALID", "FORBIDDEN"),
        (503, "BRIDGE_NONCE_UNAVAILABLE", "JAVA_BRIDGE_REJECTED"),
    ],
    ids=["signature_rejected", "nonce_dependency_unavailable"],
)
async def test_string_bridge_failures_preserve_status_without_retry_or_secret_leak(
    staff, enabled, status, upstream_code, outer_code
):
    seen = []
    member_secret, staff_secret, authorization = (
        "private-member-signing-secret",
        "private-staff-signing-secret",
        "Bearer private-upstream-auth-token",
    )
    private_values = (member_secret, staff_secret, authorization, "private-upstream-auth-token")

    def reply(request):
        seen.append(request)
        return httpx.Response(
            status,
            json={
                "code": upstream_code,
                "message": "untrusted upstream detail: " + "|".join(private_values),
                "data": None,
                "request_id": str(uuid4()),
            },
        )

    bridge = JavaBridge(
        SimpleNamespace(
            gateway_url="http://test",
            bridge_secret=SecretStr(member_secret),
            staff_bridge_secret=SecretStr(staff_secret),
        )
    )
    await bridge.client.aclose()
    bridge.client = httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(reply))
    timing, token = start_request(enabled, str(uuid4()), str(uuid4()))
    try:
        identity = bridge.staff_identity if staff else bridge.identity
        with pytest.raises(DomainError) as error:
            await identity(authorization)
        assert error.value.status == status
        assert error.value.code == outer_code
        assert len(seen) == 1  # No automatic retry of a possibly-consumed nonce.
        request = seen[0]
        assert request.method == "GET"
        assert request.url.path == (
            "/mall-admin/agent-bridge/v1/identity" if staff else "/mall-portal/agent-bridge/v1/identity"
        )
        assert request.headers["Authorization"] == authorization
        assert request.headers["X-Agent-Signature"].startswith("v1:")
        assert "Idempotency-Key" not in request.headers
        public_output = str(error.value) + repr(error.value) + error.value.message
        if enabled:
            assert request.headers["X-Agent-Parent-Request-ID"] == timing.request_id
            header = timing.header("staffMe" if staff else "memberMe", error.value.status)
            observed = json.loads(header)
            assert observed["status_code"] == status
            assert observed["java_bridge_ms"] >= 0
            public_output += header
        else:
            assert timing is None
            assert "X-Agent-Parent-Request-ID" not in request.headers
        for value in (*private_values, request.headers["X-Agent-Signature"]):
            assert value not in public_output
    finally:
        finish_request(timing, token)
        await bridge.client.aclose()
    assert bridge.client.is_closed
