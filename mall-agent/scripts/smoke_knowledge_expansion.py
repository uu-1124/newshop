"""Live scoped PDF/rollback and bounded fault-injection acceptance; labelled fixtures only."""

import io
import json
import secrets
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from expansion_client import ADMIN, MEMBER, api, login, wait_job, wait_run
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.bootstrap import Services
from app.domain.identity import DomainError
from app.infrastructure.knowledge_runtime import KnowledgeRuntime
from app.infrastructure.model_ledger import ModelLedger
from app.infrastructure.postgres.database import encoded, execute, one
from app.runtime import run
from app.settings import Settings
from app.workers.jobs import execute_job

ROOT = Path(__file__).resolve().parents[2]


def pdf(code):
    writer = PdfWriter()
    for text in (
        "TEST ONLY. Synthetic acceptance material, not actual shop policy. Page one introduction.",
        f"TEST ONLY. Page two synthetic shipping plan name is {code}.",
    ):
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
        stream.set_data(f"BT /F1 10 Tf 30 750 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def live_settings():
    cfg = json.loads((ROOT / "deployment/.run/agent-env.json").read_text())
    vector = json.loads((ROOT / "deployment/.run/milvus-env.json").read_text())
    runtime = json.loads((ROOT / "deployment/.run/runtime-env.json").read_text())
    return Settings(
        database_url=cfg["databaseUrl"],
        bridge_secret=cfg["bridgeSecret"],
        broker_secret=cfg["brokerSecret"],
        cursor_secret=cfg["cursorSecret"],
        staff_bridge_secret=runtime["staffBridgeSecret"],
        milvus_database=vector["database"],
        milvus_reader_user=vector["readerUser"],
        milvus_reader_password=vector["readerPassword"],
        milvus_builder_user=vector["builderUser"],
        milvus_builder_password=vector["builderPassword"],
    )


async def main():
    evidence = {"verified_at": datetime.now(UTC).isoformat(), "checks": []}
    code = "AuroraScope" + secrets.token_hex(3)
    output = ROOT / "deployment/.run/knowledge-expansion-smoke.json"
    previous = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {}
    resume = previous.get("test_documents_disabled", []) if previous.get("synthetic_code") else []

    def passed(check):
        evidence["checks"].append(check)
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print("PASS", check, flush=True)

    svc = Services(live_settings())
    await svc.models.setup()
    docs = []
    conversations = []
    async with (
        httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=60, trust_env=False) as staff,
        httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=45, trust_env=False) as member,
    ):
        await login(staff, True)
        await login(member, username="agent-conversation-bd63b203")

        async def task(version, kind):
            job = await api(
                staff,
                ADMIN + f"/kb/versions/{version['id']}/{kind}",
                "POST",
                {"expected_version": version["version"]},
                expected=202,
            )
            await wait_job(staff, job["id"], 300)
            return await api(staff, ADMIN + f"/kb/versions/{version['id']}")

        async def create(title, scope):
            document = await api(
                staff,
                ADMIN + "/kb/documents",
                "POST",
                {
                    "title": "[TEST ONLY] " + title,
                    "category": "TEST_ONLY_EXPANSION",
                    "visibility": "MEMBER",
                    "scope": scope,
                },
                expected=201,
            )
            docs.append(document["id"])
            return document

        async def ask(cid, text):
            key = str(uuid4())
            await api(
                member,
                MEMBER + f"/conversations/{cid}/messages",
                "POST",
                {"client_message_id": key, "text": text, "attachment_ids": []},
                expected=202,
            )
            return await wait_run(member, cid, 210)

        async def publish(document, version):
            current = await api(staff, ADMIN + f"/kb/documents/{document['id']}")
            return await api(
                staff,
                ADMIN + f"/kb/documents/{document['id']}/publish",
                "POST",
                {"expected_document_version": current["version"], "version_id": version["id"]},
            )

        try:
            product = await api(member, MEMBER + "/products/45")
            category = product["category_id"]
            context = {"product_id": "45", "category_id": category}
            if resume:
                docs.extend(resume[:2])
                document = await api(staff, ADMIN + "/kb/documents/" + docs[0])
                category_doc = await api(staff, ADMIN + "/kb/documents/" + docs[1])
                versions = await api(staff, ADMIN + f"/kb/documents/{document['id']}/versions")
                version = next(v for v in versions["items"] if v["id"] == document["active_version_id"])
                code = previous["synthetic_code"]
                for doc in (document, category_doc):
                    if not doc["enabled"]:
                        await api(
                            staff,
                            ADMIN + f"/kb/documents/{doc['id']}/enabled",
                            "PUT",
                            {"expected_version": doc["version"], "enabled": True},
                        )
                passed("real_pdf_upload_page_preview_edit_index_and_publish")
                passed("trusted_product_category_scope_and_general_exclusion")
            else:
                document = await create(
                    "商品范围PDF页码与版本验收", {"product_ids": ["45"], "category_ids": []}
                )
                raw = pdf(code)
                (ROOT / "deployment/.run/knowledge-expansion-two-page.pdf").write_bytes(raw)
                uploaded = await staff.post(
                    ADMIN + "/attachments",
                    headers={"Idempotency-Key": str(uuid4())},
                    data={"purpose": "KNOWLEDGE"},
                    files={"file": ("synthetic-two-page.pdf", raw, "application/pdf")},
                )
                assert uploaded.status_code == 201, uploaded.json().get("code")
                version = await api(
                    staff,
                    ADMIN + f"/kb/documents/{document['id']}/versions",
                    "POST",
                    {
                        "source_attachment_id": uploaded.json()["data"]["id"],
                        "change_note": "[TEST ONLY] PDF physical page boundaries",
                    },
                    expected=201,
                )
                version = await task(version, "parse")
                preview = await api(staff, ADMIN + f"/kb/versions/{version['id']}/preview")
                assert (
                    preview["page_count"] == 2
                    and [p["page"] for p in preview["pages"]] == [1, 2]
                    and code in preview["pages"][1]["text"]
                )
                version = await api(
                    staff,
                    ADMIN + f"/kb/versions/{version['id']}/preview",
                    "PATCH",
                    {
                        "expected_version": version["version"],
                        "parsed_text": preview["parsed_text"],
                        "pages": preview["pages"],
                    },
                )
                version = await task(version, "build")
                document = await publish(document, version)
                passed("real_pdf_upload_page_preview_edit_index_and_publish")
                category_doc = await create(
                    "分类范围合成资料", {"product_ids": [], "category_ids": [category]}
                )
                cv = await api(
                    staff,
                    ADMIN + f"/kb/documents/{category_doc['id']}/versions",
                    "POST",
                    {
                        "source_text": "[TEST ONLY] 分类范围合成配送资料。分类验收标记为 CategoryScopeAcceptance。不是正式店铺政策。",
                        "change_note": "[TEST ONLY] category trust",
                    },
                    expected=201,
                )
                cv = await task(await task(cv, "parse"), "build")
                await publish(category_doc, cv)
                scoped = await svc.knowledge_runtime.retrieval.retrieve(
                    "Page two synthetic shipping plan name", context
                )
                assert any(c["source"]["document_id"] == document["id"] for c in scoped)
                categories = await svc.knowledge_runtime.retrieval.retrieve("分类验收标记", context)
                assert any(c["source"]["document_id"] == category_doc["id"] for c in categories)
                general = await svc.knowledge_runtime.retrieval.retrieve(
                    "Page two synthetic shipping plan name"
                )
                assert all(c["source"]["document_id"] not in docs for c in general)
                passed("trusted_product_category_scope_and_general_exclusion")
            conv = await api(
                member,
                MEMBER + "/conversations",
                "POST",
                {
                    "title": "[TEST ONLY] 可信商品范围与精确引用",
                    "entry_context": {"kind": "PRODUCT", "product_id": "45"},
                },
                expected=201,
            )
            conversations.append(conv["id"])
            message = await ask(
                conv["id"],
                "请查询已发布知识库中的配送方案名称：Page two synthetic shipping plan name。仅提取资料中的合成测试名称，不把它当成正式店铺政策，请给出资料来源。",
            )
            sources = [
                b["source"]
                for b in message["blocks"]
                if b["type"] == "source" and b["source"]["document_id"] == document["id"]
            ]
            evidence["synthetic_answer"] = message["text"]
            evidence["synthetic_sources"] = sources
            evidence["synthetic_code"] = code
            output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
            assert (
                code.lower() in message["text"].lower()
                and sources
                and any(s["page_start"] == 2 and s["page_end"] == 2 for s in sources)
            )
            source = next(s for s in sources if s["page_start"] == 2)
            citation = await api(
                member, MEMBER + f"/citations/{source['version_id']}/chunks/{source['chunk_id']}"
            )
            assert code in citation["excerpt"]
            evidence.update(
                document_id=document["id"],
                first_version_id=version["id"],
                source=source,
                conversation_id=conv["id"],
            )
            passed("real_gpt_answer_with_exact_physical_page_two_citation")
            second_code = code + "V2"
            v2 = await api(
                staff,
                ADMIN + f"/kb/documents/{document['id']}/versions",
                "POST",
                {
                    "source_text": f"TEST ONLY. Updated synthetic shipping plan name is {second_code}. Not actual shop policy.",
                    "change_note": "[TEST ONLY] rollback candidate",
                },
                expected=201,
            )
            v2 = await task(await task(v2, "parse"), "build")
            document = await publish(document, v2)
            assert (
                await member.get(MEMBER + f"/citations/{source['version_id']}/chunks/{source['chunk_id']}")
            ).status_code == 410
            document = await publish(document, version)
            after = await ask(
                conv["id"],
                "请查询当前已发布知识库中的配送方案名称：Page two synthetic shipping plan name。仅提取合成测试名称，不作为正式店铺政策，请引用当前资料。",
            )
            assert code.lower() in after["text"].lower() and second_code.lower() not in after["text"].lower()
            assert any(
                b["type"] == "source"
                and b["source"]["version_id"] == version["id"]
                and b["source"]["page_start"] == 2
                for b in after["blocks"]
            )
            passed("real_publish_replacement_source_revocation_and_rollback")
            # Isolate the simulated unavailable endpoint to this acceptance client, never stop middleware.
            broken_settings = svc.models.base_settings.model_copy(update={"milvus_uri": "http://127.0.0.1:1"})
            broken = KnowledgeRuntime(
                svc.db,
                broken_settings,
                None,
                None,
                svc.objects,
                ModelLedger(svc.db, broken_settings),
            )
            try:
                try:
                    await broken.retrieval.retrieve("shipping plan name", context)
                except DomainError as error:
                    assert error.code == "KNOWLEDGE_INDEX_UNAVAILABLE"
                else:
                    raise AssertionError("Unavailable index should fail closed")
            finally:
                await broken.close()
            assert await svc.knowledge_runtime.retrieval.retrieve("shipping plan name", context)
            passed("real_index_connection_failure_and_recovery_without_global_outage")
            await capacity_and_gc(svc, document, version, context, evidence)
            passed("real_capacity_guard_recovery_and_orphan_index_gc")
        finally:
            for did in docs:
                latest = await api(staff, ADMIN + "/kb/documents/" + did)
                if latest["enabled"]:
                    await api(
                        staff,
                        ADMIN + f"/kb/documents/{did}/enabled",
                        "PUT",
                        {"expected_version": latest["version"], "enabled": False},
                    )
            for cid in conversations:
                snap = await api(member, MEMBER + "/conversations/" + cid)
                await api(
                    member,
                    MEMBER + "/conversations/" + cid,
                    "DELETE",
                    {"expected_version": snap["conversation"]["version"]},
                )
            evidence.update(test_documents_disabled=docs, test_conversations_deleted=conversations)
            output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
            await svc.close()


