import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from test_actions import Bridge, settings

from app.agents.dialog import DialogContext, build_dialog_graph
from app.agents.live_workflow import build_live_workflow
from app.agents.preparation_tools import PARAMETERS, parse_preparation
from app.api.runtime_tools import explicit_human_request
from app.application.extensions import Extensions
from app.bootstrap import Services
from app.domain.identity import DomainError, Principal
from app.infrastructure.dialog_provider import DialogEvent
from app.infrastructure.postgres.database import execute, one
from app.main import create_app


def model_parameters(parameters):
    return {key: None for key in PARAMETERS} | parameters


def test_preparation_schema_is_typed_and_cannot_smuggle_execution_or_identity():
    parameters = {
        "action_type": "CREATE_ORDER",
        "source": "DIRECT",
        "items": [{"product_id": "1", "sku_id": "2", "quantity": 1}],
        "address_id": "3",
        "coupon_id": None,
        "use_points": 0,
    }
    assert (
        parse_preparation(
            "prepare_business_action", json.dumps(model_parameters(parameters)), ("CREATE_ORDER",)
        )
        == parameters
    )
    for malformed in (
        model_parameters(parameters) | {"member_id": "5"},
        model_parameters(parameters) | {"action_type": "CONFIRM_ORDER"},
        model_parameters(parameters) | {"use_points": True},
    ):
        with pytest.raises(DomainError):
            parse_preparation("prepare_business_action", json.dumps(malformed), ("CREATE_ORDER",))
    with pytest.raises(DomainError):
        parse_preparation(
            "prepare_business_action", json.dumps(model_parameters(parameters)), ("CANCEL_ORDER",)
        )
    for text in (
        "转人工",
        "请帮我转人工客服",
        "我想联系真人客服",
        "能帮我转接人工客服吗",
        "能不能转接一位人工客服，谢谢",
        "我不想继续等待机器人，帮我转人工",
        "订单有问题，帮我转人工",
        "please connect me to a human agent",
    ):
        assert explicit_human_request(text), text
    for text in (
        "不要转人工",
        "解释一下转人工是什么意思",
        "如果我说转人工",
        "资料写着人工客服",
        "请解释人工客服是什么",
        "图片里是转人工三个字",
        "“请帮我转人工客服”",
        "例如请帮我转人工客服",
    ):
        assert not explicit_human_request(text), text


@pytest.mark.anyio
async def test_incomplete_preparation_asks_for_slots_without_business_io_or_unverified_stream():
    saved = []

    class Provider:
        async def stream_turn(self, inputs, prompt, tools, record, schema=None):
            if schema:
                yield DialogEvent("text", '{"intent":"SHOPPING"}')
            else:
                yield DialogEvent(
                    "completed",
                    [
                        {
                            "type": "function_call",
                            "name": "prepare_business_action",
                            "call_id": "missing-slots",
                            "arguments": json.dumps(
                                model_parameters({"action_type": "CREATE_ORDER", "source": "DIRECT"})
                            ),
                        }
                    ],
                )

    async def noop(*args):
        return {}

    async def forbidden(*args):
        pytest.fail("Missing user choices must not trigger business IO or another rendering model call")

    async def persist(answer, products):
        saved.append((answer, products))

    graph, context = build_live_workflow(
        Provider(), None, SimpleNamespace(max_model_rounds=3, actions_enabled=True, orders_enabled=True)
    )
    result = await graph.ainvoke(
        {"question": "帮我下单", "steps_used": 0},
        context=context(
            guard=noop,
            assemble=noop,
            search=forbidden,
            emit=forbidden,
            record=noop,
            route_record=noop,
            extensions=Extensions(),
            runtime_tool=forbidden,
            persist_products=persist,
            live_stream=True,
        ),
    )
    assert result["needs_user_input"] and "收货地址" in saved[0][0] and "尚未执行" in saved[0][0]
    assert saved[0][1] == []


