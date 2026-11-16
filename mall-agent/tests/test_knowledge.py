import hashlib
import io
import zipfile
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.application.conversations import Conversations
from app.application.jobs import JobHandlers, Jobs
from app.application.knowledge import Knowledge
from app.application.retrieval import AuthorizedRetrieval
from app.application.runs import Runs
from app.domain.capabilities import JobLease, JobType
from app.domain.identity import DomainError, Principal
from app.infrastructure.cursors import Cursors
from app.infrastructure.knowledge_files import KnowledgeFiles, parse_source
from app.infrastructure.postgres.database import Database, execute, one
from app.infrastructure.runtime_auth import RuntimeAuth
from app.settings import Settings
from app.workers.jobs import execute_job


class Models:
    async def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]

    async def rerank(self, query, documents, top_n):
        return [{"index": i, "relevance_score": 0.9} for i in range(min(top_n, len(documents)))]


class Indexes:
    def __init__(self):
        self.items = {}
        self.poison = []

    async def drop(self, spec):
        self.items.pop(spec["collection"], None)

    async def collections(self):
        return list(self.items)

    async def drop_unused(self, collection):
        self.items.pop(collection, None)

    async def create(self, spec):
        self.items[spec["collection"]] = []

    async def upsert(self, spec, chunks, vectors):
        self.items[spec["collection"]].extend(chunks)

    async def search(self, spec, vector, version_id, visibility="MEMBER", limit=5):
        return (
            self.poison
            + [
                {
                    "distance": 0.9,
                    "entity": c.model_dump(mode="json")
                    | {"content_sha256": hashlib.sha256(c.text.encode()).hexdigest()},
                }
                for c in self.items[spec["collection"]]
            ][:limit]
        )


class Objects:
    def __init__(self):
        self.values = {}

    def put(self, key, raw, mime):
        self.values[key] = raw

    def read(self, key, max_bytes):
        return self.values[key]


@pytest.fixture
def staff():
    return Principal(
        900000201,
        "knowledge-test",
        "STAFF",
        frozenset(
            {
                "agent:kb:read",
                "agent:kb:edit",
                "agent:kb:publish",
                "agent:attachment:upload",
                "agent:attachment:read",
                "agent:job:read",
                "agent:job:retry",
            }
        ),
    )


def setup(cfg):
    settings = Settings(
        _env_file=None,
        database_url=cfg["databaseUrl"],
        bridge_secret=cfg["bridgeSecret"],
        broker_secret=cfg["brokerSecret"],
        cursor_secret=cfg["cursorSecret"],
    )
    db = Database(cfg["databaseUrl"])
    registry = JobHandlers()
    jobs = Jobs(db, settings, registry)
    files = KnowledgeFiles(db, Objects())
    indexes = Indexes()
    models = Models()
    profile = {"id": str(uuid4()), "version": 1, "model": "isolated-fake-embedding"}
    ledger = SimpleNamespace(settings=SimpleNamespace(embedding_dimension=2), profiles={"EMBEDDING": profile})

    async def ledger_setup():
        pass

    ledger.setup = ledger_setup
    # A real model profile FK is needed by built version metadata.
    kb = Knowledge(db, jobs, Cursors(cfg["cursorSecret"]), files, ledger, models, indexes)
    kb.register(registry)
    return settings, db, kb, AuthorizedRetrieval(db, ledger, models, indexes)


async def profile(db, kb):
    p = kb.ledger.profiles["EMBEDDING"]
    async with db.transaction() as c:
        await execute(
            c,
            """INSERT INTO ag_model_profile(id,name,capability,provider,model_name,endpoint_ref,secret_ref)
            VALUES (:id,'isolated-test','EMBEDDING','test','fake','https://invalid.test','test-only')""",
            id=p["id"],
        )
        await execute(
            c,
            """INSERT INTO ag_model_profile_version(profile_id,version_no,name,enabled,is_active,capability,provider,model_name,endpoint_ref,secret_version_ref,public_options,snapshot_sha256,created_by)
            VALUES (:id,1,'isolated-test',true,true,'EMBEDDING','test','fake','https://invalid.test','test-only','{}',:sha,0)""",
            id=p["id"],
            sha="0" * 64,
        )


