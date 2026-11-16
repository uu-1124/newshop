"""Per-actor, monotonic read positions shared by buyer and staff projections."""

from app.application.expiry import visible_sql
from app.domain.identity import DomainError
from app.infrastructure.postgres.database import execute, one


async def read_position(conn, conversation_id, actor_kind, actor_id):
    row = await one(
        conn,
        f"""SELECT COALESCE((SELECT last_seq FROM ag_read_cursor
        WHERE conversation_id=:cid AND actor_kind=:kind AND actor_id=:actor),0) AS last_seq,
        (SELECT COUNT(*) FROM ag_message m WHERE m.conversation_id=:cid AND {visible_sql()}
        AND m.status<>'GENERATING' AND m.sender_kind IN ('MEMBER','ASSISTANT','STAFF')
        AND NOT (m.sender_kind=:kind AND m.sender_id=:actor)
        AND m.seq>COALESCE((SELECT last_seq FROM ag_read_cursor
            WHERE conversation_id=:cid AND actor_kind=:kind AND actor_id=:actor),0)) AS unread_count""",
        cid=conversation_id,
        kind=actor_kind,
        actor=actor_id,
    )
    return {"last_seq": str(row["last_seq"]), "unread_count": row["unread_count"]}


async def advance_read(conn, conversation_id, actor_kind, actor_id, last_seq):
    latest = await one(
        conn,
        f"""SELECT COALESCE(MAX(seq),0) AS seq FROM ag_message m
        WHERE conversation_id=:cid AND {visible_sql()} AND status<>'GENERATING'""",
        cid=conversation_id,
    )
    if last_seq > latest["seq"]:
        raise DomainError("INVALID_READ_POSITION", "已读位置超过当前可读消息", 422)
    previous = await read_position(conn, conversation_id, actor_kind, actor_id)
    if last_seq <= int(previous["last_seq"]):
        return previous, False
    await execute(
        conn,
        """INSERT INTO ag_read_cursor(conversation_id,actor_kind,actor_id,last_seq)
        VALUES (:cid,:kind,:actor,:seq) ON CONFLICT (conversation_id,actor_kind,actor_id)
        DO UPDATE SET last_seq=GREATEST(ag_read_cursor.last_seq,EXCLUDED.last_seq),
        updated_at=now(),version=ag_read_cursor.version+1""",
        cid=conversation_id,
        kind=actor_kind,
        actor=actor_id,
        seq=last_seq,
    )
    return await read_position(conn, conversation_id, actor_kind, actor_id), True
