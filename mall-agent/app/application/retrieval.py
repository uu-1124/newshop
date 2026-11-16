"""PG is the authority; vector hits never grant access to a document."""

import asyncio
import hashlib
from uuid import UUID

from app.application.expiry import visible_sql
from app.application.return_knowledge import RULE_CURRENT_SQL
from app.domain.identity import DomainError
from app.infrastructure.postgres.database import encoded, one, rows

GLOBAL_MEMBER = (
    """d.enabled AND d.visibility='MEMBER' AND d.active_version_id=v.id
    AND v.state='READY' AND v.published_at IS NOT NULL
    AND ((COALESCE(d.scope->'product_ids','[]'::jsonb)='[]'::jsonb
    AND COALESCE(d.scope->'category_ids','[]'::jsonb)='[]'::jsonb)
    OR (CAST(:product AS text) IS NOT NULL AND
        (COALESCE(d.scope->'product_ids','[]'::jsonb) @> jsonb_build_array(CAST(:product AS text))
        OR COALESCE(d.scope->'category_ids','[]'::jsonb) @> jsonb_build_array(CAST(:category AS text)))))"""
    + " AND "
    + RULE_CURRENT_SQL
)


def scope_parameters(context):
    return {
        "product": context.get("product_id") if context else None,
        "category": context.get("category_id") if context else None,
    }


def source_view(row):
    return {
        "document_id": str(row["document_id"]),
        "version_id": str(row["version_id"]),
        "chunk_id": str(row["chunk_id"]),
        "title": row["title"],
        "section": " / ".join(row["section_path"] or []),
        "page_start": row["page_start"],
        "page_end": row["page_end"],
        "available": True,
    }


async def current_source(conn, ref, lock=False):
    return (await current_sources(conn, [ref], lock=lock))[0]


async def current_sources(conn, refs, lock=False):
    """Resolve one bounded batch in the caller's transaction; preserve input order."""
    valid = []
    for ordinal, ref in enumerate(refs):
        try:
            did, vid, cid = [str(UUID(str(ref[k]))) for k in ("document_id", "version_id", "chunk_id")]
            valid.append(
                {
                    "ordinal": ordinal,
                    "did": did,
                    "vid": vid,
                    "cid": cid,
                    **scope_parameters(ref.get("scope_context")),
                }
            )
        except (ValueError, KeyError, TypeError):
            continue
    if not valid:
        return [None for _ in refs]
    if lock:
        await one(conn, "SELECT value FROM ag_setting WHERE key='agent-return-rule-catalog-v1' FOR SHARE")
    found = await rows(
        conn,
        f"""SELECT r.ordinal,d.id AS document_id,d.title,v.id AS version_id,
        c.id AS chunk_id,c.content,COALESCE(p.content,c.content) AS context_content,
        COALESCE(p.section_path,c.section_path) AS section_path,
        c.page_start,c.page_end FROM jsonb_to_recordset(CAST(:refs AS jsonb))
        AS r(ordinal int,did uuid,vid uuid,cid uuid,product text,category text)
        JOIN ag_kb_document d ON d.id=r.did JOIN ag_kb_version v ON v.document_id=d.id AND v.id=r.vid
        JOIN ag_kb_chunk c ON c.version_id=v.id LEFT JOIN ag_kb_chunk p ON p.id=c.parent_chunk_id
        WHERE c.id=r.cid AND c.chunk_kind='CHILD'
        AND {GLOBAL_MEMBER.replace("CAST(:product AS text)", "r.product").replace("CAST(:category AS text)", "r.category")}
        ORDER BY d.id,c.id,r.ordinal
        """
        + (" FOR SHARE OF d,v,c" if lock else ""),
        refs=encoded(valid),
    )
    result = [None for _ in refs]
    for row in found:
        index = row["ordinal"]
        if hashlib.sha256(row["content"].encode()).hexdigest() == refs[index].get("content_sha256"):
            result[index] = row
    return result


