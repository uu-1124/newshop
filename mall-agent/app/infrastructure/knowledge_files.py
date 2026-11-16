import asyncio
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from app.application.attachments import attachment_view
from app.domain.identity import DomainError
from app.infrastructure.postgres.database import execute, one
from app.infrastructure.staff_auth import require_permission

MIMES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "txt": "text/plain",
    "md": "text/markdown",
}
MAX_BYTES = 20 * 1024 * 1024
PARSERS = asyncio.Semaphore(2)


def parse_source(raw, kind, max_pages=200):
    try:
        process = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("knowledge_parse.py")), kind, str(max_pages)],
            input=raw,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        return json.loads(process.stdout)
    except (subprocess.SubprocessError, ValueError, OSError):
        raise DomainError(
            "KNOWLEDGE_PARSE_FAILED", "原件无法解析、没有可提取文本或超出限额，请校对后使用文本录入", 422
        ) from None


class KnowledgeFiles:
    def __init__(self, db, objects):
        self.db, self.objects = db, objects

    async def upload(self, principal, key, purpose, name, raw):
        from app.application.governance import read_policy

        require_permission(principal, "agent:attachment:upload")
        require_permission(principal, "agent:kb:edit")
        if purpose != "KNOWLEDGE":
            raise DomainError("FEATURE_NOT_ACTIVE", "员工附件当前开放知识原件上传", 409)
        if not self.objects:
            raise DomainError("FEATURE_NOT_ACTIVE", "知识原件存储未配置", 409)
        if not raw or len(raw) > MAX_BYTES:
            raise DomainError("ATTACHMENT_TOO_LARGE", "知识原件不能超过20MB", 413)
        name = name.replace("\\", "/").split("/")[-1][:255]
        kind = name.rsplit(".", 1)[-1].lower()
        if kind not in MIMES:
            raise DomainError("UNSUPPORTED_MEDIA_TYPE", "请选择PDF、DOCX或UTF-8文本/Markdown文件", 415)
        # Check bytes with the same bounded decoder used by the parsing job.
        async with self.db.transaction() as conn:
            max_pages = (await read_policy(conn))["value"]["max_document_pages"]
        async with PARSERS:
            await asyncio.to_thread(parse_source, raw, kind, max_pages)
        sha = hashlib.sha256(raw).hexdigest()
        aid = uuid5(NAMESPACE_URL, f"mall-agent/knowledge/{principal.id}/{key}")
        object_key = f"knowledge/{principal.id}/{aid}.{kind}"
        async with self.db.transaction() as conn:
            await execute(
                conn,
                "SELECT pg_advisory_xact_lock(hashtextextended(:k,0))",
                k=f"kb-upload:{principal.id}:{key}",
            )
            old = await one(conn, "SELECT * FROM ag_attachment WHERE id=:id FOR UPDATE", id=aid)
            if old and old["state"] in {"DELETING", "DELETED"}:
                raise DomainError("ATTACHMENT_UNAVAILABLE", "原件已进入清理，请重新上传", 410)
            if old and (old["sha256"] != sha or old["original_name"] != name):
                raise DomainError("IDEMPOTENCY_CONFLICT", "上传编号已用于不同原件", 409)
            if old and old["state"] == "READY":
                return attachment_view(old)
            if not old:
                await execute(
                    conn,
                    """INSERT INTO ag_attachment(id,owner_kind,owner_id,purpose,object_key,original_name,mime_type,byte_size,sha256)
                    VALUES (:id,'STAFF',:owner,'KNOWLEDGE',:key,:name,:mime,:size,:sha)""",
                    id=aid,
                    owner=principal.id,
                    key=object_key,
                    name=name,
                    mime=MIMES[kind],
                    size=len(raw),
                    sha=sha,
                )
            else:
                await execute(
                    conn,
                    "UPDATE ag_attachment SET state='UPLOADING',updated_at=now(),version=version+1 WHERE id=:id",
                    id=aid,
                )
        await asyncio.to_thread(self.objects.put, object_key, raw, MIMES[kind])
        async with self.db.transaction() as conn:
            item = await one(
                conn,
                "UPDATE ag_attachment SET state='READY',version=version+1,updated_at=now() WHERE id=:id AND state IN ('UPLOADING','FAILED','READY') RETURNING *",
                id=aid,
            )
            if not item:
                raise DomainError("ATTACHMENT_UNAVAILABLE", "原件在上传期间已进入清理", 410)
        return attachment_view(item)

    async def content(self, item):
        if not self.objects:
            raise DomainError("FEATURE_NOT_ACTIVE", "知识原件存储未配置", 409)
        raw = await asyncio.to_thread(self.objects.read, item["object_key"], MAX_BYTES)
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise DomainError("ATTACHMENT_INTEGRITY_ERROR", "知识原件校验失败", 503)
        return raw

    async def read(self, principal, aid):
        require_permission(principal, "agent:attachment:read")
        require_permission(principal, "agent:kb:read")
        async with self.db.transaction() as conn:
            item = await one(
                conn,
                """SELECT * FROM ag_attachment WHERE id=:id AND owner_kind='STAFF'
                AND owner_id=:owner AND purpose='KNOWLEDGE' AND state='READY'""",
                id=aid,
                owner=principal.id,
            )
        if not item:
            raise DomainError("NOT_FOUND", "知识原件不存在", 404)
        return await self.content(item), item["mime_type"]
