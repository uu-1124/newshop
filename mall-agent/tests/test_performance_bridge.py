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
