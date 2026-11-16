import io
from uuid import UUID, uuid4

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from test_knowledge import create, process, profile, publish, ready, setup
from test_knowledge import staff as staff

from app.domain.capabilities import JobType
from app.domain.identity import DomainError
from app.infrastructure.knowledge_parse import extract
from app.infrastructure.postgres.database import execute, one, rows


def two_page_pdf():
    writer = PdfWriter()
    for text in ("Page one policy: Alpha.", "Page two policy: Beta."):
        page = writer.add_blank_page(600, 800)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 50 750 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_pdf_parser_preserves_physical_page_ranges():
    result = extract(two_page_pdf(), "pdf")
    assert result["page_count"] == 2
    assert [page["page"] for page in result["page_map"]] == [1, 2]
    assert [result["text"][page["start"] : page["end"]] for page in result["page_map"]] == [
        "Page one policy: Alpha.",
        "Page two policy: Beta.",
    ]
    assert extract(b"Plain text", "txt")["page_map"] == []


@pytest.mark.anyio
async def test_rerank_failure_uses_only_authorized_high_score_vector_candidates(integration_config, staff):
    _, db, kb, retrieval = setup(integration_config)
    try:
        await profile(db, kb)
        doc = await create(kb, staff)
        doc = await publish(kb, staff, doc, await ready(kb, staff, doc))

        async def fail(query, documents, top_n):
            raise DomainError("MODEL_UNAVAILABLE", "reranker unavailable", 503)

        retrieval.models.rerank = fail
        hits = await retrieval.retrieve("测试")
        assert hits and all(hit["retrieval_mode"] == "VECTOR_FALLBACK" for hit in hits)
        assert all(hit["source"]["document_id"] == doc["id"] for hit in hits)
        await kb.enable(
            staff,
            UUID(doc["id"]),
            str(uuid4()),
            {"enabled": False, "expected_version": doc["version"]},
        )
        assert await retrieval.retrieve("测试") == []
    finally:
        await db.engine.dispose()


@pytest.mark.anyio
async def test_pdf_page_edit_preserves_citation_page_and_rejects_fabricated_page_order(
    integration_config, staff
):
    _, db, kb, retrieval = setup(integration_config)
    try:
        await profile(db, kb)
        document = await create(kb, staff)
        uploaded = await kb.files.upload(staff, str(uuid4()), "KNOWLEDGE", "page-test.pdf", two_page_pdf())
        v = await kb.create_version(
            staff,
            UUID(document["id"]),
            str(uuid4()),
            {"source_attachment_id": uploaded["id"], "change_note": "page test"},
        )
        v = await process(kb, staff, v, JobType.KB_PARSE)
        preview = await kb.preview(staff, UUID(v["id"]))
        assert preview["pages"][1]["page"] == 2
        with pytest.raises(DomainError) as wrong:
            await kb.edit_preview(
                staff,
                UUID(v["id"]),
                str(uuid4()),
                {"expected_version": v["version"], "parsed_text": preview["parsed_text"] + "changed"},
            )
        assert wrong.value.code == "PAGE_EDIT_REQUIRED"
        corrected = [{"page": 1, "text": "Corrected first page."}, preview["pages"][1]]
        v = await kb.edit_preview(
            staff,
            UUID(v["id"]),
            str(uuid4()),
            {
                "expected_version": v["version"],
                "parsed_text": "\n\n".join(p["text"] for p in corrected),
                "pages": corrected,
            },
        )
        v = await process(kb, staff, v, JobType.KB_INDEX)
        document = await publish(kb, staff, document, v)
        hits = await retrieval.retrieve("policy")
        assert {hit["source"]["page_start"] for hit in hits} == {1, 2}
        assert all(hit["source"]["page_start"] == hit["source"]["page_end"] for hit in hits)
        async with db.transaction() as conn:
            pages = await rows(
                conn,
                "SELECT content,page_start,page_end FROM ag_kb_chunk WHERE version_id=:id AND chunk_kind='CHILD' ORDER BY page_start",
                id=v["id"],
            )
            assert pages[0]["content"] == "Corrected first page." and pages[0]["page_start"] == 1
            assert "Beta" in pages[1]["content"] and pages[1]["page_start"] == 2
    finally:
        async with db.transaction() as conn:
            await execute(
                conn,
                "UPDATE ag_kb_document SET enabled=false WHERE created_by=:actor AND category='TEST_ONLY'",
                actor=staff.id,
            )
        await db.engine.dispose()


