"""Tombstone-bound deletion jobs; unrelated retention policies stay inactive."""

import asyncio
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.application.jobs import wake_job
from app.domain.capabilities import JobType
from app.domain.identity import DomainError
from app.infrastructure.postgres.database import encoded, execute, one, rows
from app.infrastructure.private_objects import PrivateObjects


class ErasureParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    conversation_id: UUID
    memory_epoch: int = Field(ge=2)


class AttachmentDeleteParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attachment_id: UUID


def register_conversation_cleanup(registry, db, settings, retention=None):
    def validate_erasure(parameters):
        if parameters.get("kind") == "RETENTION_POLICY" and retention is not None:
            from app.application.retention import validate_plan

            return validate_plan(parameters)
        try:
            value = ErasureParameters.model_validate(parameters)
            if value.kind not in {"CONVERSATION_DELETE", "CONTEXT_EXPIRY"}:
                raise ValueError()
            return value.model_dump(mode="json")
        except ValueError:
            raise DomainError("VALIDATION_ERROR", "会话清理参数无效", 422) from None

    def validate_attachment(parameters):
        try:
            return AttachmentDeleteParameters.model_validate(parameters).model_dump(mode="json")
        except ValueError:
            raise DomainError("VALIDATION_ERROR", "附件清理参数无效", 422) from None

    async def lock_job(conn, lease):
        job = await one(
            conn,
            """SELECT id FROM ag_job WHERE id=:id AND state='RUNNING' AND lease_owner=:owner
            AND fencing_token=:fence AND lease_until>now() FOR UPDATE""",
            id=lease.id,
            owner=lease.owner,
            fence=lease.fencing_token,
        )
        if not job:
            raise DomainError("LEASE_LOST", "清理任务租约已失效")

    async def erase(lease):
        parameters = validate_erasure(lease.parameters)
        if parameters["kind"] == "RETENTION_POLICY":
            return await retention.execute_job(lease)
        cid = parameters["conversation_id"]
        async with db.transaction() as conn:
            await lock_job(conn, lease)
            conv = await one(conn, "SELECT * FROM ag_conversation WHERE id=:cid FOR UPDATE", cid=cid)
            if parameters["kind"] == "CONTEXT_EXPIRY":
                marker = await one(
                    conn,
                    "SELECT memory_epoch FROM ag_erasure_tombstone WHERE resource_type='CONTEXT' AND resource_id=:id",
                    id=cid,
                )
                if (
                    not marker
                    or not conv
                    or not (parameters["memory_epoch"] <= marker["memory_epoch"] <= conv["memory_epoch"])
                ):
                    raise DomainError("ERASURE_NOT_AUTHORIZED", "未找到有效的到期记忆清理标记", 403)
                old = await rows(
                    conn,
                    "SELECT checkpoint_thread_id FROM ag_run WHERE conversation_id=:id AND memory_epoch<:epoch",
                    id=cid,
                    epoch=parameters["memory_epoch"],
                )
                for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                    await execute(
                        conn,
                        f"DELETE FROM langgraph_checkpoint.{table} WHERE thread_id=ANY(:threads)",
                        threads=[r["checkpoint_thread_id"] for r in old],
                    )
                return {"conversation_id": cid, "context_expiry_cleanup": True, "threads": len(old)}
            tombstone = await one(
                conn,
                "SELECT memory_epoch FROM ag_erasure_tombstone WHERE resource_type='CONVERSATION' AND resource_id=:cid",
                cid=cid,
            )
            if (
                not conv
                or not conv["deleted_at"]
                or not tombstone
                or not (conv["memory_epoch"] == tombstone["memory_epoch"] == parameters["memory_epoch"])
            ):
                raise DomainError("ERASURE_NOT_AUTHORIZED", "未找到一致的会话删除标记", 403)
            runs = await rows(
                conn, "SELECT id,checkpoint_thread_id FROM ag_run WHERE conversation_id=:cid", cid=cid
            )
            threads = [run["checkpoint_thread_id"] for run in runs]
            # Library-owned table names match the installed AsyncPostgresSaver. All
            # namespaces for these deleted runs are removed in the same PG transaction.
            for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                await execute(
                    conn,
                    f"DELETE FROM langgraph_checkpoint.{table} WHERE thread_id=ANY(:threads)",
                    threads=threads,
                )
            await execute(
                conn,
                "DELETE FROM ag_image_observation WHERE run_id IN (SELECT id FROM ag_run WHERE conversation_id=:cid)",
                cid=cid,
            )
            await execute(conn, "DELETE FROM ag_context_summary WHERE conversation_id=:cid", cid=cid)
            await execute(
                conn,
                "DELETE FROM ag_kb_citation WHERE message_id IN (SELECT id FROM ag_message WHERE conversation_id=:cid)",
                cid=cid,
            )
            released = await rows(
                conn,
                """UPDATE ag_attachment_ref SET released_at=now(),updated_at=now(),version=version+1
                WHERE message_id IN (SELECT id FROM ag_message WHERE conversation_id=:cid)
                AND NOT protected AND released_at IS NULL RETURNING attachment_id""",
                cid=cid,
            )
            scheduled = 0
            for aid in sorted({row["attachment_id"] for row in released}):
                attachment = await one(conn, "SELECT * FROM ag_attachment WHERE id=:id FOR UPDATE", id=aid)
                protected = await one(
                    conn,
                    "SELECT id FROM ag_attachment_ref WHERE attachment_id=:id AND (released_at IS NULL OR protected) LIMIT 1",
                    id=aid,
                )
                if protected or attachment["state"] == "DELETED":
                    continue
                await execute(
                    conn,
                    "UPDATE ag_attachment SET state='DELETING',updated_at=now(),version=version+1 WHERE id=:id",
                    id=aid,
                )
                job = await one(
                    conn,
                    """INSERT INTO ag_job(id,job_type,dedupe_key,parameters)
                    VALUES (:id,'ATTACHMENT_DELETE',:key,CAST(:parameters AS jsonb))
                    ON CONFLICT (job_type,dedupe_key) DO NOTHING RETURNING *""",
                    id=uuid4(),
                    key=f"attachment-delete:{aid}",
                    parameters=encoded({"attachment_id": str(aid)}),
                )
                if job:
                    await wake_job(conn, job)
                    scheduled += 1
            await execute(
                conn,
                """UPDATE ag_message SET text_content='',blocks='[]'::jsonb,sender_display_name=NULL,
                deleted_at=COALESCE(deleted_at,now()),updated_at=now() WHERE conversation_id=:cid""",
                cid=cid,
            )
            await execute(conn, "UPDATE ag_conversation SET title='已删除会话' WHERE id=:cid", cid=cid)
            await execute(conn, "UPDATE ag_staff_note SET body='已删除' WHERE conversation_id=:cid", cid=cid)
            await execute(
                conn,
                "DELETE FROM ag_feedback WHERE message_id IN (SELECT id FROM ag_message WHERE conversation_id=:cid)",
                cid=cid,
            )
            await execute(
                conn,
                """UPDATE ag_request_dedupe SET response_ref=CAST(:marker AS jsonb)
                WHERE scope LIKE 'submitFeedback:%' AND response_ref->>'message_id' IN
                (SELECT id::text FROM ag_message WHERE conversation_id=:cid)""",
                cid=cid,
                marker=encoded({"resource_deleted": True}),
            )
            await execute(
                conn,
                "UPDATE ag_media_request SET result=NULL WHERE message_id IN (SELECT id FROM ag_message WHERE conversation_id=:cid)",
                cid=cid,
            )
            await execute(conn, "UPDATE ag_handoff SET reason=NULL WHERE conversation_id=:cid", cid=cid)
            await execute(conn, "DELETE FROM ag_event_outbox WHERE conversation_id=:cid", cid=cid)
            # Preserve request hashes/idempotency and the delete receipt, while
            # removing copied message text and titles from previous responses.
            await execute(
                conn,
                """UPDATE ag_request_dedupe SET response_ref=CAST(:marker AS jsonb)
                WHERE scope<>:delete_scope
                AND (scope LIKE :suffix OR response_ref->>'id'=:cid
                    OR response_ref->'message'->>'conversation_id'=:cid OR response_ref->>'conversation_id'=:cid)""",
                marker=encoded({"resource_deleted": True}),
                delete_scope=f"deleteConversation:{cid}",
                suffix=f"%:{cid}",
                cid=cid,
            )
        return {
            "conversation_id": cid,
            "memory_epoch": parameters["memory_epoch"],
            "attachment_jobs": scheduled,
        }

    async def remove_attachment(lease):
        aid = validate_attachment(lease.parameters)["attachment_id"]
        async with db.transaction() as conn:
            await lock_job(conn, lease)
            attachment = await one(conn, "SELECT * FROM ag_attachment WHERE id=:id FOR UPDATE", id=aid)
            if not attachment or attachment["state"] == "DELETED":
                return {"attachment_id": aid, "deleted": True}
            protected = await one(
                conn,
                "SELECT id FROM ag_attachment_ref WHERE attachment_id=:id AND (released_at IS NULL OR protected) LIMIT 1",
                id=aid,
            )
            if protected or attachment["state"] != "DELETING":
                raise DomainError("ATTACHMENT_PROTECTED", "附件仍被引用，不能删除", 409)
            object_key = attachment["object_key"]
        if not settings.attachments_enabled:
            raise DomainError("ATTACHMENT_STORAGE_UNAVAILABLE", "附件存储尚未就绪", 503)
        objects = PrivateObjects(settings)
        try:
            await asyncio.to_thread(objects.delete, object_key)
        finally:
            objects.close()
        async with db.transaction() as conn:
            await lock_job(conn, lease)
            await execute(
                conn,
                """UPDATE ag_attachment SET state='DELETED',storage_deleted_at=now(),original_name='已删除附件',
                updated_at=now(),version=version+1 WHERE id=:id AND state='DELETING'""",
                id=aid,
            )
        return {"attachment_id": aid, "deleted": True}

    registry.register(
        JobType.RETENTION_EXECUTE,
        erase,
        permission="agent:retention:execute",
        validator=validate_erasure,
        timeout=1800,
    )
    registry.register(
        JobType.ATTACHMENT_DELETE,
        remove_attachment,
        permission="agent:retention:execute",
        validator=validate_attachment,
    )