@pytest.mark.anyio
async def test_shopping_model_prepares_a_card_and_cannot_claim_business_was_executed():
    prepared, saved = [], []
    aid = str(uuid4())
    parameters = {"action_type": "ADD_CART", "items": [{"product_id": "1", "sku_id": "2", "quantity": 1}]}

    class Provider:
        count = 0

        async def stream_turn(self, inputs, prompt, tools, record, schema=None):
            if schema:
                yield DialogEvent("text", '{"intent":"SHOPPING"}')
                return
            self.count += 1
            assert not {"confirm_action", "execute_order", "simulate_refund"} & {
                tool["name"] for tool in tools
            }
            if self.count <= 2:
                name = "search_products" if self.count == 1 else "prepare_business_action"
                arguments = {"query": "测试"} if self.count == 1 else model_parameters(parameters)
                yield DialogEvent(
                    "completed",
                    [
                        {
                            "type": "function_call",
                            "name": name,
                            "call_id": str(self.count),
                            "arguments": json.dumps(arguments),
                        }
                    ],
                )
            else:
                yield DialogEvent(
                    "text", "商品已经下单付款。"
                )  # The application must replace this false claim.
                yield DialogEvent("completed", [])

    async def no_op(*args):
        return {}

    async def search(query):
        return {
            "items": [{"id": "1", "sku_options": [{"id": "2", "visible": True, "available_quantity": 3}]}]
        }

    async def prepare(name, arguments):
        prepared.append((name, arguments))
        return {"id": aid, "state": "PENDING_CONFIRMATION"}

    async def persist(answer, products):
        saved.append((answer, products))

    graph, context = build_live_workflow(
        Provider(), None, SimpleNamespace(max_model_rounds=3, actions_enabled=True, orders_enabled=True)
    )
    result = await graph.ainvoke(
        {"question": "加购商品1规格2一件", "steps_used": 0},
        context=context(
            guard=no_op,
            assemble=no_op,
            search=search,
            emit=no_op,
            record=no_op,
            route_record=no_op,
            extensions=Extensions(),
            runtime_tool=prepare,
            persist_products=persist,
        ),
    )
    assert prepared == [("prepare_business_action", parameters)] and result["action_ids"] == [aid]
    assert "尚未执行" in saved[0][0] and "已经下单付款" not in saved[0][0]


@pytest.mark.anyio
async def test_cart_dialog_reads_exact_rows_then_options_then_prepares_without_extra_generation():
    parameters = {
        "action_type": "CREATE_ORDER",
        "source": "CART",
        "cart_item_ids": ["30"],
        "address_id": "10",
        "coupon_id": None,
        "use_points": 0,
    }
    observed = []

    class Provider:
        count = 0

        async def stream_turn(self, inputs, prompt, tools, record):
            self.count += 1
            assert "get_cart" in {tool["name"] for tool in tools}
            assert self.count <= 3, "Prepared cards must not require another model response"
            name, arguments = [
                ("get_cart", {"limit": 20, "after": None}),
                ("get_checkout_options", {"source": "CART", "cart_item_ids": ["30"], "items": None}),
                ("prepare_business_action", model_parameters(parameters)),
            ][self.count - 1]
            yield DialogEvent(
                "completed",
                [
                    {
                        "type": "function_call",
                        "call_id": str(self.count),
                        "name": name,
                        "arguments": json.dumps(arguments),
                    }
                ],
            )

    async def runtime_tool(name, arguments):
        observed.append((name, arguments))
        if name == "get_cart":
            assert arguments == {"limit": 20}
            return {"items": [{"id": "30", "available": True}]}
        if name == "get_checkout_options":
            return {"addresses": [{"id": "10"}], "coupons": [], "available_points": 0}
        assert arguments == parameters
        return {"id": str(uuid4()), "state": "PENDING_CONFIRMATION"}

    async def noop(*args):
        return {}

    async def no_product_fallback(*args):
        pytest.fail("Cart preparation cannot be replaced with unrelated product search")

    result = await build_dialog_graph(Provider(), None, 3).ainvoke(
        {"question": "读取购物车条目30，用地址10，不用优惠券和积分准备订单", "history": []},
        context=DialogContext(
            no_product_fallback, noop, noop, runtime_tool=runtime_tool, preparation_actions=("CREATE_ORDER",)
        ),
    )
    assert [name for name, _ in observed] == ["get_cart", "get_checkout_options", "prepare_business_action"]
    assert "尚未执行" in result["answer"]