@pytest.mark.anyio
async def test_retired_version_rollback_switches_current_source_and_checks_scope(integration_config, staff):
    _, db, kb, retrieval = setup(integration_config)
    try:
        await profile(db, kb)
        doc = await create(kb, staff)
        first = await ready(kb, staff, doc)
        doc = await publish(kb, staff, doc, first)
        second = await ready(kb, staff, doc)
        doc = await publish(kb, staff, doc, second)
        assert (await kb.get_version(staff, UUID(first["id"])))["state"] == "RETIRED"
        assert {hit["source"]["version_id"] for hit in await retrieval.retrieve("测试")} == {second["id"]}
        doc = await publish(kb, staff, doc, first)
        assert doc["active_version_id"] == first["id"]
        assert {hit["source"]["version_id"] for hit in await retrieval.retrieve("测试")} == {first["id"]}
        assert (await kb.get_version(staff, UUID(second["id"])))["state"] == "RETIRED"
        async with db.transaction() as conn:
            await execute(
                conn,
                "UPDATE ag_kb_document SET scope=CAST(:scope AS jsonb),version=version+1 WHERE id=:id",
                scope='{"product_ids":["99"],"category_ids":[]}',
                id=doc["id"],
            )
        doc = await kb.get_document(staff, UUID(doc["id"]))
        with pytest.raises(DomainError) as invalid:
            await publish(kb, staff, doc, second)
        assert invalid.value.code == "KNOWLEDGE_NOT_READY"
    finally:
        async with db.transaction() as conn:
            await execute(
                conn,
                "UPDATE ag_kb_document SET enabled=false WHERE created_by=:actor AND category='TEST_ONLY'",
                actor=staff.id,
            )
        await db.engine.dispose()


@pytest.mark.anyio
async def test_product_and_category_scopes_require_matching_trusted_context(integration_config, staff):
    _, db, kb, retrieval = setup(integration_config)
    try:
        await profile(db, kb)
        documents = {}
        for name, scope in [
            ("global", None),
            ("product", {"product_ids": ["101"], "category_ids": []}),
            ("category", {"product_ids": [], "category_ids": ["10"]}),
        ]:
            doc = await create(kb, staff, scope=scope)
            v = await ready(kb, staff, doc)
            documents[name] = await publish(kb, staff, doc, v)

        def ids(hits):
            return {hit["source"]["document_id"] for hit in hits}

        assert ids(await retrieval.retrieve("规则")) == {documents["global"]["id"]}
        matched = await retrieval.retrieve("规则", context={"product_id": "101", "category_id": "10"})
        assert ids(matched) == {doc["id"] for doc in documents.values()}
        other = await retrieval.retrieve("规则", context={"product_id": "102", "category_id": "11"})
        assert ids(other) == {documents["global"]["id"]}
    finally:
        async with db.transaction() as conn:
            await execute(
                conn,
                "UPDATE ag_kb_document SET enabled=false WHERE created_by=:actor AND category='TEST_ONLY'",
                actor=staff.id,
            )
        await db.engine.dispose()


@pytest.mark.anyio
async def test_orphan_index_cleanup_keeps_current_and_rollback_versions(integration_config, staff):
    from app.workers.jobs import execute_job

    _, db, kb, _ = setup(integration_config)
    try:
        await profile(db, kb)
        doc = await create(kb, staff)
        first = await ready(kb, staff, doc)
        doc = await publish(kb, staff, doc, first)
        second = await ready(kb, staff, doc)
        doc = await publish(kb, staff, doc, second)
        async with db.transaction() as conn:
            current = await one(conn, "SELECT * FROM ag_kb_version WHERE id=:id", id=second["id"])
            prior = await one(conn, "SELECT * FROM ag_kb_version WHERE id=:id", id=first["id"])
            original = await one(conn, "SELECT * FROM ag_job WHERE id=:id", id=current["index_job_id"])
            await execute(
                conn,
                "UPDATE ag_job SET updated_at=now()-interval '11 minutes' WHERE id=:id",
                id=original["id"],
            )
        orphan = f"agent_kb_{original['id'].hex}_1"
        kb.indexes.items[orphan] = []
        kb.indexes.items["unrelated_external_collection"] = []
        assert await kb.schedule_index_cleanup() == 1
        async with db.transaction() as conn:
            cleanup = await one(conn, "SELECT id FROM ag_job WHERE dedupe_key=:key", key=f"kb-gc:{orphan}")
        result = await execute_job(
            kb.jobs, kb.jobs.handlers, await kb.jobs.claim("index-cleaner", cleanup["id"])
        )
        assert result["state"] == "SUCCEEDED", result
        assert orphan not in kb.indexes.items
        assert (
            current["index_collection"] in kb.indexes.items and prior["index_collection"] in kb.indexes.items
        )
        assert "unrelated_external_collection" in kb.indexes.items
        forged = await kb.jobs.enqueue(
            JobType.KB_INDEX,
            str(uuid4()),
            {"version_id": second["id"], "mode": "GC", "collection": current["index_collection"]},
        )
        result = await execute_job(
            kb.jobs, kb.jobs.handlers, await kb.jobs.claim("guarded-index-cleaner", forged["id"])
        )
        assert result["state"] == "FAILED" and result["error_code"] == "KNOWLEDGE_INDEX_PROTECTED"
        assert current["index_collection"] in kb.indexes.items
    finally:
        async with db.transaction() as conn:
            await execute(
                conn,
                "UPDATE ag_kb_document SET enabled=false WHERE created_by=:actor AND category='TEST_ONLY'",
                actor=staff.id,
            )
        await db.engine.dispose()


