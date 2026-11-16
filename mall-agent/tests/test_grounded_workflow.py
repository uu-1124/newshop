import json
from types import SimpleNamespace

import pytest

from app.agents.live_workflow import build_live_workflow
from app.application.extensions import Extensions
from app.domain.identity import DomainError
from app.infrastructure.dialog_provider import DialogEvent


class Retrieval:
    def __init__(self):
        self.checks = 0
        self.reads = 0

    async def retrieve(self, query, context=None):
        self.reads += 1
        return [
            {
                "source": {"document_id": "doc", "version_id": "v1", "chunk_id": "c1", "title": "演示政策"},
                "excerpt": "演示退款不调用真实资金渠道，不表示真实资金到账。",
                "content_sha256": "a" * 64,
            }
        ]

    async def recheck(self, refs):
        self.checks += 1

    async def evidence(self, refs):
        return [
            {
                "title": "演示政策",
                "excerpt": "演示退款不调用真实资金渠道，不表示真实资金到账。",
                "version_id": "v1",
            }
        ]


@pytest.mark.anyio
async def test_guide_can_retrieve_policy_with_product_facts_and_bind_sources_before_first_visible_quote():
    retrieval, extensions = Retrieval(), Extensions()
    extensions.retrieval.implementation = retrieval
    calls, emitted = [], []

    class Provider:
        round = 0

        async def stream_turn(self, inputs, prompt, tools, record, schema=None):
            if schema:
                yield DialogEvent("text", '{"intent":"GUIDE"}')
                return
            if tools:
                assert "retrieve_knowledge" in {tool["name"] for tool in tools}
                self.round += 1
                if self.round <= 2:
                    name, query = (
                        ("retrieve_knowledge", {"query": "演示退款"})
                        if self.round == 1
                        else ("search_products", {"query": "手机"})
                    )
                    yield DialogEvent(
                        "completed",
                        [
                            {
                                "type": "function_call",
                                "call_id": str(self.round),
                                "name": name,
                                "arguments": json.dumps(query),
                            }
                        ],
                    )
                else:
                    yield DialogEvent("text", "错误草稿：终身免费保修。")
                    yield DialogEvent("completed", [])
                return
            facts = json.loads(inputs[0]["content"])["facts"]
            quote = next(f for f in facts if f["source_number"] == 1)
            price = next(f for f in facts if f["field"].endswith("price_from"))
            yield DialogEvent("text", json.dumps({"fact_id": quote["id"]}) + "\n")
            assert calls == ["bind", "emit"]
            yield DialogEvent("text", json.dumps({"fact_id": price["id"]}) + "\n")
            yield DialogEvent("completed", [])

    async def noop(*args):
        return {}

    async def search(query):
        return {
            "items": [{"id": "1", "name": "演示手机", "price_from": "99.90", "sku_options": [], "facts": []}]
        }

    async def emit(text):
        calls.append("emit")
        emitted.append(text)

    async def bind(refs):
        assert refs[0]["document_id"] == "doc"
        calls.append("bind")

    graph, context = build_live_workflow(Provider(), None, SimpleNamespace(max_model_rounds=3))
    result = await graph.ainvoke(
        {"question": "手机价格和演示退款政策", "steps_used": 0},
        context=context(
            guard=noop,
            assemble=noop,
            search=search,
            emit=emit,
            record=noop,
            route_record=noop,
            extensions=extensions,
            bind_sources=bind,
            live_stream=True,
        ),
    )
    assert result["source_refs"][0]["chunk_id"] == "c1"
    assert retrieval.reads == 1 and retrieval.checks >= 3
    assert "终身免费保修" not in "".join(emitted) and "不表示真实资金到账" in "".join(emitted)
    assert "99.90" in "".join(emitted)


@pytest.mark.anyio
async def test_live_knowledge_has_one_routing_call_and_one_selector_with_no_unverified_answer_call():
    retrieval, extensions = Retrieval(), Extensions()
    extensions.retrieval.implementation = retrieval
    observed = []

    class Provider:
        calls = 0

        async def stream_turn(self, inputs, prompt, tools, record, schema=None):
            self.calls += 1
            if schema:
                yield DialogEvent("text", '{"intent":"KNOWLEDGE"}')
                return
            assert not tools and self.calls == 2
            facts = json.loads(inputs[0]["content"])["facts"]
            quote = next(f for f in facts if f["source_number"])
            yield DialogEvent("text", json.dumps({"fact_id": quote["id"]}) + "\n")
            yield DialogEvent("completed", [])

    provider = Provider()

    async def noop(*args):
        return {}

    async def save(answer, refs):
        observed.append((answer, refs))

    graph, context = build_live_workflow(provider, None, SimpleNamespace(max_model_rounds=3))
    await graph.ainvoke(
        {"question": "演示退款", "steps_used": 0},
        context=context(
            guard=noop,
            assemble=noop,
            search=noop,
            emit=noop,
            record=noop,
            route_record=noop,
            extensions=extensions,
            persist_knowledge=save,
            bind_sources=noop,
            live_stream=True,
        ),
    )
    assert provider.calls == 2 and len(observed) == 1
    assert observed[0][1][0]["version_id"] == "v1" and "不表示真实资金到账" in observed[0][0]


@pytest.mark.anyio
async def test_policy_source_change_after_first_fact_keeps_partial_and_blocks_following_fact():
    from app.agents.answer_stream import render_answer

    emitted, bindings = [], []

    class Provider:
        async def stream_turn(self, inputs, *args):
            fact = next(f for f in json.loads(inputs[0]["content"])["facts"] if f["source_number"])
            yield DialogEvent("text", json.dumps({"fact_id": fact["id"]}) + "\n")
            yield DialogEvent("text", '{"fact_id":"G_HUMAN"}\n')
            yield DialogEvent("completed", [])

    async def guard():
        return None

    async def recheck():
        if emitted:
            raise DomainError("SOURCE_UNAVAILABLE", "来源已变化", 410)

    async def emit(text):
        emitted.append(text)

    async def bind(fact):
        bindings.append(fact.source_index)

    with pytest.raises(DomainError, match="SOURCE_UNAVAILABLE"):
        await render_answer(
            Provider(),
            "规则",
            [{"title": "演示规则", "excerpt": "仅为模拟支付。"}],
            None,
            emit,
            guard,
            recheck,
            on_fact=bind,
        )
    assert len(emitted) == 1 and bindings == [0]


@pytest.mark.anyio
async def test_general_router_cannot_bypass_policy_sources_with_numberless_warranty_claim():
    observed = []

    class Provider:
        async def stream_turn(self, inputs, prompt, tools, record, schema=None):
            if schema:
                yield DialogEvent("text", '{"intent":"GENERAL"}')
            else:
                yield DialogEvent("text", "该商品终身免费保修。")
                yield DialogEvent("completed", [])

    async def noop(*args):
        return {}

    async def emit(text):
        observed.append(text)

    graph, context = build_live_workflow(Provider(), None, SimpleNamespace(max_model_rounds=3))
    result = await graph.ainvoke(
        {"question": "保修有什么承诺", "steps_used": 0},
        context=context(
            guard=noop,
            assemble=noop,
            search=noop,
            emit=emit,
            record=noop,
            route_record=noop,
            extensions=Extensions(),
            live_stream=True,
        ),
    )
    assert (
        result["needs_user_input"]
        and "终身" not in "".join(observed)
        and "没有本轮有效资料" in "".join(observed)
    )
