"""Versioned knowledge commands. Publication is a separate authorized operation."""

import asyncio
import hashlib
import re
from typing import Literal
from uuid import UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict

from app.application.commands import staff_dedupe
from app.application.jobs import job_view
from app.domain.capabilities import JobType
from app.domain.identity import DomainError
from app.infrastructure.knowledge_files import parse_source
from app.infrastructure.milvus_candidates import CandidateChunk
from app.infrastructure.postgres.database import digest, encoded, execute, one, rows
from app.infrastructure.staff_auth import require_permission


def document_view(row):
    return {
        key: row[key]
        for key in ("title", "category", "visibility", "scope", "enabled", "version", "updated_at")
    } | {
        "id": str(row["id"]),
        "active_version_id": str(row["active_version_id"]) if row["active_version_id"] else None,
    }


def version_view(row):
    report = row["quality_report"] or {}
    return {
        key: row[key]
        for key in (
            "version_no",
            "state",
            "version",
            "chunk_count",
            "embedding_dimension",
            "published_at",
            "created_at",
        )
    } | {
        "id": str(row["id"]),
        "document_id": str(row["document_id"]),
        "embedding_model": report.get("model"),
        "quality_passed": report.get("passed"),
    }


async def save_staff(conn, principal, scope, key, body, response, resource):
    await execute(
        conn,
        """INSERT INTO ag_request_dedupe(scope,actor_kind,actor_id,idempotency_key,request_hash,response_ref,expires_at)
        VALUES (:scope,'STAFF',:actor,:key,:hash,CAST(:response AS jsonb),now()+interval '7 days')""",
        scope=scope,
        actor=principal.id,
        key=key,
        hash=digest(body),
        response=encoded(response),
    )
    await execute(
        conn,
        """INSERT INTO ag_audit_log(id,actor_kind,actor_id,operation,resource_type,resource_id,request_id,outcome)
        VALUES (:id,'STAFF',:actor,:operation,'KNOWLEDGE',:resource,:request,'SUCCEEDED')""",
        id=uuid4(),
        actor=principal.id,
        operation=scope.split(":")[0],
        resource=str(resource),
        request=key,
    )


def check_version(row, expected):
    if row["version"] != expected:
        raise DomainError("VERSION_CONFLICT", "知识状态已变化，请刷新", 409)


class VersionParameter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version_id: UUID
    mode: Literal["INDEX", "GC"] = "INDEX"
    collection: str | None = None


