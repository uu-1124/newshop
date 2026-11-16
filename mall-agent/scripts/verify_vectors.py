"""Synthetic real-model vector smoke, isolated from live knowledge and mall product search."""

import argparse
import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import grpc
import httpx
from pymilvus import MilvusClient, MilvusException

from app.infrastructure.aliyun_models import AliyunModels
from app.infrastructure.milvus_candidates import CandidateChunk, MilvusCandidates
from app.infrastructure.model_ledger import ModelLedger
from app.infrastructure.postgres.database import Database
from app.runtime import run
from app.settings import Settings


async def main(persistence):
    root = Path(__file__).resolve().parents[2]
    run_dir = root / "deployment/.run"
    secrets = json.loads((run_dir / "milvus-env.json").read_text(encoding="utf-8"))
    config = json.loads((run_dir / "agent-env.json").read_text(encoding="utf-8"))
    settings = Settings(
        database_url=config["databaseUrl"],
        bridge_secret=config["bridgeSecret"],
        broker_secret=config["brokerSecret"],
        cursor_secret=config["cursorSecret"],
    )
    logging.getLogger("pymilvus").setLevel(logging.CRITICAL)
    options = {
        "uri": "http://127.0.0.1:19530",
        "db_name": secrets["database"],
        "timeout": 15,
        "grpc_options": {"grpc.enable_http_proxy": 0},
    }
    reader = MilvusClient(**options, user=secrets["readerUser"], password=secrets["readerPassword"])
    evidence_path, data_path = run_dir / "vector-smoke.json", run_dir / "vector-smoke-data.json"
    if persistence:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        data = json.loads(data_path.read_text(encoding="utf-8"))
        index = MilvusCandidates(reader, **data["index"])
        try:
            hits = index.search(data["query_vector"], "PUBLIC", [data["version_id"]])
            assert hits[0]["id"] == data["expected_id"]
            assert {x["id"] for x in hits} == set(data["public_ids"])
            evidence["restart_persistence"] = {
                "result": "passed",
                "verified_at": datetime.now(UTC).isoformat(),
            }
            evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
            print("PASS restart persistence, stored vectors and scope filters")
        finally:
            reader.close()
        return

    builder = MilvusClient(**options, user=secrets["builderUser"], password=secrets["builderPassword"])
    db = Database(settings.database_url.get_secret_value())
    ledger = ModelLedger(db, settings)
    await ledger.setup()
    ali = AliyunModels(settings, ledger)
    evidence = {"time": datetime.now(UTC).isoformat(), "checks": [], "database": secrets["database"]}

    def passed(name, **detail):
        evidence["checks"].append({"name": name, "result": "passed", **detail})
        print("PASS", name)

    try:
        collection = "agent_e1_d1024_probe_" + uuid4().hex[:12]
        version, old_version, document = uuid4(), uuid4(), uuid4()
        texts = [
            "演示规则：商品签收后七日内可以申请退货，需保持商品完好。",
            "演示商品：蓝色无线耳机，包装内含充电盒。",
            "仅限员工的演示退货审核说明，不向买家展示。",
            "已过期的演示规则：签收后三十日内申请退货。",
        ]
        chunks = [
            CandidateChunk(
                chunk_id=uuid4(),
                parent_chunk_id=uuid4(),
                document_id=document,
                version_id=old_version if i == 3 else version,
                visibility="STAFF" if i == 2 else "PUBLIC",
                text=text,
            )
            for i, text in enumerate(texts)
        ]
        vectors = await ali.embed(texts)
        query = "签收后几天内能退货？"
        query_vector = (await ali.embed([query]))[0]
        passed("aliyun_1024_dimensions", dimension=len(query_vector), synthetic_chunks=len(chunks))
        profile = ledger.profiles["EMBEDDING"]
        index_args = {
            "collection": collection,
            "dimension": settings.embedding_dimension,
            "profile_id": profile["id"],
            "profile_version": profile["version"],
            "generation": "probe-1",
        }
        index = MilvusCandidates(builder, **index_args)
        await asyncio.to_thread(index.create)
        await asyncio.to_thread(index.upsert, chunks, vectors)
        await asyncio.to_thread(index.upsert, chunks, vectors)
        passed("isolated_create_and_idempotent_upsert", collection=collection)
        index = MilvusCandidates(reader, **index_args)
        hits = await asyncio.to_thread(index.search, query_vector, "PUBLIC", [version])
        public_ids = [str(chunk.chunk_id) for chunk in chunks[:2]]
        assert {x["id"] for x in hits} == set(public_ids)
        assert hits[0]["id"] == public_ids[0]
        passed("semantic_recall_visibility_and_version_filter", count=len(hits), first_id=hits[0]["id"])
        reranked = await ali.rerank(query, [x["entity"]["text"] for x in hits], len(hits))
        assert hits[reranked[0]["index"]]["id"] == public_ids[0]
        passed("aliyun_rerank_retrieved_candidates", first_id=public_ids[0])

        def denied(name, operation):
            try:
                operation()
            except (MilvusException, grpc.RpcError) as exc:
                assert "privilege" in str(exc).lower() or "permission" in str(exc).lower()
                passed(name)
            else:
                raise AssertionError("Restricted operation unexpectedly allowed")

        denied("reader_cannot_write", lambda: reader.delete(collection, ids=[public_ids[1]], timeout=10))
        denied(
            "builder_cannot_create_database", lambda: builder.create_database("forbidden_probe", timeout=10)
        )
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            response = await client.post("http://127.0.0.1:19530/v2/vectordb/collections/list", json={})
            assert response.status_code == 401
        passed("unauthenticated_access_denied")
        evidence["server_version"] = builder.get_server_version()
        evidence["collection"] = collection
        evidence["result"] = "passed_infrastructure_only"
        data_path.write_text(
            json.dumps(
                {
                    "index": index_args,
                    "query_vector": query_vector,
                    "version_id": str(version),
                    "expected_id": public_ids[0],
                    "public_ids": public_ids,
                }
            ),
            encoding="utf-8",
        )
        evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        builder.close()
        reader.close()
        await ali.close()
        await db.engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-persistence", action="store_true")
    run(main(parser.parse_args().verify_persistence))
