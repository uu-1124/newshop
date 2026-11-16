"""New member instructions revoke old preparations before the next model turn."""

from uuid import uuid4

import pytest
from test_actions import Bridge, settings
from test_order_lifecycle import LifecycleBridge

from app.bootstrap import Services
from app.domain.identity import DomainError, Principal
from app.infrastructure.action_security import parameter_hash
from app.infrastructure.postgres.database import execute, one


class RevisionBridge(LifecycleBridge):
    async def quote(self, token, parameters, key):
        if parameters["action_type"] in {"CANCEL_ORDER", "CREATE_RETURN"}:
            return await super().quote(token, parameters, key)
        items = parameters["items"]
        quote = await Bridge.quote(self, token, {"action_type": "ADD_CART", "items": items}, key)
        return quote | {
            "parameters": parameters,
            "parameters_hash": parameter_hash(self.member.id, parameters),
        }


CASES = [
    {"action_type": "ADD_CART", "items": [{"product_id": "1", "sku_id": "2", "quantity": 1}]},
    {
        "action_type": "CREATE_ORDER",
        "source": "DIRECT",
        "items": [{"product_id": "1", "sku_id": "2", "quantity": 1}],
        "address_id": "10",
        "coupon_id": None,
        "use_points": 0,
    },
    {"action_type": "CANCEL_ORDER", "order_id": "40"},
    {
        "action_type": "CREATE_RETURN",
        "order_id": "40",
        "order_item_id": "50",
        "quantity": 1,
        "reason_code": "60",
        "description": "演示退货",
        "attachment_ids": [],
    },
]


@pytest.mark.anyio
@pytest.mark.parametrize("parameters", CASES, ids=[p["action_type"] for p in CASES])
async def test_new_message_stales_each_old_card_and_replay_does_not_revoke_its_replacement(
    integration_config, parameters
):
    cfg = settings(integration_config).model_copy(update={"orders_enabled": True})
    svc = Services(cfg)
    buyer = Principal(1700000000 + uuid4().int % 100000000, "revision buyer")
    bridge = RevisionBridge(buyer)
    actions = svc.extensions.actions.require()
    actions.bridge = bridge
    cid = None
    try:
        conv = await svc.conversations.create(
            buyer, str(uuid4()), {"title": "[TEST ONLY] revised card", "entry_context": {"kind": "GENERAL"}}
        )
        cid = conv["id"]
        prepare_key = str(uuid4())
        prepare_body = {"expected_conversation_version": conv["version"], "parameters": parameters}
        first = await actions.prepare(
            buyer,
            "synthetic-only",
            cid,
            prepare_key,
            prepare_body,
        )
        key = str(uuid4())
        body = {
            "client_message_id": str(uuid4()),
            "text": "更改刚才的选择，先不要沿用旧卡，我会补充新的规格、数量或订单。",
            "attachment_ids": [],
        }
        sent = await svc.conversations.send(buyer, "synthetic-only", cid, key, body)
        stale = await actions.get(buyer, first["id"])
        assert stale["state"] == "STALE"
        assert stale["parameters"] == first["parameters"]
        assert stale["version"] == first["version"] + 1
        assert stale["error_code"] == "MEMBER_CONTEXT_CHANGED"
        replayed_prepare = await actions.prepare(buyer, "synthetic-only", cid, prepare_key, prepare_body)
        assert replayed_prepare["id"] == first["id"] and replayed_prepare["state"] == "STALE"
        with pytest.raises(DomainError, match="ACTION_STATE_CONFLICT"):
            await actions.confirm(
                buyer, "synthetic-only", first["id"], str(uuid4()), {"expected_version": stale["version"]}
            )
        snap = await svc.conversations.snapshot(buyer, cid)
        assert any(
            b["type"] == "action" and b["action"]["state"] == "STALE"
            for m in snap["recent_messages"]
            for b in m["blocks"]
        )
        replacement = await actions.prepare(
            buyer,
            "synthetic-only",
            cid,
            str(uuid4()),
            {"expected_conversation_version": snap["conversation"]["version"], "parameters": parameters},
        )
        replay = await svc.conversations.send(buyer, "synthetic-only", cid, key, body)
        assert replay["message"]["id"] == sent["message"]["id"]
        replay_new_key = await svc.conversations.send(buyer, "synthetic-only", cid, str(uuid4()), body)
        assert replay_new_key["message"]["id"] == sent["message"]["id"]
        assert (await actions.get(buyer, replacement["id"]))["state"] == "PENDING_CONFIRMATION"
        async with svc.db.transaction() as conn:
            counts = await one(
                conn,
                "SELECT (SELECT count(*) FROM ag_run WHERE conversation_id=:id) runs,"
                "(SELECT count(*) FROM ag_action_dispatch d JOIN ag_action a ON a.id=d.action_id WHERE a.conversation_id=:id) dispatches,"
                "(SELECT count(*) FROM ag_event_outbox WHERE conversation_id=:id AND event_type='action.updated' AND payload->'action'->>'error_code'='MEMBER_CONTEXT_CHANGED') stale_events",
                id=cid,
            )
            assert counts == {"runs": 1, "dispatches": 0, "stale_events": 1}
        assert not bridge.calls
    finally:
        if cid is not None:
            current = await svc.conversations.snapshot(buyer, cid)
            active = current["active_run"]
            if active and active["state"] == "QUEUED":
                await svc.runs.stop(
                    buyer, active["id"], str(uuid4()), {"expected_version": active["version"]}
                )
        await svc.close()


