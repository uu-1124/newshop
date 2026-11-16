from uuid import uuid4

from app.application.conversations import dedupe, owned, save_dedupe
from app.application.expiry import visible_sql
from app.domain.identity import DomainError
from app.infrastructure.postgres.database import one


def feedback_view(row):
    return {
        "id": str(row["id"]),
        "message_id": str(row["message_id"]),
        "vote": row["vote"],
        "reason": row["reason"],
    }


class Feedback:
    def __init__(self, db):
        self.db = db

    async def target(self, conn, principal, mid, lock=False):
        message = await one(conn, "SELECT conversation_id FROM ag_message WHERE id=:id", id=mid)
        if not message:
            raise DomainError("NOT_FOUND", "回答不存在或已失效", 404)
        await owned(conn, message["conversation_id"], principal.id, lock=lock)
        message = await one(conn, f"SELECT * FROM ag_message m WHERE id=:id AND {visible_sql()}", id=mid)
        if not message:
            raise DomainError("NOT_FOUND", "回答不存在或已失效", 404)
        if message["sender_kind"] != "ASSISTANT" or message["status"] != "COMPLETED":
            raise DomainError("FEEDBACK_NOT_ALLOWED", "只能评价已完成的智能助手回答", 409)

    async def get(self, principal, mid):
        async with self.db.transaction(snapshot=True) as conn:
            await self.target(conn, principal, mid)
            row = await one(
                conn,
                "SELECT * FROM ag_feedback WHERE member_id=:member AND message_id=:mid",
                member=principal.id,
                mid=mid,
            )
            return feedback_view(row) if row else None

    async def submit(self, principal, mid, key, body):
        scope = f"submitFeedback:{mid}"
        async with self.db.transaction() as conn:
            cached = await dedupe(conn, scope, principal.id, key, body)
            await self.target(conn, principal, mid, lock=True)
            if cached:
                return cached
            row = await one(
                conn,
                """INSERT INTO ag_feedback(id,message_id,member_id,vote,reason)
                VALUES (:id,:mid,:member,:vote,:reason)
                ON CONFLICT(member_id,message_id) DO UPDATE SET vote=EXCLUDED.vote,reason=EXCLUDED.reason,
                updated_at=now(),version=ag_feedback.version+1 RETURNING *""",
                id=uuid4(),
                mid=mid,
                member=principal.id,
                vote=body["vote"],
                reason=body.get("reason", "").strip() or None,
            )
            result = feedback_view(row)
            await save_dedupe(conn, scope, principal.id, key, body, result)
            return result
