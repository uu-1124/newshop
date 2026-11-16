import asyncio
import hashlib
import subprocess
import sys
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from app.application.conversations import dedupe, save_dedupe
from app.application.expiry import deadline_sql, visible_sql
from app.domain.identity import DomainError
from app.infrastructure.postgres.database import execute, one, rows

MAX_IMAGE_BYTES = 5 * 1024 * 1024
DECODERS = asyncio.Semaphore(2)


def attachment_view(row):
    return {
        "id": str(row["id"]),
        "name": row["original_name"],
        "mime_type": row["mime_type"],
        "byte_size": row["byte_size"],
        "purpose": row["purpose"],
        "state": row["state"],
        "protected": False,
        "retention_until": row["retention_until"],
    }


def decode_image(raw):
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise DomainError("ATTACHMENT_TOO_LARGE", "图片大小不能超过5MB", 413)
    valid_magic = raw.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")) or (
        raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"
    )
    if not valid_magic:
        raise DomainError("UNSUPPORTED_MEDIA_TYPE", "请选择JPEG、PNG或WebP静态图片", 415)
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).parents[1] / "infrastructure/image_decode.py")],
            input=raw,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        raise DomainError("INVALID_IMAGE", "图片无法解码或超出像素限制，请更换静态图片", 422) from None
    if not result.startswith(b"\xff\xd8\xff") or len(result) > MAX_IMAGE_BYTES:
        raise DomainError("INVALID_IMAGE", "图片处理失败", 422)
    return result


async def bind_message_attachments(conn, member_id, attachment_ids, owner_kind="MEMBER"):
    if len(attachment_ids) > 2 or len(set(attachment_ids)) != len(attachment_ids):
        raise DomainError("VALIDATION_ERROR", "每条消息最多附两张不同图片", 422)
    result = []
    for aid in sorted(attachment_ids):
        item = await one(
            conn,
            f"""SELECT * FROM ag_attachment a WHERE id=:id
            AND owner_kind=:kind AND owner_id=:owner AND purpose='CHAT_IMAGE'
            AND state='READY' AND {deadline_sql("a", "image_retention_days")}>now() FOR UPDATE""",
            id=aid,
            owner=member_id,
            kind=owner_kind,
        )
        if not item:
            raise DomainError("NOT_FOUND", "图片不存在或已失效", 404)
        if await one(
            conn, "SELECT id FROM ag_attachment_ref WHERE attachment_id=:id AND released_at IS NULL", id=aid
        ):
            raise DomainError("ATTACHMENT_ALREADY_LINKED", "该图片已经用于另一条消息，请重新上传", 409)
        result.append(item)
    by_id = {str(item["id"]): item for item in result}
    return [by_id[str(aid)] for aid in attachment_ids]