@pytest.mark.anyio
async def test_new_member_context_releases_only_unsubmitted_return_protection(integration_config):
    cfg = settings(integration_config).model_copy(update={"orders_enabled": True})
    svc = Services(cfg)
    buyer = Principal(1800000000 + uuid4().int % 100000000, "proof revision")
    actions = svc.extensions.actions.require()
    actions.bridge = RevisionBridge(buyer)
    try:
        conv = await svc.conversations.create(
            buyer, str(uuid4()), {"title": "[TEST ONLY] revised proof", "entry_context": {"kind": "GENERAL"}}
        )
        aid = uuid4()
        async with svc.db.transaction() as conn:
            await execute(
                conn,
                "INSERT INTO ag_attachment(id,owner_kind,owner_id,purpose,object_key,original_name,mime_type,byte_size,sha256,state,retention_until) VALUES(:id,'MEMBER',:owner,'CHAT_IMAGE',:key,'proof.jpg','image/jpeg',10,:sha,'READY',now()+interval '1 day')",
                id=aid,
                owner=buyer.id,
                key="test-revision/" + str(aid),
                sha="d" * 64,
            )
        first = await actions.prepare(
            buyer,
            "synthetic-only",
            conv["id"],
            str(uuid4()),
            {
                "expected_conversation_version": conv["version"],
                "parameters": CASES[-1] | {"attachment_ids": [str(aid)]},
            },
        )
        other = await svc.conversations.create(
            buyer,
            str(uuid4()),
            {"title": "[TEST ONLY] submitted proof fixture", "entry_context": {"kind": "GENERAL"}},
        )
        submitted_action = await actions.prepare(
            buyer,
            "synthetic-only",
            other["id"],
            str(uuid4()),
            {
                "expected_conversation_version": other["version"],
                "parameters": CASES[-1] | {"attachment_ids": [str(aid)]},
            },
        )
        # Explicit isolated PG fixture for an already submitted synthetic return.
        async with svc.db.transaction() as conn:
            await execute(
                conn, "UPDATE ag_action SET state='SUCCEEDED' WHERE id=:id", id=submitted_action["id"]
            )
            submitted = (
                await one(
                    conn,
                    "UPDATE ag_attachment_ref SET return_apply_id=999 WHERE action_id=:id RETURNING id",
                    id=submitted_action["id"],
                )
            )["id"]
        sent = await svc.conversations.send(
            buyer,
            "synthetic-only",
            conv["id"],
            str(uuid4()),
            {
                "client_message_id": str(uuid4()),
                "text": "这张凭证先不提交，修改申请说明",
                "attachment_ids": [],
            },
        )
        async with svc.db.transaction() as conn:
            old = await one(conn, "SELECT * FROM ag_attachment_ref WHERE action_id=:id", id=first["id"])
            protected = await one(conn, "SELECT * FROM ag_attachment_ref WHERE id=:id", id=submitted)
            assert not old["protected"] and old["released_at"] is not None
            assert protected["protected"] and protected["released_at"] is None
            assert (await one(conn, "SELECT state FROM ag_attachment WHERE id=:id", id=aid))[
                "state"
            ] == "READY"
        await svc.runs.stop(
            buyer, sent["run"]["id"], str(uuid4()), {"expected_version": sent["run"]["version"]}
        )
    finally:
        await svc.close()


@pytest.mark.anyio
async def test_new_member_message_does_not_cancel_unknown_execution_or_create_another_dispatch(
    integration_config,
):
    cfg = settings(integration_config)
    svc = Services(cfg)
    buyer = Principal(1900000000 + uuid4().int % 100000000, "pending execution revision")
    bridge = Bridge(buyer)
    actions = svc.extensions.actions.require()
    actions.bridge = bridge
    try:
        conv = await svc.conversations.create(
            buyer,
            str(uuid4()),
            {"title": "[TEST ONLY] unknown context", "entry_context": {"kind": "GENERAL"}},
        )
        prepared = await actions.prepare(
            buyer,
            "synthetic-only",
            conv["id"],
            str(uuid4()),
            {"expected_conversation_version": conv["version"], "parameters": CASES[0]},
        )
        bridge.mode = "lost_response"
        result = await actions.confirm(
            buyer, "synthetic-only", prepared["id"], str(uuid4()), {"expected_version": prepared["version"]}
        )
        assert result["state"] == "UNKNOWN"
        sent = await svc.conversations.send(
            buyer,
            "synthetic-only",
            conv["id"],
            str(uuid4()),
            {"client_message_id": str(uuid4()), "text": "修改数量，先核对刚才结果", "attachment_ids": []},
        )
        assert (await actions.get(buyer, prepared["id"]))["state"] == "UNKNOWN"
        async with svc.db.transaction() as conn:
            await execute(
                conn,
                "UPDATE ag_action_dispatch SET next_attempt_at=now(),lease_until=NULL WHERE action_id=:id",
                id=prepared["id"],
            )
        await actions.reconcile_due()
        assert (await actions.get(buyer, prepared["id"]))["state"] == "SUCCEEDED"
        assert len(bridge.calls) == 1
        await svc.runs.stop(
            buyer, sent["run"]["id"], str(uuid4()), {"expected_version": sent["run"]["version"]}
        )
    finally:
        await svc.close()