class Knowledge:
    def __init__(self, db, jobs, cursors, files, ledger, models, indexes):
        self.db, self.jobs, self.cursors, self.files = db, jobs, cursors, files
        self.ledger, self.models, self.indexes = ledger, models, indexes
        self.runtime_factory = None

    async def document(self, conn, did, lock=False):
        row = await one(
            conn, "SELECT * FROM ag_kb_document WHERE id=:id" + (" FOR UPDATE" if lock else ""), id=did
        )
        if not row:
            raise DomainError("NOT_FOUND", "知识文档不存在", 404)
        return row

    async def version(self, conn, vid, lock=False):
        row = await one(
            conn, "SELECT * FROM ag_kb_version WHERE id=:id" + (" FOR UPDATE" if lock else ""), id=vid
        )
        if not row:
            raise DomainError("NOT_FOUND", "知识版本不存在", 404)
        return row

    async def create(self, principal, key, body):
        require_permission(principal, "agent:kb:edit")
        async with self.db.transaction() as conn:
            cached = await staff_dedupe(conn, principal, "createKBDocument", key, body)
            if cached:
                return cached
            row = await one(
                conn,
                """INSERT INTO ag_kb_document(id,title,category,visibility,scope,created_by)
                VALUES (:id,:title,:category,:visibility,CAST(:scope AS jsonb),:actor) RETURNING *""",
                id=uuid4(),
                title=body["title"].strip(),
                category=body["category"].strip(),
                visibility=body["visibility"],
                scope=encoded(body["scope"]),
                actor=principal.id,
            )
            result = document_view(row)
            await save_staff(conn, principal, "createKBDocument", key, body, result, row["id"])
            return result

    async def get_document(self, principal, did):
        require_permission(principal, "agent:kb:read")
        async with self.db.transaction() as conn:
            return document_view(await self.document(conn, did))

    async def listing(self, principal, limit, cursor, query="", category="", document_id=None):
        require_permission(principal, "agent:kb:read")
        scope = f"kb:{principal.id}:{query}:{category}:{document_id}"
        position = self.cursors.decode(cursor, scope) if cursor else None
        async with self.db.transaction(snapshot=True) as conn:
            if document_id:
                await self.document(conn, document_id)
                items = await rows(
                    conn,
                    """SELECT * FROM ag_kb_version WHERE document_id=:document
                    AND (CAST(:position AS int) IS NULL OR version_no<CAST(:position AS int))
                    ORDER BY version_no DESC LIMIT :limit""",
                    document=document_id,
                    position=position[0] if position else None,
                    limit=limit + 1,
                )
            else:
                items = await rows(
                    conn,
                    """SELECT * FROM ag_kb_document WHERE title ILIKE :query AND (:category='' OR category=:category)
                    AND (CAST(:date AS timestamptz) IS NULL OR (created_at,id)<(CAST(:date AS timestamptz),CAST(:id AS uuid)))
                    ORDER BY created_at DESC,id DESC LIMIT :limit""",
                    query="%" + query.replace("%", "\\%").replace("_", "\\_") + "%",
                    category=category,
                    date=position[0] if position else None,
                    id=position[1] if position else None,
                    limit=limit + 1,
                )
        more, items = len(items) > limit, items[:limit]
        values = (
            [items[-1]["version_no"]]
            if document_id and items
            else [items[-1]["created_at"].isoformat(), str(items[-1]["id"])]
            if items
            else []
        )
        return {
            "items": [(version_view if document_id else document_view)(row) for row in items],
            "has_more": more,
            "next_cursor": self.cursors.encode(values, scope) if more else None,
        }

    async def update(self, principal, did, key, body):
        require_permission(principal, "agent:kb:edit")
        async with self.db.transaction() as conn:
            scope = f"updateKBDocument:{did}"
            cached = await staff_dedupe(conn, principal, scope, key, body)
            if cached:
                return cached
            old = await self.document(conn, did, True)
            check_version(old, body["expected_version"])
            # Visibility/scope are part of the built index. Changing them withdraws publication.
            changed = old["visibility"] != body["visibility"] or digest(old["scope"]) != digest(body["scope"])
            row = await one(
                conn,
                """UPDATE ag_kb_document SET title=:title,category=:category,visibility=:visibility,scope=CAST(:scope AS jsonb),
                active_version_id=CASE WHEN :changed THEN NULL ELSE active_version_id END,version=version+1,updated_at=now() WHERE id=:id RETURNING *""",
                id=did,
                title=body["title"].strip(),
                category=body["category"].strip(),
                visibility=body["visibility"],
                scope=encoded(body["scope"]),
                changed=changed,
            )
            result = document_view(row)
            await save_staff(conn, principal, scope, key, body, result, did)
            return result

    async def create_version(self, principal, did, key, body):
        require_permission(principal, "agent:kb:edit")
        async with self.db.transaction() as conn:
            scope = f"createKBVersion:{did}"
            cached = await staff_dedupe(conn, principal, scope, key, body)
            if cached:
                return cached
            await self.document(conn, did, True)
            attachment, text = body.get("source_attachment_id"), body.get("source_text")
            if (attachment is None) == (text is None):
                raise DomainError("VALIDATION_ERROR", "请选择一个原文来源", 422)
            if attachment:
                item = await one(
                    conn,
                    """SELECT * FROM ag_attachment WHERE id=:id AND owner_kind='STAFF' AND owner_id=:actor
                    AND purpose='KNOWLEDGE' AND state='READY' FOR UPDATE""",
                    id=attachment,
                    actor=principal.id,
                )
                if not item:
                    raise DomainError("NOT_FOUND", "原件不存在或不属于当前员工", 404)
                sha = item["sha256"]
            else:
                sha = hashlib.sha256(text.encode()).hexdigest()
            number = await one(
                conn,
                "SELECT COALESCE(max(version_no),0)+1 AS n FROM ag_kb_version WHERE document_id=:id",
                id=did,
            )
            row = await one(
                conn,
                """INSERT INTO ag_kb_version(id,document_id,version_no,source_attachment_id,source_text,source_sha256,change_note,created_by)
                VALUES (:id,:document,:number,:attachment,:text,:sha,:note,:actor) RETURNING *""",
                id=uuid4(),
                document=did,
                number=number["n"],
                attachment=attachment,
                text=text,
                sha=sha,
                note=body["change_note"],
                actor=principal.id,
            )
            if attachment:
                await execute(
                    conn,
                    """INSERT INTO ag_attachment_ref(id,attachment_id,ref_kind,kb_version_id,protected)
                    VALUES (:id,:attachment,'KB_VERSION',:version,true)""",
                    id=uuid4(),
                    attachment=attachment,
                    version=row["id"],
                )
            result = version_view(row)
            await save_staff(conn, principal, scope, key, body, result, row["id"])
            return result

    async def get_version(self, principal, vid):
        require_permission(principal, "agent:kb:read")
        async with self.db.transaction() as conn:
            return version_view(await self.version(conn, vid))

    async def preview(self, principal, vid):
        require_permission(principal, "agent:kb:read")
        async with self.db.transaction() as conn:
            row = await self.version(conn, vid)
            if row["parsed_text"] is None:
                raise DomainError("INVALID_STATE", "该版本还没有可用的解析预览", 409)
            report = row["parse_report"] or {}
            return {
                "version_id": str(vid),
                "parsed_text": row["parsed_text"],
                "warnings": report.get("warnings", []),
                "page_count": report.get("page_count"),
                "pages": [
                    {"page": page["page"], "text": row["parsed_text"][page["start"] : page["end"]]}
                    for page in report.get("page_map", [])
                ],
            }

    async def edit_preview(self, principal, vid, key, body):
        require_permission(principal, "agent:kb:edit")
        if not body["parsed_text"].strip():
            raise DomainError("VALIDATION_ERROR", "预览文本不能为空", 422)
        async with self.db.transaction() as conn:
            scope = f"editKBPreview:{vid}"
            cached = await staff_dedupe(conn, principal, scope, key, body)
            if cached:
                return cached
            row = await self.version(conn, vid, True)
            check_version(row, body["expected_version"])
            if row["state"] != "PREVIEW_READY" or row["published_at"]:
                raise DomainError("INVALID_STATE", "只能编辑尚未构建的预览；已发布内容请创建新版本", 409)
            report = dict(row["parse_report"] or {})
            pages = body.get("pages")
            if pages is not None:
                count = report.get("page_count")
                if (
                    not report.get("page_map")
                    or len(pages) != count
                    or [p["page"] for p in pages] != list(range(1, count + 1))
                ):
                    raise DomainError("INVALID_PAGE_MAP", "PDF校对需保留原始页数与页序", 422)
                joined = "\n\n".join(p["text"] for p in pages)
                if joined != body["parsed_text"]:
                    raise DomainError("INVALID_PAGE_MAP", "逐页校对正文与合并文本不一致", 422)
                page_map = []
                offset = 0
                for page in pages:
                    page_map.append(
                        {"page": page["page"], "start": offset, "end": offset + len(page["text"])}
                    )
                    offset += len(page["text"]) + 2
                report["page_map"] = page_map
            elif report.get("page_map") and body["parsed_text"] != row["parsed_text"]:
                raise DomainError("PAGE_EDIT_REQUIRED", "请按PDF原始页逐页校对，以保留准确页码", 422)
            row = await one(
                conn,
                """UPDATE ag_kb_version SET parsed_text=:text,parse_report=CAST(:report AS jsonb),quality_report=NULL,version=version+1,updated_at=now()
                WHERE id=:id RETURNING *""",
                id=vid,
                text=body["parsed_text"],
                report=encoded(report),
            )
            result = version_view(row)
            await save_staff(conn, principal, scope, key, body, result, vid)
            return result

    async def enqueue(self, principal, vid, key, expected, kind):
        require_permission(principal, "agent:kb:edit")
        body = {"expected_version": expected}
        scope = f"{kind}:{vid}"
        async with self.db.transaction() as conn:
            cached = await staff_dedupe(conn, principal, scope, key, body)
            if cached:
                return cached
            row = await self.version(conn, vid, True)
            check_version(row, expected)
            allowed = (
                {"DRAFT", "FAILED"} if kind == JobType.KB_PARSE else {"PREVIEW_READY", "FAILED", "READY"}
            )
            if (
                row["state"] not in allowed
                or row["published_at"]
                or (kind == JobType.KB_INDEX and not row["parsed_text"])
            ):
                raise DomainError("INVALID_STATE", "该知识版本当前不能开始此任务", 409)
            job = await self.jobs.enqueue_in(
                conn, kind, f"{vid}:{expected}", {"version_id": str(vid)}, principal.id
            )
            column, state = (
                ("parse_job_id", "PARSING") if kind == JobType.KB_PARSE else ("index_job_id", "INDEXING")
            )
            await execute(
                conn,
                f"UPDATE ag_kb_version SET {column}=:job,state=:state,quality_report=NULL,version=version+1,updated_at=now() WHERE id=:id",
                id=vid,
                job=job["id"],
                state=state,
            )
            result = job_view(job)
            await save_staff(conn, principal, scope, key, body, result, vid)
            return result

    async def publish(self, principal, did, key, body):
        require_permission(principal, "agent:kb:publish")
        async with self.db.transaction() as conn:
            if hasattr(self.ledger, "capture"):
                await execute(conn, "SELECT pg_advisory_xact_lock(hashtextextended('model-prompt-config',0))")
                current_profile = await one(
                    conn,
                    "SELECT * FROM ag_model_profile WHERE capability='EMBEDDING' AND is_active AND enabled",
                )
                profile = (
                    {"id": str(current_profile["id"]), "version": current_profile["version"]}
                    if current_profile
                    else {}
                )
                dimension = current_profile["public_options"].get("dimension") if current_profile else None
            else:
                profile, dimension = (
                    self.ledger.profiles["EMBEDDING"],
                    self.ledger.settings.embedding_dimension,
                )
            scope = f"publishKBVersion:{did}"
            cached = await staff_dedupe(conn, principal, scope, key, body)
            if cached:
                return cached
            doc = await self.document(conn, did, True)
            check_version(doc, body["expected_document_version"])
            version = await self.version(conn, body["version_id"], True)
            report = version["quality_report"] or {}
            job = await one(conn, "SELECT state FROM ag_job WHERE id=:id", id=version["index_job_id"])
            if (
                not job
                or job["state"] != "SUCCEEDED"
                or version["document_id"] != did
                or version["state"] not in {"READY", "RETIRED"}
                or report.get("passed") is not True
                or report.get("parsed_sha256") != hashlib.sha256(version["parsed_text"].encode()).hexdigest()
                or report.get("document_scope_sha256")
                != digest({"visibility": doc["visibility"], "scope": doc["scope"]})
                or str(version["embedding_profile_id"]) != profile.get("id")
                or version["embedding_profile_version"] != profile.get("version")
                or version["embedding_dimension"] != dimension
            ):
                raise DomainError(
                    "KNOWLEDGE_NOT_READY", "版本尚未通过索引校验，或范围发生变化，请重新构建", 409
                )
            if doc["active_version_id"] and doc["active_version_id"] != version["id"]:
                await execute(
                    conn,
                    "UPDATE ag_kb_version SET state='RETIRED',version=version+1,updated_at=now() WHERE id=:id",
                    id=doc["active_version_id"],
                )
            await execute(
                conn,
                "UPDATE ag_kb_version SET state='READY',published_at=COALESCE(published_at,now()),version=version+1,updated_at=now() WHERE id=:id",
                id=version["id"],
            )
            doc = await one(
                conn,
                "UPDATE ag_kb_document SET active_version_id=:active,version=version+1,updated_at=now() WHERE id=:id RETURNING *",
                id=did,
                active=version["id"],
            )
            result = document_view(doc)
            await save_staff(conn, principal, scope, key, body, result, did)
            return result

    async def enable(self, principal, did, key, body):
        require_permission(principal, "agent:kb:publish")
        async with self.db.transaction() as conn:
            if hasattr(self.ledger, "capture"):
                await execute(conn, "SELECT pg_advisory_xact_lock(hashtextextended('model-prompt-config',0))")
            scope = f"setKBEnabled:{did}"
            cached = await staff_dedupe(conn, principal, scope, key, body)
            if cached:
                return cached
            doc = await self.document(conn, did, True)
            check_version(doc, body["expected_version"])
            if body["enabled"] and doc["active_version_id"] and hasattr(self.ledger, "capture"):
                profile = await one(
                    conn,
                    "SELECT * FROM ag_model_profile WHERE capability='EMBEDDING' AND is_active AND enabled",
                )
                version = await self.version(conn, doc["active_version_id"])
                if (
                    not profile
                    or version["embedding_profile_id"] != profile["id"]
                    or version["embedding_profile_version"] != profile["version"]
                    or version["embedding_dimension"] != profile["public_options"].get("dimension")
                ):
                    raise DomainError(
                        "KNOWLEDGE_REBUILD_REQUIRED", "当前资料索引与活动向量模型不一致，请重新构建发布", 409
                    )
            doc = await one(
                conn,
                "UPDATE ag_kb_document SET enabled=:enabled,version=version+1,updated_at=now() WHERE id=:id RETURNING *",
                id=did,
                enabled=body["enabled"],
            )
            result = document_view(doc)
            await save_staff(conn, principal, scope, key, body, result, did)
            return result

    async def fenced_version(self, conn, lease):
        job = await one(
            conn,
            """SELECT id FROM ag_job WHERE id=:id AND lease_owner=:owner AND fencing_token=:fence
            AND state='RUNNING' AND lease_until>now() FOR UPDATE""",
            id=lease.id,
            owner=lease.owner,
            fence=lease.fencing_token,
        )
        if not job:
            raise DomainError("LEASE_LOST", "知识任务租约已失效", 409)
        row = await self.version(conn, UUID(lease.parameters["version_id"]), True)
        column = "parse_job_id" if lease.kind == JobType.KB_PARSE else "index_job_id"
        if row[column] != lease.id:
            raise DomainError("JOB_SUPERSEDED", "知识任务已被后续操作替代", 409)
        return row

    async def parse(self, lease):
        from app.application.governance import read_policy

        async with self.db.transaction() as conn:
            max_pages = (await read_policy(conn))["value"]["max_document_pages"]
            row = await self.fenced_version(conn, lease)
            if row["state"] == "PREVIEW_READY":
                return {"version_id": str(row["id"]), "state": "PREVIEW_READY"}
            if row["state"] not in {"PARSING", "FAILED"}:
                raise DomainError("JOB_SUPERSEDED", "该版本不能继续解析", 409)
            attachment = (
                await one(
                    conn,
                    "SELECT * FROM ag_attachment WHERE id=:id AND state='READY'",
                    id=row["source_attachment_id"],
                )
                if row["source_attachment_id"]
                else None
            )
        if row["source_attachment_id"]:
            if not attachment:
                raise DomainError("NOT_FOUND", "知识原件不可用", 404)
            raw = await self.files.content(attachment)
            kind = attachment["original_name"].rsplit(".", 1)[-1].lower()
        else:
            raw, kind = row["source_text"].encode(), "txt"
        parsed = await asyncio.to_thread(parse_source, raw, kind, max_pages)
        async with self.db.transaction() as conn:
            current = await self.fenced_version(conn, lease)
            if current["state"] not in {"PARSING", "FAILED"}:
                raise DomainError("JOB_SUPERSEDED", "解析结果已过期", 409)
            await execute(
                conn,
                """UPDATE ag_kb_version SET state='PREVIEW_READY',parsed_text=:text,parse_report=CAST(:report AS jsonb),
                version=version+1,updated_at=now() WHERE id=:id""",
                id=row["id"],
                text=parsed["text"],
                report=encoded(
                    {
                        "warnings": parsed["warnings"],
                        "page_count": parsed["page_count"],
                        "page_map": parsed.get("page_map", []),
                    }
                ),
            )
        return {"version_id": str(row["id"]), "state": "PREVIEW_READY"}

    async def build(self, lease):
        if lease.parameters.get("mode") == "GC":
            return await self.collect_index(lease)
        if self.runtime_factory:
            async with self.runtime_factory() as runtime:
                return await runtime.knowledge.build(lease)
        async with self.db.transaction() as conn:
            version = await self.fenced_version(conn, lease)
            if version["state"] == "READY" and version["quality_report"]:
                return {"version_id": str(version["id"]), "state": "READY"}
            if version["state"] not in {"INDEXING", "FAILED"} or not version["parsed_text"]:
                raise DomainError("JOB_SUPERSEDED", "该版本不能继续构建", 409)
            doc = await self.document(conn, version["document_id"])
        if not getattr(self.ledger, "is_frozen", False):
            await self.ledger.setup()
        profile = self.ledger.profiles["EMBEDDING"]
        generation = f"{lease.id.hex}_{lease.fencing_token}"
        collection = f"agent_kb_{generation}"
        spec = {
            "collection": collection,
            "dimension": self.ledger.settings.embedding_dimension,
            "profile_id": profile["id"],
            "profile_version": profile["version"],
            "generation": generation,
        }
        parents, children, vectors, child_pages = [], [], [], {}
        text = version["parsed_text"]
        page_map = (version["parse_report"] or {}).get("page_map", [])
        segments = (
            [(p["start"], text[p["start"] : p["end"]], p["page"]) for p in page_map]
            if page_map
            else [(0, text, None)]
        )
        for start, segment, page in segments:
            for local in range(0, len(segment), 5000):
                offset = start + local
                content = segment[local : local + 5000]
                if not content.strip():
                    continue
                parent_id = uuid5(version["id"], generation + f":p:{offset}")
                parents.append(
                    {
                        "id": parent_id,
                        "content": content,
                        "ordinal": len(parents),
                        "section": f"第{page}页" if page else f"正文片段{len(parents) + 1}",
                        "page": page,
                    }
                )
                for child_offset in range(0, len(content), 1100):
                    child = content[child_offset : child_offset + 1300]
                    if child.strip():
                        chunk = CandidateChunk(
                            chunk_id=uuid5(parent_id, str(child_offset)),
                            parent_chunk_id=parent_id,
                            document_id=doc["id"],
                            version_id=version["id"],
                            visibility="PUBLIC" if doc["visibility"] == "MEMBER" else "STAFF",
                            text=child,
                        )
                        children.append(chunk)
                        child_pages[chunk.chunk_id] = page
        if not children or len(children) > 500:
            raise DomainError("KNOWLEDGE_CHUNK_LIMIT", "知识切片过多或为空，请拆分文档", 422)
        await self.indexes.create(spec)
        for start in range(0, len(children), 10):
            batch = children[start : start + 10]
            vectors.extend(await self.models.embed([child.text for child in batch]))
            await self.indexes.upsert(spec, batch, vectors[start : start + 10])
        # Actual self-recall checks the stored generation and text identity before READY.
        hits = await self.indexes.search(spec, vectors[0], version["id"], doc["visibility"], limit=5)
        if not any(
            hit["entity"].get("content_sha256") == hashlib.sha256(children[0].text.encode()).hexdigest()
            for hit in hits
        ):
            raise DomainError("KNOWLEDGE_INDEX_VERIFY_FAILED", "知识索引回读校验失败", 503)
        report = {
            "passed": True,
            "kind": "technical-self-recall-v1",
            "semantic_accuracy_evaluated": False,
            "model": profile["model"],
            "parsed_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "document_scope_sha256": digest({"visibility": doc["visibility"], "scope": doc["scope"]}),
            "index": spec,
            "children": len(children),
            "parents": len(parents),
        }
        async with self.db.transaction() as conn:
            current = await self.fenced_version(conn, lease)
            if (
                current["state"] not in {"INDEXING", "FAILED"}
                or current["parsed_text"] != text
                or current["published_at"]
            ):
                raise DomainError("JOB_SUPERSEDED", "构建结果已过期", 409)
            await execute(conn, "DELETE FROM ag_kb_chunk WHERE version_id=:id", id=version["id"])
            for parent in parents:
                await execute(
                    conn,
                    """INSERT INTO ag_kb_chunk(id,version_id,ordinal,chunk_kind,content,section_path,page_start,page_end)
                    VALUES (:id,:version,:ordinal,'PARENT',:content,CAST(:section AS jsonb),:page,:page)""",
                    id=parent["id"],
                    version=version["id"],
                    ordinal=parent["ordinal"],
                    content=parent["content"],
                    section=encoded([parent["section"]]),
                    page=parent["page"],
                )
            for index, child in enumerate(children, len(parents)):
                await execute(
                    conn,
                    """INSERT INTO ag_kb_chunk(id,version_id,ordinal,chunk_kind,parent_chunk_id,content,vector_key,page_start,page_end)
                    VALUES (:id,:version,:ordinal,'CHILD',:parent,:content,:vector,:page,:page)""",
                    id=child.chunk_id,
                    version=version["id"],
                    ordinal=index,
                    parent=child.parent_chunk_id,
                    content=child.text,
                    vector=str(child.chunk_id),
                    page=child_pages[child.chunk_id],
                )
            await execute(
                conn,
                """UPDATE ag_kb_version SET state='READY',embedding_profile_id=:profile,embedding_profile_version=:profile_version,
                embedding_dimension=:dimension,index_collection=:collection,chunk_count=:count,quality_report=CAST(:report AS jsonb),version=version+1,updated_at=now() WHERE id=:id""",
                id=version["id"],
                profile=profile["id"],
                profile_version=profile["version"],
                dimension=spec["dimension"],
                collection=collection,
                count=len(children),
                report=encoded(report),
            )
        return {"version_id": str(version["id"]), "state": "READY", "chunk_count": len(children)}

    async def schedule_index_cleanup(self):
        """Only orphan build generations, never the index of a rollback version."""
        collections = await self.indexes.collections()
        candidates = []
        async with self.db.transaction(snapshot=True) as conn:
            for collection in collections:
                match = re.fullmatch(r"agent_kb_([a-f0-9]{32})_([1-9][0-9]*)", collection)
                if not match:
                    continue
                original = await one(
                    conn,
                    "SELECT *,updated_at<now()-interval '10 minutes' AS aged FROM ag_job WHERE id=:id AND job_type='KB_INDEX'",
                    id=UUID(match[1]),
                )
                if not original or not original["aged"] or original["parameters"].get("mode") == "GC":
                    continue
                if original["state"] == "RUNNING" and original["fencing_token"] == int(match[2]):
                    continue
                if await one(
                    conn,
                    "SELECT id FROM ag_kb_version WHERE index_collection=:collection LIMIT 1",
                    collection=collection,
                ):
                    continue
                candidates.append(
                    {
                        "version_id": original["parameters"]["version_id"],
                        "mode": "GC",
                        "collection": collection,
                    }
                )
        for parameters in candidates:
            await self.jobs.enqueue(JobType.KB_INDEX, f"kb-gc:{parameters['collection']}", parameters)
        return len(candidates)

    async def collect_index(self, lease):
        collection = lease.parameters.get("collection", "")
        match = re.fullmatch(r"agent_kb_([a-f0-9]{32})_([1-9][0-9]*)", collection)
        if not match:
            raise DomainError("KNOWLEDGE_SCOPE_INVALID", "索引清理对象不正确", 422)
        async with self.db.transaction() as conn:
            job = await one(
                conn,
                """SELECT id FROM ag_job WHERE id=:id AND state='RUNNING'
                AND lease_owner=:owner AND fencing_token=:fence AND lease_until>now() FOR UPDATE""",
                id=lease.id,
                owner=lease.owner,
                fence=lease.fencing_token,
            )
            if not job:
                raise DomainError("LEASE_LOST", "索引清理任务已失效")
            original = await one(
                conn,
                "SELECT *,updated_at<now()-interval '10 minutes' AS aged FROM ag_job WHERE id=:id AND job_type='KB_INDEX'",
                id=UUID(match[1]),
            )
            if (
                not original
                or not original["aged"]
                or original["parameters"].get("mode") == "GC"
                or original["parameters"].get("version_id") != lease.parameters["version_id"]
            ):
                raise DomainError("KNOWLEDGE_SCOPE_INVALID", "索引集合没有可核对的过期构建来源", 403)
            if original["state"] == "RUNNING" and original["fencing_token"] == int(match[2]):
                raise DomainError("KNOWLEDGE_INDEX_PROTECTED", "构建仍在运行，不能清理", 409)
            if await one(
                conn,
                "SELECT id FROM ag_kb_version WHERE index_collection=:collection LIMIT 1",
                collection=collection,
            ):
                raise DomainError("KNOWLEDGE_INDEX_PROTECTED", "当前或历史可回滚索引不能清理", 409)
        await self.indexes.drop_unused(collection)
        return {"collection": collection, "state": "DELETED", "version_id": lease.parameters["version_id"]}

    async def failed(self, conn, job):
        if job["parameters"].get("mode") == "GC":
            return
        column = "parse_job_id" if job["job_type"] == JobType.KB_PARSE else "index_job_id"
        state = "PARSING" if job["job_type"] == JobType.KB_PARSE else "INDEXING"
        await execute(
            conn,
            f"""UPDATE ag_kb_version SET state='FAILED',version=version+1,updated_at=now()
            WHERE id=CAST(:id AS uuid) AND {column}=:job AND state=:state""",
            id=job["parameters"]["version_id"],
            job=job["id"],
            state=state,
        )

    def register(self, registry):
        def validate(parameters):
            try:
                value = VersionParameter.model_validate(parameters)
                if (value.mode == "GC") != bool(value.collection):
                    raise ValueError()
                if value.collection and not re.fullmatch(
                    r"agent_kb_[a-f0-9]{32}_[1-9][0-9]*", value.collection
                ):
                    raise ValueError()
                return value.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
            except ValueError:
                raise DomainError("VALIDATION_ERROR", "知识任务参数无效", 422) from None

        registry.register(
            JobType.KB_PARSE, self.parse, "agent:kb:edit", validate, timeout=45, on_failed=self.failed
        )
        registry.register(
            JobType.KB_INDEX, self.build, "agent:kb:edit", validate, timeout=300, on_failed=self.failed
        )
