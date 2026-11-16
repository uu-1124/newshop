import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from test_runtime import settings_for

from app.api.actions import ActionPrepareInput
from app.bootstrap import Services
from app.domain.identity import DomainError, Principal
from app.infrastructure.action_security import parameter_hash
from app.infrastructure.postgres.database import encoded, execute, one, rows
from app.main import create_app


def settings(cfg):
    return settings_for(
        cfg,
        actions_enabled=True,
        handoffs_enabled=True,
        confirmation_secret="test-confirmation-" * 4,
        reconciliation_secret="test-reconciliation-" * 4,
        action_encryption_key=Fernet.generate_key().decode(),
    )


class Bridge:
    def __init__(self, member):
        self.member = member
        self.results, self.calls = {}, []
        self.mode = None

    async def quote(self, token, parameters, key):
        total = sum(item["quantity"] for item in parameters["items"]) * Decimal("2.00")
        pricing = {
            name: "0.00" for name in ("promotion_discount", "coupon_discount", "points_discount", "freight")
        }
        return {
            "quote_id": str(uuid4()),
            "parameters": parameters,
            "parameters_hash": parameter_hash(self.member.id, parameters),
            "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            "rule_version": "CART_SKU_PRICE_V1",
            "address_summary": None,
            "pricing": pricing | {"subtotal": str(total), "payable": str(total), "currency": "CNY"},
            "lines": [
                {
                    "product_id": item["product_id"],
                    "sku_id": item["sku_id"],
                    "quantity": item["quantity"],
                    "name": "测试商品",
                    "specification": "红色",
                    "unit_price": "2.00",
                    "line_payable": str(Decimal("2.00") * item["quantity"]),
                }
                for item in parameters["items"]
            ],
        }

    def result(self, operation, rejected=False):
        return {
            "operation_no": operation,
            "state": "REJECTED" if rejected else "SUCCEEDED",
            "business_type": None if rejected else "CART",
            "business_id": None,
            "error_code": "RECOVERY_NOT_EXECUTED" if rejected else None,
            "message": "test result",
            "order": None,
            "return_apply": None,
            "cart_quantity": None if rejected else 1,
            "completed_at": datetime.now(UTC).isoformat(),
        }

    async def execute_action(self, token, body):
        self.calls.append(body)
        if self.mode == "crash_before":
            raise asyncio.CancelledError()
        self.results.setdefault(body["operation_no"], self.result(body["operation_no"]))
        if self.mode == "lost_response":
            raise DomainError("JAVA_UNAVAILABLE", "unknown", 503)
        return self.results[body["operation_no"]]

    async def reconcile_action(self, operation, parameters_hash, grant):
        claims = json.loads(base64.urlsafe_b64decode(grant.split(".")[0] + "=="))
        assert claims["purpose"] == "reconcile_only" and claims["parameters_hash"] == parameters_hash
        self.results.setdefault(operation, self.result(operation, True))
        return self.results[operation]


async def prepare(svc, member, cid, quantity=1, key=None):
    snap = await svc.conversations.snapshot(member, cid)
    return await svc.extensions.actions.require().prepare(
        member,
        "Bearer TEST_RAW_TOKEN_NEVER_PERSIST",
        cid,
        key or str(uuid4()),
        {
            "expected_conversation_version": snap["conversation"]["version"],
            "parameters": {
                "action_type": "ADD_CART",
                "items": [{"product_id": "1", "sku_id": "2", "quantity": quantity}],
            },
        },
    )


async def setup(cfg):
    svc = Services(settings(cfg))
    member = Principal(2000000000 + uuid4().int % 1000000000, "test buyer")
    bridge = Bridge(member)
    svc.extensions.actions.require().bridge = bridge
    conv = await svc.conversations.create(
        member, str(uuid4()), {"title": "[TEST ONLY] cart", "entry_context": {"kind": "GENERAL"}}
    )
    return svc, member, conv["id"], bridge


