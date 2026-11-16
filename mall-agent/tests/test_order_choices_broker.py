from uuid import uuid4

import httpx
import pytest
from test_actions import settings
from test_order_choices import orders
from test_order_lifecycle import LifecycleBridge
from test_preparation_tools import live_run

from app.application.orders import cancellation_choice_facts
from app.bootstrap import Services
from app.domain.identity import DomainError, Principal
from app.infrastructure.postgres.database import execute, one
from app.main import create_app


@pytest.mark.anyio
async def test_broker_requeries_owned_targets_and_cannot_choose_from_model_or_expired_history(
    integration_config,
):
    cfg = settings(integration_config).model_copy(update={"orders_enabled": True})
    svc, buyer = Services(cfg), Principal(1800000000 + uuid4().int % 100000000, "order target buyer")
    app = create_app(cfg)
    app.state.services = svc
    bridge = LifecycleBridge(buyer)
    svc.extensions.actions.require().bridge = bridge
    reads, quotes = [], []
    context = {}
    original = bridge.quote

    async def identity(token):
        return buyer

    async def get(path, token, params=None):
        assert token == "Bearer SYNTHETIC_ONLY"
        assert path.startswith("/mall-portal/agent-bridge/v1/orders")
        assert not params or "member_id" not in params
        reads.append((path, params))
        if context.get("fence"):
            async with svc.db.transaction() as conn:
                await execute(
                    conn,
                    "UPDATE ag_conversation SET control_epoch=control_epoch+1 WHERE id=:id",
                    id=context["cid"],
                )
        if path.endswith("/orders"):
            return {"items": list(orders().values()), "has_more": False, "next_cursor": None}
        target = path.rsplit("/", 1)[1]
        if context.get("foreign"):
            raise DomainError("NOT_FOUND", "订单不存在或不属于本人", 404)
        return orders()[target] if not context.get("wrong_id") else orders()["41" if target == "40" else "40"]

    async def quote(token, params, key):
        quotes.append(params)
        if context.get("expire_during_quote"):
            async with svc.db.transaction() as conn:
                await execute(
                    conn,
                    "UPDATE ag_message SET retention_until=now()-interval '1 day' WHERE id=:id",
                    id=context["history_id"],
                )
        result = await original(token, params, key)
        result["cancellation"]["order_sn"] = orders()[params["order_id"]]["order_sn"]
        return result

    svc.bridge.identity, svc.bridge.get = identity, get
    bridge.quote = quote
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for question, target, code, extra in [
                ("还没决定取消哪笔订单", "40", "MEMBER_CHOICE_REQUIRED", {}),
                ("准备取消订单41", "40", "MEMBER_CHOICE_REQUIRED", {}),
                ("准备取消订单40", "40", "JAVA_PROTOCOL_ERROR", {"wrong_id": True}),
                ("准备取消订单40", "40", "NOT_FOUND", {"foreign": True}),
                ("准备取消订单40", "40", "LEASE_LOST", {"fence": True}),
                ("准备取消订单40", "40", "MESSAGE_EXPIRED", {"expire_trigger": True}),
                ("继续准备取消卡", "40", "MEMBER_CHOICE_REQUIRED", {"expired_history": True}),
                (
                    "继续准备取消卡",
                    "40",
                    "MEMBER_CHOICE_REQUIRED",
                    {"expire_during_quote": True},
                ),
                ("继续准备取消卡", "40", "OK", {"valid_history": True}),
                ("准备取消完整订单号202611140100000041", "41", "OK", {}),
                ("准备取消晴空蓝256G那笔订单", "41", "OK", {}),
            ]:
                context.clear()
                context.update(extra)
                rid, cid = await live_run(svc, buyer, question, "Bearer SYNTHETIC_ONLY")
                context["cid"] = cid
                if any(
                    extra.get(flag) for flag in ("expired_history", "expire_during_quote", "valid_history")
                ):
                    context["history_id"] = uuid4()
                    async with svc.db.transaction() as conn:
                        await execute(
                            conn,
                            "UPDATE ag_message SET seq=2 WHERE id=(SELECT trigger_message_id FROM ag_run WHERE id=:rid)",
                            rid=rid,
                        )
                        await execute(
                            conn, "UPDATE ag_conversation SET next_message_seq=3 WHERE id=:cid", cid=cid
                        )
                        await execute(
                            conn,
                            """INSERT INTO ag_message(id,conversation_id,seq,sender_kind,sender_id,
                            sender_display_name,status,text_content,retention_until)
                            VALUES(:id,:cid,1,'MEMBER',:member,'合成会员','COMPLETED','准备取消订单40',
                            now()+make_interval(days=>:days))""",
                            id=context["history_id"],
                            cid=cid,
                            member=buyer.id,
                            days=-1 if extra.get("expired_history") else 1,
                        )
                if extra.get("expire_trigger"):
                    async with svc.db.transaction() as conn:
                        await execute(
                            conn,
                            "UPDATE ag_message SET retention_until=now()-interval '1 day' WHERE run_id=:rid OR id=(SELECT trigger_message_id FROM ag_run WHERE id=:rid)",
                            rid=rid,
                        )
                prior = len(quotes)
                body = {
                    "lease_owner": "prepare-worker",
                    "fencing_token": 1,
                    "tool": "prepare_business_action",
                    "arguments": {"action_type": "CANCEL_ORDER", "order_id": target},
                }
                headers = {"X-Broker-Key": cfg.broker_secret.get_secret_value()}
                path = f"/internal/runtime-auth/{rid}/tools"
                result = await client.post(path, json=body, headers=headers)
                assert result.json()["code"] == code
                if code == "OK":
                    assert result.status_code == 200 and len(quotes) == prior + 1
                    before = len(reads)
                    replay = await client.post(path, json=body, headers=headers)
                    assert replay.json()["data"]["id"] == result.json()["data"]["id"]
                    assert len(reads) == before and len(quotes) == prior + 1
                else:
                    assert len(quotes) == prior + int(extra.get("expire_during_quote", False))
                    assert result.status_code in {409, 404, 410, 503}
                    async with svc.db.transaction() as conn:
                        assert not (
                            await one(
                                conn, "SELECT count(*) n FROM ag_action WHERE conversation_id=:cid", cid=cid
                            )
                        )["n"]
                async with svc.db.transaction() as conn:
                    await execute(
                        conn,
                        "UPDATE ag_run SET state='INTERRUPTED',lease_owner=NULL,lease_until=NULL,finished_at=now() WHERE id=:id",
                        id=rid,
                    )
            assert not bridge.calls
    finally:
        await svc.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "change",
    [
        "wrong_target",
        "missing_number",
        "bad_status",
        "bad_page",
        "duplicate",
        "oversized",
        "bad_amount",
        "bad_line",
        "bad_name",
        "bad_quantity",
        "bad_specification",
    ],
)
async def test_current_order_protocol_errors_do_not_become_member_choice_facts(change):
    target = orders()["40"]
    page = {"items": list(orders().values()), "has_more": False}
    if change == "wrong_target":
        target = orders()["41"]
    elif change == "missing_number":
        target = target | {"order_sn": " "}
    elif change == "bad_status":
        target = target | {"status": False}
    elif change == "bad_page":
        page = {"items": [], "has_more": None}
    elif change == "duplicate":
        page = page | {"items": [orders()["40"], orders()["40"]]}
    elif change == "oversized":
        page = page | {"items": [orders()["40"]] * 21}
    elif change == "bad_amount":
        target = target | {"pay_amount": "NaN"}
    elif change == "bad_line":
        target = target | {"lines": ["not an order line"]}
    else:
        field, value = {
            "bad_name": ("name", 123),
            "bad_quantity": ("quantity", True),
            "bad_specification": ("specification", []),
        }[change]
        target = target | {"lines": [target["lines"][0] | {field: value}]}

    class Bridge:
        async def get(self, path, token, params=None):
            assert token == "owner-token"
            return page if path.endswith("/orders") else target

    with pytest.raises(DomainError, match="JAVA_PROTOCOL_ERROR"):
        await cancellation_choice_facts(Bridge(), "owner-token", "40")
