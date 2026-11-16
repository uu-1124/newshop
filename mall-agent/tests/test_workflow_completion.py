import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.agents.knowledge_answer import answer_knowledge
from app.agents.live_workflow import build_live_workflow
from app.agents.workflow import WorkflowContext, build_workflow_graph
from app.application.extensions import Extensions
from app.bootstrap import Services
from app.domain.identity import DomainError, Principal
from app.infrastructure.dialog_provider import DialogEvent
from app.infrastructure.postgres.database import execute, one
from app.settings import Settings


@pytest.mark.anyio
async def test_workflow_supplement_is_bounded_and_keeps_fences_and_step_budget():
    calls = []

    async def guard(state):
        calls.append(("guard", state.get("steps_used", 0)))

    def node(name):
        async def invoke(state):
            calls.append((name, state.get("review_attempts", 0)))
            if name == "N03":
                return {"intent": "ORDER", "needs_requery": False}
            if name == "N11" and not state.get("review_attempts"):
                return {"needs_requery": True}
            return {}

        return invoke

    context = WorkflowContext(guard, {f"N{n:02}": node(f"N{n:02}") for n in range(1, 13)})
    result = await build_workflow_graph().ainvoke({"question": "订单"}, context=context)
    assert [name for name, _ in calls if name != "guard"] == [
        "N01",
        "N02",
        "N03",
        "N07",
        "N10",
        "N11",
        "N03",
        "N07",
        "N10",
        "N11",
        "N12",
    ]
    assert result["review_attempts"] == 1 and result["steps_used"] == 11
    assert [step for name, step in calls if name == "guard"] == list(range(11))

    async def always_requery(state):
        return {"needs_requery": True}

    context.nodes["N11"] = always_requery
    with pytest.raises(DomainError, match="WORKFLOW_REQUERY_EXHAUSTED"):
        await build_workflow_graph().ainvoke({"question": "订单"}, context=context)


def source(version):
    return {
        "source": {
            "document_id": "document",
            "version_id": version,
            "chunk_id": "chunk-" + version,
            "title": "演示规则",
            "section": "说明",
            "available": True,
        },
        "content_sha256": "a" * 64,
        "excerpt": version + "演示内容",
    }


class ChangingRetrieval:
    def __init__(self, invalidate_at):
        self.reads = 0
        self.checks = 0
        self.invalidate_at = invalidate_at

    async def retrieve(self, question):
        self.reads += 1
        return [source("v" + str(self.reads))]

    async def recheck(self, refs):
        self.checks += 1
        if self.checks in self.invalidate_at:
            raise DomainError("SOURCE_UNAVAILABLE", "来源变化", 410)


class KnowledgeProvider:
    def __init__(self):
        self.calls = 0

    async def stream_turn(self, inputs, prompt, tools, record, schema=None):
        self.calls += 1
        if schema.get("properties", {}).get("intent"):
            yield DialogEvent("text", '{"intent":"KNOWLEDGE"}')
        else:
            payload = json.loads(inputs[-1]["content"])
            yield DialogEvent(
                "text", json.dumps({"answer": payload["sources"][0]["excerpt"], "source_numbers": [1]})
            )


@pytest.mark.anyio
async def test_source_withdrawn_during_answer_is_retrieved_once_and_only_current_answer_survives():
    retrieval = ChangingRetrieval({1})
    provider = KnowledgeProvider()
    result = await answer_knowledge(provider, retrieval, "规则", None)
    assert retrieval.reads == provider.calls == 2
    assert "v2" in result["answer"] and "v1" not in result["answer"]
    assert result["source_refs"][0]["version_id"] == "v2"


@pytest.mark.anyio
async def test_source_changed_between_specialist_and_n11_reenters_once_without_reclassifying():
    retrieval = ChangingRetrieval({2})
    provider = KnowledgeProvider()
    extensions = Extensions()
    extensions.retrieval.implementation = retrieval
    persisted = []

    async def nothing(*args):
        return {}

    async def save(answer, refs):
        persisted.append((answer, refs))

    graph, context = build_live_workflow(provider, None, SimpleNamespace(max_model_rounds=3))
    result = await graph.ainvoke(
        {"question": "规则", "steps_used": 0},
        context=context(
            guard=nothing,
            assemble=nothing,
            search=nothing,
            emit=nothing,
            record=nothing,
            route_record=nothing,
            extensions=extensions,
            persist_knowledge=save,
        ),
    )
    assert retrieval.reads == 2 and provider.calls == 3  # one route, two knowledge answers
    assert result["review_attempts"] == 1 and len(persisted) == 1
    assert "v2" in persisted[0][0] and "v1" not in persisted[0][0]


