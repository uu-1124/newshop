import asyncio
import json
from types import SimpleNamespace

import pytest

from app.agents.answer_stream import SentenceGate, render_answer
from app.agents.live_workflow import build_live_workflow
from app.application.extensions import Extensions
from app.domain.identity import DomainError
from app.infrastructure.dialog_provider import DialogEvent


def test_sentence_gate_checks_complete_sentences_and_split_privacy_or_links_before_emitting():
    gate = SentenceGate("订单金额199.90，数量2，演示退款。", True)
    assert gate.feed("1. 订单金额¥199.90。") == ["1. 订单金额¥199.90。"]
    assert gate.feed("共2件。") == ["共2件。"]
    for chunks in (
        ["价格¥299.90。"],
        ["请访问https", "://bad.example。"],
        ["电话138", "00000000。"],
        ["已为你取消订单。"],
    ):
        validator = SentenceGate("订单金额199.90，数量2。", True)
        with pytest.raises(DomainError):
            for chunk in chunks:
                validator.feed(chunk)
    assert SentenceGate("没有数字", True).feed("暂时没有可用依据。") == ["暂时没有可用依据。"]


@pytest.mark.anyio
async def test_live_stream_emits_while_supplier_is_still_generating_and_rechecks_each_sentence():
    first_visible = asyncio.Event()
    emitted, checks = [], []

    class Provider:
        async def stream_turn(self, inputs, prompt, tools, record):
            assert not tools
            facts = json.loads(inputs[0]["content"])["facts"]
            amount = next(f for f in facts if f["field"].endswith("pay_amount"))
            yield DialogEvent("text", json.dumps({"fact_id": amount["id"]}) + "\n")
            # A full-answer buffering implementation deadlocks here and fails.
            await asyncio.wait_for(first_visible.wait(), 1)
            yield DialogEvent("text", '{"fact_id":"G_DEMO"}\n')
            yield DialogEvent("completed", [])

    async def emit(text):
        emitted.append(text)
        first_visible.set()

    async def guard():
        checks.append("run")

    async def evidence():
        checks.append("source")

    answer = await render_answer(
        Provider(), "查询订单", {"pay_amount": "199.90", "is_simulated": True}, None, emit, guard, evidence
    )
    assert answer == "".join(emitted) and "实付金额为¥199.90" in answer and "不代表真实扣款" in answer
    assert checks == ["run", "source", "run", "source"]


@pytest.mark.anyio
async def test_stop_or_source_change_prevents_any_following_stream_segment():
    emitted = []

    class Provider:
        async def stream_turn(self, *args):
            yield DialogEvent("text", '{"fact_id":"G_UNKNOWN"}\n')
            yield DialogEvent("text", '{"fact_id":"G_HUMAN"}\n')
            yield DialogEvent("completed", [])

    async def emit(text):
        emitted.append(text)

    async def guard():
        if emitted:
            raise DomainError("LEASE_LOST", "已停止", 409)

    async def evidence():
        return None

    with pytest.raises(DomainError, match="LEASE_LOST"):
        await render_answer(Provider(), "查询", {}, None, emit, guard, evidence)
    assert len(emitted) == 1 and "当前资料不足" in emitted[0]


@pytest.mark.anyio
async def test_final_stream_uses_current_facts_and_does_not_replay_initial_draft():
    emitted, persisted = [], []
    query = {"kind": "ORDERS", "limit": 20}
    order = {
        "id": "1",
        "order_sn": "202611130100000001",
        "lines": [],
        "pay_amount": "199.90",
        "is_simulated": True,
    }

    class Provider:
        calls = 0

        async def stream_turn(self, inputs, prompt, tools, record, schema=None):
            self.calls += 1
            if schema:
                yield DialogEvent("text", '{"intent":"ORDER"}')
            elif tools:
                if self.calls == 2:
                    yield DialogEvent(
                        "completed",
                        [
                            {
                                "type": "function_call",
                                "name": "query_my_orders",
                                "call_id": "1",
                                "arguments": json.dumps(query),
                            }
                        ],
                    )
                else:
                    yield DialogEvent("text", "内部草稿不应直接展示。")
                    yield DialogEvent("completed", [])
            else:
                assert "199.90" in inputs[0]["content"]
                facts = json.loads(inputs[0]["content"])["facts"]
                amount = next(f for f in facts if f["field"].endswith("pay_amount"))
                yield DialogEvent("text", json.dumps({"fact_id": amount["id"]}) + "\n")
                assert len(emitted) == 1 and "199.90" in emitted[0]
                yield DialogEvent("text", '{"fact_id":"G_DEMO"}\n')
                yield DialogEvent("completed", [])

    async def noop(*args):
        return {}

    async def query_orders(query):
        return {"items": [order]}

    async def emit(text):
        emitted.append(text)

    async def persist(answer, orders):
        persisted.append((answer, orders))

    graph, context = build_live_workflow(
        Provider(), None, SimpleNamespace(max_model_rounds=3, orders_enabled=True)
    )
    await graph.ainvoke(
        {"question": "订单查询", "steps_used": 0},
        context=context(
            guard=noop,
            assemble=noop,
            search=noop,
            emit=emit,
            record=noop,
            route_record=noop,
            extensions=Extensions(),
            order_query=query_orders,
            persist_orders=persist,
            live_stream=True,
        ),
    )
    assert len(persisted) == 1 and persisted[0] == ("".join(emitted), [order])
    assert "199.90" in persisted[0][0] and "不代表真实扣款" in persisted[0][0]
    assert "内部草稿" not in "".join(emitted)
