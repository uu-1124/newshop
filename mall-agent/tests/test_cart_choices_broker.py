"""Broker independently re-reads current cart facts before any quote."""

from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from test_actions import settings
from test_orders import OrderBridge
from test_preparation_tools import live_run

from app.application.cart import cart_for_member_choice
from app.bootstrap import Services
from app.domain.identity import DomainError, Principal
from app.infrastructure.postgres.database import execute
from app.main import create_app


def row(identifier="30", available=True):
    return {
        "id": identifier,
        "product_id": "1",
        "sku_id": str(int(identifier) + 10),
        "name": "合成手机Reno8",
        "specification": "[]",
        "quantity": 1,
        "available": available,
        "available_quantity": 5,
        "unit_price": "2.00",
    }


def page(items, more=False):
    return {
        "items": items,
        "has_more": more,
        "next_cursor": items[-1]["id"] if more else None,
        "checked_at": datetime.now(UTC).isoformat(),
    }


@pytest.mark.anyio
async def test_cart_broker_rejects_model_choices_expired_selection_and_changed_fence_before_quote(
    integration_config,
):
    cfg = settings(integration_config).model_copy(update={"orders_enabled": True})
    svc, buyer = Services(cfg), Principal(1500000000 + uuid4().int % 100000000, "cart chooser")
    app = create_app(cfg)
    app.state.services = svc
    bridge = OrderBridge(buyer)
    svc.extensions.actions.require().bridge = bridge
    quotes, reads = [], []
    context = {}
    original_quote = bridge.quote

    async def identity(token):
        return buyer

    async def cart(token, query):
        reads.append(query.model_dump())
        assert token == "Bearer SYNTHETIC_ONLY"
        if context.get("fence"):
            async with svc.db.transaction() as conn:
                await execute(
                    conn,
                    "UPDATE ag_conversation SET control_epoch=control_epoch+1 WHERE id=:id",
                    id=context["cid"],
                )
        return page([row(available=context.get("available", True)), row("31")])

    async def options(*args):
        return {"addresses": [{"id": "10"}], "coupons": [], "available_points": 0}

    async def quote(token, params, key):
        quotes.append(params)
        return await original_quote(token, params, key)

    svc.bridge.identity, svc.bridge.cart = identity, cart
    svc.extensions.actions.require().checkout_options = options
    bridge.quote = quote
    params = {
        "action_type": "CREATE_ORDER",
        "source": "CART",
        "cart_item_ids": ["30"],
        "address_id": "10",
        "coupon_id": None,
        "use_points": 0,
    }
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for question, choice, code, extra in [
                ("购物车买哪些还没决定", ["30"], "MEMBER_CHOICE_REQUIRED", {}),
                ("选择购物车条目31", ["30"], "MEMBER_CHOICE_REQUIRED", {}),
                ("选择购物车条目30", ["30", "31"], "MEMBER_CHOICE_REQUIRED", {}),
                ("选择购物车条目30", ["30"], "MEMBER_CHOICE_REQUIRED", {"available": False}),
                ("选择购物车条目30", ["30"], "LEASE_LOST", {"fence": True}),
                ("选择购物车条目30", ["30"], "OK", {}),
            ]:
                context.clear()
                context.update(extra)
                rid, cid = await live_run(
                    svc, buyer, question + "。用地址10，不用券和积分", "Bearer SYNTHETIC_ONLY"
                )
                context["cid"] = cid
                before = len(quotes)
                headers = {"X-Broker-Key": cfg.broker_secret.get_secret_value()}
                body = {
                    "lease_owner": "prepare-worker",
                    "fencing_token": 1,
                    "tool": "prepare_business_action",
                    "arguments": params | {"cart_item_ids": choice},
                }
                path = f"/internal/runtime-auth/{rid}/tools"
                response = await client.post(path, headers=headers, json=body)
                assert response.json()["code"] == code
                if code == "OK":
                    assert response.status_code == 200 and len(quotes) == before + 1
                    read_count = len(reads)
                    replay = await client.post(path, headers=headers, json=body)
                    assert replay.json()["data"]["id"] == response.json()["data"]["id"]
                    assert len(reads) == read_count and len(quotes) == before + 1
                else:
                    assert response.status_code == 409 and len(quotes) == before
            assert not bridge.calls
    finally:
        await svc.close()


@pytest.mark.anyio
async def test_large_cart_is_bounded_and_direct_ids_past_first_hundred_remain_available():
    calls = []

    class Bridge:
        async def cart(self, token, query):
            assert token == "owner-token"
            calls.append((query.limit, query.after))
            start = int(query.after or "0") + 1
            data = [row(str(i)) for i in range(start, min(151, start + query.limit))]
            return page(data, int(data[-1]["id"]) < 150)

    rows, complete = await cart_for_member_choice(Bridge(), "owner-token", ["145", "146"])
    assert not complete and len(calls) == 7 and {"145", "146"} <= rows.keys()
    assert calls[-2:] == [(1, "144"), (1, "145")]


@pytest.mark.anyio
async def test_cart_requery_protocol_mismatch_does_not_return_partial_evidence():
    class Bridge:
        async def cart(self, token, query):
            return page([row("30"), row("30")])

    with pytest.raises(DomainError, match="JAVA_PROTOCOL_ERROR"):
        await cart_for_member_choice(Bridge(), "owner-token", ["30"])