@pytest.mark.anyio
async def test_wrong_cart_row_cannot_reach_checkout_io():
    calls = []

    class Provider:
        count = 0

        async def stream_turn(self, inputs, prompt, tools, record):
            self.count += 1
            if self.count == 3:
                assert "BUSINESS_SOURCE_REQUIRED" in inputs[-1]["output"]
                yield DialogEvent("text", "请选择本次购物车中实际返回的条目。")
                yield DialogEvent("completed", [])
                return
            name, arguments = (
                ("get_cart", {"limit": 20, "after": None})
                if self.count == 1
                else ("get_checkout_options", {"source": "CART", "cart_item_ids": ["31"], "items": None})
            )
            yield DialogEvent(
                "completed",
                [
                    {
                        "type": "function_call",
                        "call_id": str(self.count),
                        "name": name,
                        "arguments": json.dumps(arguments),
                    }
                ],
            )

    async def runtime_tool(name, arguments):
        calls.append(name)
        assert name == "get_cart"
        return {"items": [{"id": "30", "available": True}]}

    async def noop(*args):
        return {}

    result = await build_dialog_graph(Provider(), None, 3).ainvoke(
        {"question": "准备购物车订单", "history": []},
        context=DialogContext(
            noop, noop, noop, runtime_tool=runtime_tool, preparation_actions=("CREATE_ORDER",)
        ),
    )
    assert calls == ["get_cart"] and "实际返回" in result["answer"]


@pytest.mark.anyio
async def test_unknown_order_read_is_returned_as_recoverable_tool_data_without_business_write():
    seen = []

    class Provider:
        count = 0

        async def stream_turn(self, inputs, prompt, tools, record):
            self.count += 1
            if self.count == 3:
                yield DialogEvent("text", "没有找到该订单，请在本人订单列表中选择。")
                yield DialogEvent("completed", [])
                return
            if self.count == 2:
                assert "NOT_FOUND" in inputs[-1]["output"]
            query = {"kind": "ORDER", "order_id": "99"} if self.count == 1 else {"kind": "ORDERS"}
            yield DialogEvent(
                "completed",
                [
                    {
                        "type": "function_call",
                        "call_id": str(self.count),
                        "name": "query_my_orders",
                        "arguments": json.dumps(query),
                    }
                ],
            )

    async def query_order(query):
        seen.append(query)
        if query["kind"] == "ORDER":
            raise DomainError("NOT_FOUND", "订单不存在或不属于本人", 404)
        return {"items": []}

    async def noop(*args):
        return {}

    async def no_write(*args):
        pytest.fail("A missing order cannot create a preparation card")

    result = await build_dialog_graph(Provider(), None, 3).ainvoke(
        {"question": "查询订单99", "history": []},
        context=DialogContext(
            noop,
            noop,
            noop,
            order_query=query_order,
            runtime_tool=no_write,
            preparation_actions=("CANCEL_ORDER",),
        ),
    )
    assert [item["kind"] for item in seen] == ["ORDER", "ORDERS"]
    assert "本人订单列表" in result["answer"]