@pytest.mark.anyio
async def test_knowledge_index_outage_keeps_publication_and_recovers(integration_config, staff):
    _, db, kb, retrieval = setup(integration_config)
    try:
        await profile(db, kb)
        doc = await create(kb, staff)
        v = await ready(kb, staff, doc)
        doc = await publish(kb, staff, doc, v)
        normal = kb.indexes.search

        async def outage(*args, **kwargs):
            raise DomainError("KNOWLEDGE_INDEX_UNAVAILABLE", "test simulated outage", 503)

        kb.indexes.search = outage
        with pytest.raises(DomainError) as failure:
            await retrieval.retrieve("规则")
        assert failure.value.code == "KNOWLEDGE_INDEX_UNAVAILABLE"
        assert (await kb.get_document(staff, UUID(doc["id"])))["active_version_id"] == v["id"]
        kb.indexes.search = normal
        assert {h["source"]["version_id"] for h in await retrieval.retrieve("规则")} == {v["id"]}
    finally:
        async with db.transaction() as conn:
            await execute(
                conn,
                "UPDATE ag_kb_document SET enabled=false WHERE created_by=:actor AND category='TEST_ONLY'",
                actor=staff.id,
            )
        await db.engine.dispose()


@pytest.mark.anyio
async def test_capacity_overflow_is_explicit_before_model_or_vector_io(integration_config, staff):
    _, db, kb, retrieval = setup(integration_config)
    try:
        await profile(db, kb)
        doc = await create(kb, staff)
        v = await ready(kb, staff, doc)
        doc = await publish(kb, staff, doc, v)
        async with db.transaction() as conn:
            for _ in range(32):
                did, vid = uuid4(), uuid4()
                await execute(
                    conn,
                    """INSERT INTO ag_kb_document(id,title,category,visibility,scope,created_by)
                    VALUES (:id,'capacity synthetic','TEST_ONLY','MEMBER','{}'::jsonb,:actor)""",
                    id=did,
                    actor=staff.id,
                )
                await execute(
                    conn,
                    """INSERT INTO ag_kb_version(id,document_id,version_no,state,source_sha256,created_by,published_at)
                    VALUES (:id,:did,1,'READY',:sha,:actor,now())""",
                    id=vid,
                    did=did,
                    sha="a" * 64,
                    actor=staff.id,
                )
                await execute(
                    conn, "UPDATE ag_kb_document SET active_version_id=:vid WHERE id=:id", vid=vid, id=did
                )
        with pytest.raises(DomainError) as capacity:
            await retrieval.retrieve("synthetic capacity")
        assert capacity.value.code == "KNOWLEDGE_CAPACITY_EXCEEDED"
        async with db.transaction() as conn:
            await execute(
                conn,
                "UPDATE ag_kb_document SET enabled=false WHERE title='capacity synthetic' AND created_by=:actor",
                actor=staff.id,
            )
        assert {hit["source"]["version_id"] for hit in await retrieval.retrieve("规则")} == {v["id"]}
    finally:
        async with db.transaction() as conn:
            await execute(
                conn,
                "UPDATE ag_kb_document SET enabled=false WHERE created_by=:actor AND category='TEST_ONLY'",
                actor=staff.id,
            )
        await db.engine.dispose()
