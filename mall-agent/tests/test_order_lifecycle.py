import base64
import hmac
import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from test_actions import Bridge, settings

from app.api.actions import ActionPrepareInput, CheckoutSelection
from app.api.admin import staff
from app.api.orders import NodeCreate, OrderRead, ReturnReview
from app.application.orders import validate_cancellation_quote
from app.bootstrap import Services
from app.domain.identity import DomainError, Principal
from app.infrastructure.action_security import parameter_hash
from app.infrastructure.postgres.database import execute, one, rows
from app.main import create_app


@pytest.mark.anyio
async def test_order_number_is_resolved_with_current_member_token_before_detail_read():
    from app.application.orders import query_orders

    class QueryBridge:
        calls = []

        async def get(self, path, token, params=None):
            self.calls.append((path, token, params))
            return (
                {"items": [{"id": "113"}]} if path.endswith("/orders") else {"order_id": "113", "nodes": []}
            )

    bridge = QueryBridge()
    query = OrderRead(kind="LOGISTICS", order_sn="202611120100000004")
    assert (await query_orders(bridge, "member-token", query))["order_id"] == "113"
    assert bridge.calls[0][2] == {"order_sn": "202611120100000004", "limit": 2}
    assert bridge.calls[1][0].endswith("/orders/113/logistics")
    assert all(call[1] == "member-token" for call in bridge.calls)


@pytest.mark.anyio
@pytest.mark.parametrize("intent", ["ORDER", "RETURN"])
async def test_order_and_return_graph_nodes_only_offer_current_member_read_tools(intent):
    from types import SimpleNamespace

    from app.agents.live_workflow import build_live_workflow
    from app.application.extensions import Extensions
    from app.infrastructure.dialog_provider import DialogEvent

    class Provider:
        count = 0

        async def stream_turn(self, inputs, prompt, tools, record, schema=None):
            if schema:
                yield DialogEvent("text", json.dumps({"intent": intent}))
                return
            assert [t["name"] for t in tools] == ["query_my_orders"]
            self.count += 1
            if self.count == 1:
                yield DialogEvent(
                    "completed",
                    [
                        {
                            "type": "function_call",
                            "name": "query_my_orders",
                            "call_id": "read",
                            "arguments": json.dumps({"kind": "ORDERS", "limit": 5}),
                        }
                    ],
                )
            else:
                assert "DEMO-ORDER" in inputs[-1]["output"]
                yield DialogEvent("text", "你的演示订单仍待付款；取消须点击确认卡。")
                yield DialogEvent("completed", [])

    queried, saved = [], []

    async def query(value):
        queried.append(value)
        return {
            "items": [{"id": "1", "order_sn": "DEMO-ORDER", "lines": [], "status": 0}],
            "has_more": False,
            "next_cursor": None,
        }

    async def ignore(*unused):
        pass

    async def assemble(*unused):
        return {"history": []}

    async def persist(answer, orders):
        saved.append((answer, orders))

    graph, context = build_live_workflow(
        Provider(), None, SimpleNamespace(max_model_rounds=3, orders_enabled=True)
    )
    await graph.ainvoke(
        {"question": "读取本人订单和售后", "steps_used": 0},
        context=context(
            guard=ignore,
            assemble=assemble,
            search=ignore,
            emit=ignore,
            record=ignore,
            route_record=ignore,
            extensions=Extensions(),
            order_query=query,
            persist_orders=persist,
        ),
    )
    assert len(queried) == 2 and queried[0] == queried[1] and queried[0]["kind"] == "ORDERS"
    assert saved[0][1][0]["id"] == "1"


class LifecycleBridge(Bridge):
    async def quote(self, token, parameters, key):
        q = await super().quote(
            token,
            {"action_type": "ADD_CART", "items": [{"product_id": "1", "sku_id": "2", "quantity": 1}]},
            key,
        )
        q.update(parameters=parameters, parameters_hash=parameter_hash(self.member.id, parameters))
        if parameters["action_type"] == "CANCEL_ORDER":
            q["lines"] = []
            q["cancellation"] = {
                "id": parameters["order_id"],
                "order_sn": "TEST-20261113-1",
                "pay_amount": q["pricing"]["payable"],
                "status": 0,
            }
            q["rule_version"] = "CANCEL_PENDING_V2"
        return q

    def result(self, operation, rejected=False):
        return super().result(operation, rejected) | {
            "business_type": None if rejected else "RETURN",
            "business_id": None if rejected else "991",
            "cart_quantity": None,
        }


