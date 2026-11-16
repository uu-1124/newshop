import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_item_choices import params, products

from app.agents.live_workflow import build_live_workflow
from app.agents.preparation_tools import PARAMETERS
from app.application.extensions import Extensions
from app.infrastructure.dialog_provider import DialogEvent


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["ADD_CART", "CREATE_ORDER"])
@pytest.mark.parametrize(
    "question,history,allowed",
    [
        ("帮我把Reno8加入购物车，规格和数量我还没决定", [], False),
        ("商品45规格239，数量还没想好", [], False),
        ("商品45规格239两件", [], False),
        ("商品45规格239一件", [], True),
        ("Reno8紫色128G一台，准备购买", [], True),
        ("刚才那款数量改成一件", [{"role": "user", "content": "Reno8鸢尾紫128G两件"}], True),
        ("准备卡片", [{"role": "assistant", "content": "商品45规格239一件"}], False),
    ],
)
async def test_graph_accepts_explicit_member_items_and_immediately_clarifies_missing_choices(
    kind, question, history, allowed
):
    parameters = params(kind=kind)
    parameters["address_id"] = "20" if kind == "CREATE_ORDER" else parameters.get("address_id")
    parameters = {k: v for k, v in parameters.items() if v is not None}
    question += "。用地址20，不用优惠券和积分"
    steps = [("search_products", {"query": "Reno8"})]
    if kind == "CREATE_ORDER":
        steps.append(
            (
                "get_checkout_options",
                {"source": "DIRECT", "items": parameters["items"], "cart_item_ids": None},
            )
        )
    steps.append(("prepare_business_action", {key: None for key in PARAMETERS} | parameters))
    observed, answers = [], []
    aid = str(uuid4())

    class Provider:
        count = 0

        async def stream_turn(self, inputs, prompt, tools, record, schema=None):
            if schema:
                yield DialogEvent("text", '{"intent":"SHOPPING"}')
                return
            assert self.count < len(steps), "Choice failures should clarify without another generation"
            name, arguments = steps[self.count]
            self.count += 1
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

    async def noop(*args):
        return {}

    async def search(query):
        return {"items": list(products().values())}

    async def runtime(name, arguments):
        observed.append(name)
        if name == "get_checkout_options":
            return {"addresses": [{"id": "20"}], "coupons": [], "available_points": 0}
        return {"id": aid, "state": "PENDING_CONFIRMATION"}

    async def persist(answer, facts):
        answers.append(answer)

    graph, context = build_live_workflow(
        Provider(), None, SimpleNamespace(max_model_rounds=4, actions_enabled=True, orders_enabled=True)
    )
    result = await graph.ainvoke(
        {"question": question, "history": history, "steps_used": 0},
        context=context(
            guard=noop,
            assemble=noop,
            search=search,
            emit=noop,
            record=noop,
            route_record=noop,
            extensions=Extensions(),
            runtime_tool=runtime,
            persist_products=persist,
        ),
    )
    assert ("prepare_business_action" in observed) == allowed
    assert bool(result.get("action_ids")) == allowed
    assert "尚未执行" in answers[0]
    if not allowed:
        assert result["needs_user_input"] and "明确选择" in answers[0]


@pytest.mark.anyio
async def test_paused_member_choice_keeps_clarification_without_unrelated_catalog_fallback():
    saved = []

    class Provider:
        async def stream_turn(self, inputs, prompt, tools, record, schema=None):
            if schema:
                yield DialogEvent("text", '{"intent":"SHOPPING"}')
                return
            yield DialogEvent("text", "请先选择规格和数量，本轮尚未执行业务。")
            yield DialogEvent("completed", [])

    async def noop(*args):
        return {}

    async def forbidden(*args):
        pytest.fail("A paused preparation must not fetch an unrelated catalog sample")

    async def persist(answer, items):
        saved.append((answer, items))

    graph, context = build_live_workflow(
        Provider(), None, SimpleNamespace(max_model_rounds=3, actions_enabled=True, orders_enabled=True)
    )
    result = await graph.ainvoke(
        {"question": "规格和数量还没决定，先问清楚，不要准备确认卡", "steps_used": 0},
        context=context(
            guard=noop,
            assemble=noop,
            search=forbidden,
            emit=noop,
            record=noop,
            route_record=noop,
            extensions=Extensions(),
            runtime_tool=forbidden,
            persist_products=persist,
        ),
    )
    assert result["needs_user_input"] and saved[0][1] == []