@pytest.mark.anyio
async def test_prepare_cancel_isolation_and_immutable_quote(integration_config):
    svc, member, cid, bridge = await setup(integration_config)
    a = svc.extensions.actions.require()
    try:
        action = await prepare(svc, member, cid)
        assert action["state"] == "PENDING_CONFIRMATION" and not bridge.calls
        snap = await svc.conversations.snapshot(member, cid)
        assert snap["recent_messages"][-1]["blocks"][0]["action"]["id"] == action["id"]
        with pytest.raises(DomainError, match="NOT_FOUND"):
            await a.get(Principal(member.id + 1, "other"), action["id"])
        cancelled = await a.cancel(
            member, action["id"], str(uuid4()), {"expected_version": action["version"]}
        )
        assert cancelled["state"] == "CANCELLED" and not bridge.calls
        snap = await svc.conversations.snapshot(member, cid)
        assert snap["recent_messages"][-1]["blocks"][0]["action"]["state"] == "CANCELLED"
        with pytest.raises(DomainError):
            await a.confirm(
                member, "unit", action["id"], str(uuid4()), {"expected_version": cancelled["version"]}
            )
    finally:
        await svc.close()


@pytest.mark.anyio
async def test_concurrent_confirmation_executes_once_and_never_persists_token(integration_config):
    svc, member, cid, bridge = await setup(integration_config)
    a = svc.extensions.actions.require()
    try:
        action = await prepare(svc, member, cid)
        key = str(uuid4())
        results = await asyncio.gather(
            *(
                a.confirm(
                    member, "Bearer TEST_RAW_TOKEN_NEVER_PERSIST", action["id"], key, {"expected_version": 1}
                )
                for _ in range(2)
            )
        )
        assert len(bridge.calls) == 1 and results[0]["id"] == results[1]["id"]
        assert (await a.get(member, action["id"]))["state"] == "SUCCEEDED"
        with pytest.raises(DomainError, match="VERSION_CONFLICT"):
            await a.confirm(member, "unit", action["id"], str(uuid4()), {"expected_version": 1})
        async with svc.db.transaction() as conn:
            row = await one(conn, "SELECT * FROM ag_action_dispatch WHERE action_id=:id", id=action["id"])
            assert row["state"] == "TERMINAL"
            assert b"reconcile_only" not in bytes(row["reconcile_grant_ciphertext"])
            data = await rows(
                conn, "SELECT response_ref FROM ag_request_dedupe WHERE actor_id=:id", id=member.id
            )
            assert "TEST_RAW_TOKEN_NEVER_PERSIST" not in encoded([dict(row) for row in data])
            audits = await one(
                conn,
                "SELECT count(*) AS n FROM ag_audit_log WHERE resource_id=:id AND operation='confirmAction'",
                id=action["id"],
            )
            assert audits["n"] == 1
    finally:
        await svc.close()


@pytest.mark.anyio
@pytest.mark.parametrize("mode,expected", [("lost_response", "SUCCEEDED"), ("crash_before", "FAILED")])
async def test_unknown_and_crash_recovery_only_reconcile(integration_config, mode, expected):
    svc, member, cid, bridge = await setup(integration_config)
    a = svc.extensions.actions.require()
    bridge.mode = mode
    try:
        action = await prepare(svc, member, cid)
        if mode == "crash_before":
            with pytest.raises(asyncio.CancelledError):
                await a.confirm(member, "unit", action["id"], str(uuid4()), {"expected_version": 1})
        else:
            result = await a.confirm(member, "unit", action["id"], str(uuid4()), {"expected_version": 1})
            assert result["state"] == "UNKNOWN"
        with pytest.raises(DomainError, match="ACTION_UNRESOLVED"):
            await prepare(svc, member, cid)
        async with svc.db.transaction() as conn:
            await execute(
                conn,
                "UPDATE ag_action_dispatch SET next_attempt_at=now(),lease_until=now()-interval '1 second' WHERE action_id=:id",
                id=action["id"],
            )
        await asyncio.gather(a.reconcile_due(), a.reconcile_due())
        assert (await a.get(member, action["id"]))["state"] == expected
        assert len(bridge.calls) == 1
        snap = await svc.conversations.snapshot(member, cid)
        assert snap["recent_messages"][0]["blocks"][0]["action"]["state"] == expected
        assert not snap["pending_actions"]
    finally:
        await svc.close()