def test_cancellation_quote_must_bind_authoritative_display_order_and_money():
    params = {"action_type": "CANCEL_ORDER", "order_id": "123"}
    summary = {"id": "123", "order_sn": "202611130100000001", "pay_amount": "199.90", "status": 0}
    quote = {"cancellation": summary, "pricing": {"payable": "199.90"}, "rule_version": "CANCEL_PENDING_V2"}
    validate_cancellation_quote(quote, params)
    for fields in (
        {"id": "124"},
        {"order_sn": ""},
        {"pay_amount": "0.00"},
        {"status": False},
        {"status": 1},
        {"pay_amount": 199.90},
    ):
        with pytest.raises(DomainError) as error:
            validate_cancellation_quote(quote | {"cancellation": summary | fields}, params)
        assert error.value.code == "JAVA_PROTOCOL_ERROR"
    for invalid in ({}, quote | {"cancellation": None}, quote | {"rule_version": "CANCEL_PENDING_V1"}):
        with pytest.raises(DomainError):
            validate_cancellation_quote(invalid, params)


def test_direct_and_return_inputs_are_strict_and_keep_canonical_null_coupon():
    selection = {
        "action_type": "CREATE_ORDER",
        "source": "DIRECT",
        "items": [{"product_id": "1", "sku_id": "2", "quantity": 2}],
    }
    assert CheckoutSelection.model_validate(selection).wire() == selection
    params = selection | {"address_id": "3", "coupon_id": None, "use_points": 0}
    assert (
        ActionPrepareInput.model_validate({"expected_conversation_version": 1, "parameters": params}).wire()[
            "parameters"
        ]
        == params
    )
    for changes in (
        {"cart_item_ids": ["1"]},
        {"items": []},
        {"items": [{"product_id": "1", "sku_id": "2", "quantity": True}]},
        {"source": "CART"},
    ):
        with pytest.raises(ValueError):
            CheckoutSelection.model_validate(selection | changes)
    with pytest.raises(ValueError):
        ReturnReview(expected_version=1, decision="REJECT", note="原因", approved_amount="1.00")
    with pytest.raises(ValueError):
        NodeCreate(
            occurred_at="2026-11-12T12:00:00", location="x", description="x", expected_shipment_version=1
        )
    with pytest.raises(DomainError):
        OrderRead(kind="ORDER", order_id="9223372036854775808")


