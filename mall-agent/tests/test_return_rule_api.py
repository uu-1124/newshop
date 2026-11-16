from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError
from test_runtime import settings_for

from app.api.admin import staff
from app.api.orders import ReturnRuleInput
from app.bootstrap import Services
from app.domain.identity import Principal
from app.main import create_app


def test_return_rule_requires_bounded_hours_note_and_timezone():
    body = {
        "expected_latest_version": 1,
        "return_window_hours": 168,
        "allow_shipped": True,
        "effective_at": "2026-11-14T20:00:00+08:00",
        "note": "新版本",
    }
    assert ReturnRuleInput(**body).model_dump(mode="json")["effective_at"] == "2026-11-14T12:00:00Z"
    for changed in [
        {"return_window_hours": 0},
        {"return_window_hours": 721},
        {"return_window_hours": True},
        {"allow_shipped": "true"},
        {"note": "  "},
        {"effective_at": "2026-11-14T12:00:00"},
    ]:
        with pytest.raises(ValidationError):
            ReturnRuleInput(**(body | changed))


@pytest.mark.anyio
async def test_rule_facade_checks_permissions_and_forwards_utc_and_original_idempotency(integration_config):
    from uuid import uuid4

    svc = Services(settings_for(integration_config))
    principal = Principal(
        900003333, "rule reviewer", "STAFF", frozenset({"agent:return:read", "agent:return:review"})
    )
    app = create_app(svc.runs.settings)
    app.state.services = svc

    async def current():
        return principal

    app.dependency_overrides[staff] = current
    svc.bridge.get = AsyncMock(return_value={"items": [], "active_rule_version": "ITEM_NET_DEMO_V2"})
    svc.extensions.after_sale.implementation = type(
        "AfterSale", (), {"staff_transition": AsyncMock(return_value={"id": "2", "version_no": 2})}
    )()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            key = str(uuid4())
            headers = {"Authorization": "Bearer synthetic", "Idempotency-Key": key}
            result = await client.get("/mall-agent/v1/admin/return-rules", headers=headers)
            assert result.status_code == 200
            body = {
                "expected_latest_version": 1,
                "return_window_hours": 168,
                "allow_shipped": True,
                "effective_at": "2026-11-14T20:00:00+08:00",
                "note": "test",
            }
            result = await client.post("/mall-agent/v1/admin/return-rules", json=body, headers=headers)
            assert result.status_code == 200
            args = svc.extensions.after_sale.require().staff_transition.call_args.args
            assert (
                args[1:3] == ("/return-rules", "POST")
                and args[3]["effective_at"] == "2026-11-14T12:00:00Z"
                and str(args[4]) == key
            )
            principal = Principal(principal.id, "read only", "STAFF", frozenset({"agent:return:read"}))
            assert (
                await client.post("/mall-agent/v1/admin/return-rules", json=body, headers=headers)
            ).status_code == 403
            assert svc.extensions.after_sale.require().staff_transition.await_count == 1
    finally:
        await svc.close()
