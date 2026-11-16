import json

import httpx
import pytest

from app.agents.dialog import DialogContext, build_dialog_graph
from app.domain.identity import DomainError
from app.infrastructure.aliyun_models import AliyunModels
from app.infrastructure.dialog_provider import DialogEvent, ResponsesDialogProvider
from app.settings import Settings


def config(**extra):
    return Settings(
        database_url="postgresql+psycopg://test:test@localhost/test",
        bridge_secret="test",
        broker_secret="test",
        cursor_secret="test",
        dialog_api_key="private-unit-key",
        **extra,
    )


class Recorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, data):
        self.calls.append(data)
        return "unit-call-id"

    def recorder(self, _):
        return self


class Fragments(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        for start in range(0, len(self.data), 7):
            yield self.data[start : start + 7]


def wire(*events):
    return "".join("data: " + json.dumps(e, ensure_ascii=False) + "\n\n" for e in events).encode()


@pytest.mark.anyio
async def test_responses_fragmented_utf8_hides_reasoning_and_commentary_and_records_usage():
    data = wire(
        {"type": "response.output_item.added", "item": {"id": "reason", "type": "reasoning"}},
        {"type": "response.reasoning_summary_text.delta", "delta": "never-show"},
        {
            "type": "response.output_item.added",
            "item": {"id": "comment", "type": "message", "phase": "commentary"},
        },
        {"type": "response.output_text.delta", "item_id": "comment", "delta": "private-plan"},
        {
            "type": "response.output_item.added",
            "item": {"id": "answer", "type": "message", "phase": "final_answer"},
        },
        {"type": "response.output_text.delta", "item_id": "answer", "delta": "你好😀"},
        {
            "type": "response.completed",
            "response": {
                "id": "response-1",
                "status": "completed",
                "output": [],
                "usage": {"input_tokens": 12, "output_tokens": 3},
            },
        },
    )

    def handler(request):
        assert request.url.path == "/v1/responses"
        assert json.loads(request.content)["model"] == "gpt-6.1-sol"
        return httpx.Response(200, stream=Fragments(data))

    record = Recorder()
    provider = ResponsesDialogProvider(config(), httpx.MockTransport(handler))
    try:
        events = [e async for e in provider.stream_turn([], "system", [], record)]
        assert [e.data for e in events if e.kind == "text"] == ["你好😀"]
        assert record.calls[-1]["status"] == "SUCCEEDED"
        assert record.calls[-1]["usage"]["input_tokens"] == 12
        assert record.calls[-1]["provider_request_id"] == "response-1"
    finally:
        await provider.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "status,code", [(401, "MODEL_AUTH_FAILED"), (403, "MODEL_ACCESS_DENIED"), (429, "MODEL_RATE_LIMITED")]
)
async def test_provider_error_never_exposes_response_or_retries(status, code):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={"error": "private-unit-key must never escape"})

    provider, record = ResponsesDialogProvider(config(), httpx.MockTransport(handler)), Recorder()
    try:
        with pytest.raises(DomainError) as caught:
            _ = [e async for e in provider.stream_turn([], "system", [], record)]
        assert caught.value.code == code
        assert "private-unit-key" not in str(caught.value)
        assert len(requests) == 1
        assert record.calls[-1]["usage"] is None
    finally:
        await provider.close()


@pytest.mark.anyio
async def test_stream_without_completion_cannot_be_marked_successful():
    provider = ResponsesDialogProvider(
        config(),
        httpx.MockTransport(
            lambda _: httpx.Response(
                200, content=wire({"type": "response.created", "response": {"id": "unfinished"}})
            )
        ),
    )
    record = Recorder()
    try:
        with pytest.raises(DomainError) as caught:
            _ = [e async for e in provider.stream_turn([], "system", [], record)]
        assert caught.value.code == "MODEL_STREAM_INTERRUPTED"
        assert record.calls[-1]["status"] == "UNKNOWN"
    finally:
        await provider.close()