async def create(kb, staff, visibility="MEMBER", scope=None):
    return await kb.create(
        staff,
        str(uuid4()),
        {
            "title": "隔离测试资料 " + str(uuid4()),
            "category": "TEST_ONLY",
            "visibility": visibility,
            "scope": scope or {"product_ids": [], "category_ids": []},
        },
    )


async def version(kb, staff, doc, text="这是隔离测试资料。测试取件码为 DEMO-6812，不代表任何店铺政策。"):
    return await kb.create_version(
        staff, UUID(doc["id"]), str(uuid4()), {"source_text": text, "change_note": "隔离测试"}
    )


async def process(kb, staff, v, kind):
    job = await kb.enqueue(staff, UUID(v["id"]), str(uuid4()), v["version"], kind)
    claim = await kb.jobs.claim("knowledge-test-worker", UUID(job["id"]))
    result = await execute_job(kb.jobs, kb.jobs.handlers, claim)
    assert result["state"] == "SUCCEEDED", result["error_code"]
    return await kb.get_version(staff, UUID(v["id"]))


async def ready(kb, staff, doc):
    v = await version(kb, staff, doc)
    v = await process(kb, staff, v, JobType.KB_PARSE)
    return await process(kb, staff, v, JobType.KB_INDEX)


async def publish(kb, staff, doc, v):
    return await kb.publish(
        staff,
        UUID(doc["id"]),
        str(uuid4()),
        {"expected_document_version": doc["version"], "version_id": v["id"]},
    )


@pytest.mark.anyio
async def test_knowledge_lifecycle_authority_citations_and_revocation(integration_config, staff):
    settings, db, kb, retrieval = setup(integration_config)
    try:
        await profile(db, kb)
        doc = await create(kb, staff)
        v = await version(kb, staff, doc)
        with pytest.raises(DomainError, match="KNOWLEDGE_NOT_READY"):
            await publish(kb, staff, doc, v)
        assert await retrieval.retrieve("取件码") == []
        v = await process(kb, staff, v, JobType.KB_PARSE)
        preview = await kb.preview(staff, UUID(v["id"]))
        assert "DEMO-6812" in preview["parsed_text"]
        v = await kb.edit_preview(
            staff,
            UUID(v["id"]),
            str(uuid4()),
            {"expected_version": v["version"], "parsed_text": preview["parsed_text"] + "\n人工校对通过。"},
        )
        v = await process(kb, staff, v, JobType.KB_INDEX)
        assert await retrieval.retrieve("取件码") == []
        doc = await publish(kb, staff, doc, v)
        hits = await retrieval.retrieve("取件码")
        assert len(hits) == 1
        ref = hits[0]["source"] | {"content_sha256": hits[0]["content_sha256"]}
        member = Principal(900000202, "kb-member")
        other = Principal(900000203, "kb-other")
        conversations = Conversations(db, RuntimeAuth(180), Cursors(integration_config["cursorSecret"]))
        conv = await conversations.create(
            member, uuid4(), {"title": "知识隔离测试", "entry_context": {"kind": "GENERAL"}}
        )
        await conversations.send(
            member,
            "test-only",
            conv["id"],
            uuid4(),
            {"client_message_id": str(uuid4()), "text": "测试取件码", "attachment_ids": []},
        )
        runs = Runs(db, settings)
        claimed = await runs.claim("kb-generation-test")
        assert claimed["conversation_id"] == UUID(conv["id"])
        # Withdrawal after model generation but before emission must leave no text/event/citation.
        doc = await kb.enable(
            staff, UUID(doc["id"]), str(uuid4()), {"expected_version": doc["version"], "enabled": False}
        )
        with pytest.raises(DomainError, match="SOURCE_UNAVAILABLE"):
            await runs.append_knowledge(claimed, "测试答复", [ref])
        async with db.transaction() as c:
            assert (
                await one(c, "SELECT text_content FROM ag_message WHERE id=:id", id=claimed["assistant_id"])
            )["text_content"] == ""
        doc = await kb.enable(
            staff, UUID(doc["id"]), str(uuid4()), {"expected_version": doc["version"], "enabled": True}
        )
        await runs.append_knowledge(claimed, "测试取件码为 DEMO-6812。", [ref])
        await runs.finish(claimed)
        source = await retrieval.read_citation(member, UUID(v["id"]), UUID(ref["chunk_id"]))
        assert "DEMO-6812" in source["excerpt"]
        with pytest.raises(DomainError, match="SOURCE_UNAVAILABLE"):
            await retrieval.read_citation(other, UUID(v["id"]), UUID(ref["chunk_id"]))
        assert all(
            "DEMO-6812" not in h["content"]
            for h in await runs.history(claimed | {"trigger_message_id": claimed["assistant_id"]})
        )
        # New publication invalidates old sources even if old vectors still exist.
        v2 = await ready(kb, staff, doc)
        doc = await publish(kb, staff, doc, v2)
        with pytest.raises(DomainError, match="SOURCE_UNAVAILABLE"):
            await retrieval.read_citation(member, UUID(v["id"]), UUID(ref["chunk_id"]))
        assert all(h["source"]["version_id"] == v2["id"] for h in await retrieval.retrieve("取件码"))
        await kb.enable(
            staff, UUID(doc["id"]), str(uuid4()), {"expected_version": doc["version"], "enabled": False}
        )
    finally:
        async with db.transaction() as c:
            await execute(
                c,
                "UPDATE ag_kb_document SET enabled=false WHERE created_by=900000201 AND category='TEST_ONLY'",
            )
        await db.engine.dispose()