@pytest.mark.anyio
async def test_new_preparation_expiry_and_handoff_fence_confirmation(integration_config):
    svc, member, cid, bridge = await setup(integration_config)
    a = svc.extensions.actions.require()
    try:
        first = await prepare(svc, member, cid)
        second = await prepare(svc, member, cid, 2)
        assert (await a.get(member, first["id"]))["state"] == "STALE"
        async with svc.db.transaction() as conn:
            await execute(
                conn,
                "UPDATE ag_action SET expires_at=now()-interval '1 second' WHERE id=:id",
                id=second["id"],
            )
        with pytest.raises(DomainError):
            await a.confirm(
                member, "unit", second["id"], str(uuid4()), {"expected_version": second["version"]}
            )
        assert (await a.get(member, second["id"]))["state"] == "EXPIRED"
        third = await prepare(svc, member, cid)
        snap = await svc.conversations.snapshot(member, cid)
        await svc.extensions.handoffs.require().request(
            member, cid, str(uuid4()), {"expected_conversation_version": snap["conversation"]["version"]}
        )
        with pytest.raises(DomainError):
            await a.confirm(member, "unit", third["id"], str(uuid4()), {"expected_version": third["version"]})
        assert (await a.get(member, third["id"]))["state"] == "STALE" and not bridge.calls
    finally:
        await svc.close()


def test_cart_input_rejects_model_money_other_actions_and_duplicate_skus():
    body = {
        "expected_conversation_version": 1,
        "parameters": {
            "action_type": "ADD_CART",
            "items": [{"product_id": "1", "sku_id": "2", "quantity": 1}],
        },
    }
    assert ActionPrepareInput.model_validate(body)
    for parameters in (
        body["parameters"] | {"price": "0.01"},
        body["parameters"] | {"action_type": "CREATE_ORDER"},
        body["parameters"] | {"items": body["parameters"]["items"] * 2},
        body["parameters"] | {"items": [{"product_id": "1", "sku_id": "2", "quantity": True}]},
    ):
        with pytest.raises(ValueError):
            ActionPrepareInput.model_validate(body | {"parameters": parameters})


@pytest.mark.anyio
async def test_stop_generation_stales_existing_confirmation(integration_config):
    svc, member, cid, bridge = await setup(integration_config)
    try:
        action = await prepare(svc, member, cid)
        sent = await svc.conversations.send(
            member,
            "unit",
            cid,
            str(uuid4()),
            {"client_message_id": str(uuid4()), "text": "another question", "attachment_ids": []},
        )
        await svc.runs.stop(
            member, sent["run"]["id"], str(uuid4()), {"expected_version": sent["run"]["version"]}
        )
        snap = await svc.conversations.snapshot(member, cid)
        card = next(
            b["action"] for m in snap["recent_messages"] for b in m["blocks"] if b["type"] == "action"
        )
        assert card["state"] == "STALE"
        with pytest.raises(DomainError):
            await svc.extensions.actions.require().confirm(
                member, "unit", action["id"], str(uuid4()), {"expected_version": action["version"]}
            )
        assert not bridge.calls
    finally:
        await svc.close()