async def capacity_and_gc(svc, document, version, context, evidence):
    # Metadata-only pressure fixtures fail before any vector IO and are disabled after the check.
    cfg = json.loads((ROOT / "deployment/.run/agent-env.json").read_text())
    import psycopg

    connection = psycopg.connect(cfg["migrationUrl"].replace("postgresql+psycopg://", "postgresql://"))
    fixture_ids = []
    try:
        for _ in range(33):
            did, vid = uuid4(), uuid4()
            fixture_ids.append(did)
            connection.execute(
                "INSERT INTO ag_kb_document(id,title,category,visibility,scope,created_by,enabled) VALUES (%s,'[TEST ONLY] capacity pressure','TEST_ONLY_EXPANSION','MEMBER',%s::jsonb,0,true)",
                (did, encoded({"product_ids": ["45"], "category_ids": []})),
            )
            connection.execute(
                "INSERT INTO ag_kb_version(id,document_id,version_no,source_text,parsed_text,state,published_at,source_sha256,created_by) VALUES (%s,%s,1,'TEST ONLY','TEST ONLY','READY',now(),repeat('0',64),0)",
                (vid, did),
            )
            connection.execute("UPDATE ag_kb_document SET active_version_id=%s WHERE id=%s", (vid, did))
        connection.commit()
        try:
            await svc.knowledge_runtime.retrieval.retrieve("shipping plan name", context)
        except DomainError as error:
            assert error.code == "KNOWLEDGE_CAPACITY_EXCEEDED"
        else:
            raise AssertionError("Capacity guard did not reject pressure")
    finally:
        connection.rollback()
        connection.execute("UPDATE ag_kb_document SET enabled=false WHERE id=ANY(%s)", (fixture_ids,))
        connection.commit()
        connection.close()
    assert await svc.knowledge_runtime.retrieval.retrieve("shipping plan name", context)
    evidence["capacity_fixture_count"] = 33
    jid = uuid4()
    collection = f"agent_kb_{jid.hex}_1"
    async with svc.db.transaction() as conn:
        await execute(
            conn,
            """INSERT INTO ag_job(id,job_type,dedupe_key,parameters,state,attempts,fencing_token,updated_at)
            VALUES (:id,'KB_INDEX',:key,CAST(:params AS jsonb),'FAILED',1,1,now()-interval '11 minutes')""",
            id=jid,
            key=f"test-only-orphan:{jid}",
            params=encoded({"version_id": version["id"]}),
        )
        v = await one(conn, "SELECT * FROM ag_kb_version WHERE id=:id", id=version["id"])
    spec = (v["quality_report"] or {})["index"] | {"collection": collection, "generation": f"{jid.hex}_1"}
    await svc.knowledge_runtime.indexes.create(spec)
    assert collection in await svc.knowledge_runtime.indexes.collections()
    await svc.knowledge_runtime.knowledge.schedule_index_cleanup()
    async with svc.db.transaction() as conn:
        job = await one(conn, "SELECT * FROM ag_job WHERE dedupe_key=:key", key="kb-gc:" + collection)
    if job["state"] != "SUCCEEDED":
        claimed = await svc.jobs.claim("test-only-index-gc", job["id"])
        if claimed:
            await execute_job(svc.jobs, svc.job_handlers, claimed)
    import asyncio

    for _ in range(40):
        if collection not in await svc.knowledge_runtime.indexes.collections():
            break
        await asyncio.sleep(1)
    assert collection not in await svc.knowledge_runtime.indexes.collections()
    assert v["index_collection"] in await svc.knowledge_runtime.indexes.collections()
    evidence.update(
        gc_collection=collection, gc_job_id=str(job["id"]), active_and_rollback_indexes_preserved=True
    )


if __name__ == "__main__":
    run(main())
