"""Trusted task-local billing attribution, independent of model/tool input."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID

from app.application.expiry import deadline_sql, visible_sql
from app.domain.identity import DomainError
from app.infrastructure.postgres.database import one


@dataclass(frozen=True)
class ModelCallContext:
    run_id: UUID | None = None
    job_id: UUID | None = None
    media_request_id: UUID | None = None
    lease_owner: str | None = None
    fencing_token: int | None = None

    def __post_init__(self):
        if sum(x is not None for x in (self.run_id, self.job_id, self.media_request_id)) != 1:
            raise ValueError("Model call context must identify exactly one run, job or media request")
        if not self.lease_owner or type(self.fencing_token) is not int or self.fencing_token < 1:
            raise ValueError("Model call context requires a current execution lease")


current_model_call = ContextVar("current_model_call", default=None)


@contextmanager
def model_call_scope(context):
    token = current_model_call.set(context)
    try:
        yield
    finally:
        current_model_call.reset(token)


async def source_actor(conn, context, run_id, job_id):
    """Read authoritative ownership before reservation; never trust adapter data."""
    if context and context.media_request_id:
        row = await one(
            conn,
            f"""SELECT r.member_id FROM ag_media_request r
            WHERE r.id=:id AND r.state='RUNNING' AND r.lease_until>clock_timestamp()
            AND r.lease_owner=:owner AND r.fencing_token=:fence
            AND r.retention_until>clock_timestamp()
            AND ((r.kind='ASR' AND EXISTS(SELECT 1 FROM ag_attachment a WHERE a.id=r.attachment_id
                AND a.owner_kind='MEMBER' AND a.owner_id=r.member_id AND a.state='READY'
                AND {deadline_sql("a", "audio_retention_days", 7)}>clock_timestamp()))
             OR (r.kind='TTS' AND EXISTS(SELECT 1 FROM ag_message m JOIN ag_conversation c ON c.id=m.conversation_id
                WHERE m.id=r.message_id AND c.member_id=r.member_id AND c.deleted_at IS NULL
                AND {visible_sql()})))""",
            id=context.media_request_id,
            owner=context.lease_owner,
            fence=context.fencing_token,
        )
        if not row:
            raise DomainError("MEDIA_REQUEST_EXPIRED", "语音请求或来源已失效", 409)
        return "MEMBER", row["member_id"]
    if run_id is not None:
        condition = ""
        parameters = {"id": run_id}
        if context:
            condition = f"""AND r.state='RUNNING' AND r.lease_until>now()
                AND r.lease_owner=:owner AND r.fencing_token=:fence
                AND r.control_epoch=c.control_epoch AND r.memory_epoch=c.memory_epoch
                AND (c.state='AI_ACTIVE' OR (c.state='WAITING_HUMAN' AND c.waiting_ai_enabled))
                AND {visible_sql()}"""
            parameters |= {"owner": context.lease_owner, "fence": context.fencing_token}
        row = await one(
            conn,
            """SELECT c.member_id FROM ag_run r
            JOIN ag_conversation c ON c.id=r.conversation_id
            JOIN ag_message m ON m.id=r.trigger_message_id
            WHERE r.id=:id AND c.deleted_at IS NULL """
            + condition,
            **parameters,
        )
        if not row:
            raise DomainError("LEASE_LOST", "模型调用所属运行已失效", 409)
        return "MEMBER", row["member_id"]
    if job_id is not None:
        condition = ""
        parameters = {"id": job_id}
        if context:
            condition = (
                "AND state='RUNNING' AND lease_until>now() AND lease_owner=:owner AND fencing_token=:fence"
            )
            parameters |= {"owner": context.lease_owner, "fence": context.fencing_token}
        row = await one(conn, "SELECT created_by FROM ag_job WHERE id=:id " + condition, **parameters)
        if not row:
            raise DomainError("LEASE_LOST", "模型调用所属任务已失效", 409)
        return ("STAFF", row["created_by"]) if row["created_by"] else ("SYSTEM", None)
    return None