@pytest.mark.anyio
async def test_runtime_cart_read_revalidates_identity_input_and_post_io_fence(integration_config):
    cfg = settings(integration_config).model_copy(update={"orders_enabled": True})
    svc = Services(cfg)
    app = create_app(cfg)
    app.state.services = svc
    buyer = Principal(1400000000 + uuid4().int % 100000000, "cart reader")
    calls = []

    async def identity(token):
        return buyer

    async def cart(token, query):
        calls.append(query.model_dump(exclude_none=True))
        return {"items": [], "next_cursor": None, "has_more": False, "checked_at": "2026-11-13T00:00:00Z"}

    svc.bridge.identity = identity
    svc.bridge.cart = cart
    try:
        rid, cid = await live_run(svc, buyer, "查看购物车", "Bearer SYNTHETIC_ONLY")
        body = {
            "lease_owner": "prepare-worker",
            "fencing_token": 1,
            "tool": "get_cart",
            "arguments": {"limit": 20},
        }
        headers = {"X-Broker-Key": cfg.broker_secret.get_secret_value()}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            path = f"/internal/runtime-auth/{rid}/tools"
            assert (await client.post(path, json=body)).status_code == 404
            assert (await client.post(path, json=body, headers=headers)).status_code == 200
            assert calls == [{"limit": 20}]
            assert (
                await client.post(path, json=body | {"arguments": {"member_id": "99"}}, headers=headers)
            ).status_code == 422
            assert (
                await client.post(path, json=body | {"lease_owner": "other"}, headers=headers)
            ).status_code == 409

            async def takeover(token, query):
                async with svc.db.transaction() as conn:
                    await execute(
                        conn, "UPDATE ag_conversation SET control_epoch=control_epoch+1 WHERE id=:id", id=cid
                    )
                return await cart(token, query)

            svc.bridge.cart = takeover
            result = await client.post(path, json=body, headers=headers)
            assert result.status_code == 409 and result.json()["code"] == "LEASE_LOST"
            async with svc.db.transaction() as conn:
                assert (
                    await one(conn, "SELECT count(*) n FROM ag_action WHERE conversation_id=:id", id=cid)
                )["n"] == 0
    finally:
        await svc.close()


@pytest.mark.anyio
async def test_broker_checks_member_checkout_choices_from_visible_messages_before_quoting(integration_config):
    from test_orders import OrderBridge

    cfg = settings(integration_config).model_copy(update={"orders_enabled": True})
    svc = Services(cfg)
    app = create_app(cfg)
    app.state.services = svc
    buyer = Principal(1600000000 + uuid4().int % 100000000, "checkout chooser")
    bridge = OrderBridge(buyer)
    svc.extensions.actions.require().bridge = bridge
    quote_calls = []
    original_quote = bridge.quote

    async def identity(token):
        return buyer

    async def options(principal, token, body, key):
        return {
            "addresses": [
                {"id": "10", "label": "演示地址甲", "is_default": True},
                {"id": "11", "label": "演示地址乙", "is_default": False},
            ],
            "coupons": [
                {"id": "20", "name": "演示满减券", "available": True},
                {"id": "21", "name": "演示优惠券乙", "available": True},
            ],
            "available_points": 200,
        }

    async def quote(token, parameters, key):
        quote_calls.append(parameters)
        return await original_quote(token, parameters, key)

    async def cart(token, query):
        return {
            "items": [
                {
                    "id": "30",
                    "product_id": "1",
                    "sku_id": "2",
                    "name": "测试购物车",
                    "specification": "[]",
                    "quantity": 1,
                    "available": True,
                    "available_quantity": 10,
                    "unit_price": "2.00",
                }
            ],
            "has_more": False,
            "next_cursor": None,
            "checked_at": "2026-11-14T00:00:00Z",
        }

    svc.bridge.identity = identity
    svc.bridge.cart = cart
    svc.extensions.actions.require().checkout_options = options
    bridge.quote = quote
    parameters = {
        "action_type": "CREATE_ORDER",
        "source": "CART",
        "cart_item_ids": ["30"],
        "address_id": "10",
        "coupon_id": None,
        "use_points": 0,
    }
    try:
        headers = {"X-Broker-Key": cfg.broker_secret.get_secret_value()}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for question, expected, changes in (
                ("帮我下单", 409, {}),
                ("用地址10，不要演示满减券，不用积分", 409, {"coupon_id": "20"}),
                ("用地址10，不用券，不要用100积分", 409, {"use_points": 100}),
                ("不用地址10，改用地址11，不用券和积分", 409, {}),
                ("购物车条目30，用演示地址甲，不用优惠券和积分准备订单", 200, {}),
                ("不用地址10，改用地址11，不用券和积分", 200, {"address_id": "11"}),
                ("用地址10，不用券，积分从100改成200", 200, {"use_points": 200}),
                ("用地址10，不用优惠券20，改用优惠券21，不用积分", 200, {"coupon_id": "21"}),
            ):
                rid, cid = await live_run(
                    svc, buyer, "选择购物车条目30。" + question, "Bearer SYNTHETIC_ONLY"
                )
                body = {
                    "lease_owner": "prepare-worker",
                    "fencing_token": 1,
                    "tool": "prepare_business_action",
                    "arguments": parameters | changes,
                }
                prior_calls = len(quote_calls)
                response = await client.post(
                    f"/internal/runtime-auth/{rid}/tools", json=body, headers=headers
                )
                assert response.status_code == expected, response.text
                if expected == 409:
                    assert response.json()["code"] == "MEMBER_CHOICE_REQUIRED"
                    assert len(quote_calls) == prior_calls
                else:
                    assert response.json()["data"]["state"] == "PENDING_CONFIRMATION"
            assert len(quote_calls) == 4 and not bridge.calls
    finally:
        await svc.close()


