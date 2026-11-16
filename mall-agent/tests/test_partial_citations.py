from uuid import UUID, uuid4

import pytest
from test_knowledge import create, profile, publish, ready, setup
from test_knowledge import staff as knowledge_staff_fixture

from app.application.conversations import Conversations
from app.application.runs import Runs
from app.domain.identity import DomainError, Principal
from app.infrastructure.cursors import Cursors
from app.infrastructure.postgres.database import execute
from app.infrastructure.runtime_auth import RuntimeAuth

staff = knowledge_staff_fixture


@pytest.mark.anyio
@pytest.mark.parametrize("terminal", ["STOPPED", "FAILED", "INTERRUPTED"])
async def test_visible_policy_partial_keeps_citation_access_after_stop_failure_or_disconnect(
    integration_config, staff, terminal
):
    settings, db, kb, retrieval = setup(integration_config)
    member = Principal(1800000000 + uuid4().int % 100000000, "partial citation buyer")
    conversations = Conversations(db, RuntimeAuth(180), Cursors(integration_config["cursorSecret"]))
    runs = Runs(db, settings)
    try:
        await profile(db, kb)
        doc = await create(kb, staff)
        version = await ready(kb, staff, doc)
        await publish(kb, staff, doc, version)
        candidates = await retrieval.retrieve("测试资料")
        candidate = next(c for c in candidates if c["source"]["document_id"] == doc["id"])
        ref = candidate["source"] | {"content_sha256": candidate["content_sha256"]}
        conv = await conversations.create(
            member,
            str(uuid4()),
            {"title": "[TEST ONLY] partial citation", "entry_context": {"kind": "GENERAL"}},
        )
        sent = await conversations.send(
            member,
            "synthetic-only",
            conv["id"],
            str(uuid4()),
            {"client_message_id": str(uuid4()), "text": "查询资料", "attachment_ids": []},
        )
        claimed = await runs.claim("partial-citation-worker")
        assert str(claimed["id"]) == sent["run"]["id"]
        await runs.bind_stream_sources(claimed, [ref])
        assert await runs.append(claimed, "资料中的已核验部分。\n")
        if terminal == "STOPPED":
            current = await runs.get(member, claimed["id"])
            await runs.stop(member, claimed["id"], uuid4(), {"expected_version": current["version"]})
            await runs.finish(claimed)
        elif terminal == "FAILED":
            await runs.finish(claimed, "MODEL_STREAM_INTERRUPTED")
        else:
            async with db.transaction() as conn:
                await execute(
                    conn,
                    "UPDATE ag_run SET lease_until=now()-interval '1 second' WHERE id=:id",
                    id=claimed["id"],
                )
            await runs.finish(claimed)
        snap = await conversations.snapshot(member, conv["id"])
        message = next(m for m in snap["recent_messages"] if m["run_id"] == sent["run"]["id"])
        assert message["status"] == terminal and message["text"] == "资料中的已核验部分。\n"
        assert message["blocks"][0]["type"] == "source"
        assert message["blocks"][0]["source"]["version_id"] == version["id"]
        source = await retrieval.read_citation(member, UUID(version["id"]), UUID(ref["chunk_id"]))
        assert source["source"]["available"] and "隔离测试资料" in source["excerpt"]
        with pytest.raises(DomainError, match="SOURCE_UNAVAILABLE"):
            await retrieval.read_citation(
                Principal(member.id + 1, "other"), UUID(version["id"]), UUID(ref["chunk_id"])
            )
        async with db.transaction() as conn:
            await execute(
                conn, "UPDATE ag_kb_document SET enabled=false WHERE id=CAST(:id AS uuid)", id=doc["id"]
            )
        with pytest.raises(DomainError, match="SOURCE_UNAVAILABLE"):
            await retrieval.read_citation(member, UUID(version["id"]), UUID(ref["chunk_id"]))
        withdrawn = await conversations.snapshot(member, conv["id"])
        partial = next(m for m in withdrawn["recent_messages"] if m["run_id"] == sent["run"]["id"])
        assert partial["blocks"][0]["source"]["available"] is False
    finally:
        await db.engine.dispose()


@pytest.mark.anyio
async def test_source_withdrawal_between_binding_and_delta_cannot_commit_text(integration_config, staff):
    settings, db, kb, retrieval = setup(integration_config)
    member = Principal(1900000000 + uuid4().int % 100000000, "withdrawal buyer")
    conversations = Conversations(db, RuntimeAuth(180), Cursors(integration_config["cursorSecret"]))
    runs = Runs(db, settings)
    claimed = None
    try:
        await profile(db, kb)
        doc = await create(kb, staff)
        version = await ready(kb, staff, doc)
        await publish(kb, staff, doc, version)
        candidate = next(
            c for c in await retrieval.retrieve("测试资料") if c["source"]["document_id"] == doc["id"]
        )
        ref = candidate["source"] | {"content_sha256": candidate["content_sha256"]}
        conv = await conversations.create(
            member, str(uuid4()), {"title": "[TEST ONLY] source race", "entry_context": {"kind": "GENERAL"}}
        )
        await conversations.send(
            member,
            "synthetic-only",
            conv["id"],
            str(uuid4()),
            {"client_message_id": str(uuid4()), "text": "查询资料", "attachment_ids": []},
        )
        claimed = await runs.claim("source-race-worker")
        await runs.bind_stream_sources(claimed, [ref])
        async with db.transaction() as conn:
            await execute(
                conn, "UPDATE ag_kb_document SET enabled=false WHERE id=CAST(:id AS uuid)", id=doc["id"]
            )
        with pytest.raises(DomainError, match="SOURCE_UNAVAILABLE"):
            await runs.append(claimed, "不得显示的旧资料。", [ref])
        snapshot = await conversations.snapshot(member, conv["id"])
        assistant = next(m for m in snapshot["recent_messages"] if m["sender_kind"] == "ASSISTANT")
        assert not assistant["text"]
    finally:
        if claimed:
            await runs.finish(claimed, "SOURCE_UNAVAILABLE")
        await db.engine.dispose()