class AuthorizedRetrieval:
    def __init__(self, db, ledger, models, indexes):
        self.db, self.ledger, self.models, self.indexes = db, ledger, models, indexes
        self.bridge = None
        self.runtime_factory = None

    async def retrieve(self, query, context=None):
        if self.runtime_factory:
            async with self.runtime_factory() as runtime:
                return await runtime.retrieval.retrieve(query, context)
        if not query.strip() or len(query) > 8000:
            raise DomainError("VALIDATION_ERROR", "知识问题须为1至8000字", 422)
        async with self.db.transaction(snapshot=True) as conn:
            versions = await rows(
                conn,
                f"""SELECT v.* FROM ag_kb_document d
                JOIN ag_kb_version v ON v.document_id=d.id WHERE {GLOBAL_MEMBER}
                ORDER BY d.id LIMIT 33""",
                **scope_parameters(context),
            )
        if not versions:
            return []
        if len(versions) > 32:
            raise DomainError(
                "KNOWLEDGE_CAPACITY_EXCEEDED", "本次范围检索超过32份有效资料，请联系管理员", 503
            )
        profile = self.ledger.profiles["EMBEDDING"]
        for v in versions:
            if (
                str(v["embedding_profile_id"]) != profile["id"]
                or v["embedding_profile_version"] != profile["version"]
                or v["embedding_dimension"] != self.ledger.settings.embedding_dimension
            ):
                raise DomainError(
                    "KNOWLEDGE_REBUILD_REQUIRED", "知识索引模型版本已变化，需要重新构建并发布", 503
                )
        vector = (await self.models.embed([query]))[0]
        gate = asyncio.Semaphore(4)

        async def search(v):
            async with gate:
                spec = (v["quality_report"] or {}).get("index")
                if (
                    not spec
                    or spec.get("collection") != v["index_collection"]
                    or spec.get("profile_id") != profile["id"]
                    or spec.get("profile_version") != profile["version"]
                    or spec.get("dimension") != v["embedding_dimension"]
                ):
                    raise DomainError("KNOWLEDGE_REBUILD_REQUIRED", "知识索引元数据不一致，请重新构建", 503)
                return await self.indexes.search(spec, vector, v["id"], limit=5)

        batches = await asyncio.gather(*(search(v) for v in versions))
        hits = sorted([h for batch in batches for h in batch], key=lambda h: h["distance"], reverse=True)
        candidates, seen = [], set()
        eligible_hits = [
            hit for hit in hits if hit.get("entity", {}).get("version_id") in {str(v["id"]) for v in versions}
        ]
        hit_refs = [
            {
                k: hit.get("entity", {}).get(k)
                for k in ("document_id", "version_id", "chunk_id", "content_sha256")
            }
            | ({"scope_context": context} if context else {})
            for hit in eligible_hits
        ]
        async with self.db.transaction() as conn:
            checked = await current_sources(conn, hit_refs)
            for hit, ref, row in zip(eligible_hits, hit_refs, checked):
                entity = hit.get("entity", {})
                if not row or row["content"] != entity.get("text") or ref["chunk_id"] in seen:
                    continue
                seen.add(ref["chunk_id"])
                candidates.append(
                    {
                        "source": source_view(row),
                        "content_sha256": ref["content_sha256"],
                        "excerpt": row["content"],
                        "vector_score": hit["distance"],
                    }
                )
                if len(candidates) == 20:
                    break
        if not candidates:
            return []
        try:
            ranked = await self.models.rerank(
                query, [c["excerpt"] for c in candidates], min(5, len(candidates))
            )
        except DomainError as error:
            if error.status not in {429, 502, 503, 504}:
                raise
            # Degrade ranking only. Every candidate was already authorized and hash-checked
            # against PG; never use low-score hits to mask an unavailable reranker.
            return [
                c | {"retrieval_mode": "VECTOR_FALLBACK", "degradation_code": error.code}
                for c in candidates
                if c["vector_score"] >= 0.65
            ][:5]
        return [candidates[r["index"]] for r in ranked if r["relevance_score"] >= 0.35]

    async def recheck(self, refs):
        async with self.db.transaction() as conn:
            if any(row is None for row in await current_sources(conn, refs)):
                raise DomainError("SOURCE_UNAVAILABLE", "知识来源已撤回或更新，请重新提问", 410)

    async def evidence(self, refs):
        result = []
        async with self.db.transaction() as conn:
            for row in await current_sources(conn, refs):
                if not row:
                    raise DomainError("SOURCE_UNAVAILABLE", "知识来源已撤回或更新", 410)
                result.append(
                    {
                        "title": row["title"],
                        "excerpt": row["context_content"],
                        "version_id": str(row["version_id"]),
                    }
                )
        return result

    async def read_citation(self, principal, version_id, chunk_id, token=None):
        async with self.db.transaction() as conn:
            ref = await one(
                conn,
                f"""SELECT k.*,c.entry_context FROM ag_kb_citation k
                JOIN ag_message m ON m.id=k.message_id JOIN ag_conversation c ON c.id=m.conversation_id
                WHERE k.version_id=:vid AND k.chunk_id=:cid AND c.member_id=:member
                AND c.deleted_at IS NULL AND {visible_sql()} AND m.status IN ('COMPLETED','GENERATING','STOPPED','FAILED','INTERRUPTED') LIMIT 1""",
                vid=version_id,
                cid=chunk_id,
                member=principal.id,
            )
        if ref and ref["entry_context"].get("kind") == "PRODUCT":
            if not self.bridge or not token:
                raise DomainError("SOURCE_UNAVAILABLE", "商品上下文无法复核", 410)
            product = await self.bridge.product(token, ref["entry_context"]["product_id"])
            ref = dict(ref) | {
                "scope_context": {"product_id": product["id"], "category_id": product["category_id"]}
            }
        async with self.db.transaction() as conn:
            # Ownership/deletion can change during Java IO; recheck before returning text.
            if ref:
                owned = await one(
                    conn,
                    f"""SELECT m.id FROM ag_kb_citation k JOIN ag_message m ON m.id=k.message_id
                    JOIN ag_conversation c ON c.id=m.conversation_id WHERE k.version_id=:vid AND k.chunk_id=:chunk
                    AND c.member_id=:member AND c.deleted_at IS NULL AND {visible_sql()}
                    AND m.status IN ('COMPLETED','GENERATING','STOPPED','FAILED','INTERRUPTED') LIMIT 1""",
                    vid=version_id,
                    chunk=chunk_id,
                    member=principal.id,
                )
                if not owned:
                    ref = None
            row = await current_source(conn, ref, lock=True) if ref else None
            if not row:
                raise DomainError("SOURCE_UNAVAILABLE", "该来源不可用或不属于当前会话", 410)
            return {"source": source_view(row), "excerpt": row["content"]}
