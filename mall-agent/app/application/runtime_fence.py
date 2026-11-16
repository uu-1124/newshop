"""Fences for commands prepared by a live run, never business execution authority."""

from dataclasses import dataclass
from uuid import UUID

from app.application.expiry import assert_message_visible
from app.domain.identity import DomainError
from app.infrastructure.postgres.database import one


@dataclass(frozen=True)
class RuntimeFence:
    run_id: UUID
    lease_owner: str
    fencing_token: int


async def assert_runtime_fence(conn, conversation, fence):
    if fence is None:
        return
    run = await one(
        conn,
        """SELECT id,trigger_message_id FROM ag_run WHERE id=:id AND conversation_id=:cid
        AND state='RUNNING' AND lease_until>now() AND lease_owner=:owner
        AND fencing_token=:fence AND control_epoch=:epoch AND memory_epoch=:memory FOR UPDATE""",
        id=fence.run_id,
        cid=conversation["id"],
        owner=fence.lease_owner,
        fence=fence.fencing_token,
        epoch=conversation["control_epoch"],
        memory=conversation["memory_epoch"],
    )
    if not run or conversation["deleted_at"]:
        raise DomainError("LEASE_LOST", "运行租约或会话状态已失效", 409)
    await assert_message_visible(conn, run["trigger_message_id"])