async def live_run(svc, principal, question, token):
    conversation = await svc.conversations.create(
        principal, str(uuid4()), {"title": "[TEST ONLY] prepare tool", "entry_context": {"kind": "GENERAL"}}
    )
    message = await svc.conversations.send(
        principal,
        token,
        conversation["id"],
        str(uuid4()),
        {"client_message_id": str(uuid4()), "text": question, "attachment_ids": []},
    )
    rid = message["run"]["id"]
    async with svc.db.transaction() as conn:
        await execute(
            conn,
            "UPDATE ag_run SET state='RUNNING',lease_owner='prepare-worker',lease_until=now()+interval '5 minutes' WHERE id=CAST(:id AS uuid)",
            id=rid,
        )
    return UUID(rid), UUID(conversation["id"])


@pytest.mark.anyio
async def test_runtime_broker_prepares_idempotently_and_fences_before_and_after_java_io(integration_config):
    cfg = settings(integration_config).model_copy(update={"orders_enabled": True})
    svc = Services(cfg)
    app = create_app(cfg)
    app.state.services = svc
    buyer = Principal(900000000 + uuid4().int % 100000000, "prepare buyer")
    bridge = Bridge(buyer)
    svc.extensions.actions.require().bridge = bridge
    quotes = []
    original = bridge.quote

    async def identity(token):
        return buyer

    async def product(token, pid):
        return {
            "id": pid,
            "name": "测试商品",
            "sku_options": [{"id": "2", "visible": True, "available_quantity": 10, "attributes": []}],
        }

    svc.bridge.identity = identity
    svc.bridge.product = product
    try:
        rid, cid = await live_run(svc, buyer, "商品1规格2一件，准备加购", "Bearer SYNTHETIC_ONLY")
        headers = {"X-Broker-Key": cfg.broker_secret.get_secret_value()}
        body = {
            "lease_owner": "prepare-worker",
            "fencing_token": 1,
            "tool": "prepare_business_action",
            "arguments": {
                "action_type": "ADD_CART",
                "items": [{"product_id": "1", "sku_id": "2", "quantity": 1}],
            },
        }

        async def quote(token, parameters, key):
            quotes.append(key)
            return await original(token, parameters, key)

        bridge.quote = quote
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            path = f"/internal/runtime-auth/{rid}/tools"
            assert (await client.post(path, json=body)).status_code == 404
            assert (
                await client.post(path, headers=headers, json=body | {"lease_owner": "stale-worker"})
            ).status_code == 409
            first = await client.post(path, headers=headers, json=body)
            assert first.status_code == 200, first.text
            replay = await client.post(path, headers=headers, json=body)
            assert replay.status_code == 200 and replay.json()["data"]["id"] == first.json()["data"]["id"]
            assert len(quotes) == 1 and not bridge.calls
            assert first.json()["data"]["state"] == "PENDING_CONFIRMATION"
            denied = await client.post(path, headers=headers, json=body | {"tool": "confirm_action"})
            assert denied.status_code == 422
            injected = body | {"arguments": body["arguments"] | {"member_id": "123"}}
            assert (await client.post(path, headers=headers, json=injected)).status_code == 422

            async def takeover(token, pid):
                async with svc.db.transaction() as conn:
                    await execute(
                        conn, "UPDATE ag_conversation SET control_epoch=control_epoch+1 WHERE id=:id", id=cid
                    )
                return await product(token, pid)

            svc.bridge.product = takeover
            changed = body | {
                "arguments": {
                    "action_type": "ADD_CART",
                    "items": [{"product_id": "1", "sku_id": "2", "quantity": 2}],
                }
            }
            stopped = await client.post(path, headers=headers, json=changed)
            assert stopped.status_code == 409 and stopped.json()["code"] == "LEASE_LOST"
            async with svc.db.transaction() as conn:
                assert (
                    await one(conn, "SELECT count(*) n FROM ag_action WHERE conversation_id=:cid", cid=cid)
                )["n"] == 1
    finally:
        await svc.close()