class Attachments:
    def __init__(self, db, objects, settings):
        self.db, self.objects, self.settings = db, objects, settings

    def require_enabled(self):
        if not self.settings.attachments_enabled:
            raise DomainError("FEATURE_NOT_ACTIVE", "图片咨询尚未开放", 409)

    async def upload(self, principal, key, purpose, name, raw):
        self.require_enabled()
        staff = principal.kind == "STAFF"
        if staff:
            from app.infrastructure.staff_auth import require_permission

            require_permission(principal, "agent:attachment:upload")
            require_permission(principal, "agent:conversation:reply")
        if purpose == "KNOWLEDGE":
            raise DomainError("FORBIDDEN", "买家不能上传知识原件", 403)
        if purpose != "CHAT_IMAGE":
            raise DomainError("FEATURE_NOT_ACTIVE", "当前仅开放聊天图片上传", 409)
        async with DECODERS:
            content = await asyncio.to_thread(decode_image, raw)
        name = name.replace("\\", "/").split("/")[-1][:255] or "图片.jpg"
        body = {"purpose": purpose, "name": name, "raw_sha256": hashlib.sha256(raw).hexdigest()}
        sha = hashlib.sha256(content).hexdigest()
        scope = "uploadStaffAttachment" if staff else "uploadMemberAttachment"
        namespace = f"STAFF/{principal.id}" if staff else str(principal.id)
        aid = uuid5(NAMESPACE_URL, f"mall-agent/image/{namespace}/{key}")
        object_key = f"chat/{namespace}/{aid}.jpg"
        from app.application.commands import staff_dedupe

        async def previous_response(conn):
            return (
                await staff_dedupe(conn, principal, scope, key, body)
                if staff
                else await dedupe(conn, scope, principal.id, key, body)
            )

        async with self.db.transaction() as conn:
            cached = await previous_response(conn)
            if cached:
                return cached
            previous = await one(conn, "SELECT * FROM ag_attachment WHERE id=:id FOR UPDATE", id=aid)
            if previous and previous["state"] in {"DELETING", "DELETED"}:
                raise DomainError("ATTACHMENT_UNAVAILABLE", "附件已进入清理，请重新选择图片", 410)
            if previous and (previous["sha256"] != sha or previous["original_name"] != name):
                raise DomainError("IDEMPOTENCY_CONFLICT", "上传编号已经用于不同图片", 409)
            if not previous:
                await execute(
                    conn,
                    "SELECT pg_advisory_xact_lock(hashtextextended(:key,0))",
                    key=f"image-upload:{principal.kind}:{principal.id}",
                )
                count = await one(
                    conn,
                    """SELECT count(*) AS n FROM ag_attachment
                    WHERE owner_kind=:kind AND owner_id=:owner AND created_at>=now()-interval '1 day'""",
                    owner=principal.id,
                    kind=principal.kind,
                )
                if count["n"] >= 20:
                    raise DomainError("ATTACHMENT_QUOTA_EXCEEDED", "已达到今日图片上传次数上限", 429)
                await execute(
                    conn,
                    """INSERT INTO ag_attachment
                    (id,owner_kind,owner_id,purpose,object_key,original_name,mime_type,byte_size,sha256,retention_until)
                    VALUES (:id,:kind,:owner,'CHAT_IMAGE',:key,:name,'image/jpeg',:size,:sha,
                    now()+make_interval(days=>COALESCE((SELECT (value->>'image_retention_days')::int FROM ag_setting WHERE key='agent-runtime-v1'),90)))""",
                    id=aid,
                    kind=principal.kind,
                    owner=principal.id,
                    key=object_key,
                    name=name,
                    size=len(content),
                    sha=sha,
                )
            else:
                await execute(
                    conn,
                    "UPDATE ag_attachment SET state='UPLOADING',updated_at=now(),version=version+1 WHERE id=:id",
                    id=aid,
                )
        # Object IO never holds a SQL transaction. Failed/staged rows remain private and can be retried by key.
        await asyncio.to_thread(self.objects.put, object_key, content)
        async with self.db.transaction() as conn:
            cached = await previous_response(conn)
            if cached:
                return cached
            item = await one(
                conn,
                """UPDATE ag_attachment SET state='READY',updated_at=now(),version=version+1
                WHERE id=:id AND state IN ('UPLOADING','FAILED','READY') RETURNING *""",
                id=aid,
            )
            if not item:
                raise DomainError("ATTACHMENT_UNAVAILABLE", "附件在上传期间已进入清理", 410)
            result = attachment_view(item)
            if staff:
                from app.application.handoffs import save_command

                await save_command(conn, principal, scope, key, body, result, aid)
            else:
                await save_dedupe(conn, scope, principal.id, key, body, result)
            return result

    async def _visible_image(self, conn, principal, aid):
        item = await one(
            conn,
            f"SELECT * FROM ag_attachment a WHERE id=:id AND purpose='CHAT_IMAGE' AND state='READY' AND ({deadline_sql('a', 'image_retention_days')}>now() OR EXISTS(SELECT 1 FROM ag_attachment_ref p WHERE p.attachment_id=a.id AND p.ref_kind='RETURN_PROOF' AND p.protected AND p.released_at IS NULL))",
            id=aid,
        )
        if not item:
            raise DomainError("NOT_FOUND", "图片不存在或已失效", 404)
        proofs = await rows(
            conn,
            "SELECT a.member_id,r.return_apply_id FROM ag_attachment_ref r JOIN ag_action a ON a.id=r.action_id WHERE r.attachment_id=:id AND r.ref_kind='RETURN_PROOF' AND r.protected AND r.released_at IS NULL",
            id=aid,
        )
        if any(
            (principal.kind == "MEMBER" and row["member_id"] == principal.id)
            or (
                principal.kind == "STAFF"
                and row["return_apply_id"] is not None
                and "agent:return:read" in principal.permissions
            )
            for row in proofs
        ):
            return item
        refs = await rows(
            conn,
            f"""SELECT c.id,c.member_id,r.released_at,c.deleted_at AS conversation_deleted,
            CASE WHEN {visible_sql()} THEN NULL ELSE now() END AS message_deleted
            FROM ag_attachment_ref r JOIN ag_message m ON m.id=r.message_id
            JOIN ag_conversation c ON c.id=m.conversation_id WHERE r.attachment_id=:id""",
            id=aid,
        )
        if not refs:
            allowed = item["owner_kind"] == principal.kind and item["owner_id"] == principal.id
        elif principal.kind == "MEMBER":
            allowed = any(
                r["member_id"] == principal.id
                and not r["released_at"]
                and not r["conversation_deleted"]
                and not r["message_deleted"]
                for r in refs
            )
        else:
            from app.application.handoffs import visible_conversation

            allowed = False
            for ref in refs:
                if ref["released_at"] or ref["conversation_deleted"] or ref["message_deleted"]:
                    continue
                try:
                    await visible_conversation(conn, principal, ref["id"])
                    allowed = True
                    break
                except DomainError as error:
                    if error.status != 404:
                        raise
        if not allowed:
            raise DomainError("NOT_FOUND", "图片不存在或已失效", 404)
        return item

    async def read(self, principal, aid):
        self.require_enabled()
        if principal.kind == "STAFF":
            from app.infrastructure.staff_auth import require_permission

            require_permission(principal, "agent:attachment:read")
        async with self.db.transaction(snapshot=True) as conn:
            item = await self._visible_image(conn, principal, aid)
        raw = await asyncio.to_thread(self.objects.read, item["object_key"])
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise DomainError("ATTACHMENT_INTEGRITY_ERROR", "图片校验失败", 503)
        async with self.db.transaction(snapshot=True) as conn:
            await self._visible_image(conn, principal, aid)
        return raw

    async def for_run(self, claimed):
        self.require_enabled()
        async with self.db.transaction(snapshot=True) as conn:
            items = await rows(
                conn,
                f"""SELECT a.* FROM ag_attachment a
                JOIN ag_attachment_ref ref ON ref.attachment_id=a.id
                JOIN ag_message m ON m.id=ref.message_id
                JOIN LATERAL jsonb_array_elements(m.blocks) WITH ORDINALITY AS b(block,position)
                    ON b.block->>'type'='attachment' AND b.block->'attachment'->>'id'=a.id::text
                JOIN ag_conversation c ON c.id=m.conversation_id
                JOIN ag_run r ON r.trigger_message_id=m.id
                WHERE r.id=:run AND r.lease_owner=:owner AND r.fencing_token=:fence
                AND r.state='RUNNING' AND r.lease_until>now() AND c.control_epoch=r.control_epoch
                AND c.deleted_at IS NULL AND {visible_sql()} AND ref.released_at IS NULL AND c.memory_epoch=r.memory_epoch
                AND a.owner_kind='MEMBER' AND a.owner_id=c.member_id AND a.purpose='CHAT_IMAGE'
                AND a.state='READY' AND {deadline_sql("a", "image_retention_days")}>now() ORDER BY b.position""",
                run=claimed["id"],
                owner=claimed["lease_owner"],
                fence=claimed["fencing_token"],
            )
        return items