@pytest.mark.anyio
async def test_staff_scoped_draft_and_forged_vector_hits_are_rejected(integration_config, staff):
    _, db, kb, retrieval = setup(integration_config)
    try:
        await profile(db, kb)
        docs = []
        for vis, scope in [("STAFF", None), ("MEMBER", {"product_ids": ["1"], "category_ids": []})]:
            d = await create(kb, staff, vis, scope)
            v = await ready(kb, staff, d)
            docs.append(await publish(kb, staff, d, v))
        assert await retrieval.retrieve("取件码") == []
        d = await create(kb, staff)
        v = await ready(kb, staff, d)
        d = await publish(kb, staff, d, v)
        own = await retrieval.retrieve("取件码")
        assert len(own) == 1
        kb.indexes.poison = [
            {
                "distance": 1.0,
                "entity": {
                    "document_id": docs[0]["id"],
                    "version_id": docs[0]["active_version_id"],
                    "chunk_id": own[0]["source"]["chunk_id"],
                    "text": "内部机密",
                    "content_sha256": "0" * 64,
                },
            }
        ]
        assert len(await retrieval.retrieve("取件码")) == 1
        kb.indexes.items[next(k for k, vv in kb.indexes.items.items() if str(vv[0].version_id) == v["id"])][
            0
        ].text = "篡改的索引文本"
        assert await retrieval.retrieve("取件码") == []
        for doc in docs + [d]:
            await kb.enable(
                staff, UUID(doc["id"]), str(uuid4()), {"expected_version": doc["version"], "enabled": False}
            )
    finally:
        async with db.transaction() as c:
            await execute(
                c,
                "UPDATE ag_kb_document SET enabled=false WHERE created_by=900000201 AND category='TEST_ONLY'",
            )
        await db.engine.dispose()