@pytest.mark.anyio
async def test_source_continuously_changed_never_persists_a_stale_policy():
    retrieval = ChangingRetrieval(set(range(1, 10)))
    result = await answer_knowledge(KnowledgeProvider(), retrieval, "规则", None)
    assert retrieval.reads == 2 and not result["source_refs"]
    assert "无法核实" in result["answer"] and "v1" not in result["answer"]


@pytest.mark.anyio
async def test_early_member_memory_is_bounded_current_epoch_expiry_filtered_and_never_uses_staff_notes(
    integration_config,
):
    cfg = integration_config
    settings = Settings(
        _env_file=None,
        database_url=cfg["databaseUrl"],
        bridge_secret=cfg["bridgeSecret"],
        broker_secret=cfg["brokerSecret"],
        cursor_secret=cfg["cursorSecret"],
    )
    svc = Services(settings)
    member = Principal(600000000 + uuid4().int % 100000000, "memory test")
    try:
        conversation = await svc.conversations.create(
            member, str(uuid4()), {"title": "memory test", "entry_context": {"kind": "GENERAL"}}
        )
        cid, trigger = conversation["id"], uuid4()
        async with svc.db.transaction() as conn:
            await execute(
                conn,
                "INSERT INTO ag_staff_profile(admin_id,display_name) VALUES (:actor,'memory isolation staff') ON CONFLICT DO NOTHING",
                actor=member.id + 1,
            )
            for seq in range(1, 31):
                text = "早期偏好黑色预算2000，手机号13800000000" if seq == 1 else f"用户需求{seq}"
                await execute(
                    conn,
                    """INSERT INTO ag_message(id,conversation_id,seq,sender_kind,sender_id,status,text_content,retention_until)
                    VALUES (:id,:cid,:seq,'MEMBER',:member,'COMPLETED',:text,:expiry)""",
                    id=trigger if seq == 30 else uuid4(),
                    cid=cid,
                    seq=seq,
                    member=member.id,
                    text=text,
                    expiry=datetime.now(UTC) + timedelta(days=1 if seq == 1 else 2),
                )
            await execute(
                conn,
                """INSERT INTO ag_staff_note(id,conversation_id,admin_id,client_note_id,body,retention_until)
                VALUES (:id,:cid,:actor,:client,'STAFF_PRIVATE_SHOULD_NEVER_ENTER_MEMORY',now()+interval '2 days')""",
                id=uuid4(),
                client=uuid4(),
                actor=member.id + 1,
                cid=cid,
            )
            await execute(conn, "UPDATE ag_conversation SET next_message_seq=31 WHERE id=:cid", cid=cid)
        claimed = {
            "conversation_id": cid,
            "member_id": member.id,
            "memory_epoch": 1,
            "trigger_message_id": trigger,
        }
        history = await svc.runs.history(claimed)
        joined = "\n".join(item["content"] for item in history)
        assert "早期偏好黑色预算2000" in joined and "13800000000" not in joined
        assert "STAFF_PRIVATE" not in joined and sum(len(item["content"]) for item in history) <= 12000
        async with svc.db.transaction() as conn:
            stored = await one(conn, "SELECT * FROM ag_context_summary WHERE conversation_id=:cid", cid=cid)
            assert stored["memory_epoch"] == 1 and stored["through_seq"] < 18
            assert stored["retention_until"] < datetime.now(UTC) + timedelta(days=1, seconds=10)
            await execute(
                conn,
                "UPDATE ag_message SET retention_until=now()-interval '1 second' WHERE conversation_id=:cid AND seq IN (1,28)",
                cid=cid,
            )
        joined = "\n".join(item["content"] for item in await svc.runs.history(claimed))
        assert "早期偏好" not in joined and "用户需求28" not in joined
        async with svc.db.transaction() as conn:
            await execute(conn, "UPDATE ag_conversation SET memory_epoch=2 WHERE id=:cid", cid=cid)
        with pytest.raises(DomainError, match="LEASE_LOST"):
            await svc.runs.history(claimed)
        async with svc.db.transaction() as conn:
            await execute(conn, "UPDATE ag_conversation SET deleted_at=now() WHERE id=:cid", cid=cid)
        with pytest.raises(DomainError):
            await svc.runs.history(claimed | {"memory_epoch": 2})
    finally:
        await svc.close()
