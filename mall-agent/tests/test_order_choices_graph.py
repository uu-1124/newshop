"""Actual dialog graph must not turn a model-selected order into a member choice."""

import json
from types import SimpleNamespace

import pytest
from test_preparation_tools import model_parameters

from app.agents.dialog import DialogContext, build_dialog_graph
from app.agents.live_workflow import build_live_workflow
from app.application.extensions import Extensions
from app.domain.identity import DomainError
from app.infrastructure.dialog_provider import DialogEvent


@pytest.mark.anyio
@pytest.mark.parametrize(
    "question,expected",
    [
        (
            "刚才那笔不再选，改为另一笔未付款订单。先读取本人未付款订单列表和目标详情，再准备新的取消确认卡。只准备，不点击确认，不执行取消或付款。",
            "41",
        ),
        ("查看本人订单41的详情就好，不要准备卡片。", None),
        ("解释“准备取消确认卡”是什么意思。", None),
        ("这两笔我还没决定取消哪笔，先澄清，不要准备新卡，不执行任何取消。", None),
    ],
)
async def test_explicit_cancel_card_is_prepared_after_model_stops_at_read_only_facts(question, expected):
    seen = []
    orders = [
        {"id": "40", "order_sn": "DEMO40", "status": 0, "lines": []},
        {"id": "41", "order_sn": "DEMO41", "status": 0, "lines": []},
    ]

    class Provider:
        step = 0

        async def stream_turn(self, inputs, prompt, tools, record):
            self.step += 1
            if self.step <= 2:
                query = (
                    {"kind": "ORDERS", "status": 0} if self.step == 1 else {"kind": "ORDER", "order_id": "41"}
                )
                yield DialogEvent(
                    "completed",
                    [
                        {
                            "type": "function_call",
                            "name": "query_my_orders",
                            "call_id": str(self.step),
                            "arguments": json.dumps(query),
                        }
                    ],
                )
            else:
                assert self.step == 3, "A deterministic preparation must not loop back to the model"
                yield DialogEvent("text", "这笔订单是待付款状态。")
                yield DialogEvent("completed", [])

    async def noop(*args):
        return {}

    async def query(body):
        return {"items": orders, "has_more": False} if body["kind"] == "ORDERS" else orders[1]

    async def prepare(name, body):
        seen.append((name, body))
        return {"id": "dfe60969-63f0-47d8-ae5a-eaf784e926f1", "state": "PENDING_CONFIRMATION"}

    result = await build_dialog_graph(Provider(), None, 3).ainvoke(
        {
            "question": question,
            "history": [{"role": "user", "content": "准备取消订单编号40的确认卡，只准备不执行。"}],
        },
        context=DialogContext(
            noop, noop, noop, order_query=query, runtime_tool=prepare, preparation_actions=("CANCEL_ORDER",)
        ),
    )
    assert [body["order_id"] for _, body in seen] == ([expected] if expected else [])
    if expected:
        assert result["tool_results"][-1]["name"] == "prepare_business_action"


@pytest.mark.anyio
async def test_reading_a_pending_order_does_not_choose_it_when_member_is_undecided():
    observed = []
    steps = [
        ("query_my_orders", {"kind": "ORDER", "order_id": "40"}),
        ("prepare_business_action", model_parameters({"action_type": "CANCEL_ORDER", "order_id": "40"})),
    ]

    class Provider:
        count = 0

        async def stream_turn(self, inputs, prompt, tools, record):
            assert self.count < len(steps), "A missing choice should clarify without another model loop"
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

    async def order(query):
        return {"id": "40", "order_sn": "DEMO40", "status": 0, "lines": []}

    async def runtime(name, arguments):
        observed.append(name)
        return {"id": "dfe60969-63f0-47d8-ae5a-eaf784e926f1", "state": "PENDING_CONFIRMATION"}

    result = await build_dialog_graph(Provider(), None, 2).ainvoke(
        {"question": "先读取本人订单，我还没决定取消哪一笔。不要替我选择订单。", "history": []},
        context=DialogContext(
            noop, noop, noop, order_query=order, runtime_tool=runtime, preparation_actions=("CANCEL_ORDER",)
        ),
    )
    assert not observed
    assert result["needs_user_input"] and "订单" in result["answer"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "question,clarify",
    [
        ("这两笔我还没决定取消哪笔，先澄清，不要准备新卡，不执行任何取消。", True),
        ("订单目标还没决定了，撤回这次选择，先澄清，不要准备新卡。", True),
        ("刚才的订单先不要取消了，撤回本次选择，先不办理。不要准备取消卡。", True),
        ("取消刚才这笔，先准备卡不执行；如果目标不明确请先问我。", True),
        ("订单40当前是否已付款，请查实际状态", False),
        ("解释“不要准备取消卡”是什么意思", False),
    ],
)
async def test_order_node_accepts_member_clarification_without_inventing_order_facts(question, clarify):
    saved = []

    class Provider:
        async def stream_turn(self, inputs, prompt, tools, record, schema=None):
            if schema:
                yield DialogEvent("text", '{"intent":"ORDER"}')
            else:
                yield DialogEvent("text", "订单已取消，库存已恢复")
                yield DialogEvent("completed", [])

    async def noop(*args):
        return {}

    async def forbidden(*args):
        pytest.fail("A no-tool clarification must never perform business IO")

    async def persist(answer, orders):
        saved.append((answer, orders))

    graph, context = build_live_workflow(
        Provider(), None, SimpleNamespace(orders_enabled=True, max_model_rounds=2)
    )

    async def invoke():
        return await graph.ainvoke(
            {"question": question, "history": [], "steps_used": 0},
            context=context(
                guard=noop,
                assemble=noop,
                search=forbidden,
                emit=forbidden,
                record=noop,
                route_record=noop,
                extensions=Extensions(),
                order_query=forbidden,
                runtime_tool=forbidden,
                persist_orders=persist,
            ),
        )

    if clarify:
        result = await invoke()
        assert result["needs_user_input"] and result["orders"] == []
        assert saved and "没有执行" in saved[0][0] and "已取消" not in saved[0][0]
    else:
        with pytest.raises(DomainError, match="ORDER_SOURCE_REQUIRED"):
            await invoke()
        assert not saved