@pytest.mark.anyio
async def test_graph_executes_only_completed_validated_tools_and_continues_with_real_result():
    class Provider:
        count = 0

        async def stream_turn(self, inputs, instructions, tools, record):
            self.count += 1
            if self.count == 1:
                yield DialogEvent(
                    "completed",
                    [
                        {
                            "type": "function_call",
                            "call_id": "call1",
                            "name": "search_products",
                            "arguments": '{"query":"手机"}',
                        }
                    ],
                )
            else:
                assert json.loads(inputs[-1]["output"])["items"][0]["id"] == "26"
                assert inputs[-1]["call_id"] == "call1"
                yield DialogEvent("text", "已查到真实商品。")
                yield DialogEvent("completed", [])

    searches, emitted = [], []

    async def search(query):
        searches.append(query)
        return {"items": [{"id": "26", "price_from": "3788.00"}]}

    async def emit(text):
        emitted.append(text)

    graph = build_dialog_graph(Provider(), None)
    result = await graph.ainvoke(
        {"question": "查询手机", "history": [], "answer": "", "tool_results": []},
        context=DialogContext(search, emit, Recorder()),
    )
    assert searches == ["手机"]
    assert result["answer"] == "".join(emitted)
    assert "token" not in json.dumps(result)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "name,args", [("execute_order", "{}"), ("search_products", '{"query":"手机","member_id":2}')]
)
async def test_graph_rejects_unregistered_or_identity_injecting_tool(name, args):
    class Provider:
        async def stream_turn(self, *unused):
            yield DialogEvent(
                "completed", [{"type": "function_call", "call_id": "call1", "name": name, "arguments": args}]
            )

    async def forbidden(*unused):
        pytest.fail("Invalid tool reached executable boundary")

    with pytest.raises(DomainError):
        await build_dialog_graph(Provider(), None).ainvoke(
            {"question": "test", "history": [], "answer": "", "tool_results": []},
            context=DialogContext(forbidden, forbidden, Recorder()),
        )


@pytest.mark.anyio
async def test_aliyun_validates_vector_dimension_and_rerank_candidate_bounds():
    def handler(request):
        if request.url.path.endswith("embeddings"):
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})
        return httpx.Response(200, json={"output": {"results": [{"index": 100, "relevance_score": 0.9}]}})

    adapter = AliyunModels(config(), Recorder(), httpx.MockTransport(handler))
    try:
        with pytest.raises(DomainError) as dimension:
            await adapter.embed(["test"])
        assert adapter.ledger.calls[-1]["status"] == "FAILED"
        assert adapter.ledger.calls[-1]["error_code"] == "MODEL_DIMENSION_MISMATCH"
        assert dimension.value.code == "MODEL_DIMENSION_MISMATCH"
        with pytest.raises(DomainError):
            await adapter.rerank("query", ["a", "b"], 2)
        assert adapter.ledger.calls[-1]["status"] == "FAILED"
        assert adapter.ledger.calls[-1]["error_code"] == "MODEL_PROTOCOL_ERROR"
    finally:
        await adapter.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "capability,payload",
    [
        ("EMBEDDING", {"data": None}),
        ("EMBEDDING", {"data": [{"index": False, "embedding": [0.1, 0.2]}]}),
        ("EMBEDDING", {"data": [{"index": 0, "embedding": [True] + [0.2] * 63}]}),
        ("EMBEDDING", {"data": [{"index": 0, "embedding": ["PRIVATE_PROVIDER_VALUE"] + [0.2] * 63}]}),
        ("RERANK", {"output": []}),
        ("RERANK", {"output": {"results": [{"index": False, "relevance_score": 0.8}]}}),
        ("RERANK", {"output": {"results": [{"index": 0, "relevance_score": True}]}}),
        ("RERANK", {"output": {"results": [{"index": 0, "relevance_score": "PRIVATE_PROVIDER_VALUE"}]}}),
    ],
)
async def test_auxiliary_protocol_failures_are_recorded_as_failures_and_do_not_leak_payload(
    capability, payload
):
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(
            200, json=payload | {"request_id": "SYNTHETIC_PROTOCOL", "usage": {"total_tokens": 2}}
        )

    adapter = AliyunModels(config(embedding_dimension=64), Recorder(), httpx.MockTransport(handler))
    try:
        with pytest.raises(DomainError) as failed:
            if capability == "EMBEDDING":
                await adapter.embed(["test"])
            else:
                await adapter.rerank("query", ["one"], 1)
        assert "PRIVATE_PROVIDER_VALUE" not in str(failed.value)
        assert len(seen) == 1
        completion = adapter.ledger.calls[-1]
        assert completion["status"] == "FAILED" and completion["error_code"] == failed.value.code
        assert (
            completion["usage"] == {"total_tokens": 2}
            and completion["provider_request_id"] == "SYNTHETIC_PROTOCOL"
        )
    finally:
        await adapter.close()


@pytest.mark.anyio
async def test_graph_batches_tiny_deltas_without_losing_text():
    text = "分" * 130

    class Provider:
        async def stream_turn(self, *unused):
            for char in text:
                yield DialogEvent("text", char)
            yield DialogEvent("completed", [])

    emitted = []

    async def emit(chunk):
        emitted.append(chunk)

    async def forbidden(*unused):
        pytest.fail("No tool call was requested")

    result = await build_dialog_graph(Provider(), None).ainvoke(
        {"question": "test", "history": [], "answer": "", "tool_results": []},
        context=DialogContext(forbidden, emit, Recorder()),
    )
    assert "".join(emitted) == result["answer"] == text
    assert len(emitted) == 3