@pytest.mark.anyio
async def test_action_http_contract_strict_inputs_and_owned_snapshot(integration_config):
    config = settings(integration_config)
    app = create_app(config)
    member = Principal(3000000000 + uuid4().int % 1000000000, "test buyer")
    bridge = Bridge(member)
    from pathlib import Path

    from jsonschema import Draft202012Validator

    contract = json.loads(
        (Path(__file__).resolve().parents[2] / "docs/api/openapi.json").read_text(encoding="utf-8")
    )

    def validate(operation, response):
        Draft202012Validator(
            {"$ref": f"#/components/schemas/{operation}Response", "components": contract["components"]}
        ).validate(response.json())

    async with app.router.lifespan_context(app):
        svc = app.state.services

        async def identity(token):
            if token == "Bearer other":
                return Principal(member.id + 1, "other")
            return member

        svc.bridge.identity = identity
        svc.extensions.actions.require().bridge = bridge
        conv = await svc.conversations.create(
            member, str(uuid4()), {"title": "[TEST ONLY] HTTP cart", "entry_context": {"kind": "GENERAL"}}
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {"Authorization": "Bearer buyer", "Idempotency-Key": str(uuid4())}
            body = {
                "expected_conversation_version": 1,
                "parameters": {
                    "action_type": "ADD_CART",
                    "items": [{"product_id": "1", "sku_id": "2", "quantity": 1}],
                },
            }
            path = f"/mall-agent/v1/member/conversations/{conv['id']}/actions"
            assert (
                await client.post(path, headers=headers, json=body | {"confirmed": True})
            ).status_code == 422
            prepared = await client.post(path, headers=headers, json=body)
            assert prepared.status_code == 201
            validate("prepareAction", prepared)
            aid = prepared.json()["data"]["id"]
            assert (
                await client.get(
                    f"/mall-agent/v1/member/actions/{aid}", headers={"Authorization": "Bearer other"}
                )
            ).status_code == 404
            confirmed = await client.post(
                f"/mall-agent/v1/member/actions/{aid}/confirm",
                headers=headers | {"Idempotency-Key": str(uuid4())},
                json={"expected_version": 1},
            )
            assert confirmed.status_code == 202
            validate("confirmAction", confirmed)
            snap = await client.get(f"/mall-agent/v1/member/conversations/{conv['id']}", headers=headers)
            validate("getConversation", snap)


@pytest.mark.anyio
async def test_shopping_graph_falls_back_to_labeled_catalog_without_auto_execute():
    from types import SimpleNamespace

    from app.agents.live_workflow import build_live_workflow
    from app.application.extensions import Extensions
    from app.infrastructure.dialog_provider import DialogEvent

    class Provider:
        calls = 0

        async def stream_turn(self, inputs, prompt, tools, record, schema=None):
            if schema:
                yield DialogEvent("text", '{"intent":"SHOPPING"}')
                return
            self.calls += 1
            if self.calls == 1:
                yield DialogEvent(
                    "completed",
                    [
                        {
                            "type": "function_call",
                            "name": "search_products",
                            "call_id": "products",
                            "arguments": '{"query":"too-specific"}',
                        }
                    ],
                )
            else:
                yield DialogEvent("text", "请先选择商品")
                yield DialogEvent("completed", [])

    queries, saved = [], []
    product = {"id": "1", "name": "sample", "sku_options": [{"id": "2"}]}

    async def search(query):
        queries.append(query)
        return {"items": [product] if query is None else []}

    async def guard(state):
        return None

    async def assemble(state):
        return {"history": []}

    async def record(*args):
        return None

    async def persist(answer, products):
        saved.append((answer, products))

    graph, factory = build_live_workflow(
        Provider(), None, SimpleNamespace(max_model_rounds=3, actions_enabled=True)
    )
    result = await graph.ainvoke(
        {"question": "加购物车", "steps_used": 0},
        context=factory(
            guard=guard,
            assemble=assemble,
            search=search,
            emit=record,
            record=record,
            route_record=record,
            extensions=Extensions(),
            persist_products=persist,
        ),
    )
    assert queries == ["too-specific", None]
    assert result["intent"] == "SHOPPING" and saved[0][1] == [product]
    assert "样本" in saved[0][0] and "确认" in saved[0][0]