@pytest.mark.anyio
async def test_broker_rechecks_member_item_choices_from_visible_owned_history_before_quote(
    integration_config,
):
    import copy

    from test_item_choices import products
    from test_orders import OrderBridge

    cfg = settings(integration_config).model_copy(update={"orders_enabled": True})
    svc = Services(cfg)
    app = create_app(cfg)
    app.state.services = svc
    buyer = Principal(1700000000 + uuid4().int % 100000000, "item chooser")
    bridge = OrderBridge(buyer)
    svc.extensions.actions.require().bridge = bridge
    quote_calls, product_calls = [], []
    original_quote = bridge.quote

    async def identity(token):
        return buyer

    async def product(token, pid):
        assert token == "Bearer SYNTHETIC_ONLY"
        product_calls.append(pid)
        return copy.deepcopy(products()[pid])

    async def options(principal, token, body, key):
        return {"addresses": [{"id": "10", "label": "演示地址"}], "coupons": [], "available_points": 0}

    async def quote(token, parameters, key):
        quote_calls.append(parameters)
        return await original_quote(token, parameters, key)

    svc.bridge.identity, svc.bridge.product = identity, product
    svc.extensions.actions.require().checkout_options = options
    bridge.quote = quote
    try:
        headers = {"X-Broker-Key": cfg.broker_secret.get_secret_value()}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for kind in ("ADD_CART", "CREATE_ORDER"):
                parameters = {
                    "action_type": kind,
                    "items": [{"product_id": "45", "sku_id": "239", "quantity": 1}],
                }
                if kind == "CREATE_ORDER":
                    parameters |= {"source": "DIRECT", "address_id": "10", "coupon_id": None, "use_points": 0}
                for question, expected in (
                    ("商品45，规格和数量还没决定", 409),
                    ("商品45规格239，不要一件", 409),
                    ("商品45规格239，数量2", 409),
                    ("商品46规格250一件", 409),
                    ("“商品45规格239数量1”", 409),
                    ("Reno8鸢尾紫128G一台", 200),
                ):
                    question += "。用地址10，不用优惠券和积分"
                    rid, cid = await live_run(svc, buyer, question, "Bearer SYNTHETIC_ONLY")
                    body = {
                        "lease_owner": "prepare-worker",
                        "fencing_token": 1,
                        "tool": "prepare_business_action",
                        "arguments": parameters,
                    }
                    before = len(quote_calls)
                    response = await client.post(
                        f"/internal/runtime-auth/{rid}/tools", json=body, headers=headers
                    )
                    assert response.status_code == expected
                    if expected == 409:
                        assert response.json()["code"] == "MEMBER_CHOICE_REQUIRED"
                        assert len(quote_calls) == before
                        async with svc.db.transaction() as conn:
                            assert (
                                await one(
                                    conn, "SELECT count(*) n FROM ag_action WHERE conversation_id=:id", id=cid
                                )
                            )["n"] == 0
                    else:
                        assert response.json()["data"]["state"] == "PENDING_CONFIRMATION"
                        replay = await client.post(
                            f"/internal/runtime-auth/{rid}/tools", json=body, headers=headers
                        )
                        assert replay.status_code == 200
                        assert len(quote_calls) == before + 1
            assert len(quote_calls) == 2 and len(product_calls) == 12 and not bridge.calls
            # The broker must not accept an old/expired selection or a model-supplied
            # replacement for a more recent member question.
            for latest, expiry, expected in (
                ("刚才那款改成256G，两件", False, 200),
                ("规格和数量还没决定", False, 409),
                ("刚才那款改成256G，两件", True, 409),
            ):
                prior_rid, cid = await live_run(
                    svc, buyer, "我要Reno8鸢尾紫128G一台", "Bearer SYNTHETIC_ONLY"
                )
                async with svc.db.transaction() as conn:
                    await execute(
                        conn,
                        "UPDATE ag_run SET state='COMPLETED',finished_at=now() WHERE id=:id",
                        id=prior_rid,
                    )
                    if expiry:
                        await execute(
                            conn,
                            "UPDATE ag_message SET retention_until=now()-interval '1 second' WHERE conversation_id=:id",
                            id=cid,
                        )
                message = await svc.conversations.send(
                    buyer,
                    "Bearer SYNTHETIC_ONLY",
                    cid,
                    str(uuid4()),
                    {"client_message_id": str(uuid4()), "text": latest, "attachment_ids": []},
                )
                rid = UUID(message["run"]["id"])
                async with svc.db.transaction() as conn:
                    await execute(
                        conn,
                        "UPDATE ag_run SET state='RUNNING',lease_owner='prepare-worker',lease_until=now()+interval '5 minutes' WHERE id=:id",
                        id=rid,
                    )
                before = len(quote_calls)
                body = {
                    "lease_owner": "prepare-worker",
                    "fencing_token": 1,
                    "tool": "prepare_business_action",
                    "arguments": {
                        "action_type": "ADD_CART",
                        "items": [{"product_id": "45", "sku_id": "240", "quantity": 2}],
                    },
                }
                response = await client.post(
                    f"/internal/runtime-auth/{rid}/tools", json=body, headers=headers
                )
                assert response.status_code == expected
                assert len(quote_calls) == before + (expected == 200)

            # A mismatched authoritative read is a protocol failure, never a choice.
            async def wrong_product(token, pid):
                return copy.deepcopy(products()["46"])

            svc.bridge.product = wrong_product
            rid, _ = await live_run(svc, buyer, "商品45规格239一件", "Bearer SYNTHETIC_ONLY")
            response = await client.post(
                f"/internal/runtime-auth/{rid}/tools",
                headers=headers,
                json={
                    "lease_owner": "prepare-worker",
                    "fencing_token": 1,
                    "tool": "prepare_business_action",
                    "arguments": {
                        "action_type": "ADD_CART",
                        "items": [{"product_id": "45", "sku_id": "239", "quantity": 1}],
                    },
                },
            )
            assert response.status_code == 503 and response.json()["code"] == "JAVA_PROTOCOL_ERROR"
            assert len(quote_calls) == 3 and not bridge.calls
    finally:
        await svc.close()