@pytest.mark.anyio
async def test_parse_failure_retry_ownership_cas_and_stale_fence(integration_config, staff):
    _, db, kb, _ = setup(integration_config)
    try:
        d = await create(kb, staff)
        v = await version(kb, staff, d)
        job = await kb.enqueue(staff, UUID(v["id"]), str(uuid4()), v["version"], JobType.KB_PARSE)
        claim = await kb.jobs.claim("expired-parser", UUID(job["id"]))
        lease = JobLease(
            claim["id"], JobType.KB_PARSE, claim["lease_owner"], claim["fencing_token"], claim["parameters"]
        )
        async with db.transaction() as c:
            await execute(
                c, "UPDATE ag_job SET lease_until=now()-interval '1 second' WHERE id=:id", id=claim["id"]
            )
        with pytest.raises(DomainError, match="LEASE_LOST"):
            await kb.parse(lease)
        await kb.jobs.recover_expired()
        async with db.transaction() as c:
            await execute(c, "UPDATE ag_job SET next_attempt_at=now() WHERE id=:id", id=claim["id"])
        current = await kb.jobs.claim("new-parser", claim["id"])
        assert current["fencing_token"] > claim["fencing_token"]
        assert (await execute_job(kb.jobs, kb.jobs.handlers, current))["state"] == "SUCCEEDED"
        with pytest.raises(DomainError, match="VERSION_CONFLICT"):
            await kb.edit_preview(
                staff,
                UUID(v["id"]),
                str(uuid4()),
                {"expected_version": v["version"], "parsed_text": "旧请求"},
            )
        f = kb.files
        raw = "知识原件，仅用于测试。".encode()
        a = await f.upload(staff, str(uuid4()), "KNOWLEDGE", "test.txt", raw)
        assert (await f.read(staff, UUID(a["id"])))[0] == raw
        other = replace(staff, id=900000299)
        with pytest.raises(DomainError, match="NOT_FOUND"):
            await f.read(other, UUID(a["id"]))
        with pytest.raises(DomainError, match="NOT_FOUND"):
            await kb.create_version(
                other, UUID(d["id"]), str(uuid4()), {"source_attachment_id": a["id"], "change_note": "test"}
            )
        v = await version(kb, staff, d)
        # Invalid UTF-8 source is not possible through DTO; NUL must be rejected by the worker.
        async with db.transaction() as c:
            await execute(c, "UPDATE ag_kb_version SET source_text=E'\\001' WHERE id=:id", id=v["id"])

        # A deterministic terminal failure invokes the domain callback.
        async def bad(lease):
            raise DomainError("KNOWLEDGE_PARSE_FAILED", "测试失败", 422)

        kb.jobs.handlers._handlers[JobType.KB_PARSE] = bad
        j = await kb.enqueue(staff, UUID(v["id"]), str(uuid4()), v["version"], JobType.KB_PARSE)
        c = await kb.jobs.claim("bad-parser", UUID(j["id"]))
        assert (await execute_job(kb.jobs, kb.jobs.handlers, c))["state"] == "FAILED"
        assert (await kb.get_version(staff, UUID(v["id"])))["state"] == "FAILED"
    finally:
        async with db.transaction() as c:
            await execute(
                c,
                "UPDATE ag_kb_document SET enabled=false WHERE created_by=900000201 AND category='TEST_ONLY'",
            )
        await db.engine.dispose()


def test_bounded_file_decoders():
    assert parse_source("文本\r\n正文".encode(), "md")["text"] == "文本\n正文"
    for raw, kind in [(b"%PDF-invalid", "pdf"), (b"\xff", "txt"), (b"\x00", "txt")]:
        with pytest.raises(DomainError, match="KNOWLEDGE_PARSE_FAILED"):
            parse_source(raw, kind)
    docx = io.BytesIO()
    with zipfile.ZipFile(docx, "w") as z:
        z.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>测试段落</w:t></w:r></w:p></w:body></w:document>',
        )
    assert "测试段落" in parse_source(docx.getvalue(), "docx")["text"]


@pytest.mark.anyio
async def test_knowledge_model_refs_are_whitelisted_and_withdrawal_interrupts_answer():
    import json

    from app.agents.knowledge_answer import answer_knowledge
    from app.infrastructure.dialog_provider import DialogEvent

    source = {
        "document_id": str(uuid4()),
        "version_id": str(uuid4()),
        "chunk_id": str(uuid4()),
        "title": "公开测试",
        "section": "正文",
        "page_start": None,
        "page_end": None,
        "available": True,
    }

    class Retrieval:
        withdrawn = False
        empty = False

        async def retrieve(self, query):
            return (
                []
                if self.empty
                else [{"source": source, "content_sha256": "0" * 64, "excerpt": "公开测试配送方案说明"}]
            )

        async def recheck(self, refs):
            if self.withdrawn:
                raise DomainError("SOURCE_UNAVAILABLE", "已撤回", 410)

    class Provider:
        numbers = [1]

        async def stream_turn(self, *args):
            yield DialogEvent(
                "text", json.dumps({"answer": "公开测试配送方案说明", "source_numbers": self.numbers})
            )

    provider, retrieval = Provider(), Retrieval()
    result = await answer_knowledge(provider, retrieval, "配送方案", None)
    assert len(result["source_refs"]) == 1 and result["source_refs"][0]["chunk_id"] == source["chunk_id"]
    provider.numbers = [2]
    with pytest.raises(DomainError, match="MODEL_PROTOCOL_ERROR"):
        await answer_knowledge(provider, retrieval, "配送方案", None)
    provider.numbers = [1]
    retrieval.withdrawn = True
    result = await answer_knowledge(provider, retrieval, "配送方案", None)
    assert not result["source_refs"] and "持续更新" in result["answer"]
    retrieval.empty = True
    assert not (await answer_knowledge(provider, retrieval, "配送方案", None))["source_refs"]
