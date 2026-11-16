import base64
import json
from uuid import uuid4

import pytest
from test_actions import Bridge, settings

from app.api.actions import ActionPrepareInput, CheckoutSelection
from app.bootstrap import Services
from app.domain.identity import DomainError, Principal
from app.infrastructure.action_security import parameter_hash
from app.infrastructure.postgres.database import execute


def parameters():
    return {
        "action_type": "CREATE_ORDER",
        "source": "CART",
        "cart_item_ids": ["1"],
        "address_id": "2",
        "coupon_id": None,
        "use_points": 0,
    }


class OrderBridge(Bridge):
    async def quote(self, token, params, key):
        result = await super().quote(
            token,
            {"action_type": "ADD_CART", "items": [{"product_id": "1", "sku_id": "3", "quantity": 1}]},
            key,
        )
        return result | {
            "parameters": params,
            "parameters_hash": parameter_hash(self.member.id, params),
            "address_summary": "测试地址",
            "rule_version": "ORDER_CHECKOUT_V1",
        }

    def result(self, operation, rejected=False):
        return super().result(operation, rejected) | {
            "business_type": None if rejected else "ORDER",
            "business_id": None if rejected else "9",
        }


@pytest.mark.anyio
async def test_order_confirmation_recovery_and_cross_conversation_unresolved_guard(integration_config):
    cfg = settings(integration_config).model_copy(update={"orders_enabled": True})
    svc = Services(cfg)
    member = Principal(2000000000 + uuid4().int % 1000000000, "test buyer")
    bridge = OrderBridge(member)
    actions = svc.extensions.actions.require()
    actions.bridge = bridge
    try:
        conv = await svc.conversations.create(
            member, str(uuid4()), {"title": "[TEST ONLY] checkout", "entry_context": {"kind": "GENERAL"}}
        )
        body = {"expected_conversation_version": conv["version"], "parameters": parameters()}
        action = await actions.prepare(member, "unit", conv["id"], str(uuid4()), body)
        assert action["action_type"] == "CREATE_ORDER" and not bridge.calls
        other = await svc.conversations.create(
            member, str(uuid4()), {"title": "[TEST ONLY] other", "entry_context": {"kind": "GENERAL"}}
        )
        bridge.mode = "lost_response"
        confirmed = await actions.confirm(
            member, "unit", action["id"], str(uuid4()), {"expected_version": action["version"]}
        )
        assert confirmed["state"] == "UNKNOWN" and len(bridge.calls) == 1
        claims = json.loads(
            base64.urlsafe_b64decode(bridge.calls[0]["confirmation_receipt"].split(".")[0] + "==")
        )
        assert claims["action_type"] == "CREATE_ORDER" and claims["audience"] == "mall-portal-agent-order"
        with pytest.raises(DomainError) as blocked:
            await actions.prepare(
                member,
                "unit",
                other["id"],
                str(uuid4()),
                body | {"expected_conversation_version": other["version"]},
            )
        assert blocked.value.code == "ACTION_UNRESOLVED"
        async with svc.db.transaction() as conn:
            await execute(
                conn,
                "UPDATE ag_action_dispatch SET next_attempt_at=now(),lease_until=NULL WHERE action_id=:id",
                id=action["id"],
            )
        await actions.reconcile_due()
        recovered = await actions.get(member, action["id"])
        assert recovered["state"] == "SUCCEEDED" and recovered["result"]["business_type"] == "ORDER"
        assert len(bridge.calls) == 1
    finally:
        await svc.close()


def test_order_dto_only_accepts_explicit_cart_ids_and_buyer_choices():
    body = {"expected_conversation_version": 1, "parameters": parameters()}
    assert ActionPrepareInput.model_validate(body)
    for change in (
        {"cart_item_ids": ["1", "1"]},
        {"cart_item_ids": []},
        {"cart_item_ids": ["9223372036854775808"]},
        {"source": "DIRECT"},
        {"use_points": True},
        {"use_points": -1},
        {"pay_amount": "0.01"},
        {"address_id": "9223372036854775808"},
    ):
        with pytest.raises(ValueError):
            ActionPrepareInput.model_validate(body | {"parameters": parameters() | change})
    assert CheckoutSelection.model_validate(
        {"action_type": "CREATE_ORDER", "source": "CART", "cart_item_ids": ["1"]}
    )