@pytest.mark.anyio
async def test_human_tool_requires_actual_explicit_member_message_and_stops_ai(integration_config):
    cfg = settings(integration_config)
    svc = Services(cfg)
    app = create_app(cfg)
    app.state.services = svc
    buyer = Principal(1000000000 + uuid4().int % 100000000, "handoff buyer")

    async def identity(token):
        return buyer

    svc.bridge.identity = identity
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for question, expected in (("请解释转人工是什么意思", 409), ("请帮我转人工", 200)):
                rid, cid = await live_run(svc, buyer, question, "Bearer SYNTHETIC_ONLY")
                result = await client.post(
                    f"/internal/runtime-auth/{rid}/tools",
                    headers={"X-Broker-Key": cfg.broker_secret.get_secret_value()},
                    json={
                        "lease_owner": "prepare-worker",
                        "fencing_token": 1,
                        "tool": "request_human",
                        "arguments": {},
                    },
                )
                assert result.status_code == expected, result.text
                snapshot = await svc.conversations.snapshot(buyer, cid)
                assert (snapshot["conversation"]["state"] == "WAITING_HUMAN") == (expected == 200)
                if expected == 200:
                    assert not snapshot["active_run"] and not snapshot["conversation"]["waiting_ai_enabled"]
    finally:
        await svc.close()