@pytest.mark.anyio
async def test_return_proof_protection_attestation_unknown_recovery_and_owner_isolation(integration_config):
    cfg = settings(integration_config).model_copy(update={"orders_enabled": True})
    svc = Services(cfg)
    buyer = Principal(2000000000 + uuid4().int % 1000000000, "test buyer")
    bridge = LifecycleBridge(buyer)
    action_service = svc.extensions.actions.require()
    action_service.bridge = bridge
    attachment = uuid4()
    try:
        conv = await svc.conversations.create(
            buyer, str(uuid4()), {"title": "[TEST ONLY] return proof", "entry_context": {"kind": "GENERAL"}}
        )
        async with svc.db.transaction() as conn:
            await execute(
                conn,
                "INSERT INTO ag_attachment(id,owner_kind,owner_id,purpose,object_key,original_name,mime_type,byte_size,sha256,state,retention_until) VALUES(:id,'MEMBER',:owner,'CHAT_IMAGE',:key,'proof.jpg','image/jpeg',10,:sha,'READY',now()+interval '1 day')",
                id=attachment,
                owner=buyer.id,
                key="test/" + str(attachment),
                sha="a" * 64,
            )
        params = {
            "action_type": "CREATE_RETURN",
            "order_id": "1",
            "order_item_id": "2",
            "quantity": 1,
            "reason_code": "1",
            "description": "合成退货说明",
            "attachment_ids": [str(attachment)],
        }
        body = {"expected_conversation_version": conv["version"], "parameters": params}
        action = await action_service.prepare(buyer, "unit", conv["id"], str(uuid4()), body)
        async with svc.db.transaction() as conn:
            proof = await one(conn, "SELECT * FROM ag_attachment_ref WHERE action_id=:id", id=action["id"])
            assert proof["protected"] and proof["ref_kind"] == "RETURN_PROOF"
        bridge.mode = "lost_response"
        pending = await action_service.confirm(
            buyer, "unit", action["id"], str(uuid4()), {"expected_version": action["version"]}
        )
        assert pending["state"] == "UNKNOWN" and len(bridge.calls) == 1
        sent = bridge.calls[0]["proofs"][0]
        payload, signature = sent["attestation"].split(".")
        assert hmac.compare_digest(
            hmac.digest(cfg.confirmation_secret.get_secret_value().encode(), payload.encode(), "sha256"),
            base64.urlsafe_b64decode(signature + "=="),
        )
        claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
        assert claims["purpose"] == "return_proof" and claims["operation_no"] == action["id"]
        assert claims["sha256"] == "a" * 64 and int(claims["expires_at"]) > datetime.now(UTC).timestamp()
        async with svc.db.transaction() as conn:
            await execute(
                conn,
                "UPDATE ag_action_dispatch SET next_attempt_at=now(),lease_until=NULL WHERE action_id=:id",
                id=action["id"],
            )
        await action_service.reconcile_due()
        recovered = await action_service.get(buyer, action["id"])
        assert recovered["state"] == "SUCCEEDED" and len(bridge.calls) == 1
        async with svc.db.transaction() as conn:
            proof = await one(conn, "SELECT * FROM ag_attachment_ref WHERE action_id=:id", id=action["id"])
            assert proof["return_apply_id"] == 991 and proof["protected"]
            assert (await svc.attachments._visible_image(conn, buyer, attachment))["id"] == attachment
            with pytest.raises(DomainError):
                await svc.attachments._visible_image(conn, replace(buyer, id=buyer.id + 1), attachment)
            operator = Principal(3, "demo staff", "STAFF", frozenset({"agent:return:read"}))
            assert (await svc.attachments._visible_image(conn, operator, attachment))["id"] == attachment
        with pytest.raises(DomainError):
            await action_service.get(replace(buyer, id=buyer.id + 1), action["id"])
    finally:
        await svc.close()


@pytest.mark.anyio
async def test_staff_lifecycle_facade_denies_missing_permission_and_audits_success(integration_config):
    cfg = settings(integration_config).model_copy(update={"orders_enabled": True})
    svc = Services(cfg)
    app = create_app(cfg)
    app.state.services = svc
    identity = Principal(2000000000 + uuid4().int % 1000000000, "staff", "STAFF", frozenset())
    app.dependency_overrides[staff] = lambda: identity
    calls = []

    async def command(token, path, method, body, key):
        calls.append((path, method, body, key))
        return {"id": "99", "stage": "GOODS_RECEIVED"}

    svc.bridge.staff_command = command
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {"Authorization": "unit", "Idempotency-Key": str(uuid4())}
            response = await client.post(
                "/mall-agent/v1/admin/returns/99/receive",
                headers=headers,
                json={"expected_version": 2, "note": "确认演示收货"},
            )
            assert response.status_code == 403 and not calls
            identity = replace(identity, permissions=frozenset({"agent:return:receive"}))
            response = await client.post(
                "/mall-agent/v1/admin/returns/99/receive",
                headers=headers,
                json={"expected_version": 2, "note": "确认演示收货"},
            )
            assert response.status_code == 200 and len(calls) == 1
            async with svc.db.transaction() as conn:
                audit = await rows(
                    conn,
                    "SELECT * FROM ag_audit_log WHERE actor_id=:actor AND request_id=CAST(:key AS uuid)",
                    actor=identity.id,
                    key=headers["Idempotency-Key"],
                )
                assert len(audit) == 1 and audit[0]["details"] == {"simulated": True}
    finally:
        await svc.close()
