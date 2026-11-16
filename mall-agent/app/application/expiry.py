"""One visibility rule for expired messages, notes and replayed content."""

from datetime import UTC, datetime
from uuid import UUID

from app.domain.identity import DomainError
from app.infrastructure.postgres.database import one


def deadline_sql(alias="m", policy="message_retention_days", default=90):
    if alias not in {"m", "n", "a", "e", ""} or policy not in {
        "message_retention_days",
        "image_retention_days",
        "audio_retention_days",
        "tts_retention_days",
        "event_retention_days",
    }:
        raise ValueError("Unreviewed retention SQL")
    prefix = alias + "." if alias else ""
    return f"""LEAST({prefix}retention_until,{prefix}created_at+make_interval(days=>COALESCE(
        (SELECT (value->>'{policy}')::int FROM ag_setting WHERE key='agent-runtime-v1'),{default})))"""


def visible_sql(alias="m"):
    prefix = alias + "." if alias else ""
    return f"{prefix}deleted_at IS NULL AND {deadline_sql(alias)}>now()"


async def filter_event(conn, event):
    visible = await filter_events(conn, [event])
    return visible[0] if visible else None


async def filter_events(conn, events):
    """Recheck one replay batch in its caller's snapshot, including copied projections."""
    from app.application.views import conversation_projection, messages_projection
    from app.infrastructure.postgres.database import rows

    events = [event for event in events if event["retention_until"] > datetime.now(UTC)]
    if not events:
        return []
    retained = {
        row["event_id"]
        for row in await rows(
            conn,
            f"""SELECT event_id FROM ag_event_outbox e WHERE event_id=ANY(CAST(:ids AS bigint[]))
            AND {deadline_sql("e", "event_retention_days", 7)}>now()""",
            ids=list({event["event_id"] for event in events}),
        )
    }
    eligible, message_ids, projection_ids, conversation_ids = [], set(), set(), set()
    for event in events:
        if event["event_id"] not in retained:
            continue
        payload = event["payload"]
        mid = payload.get("message_id") or payload.get("message", {}).get("id")
        try:
            mid = UUID(str(mid)) if mid else None
        except ValueError:
            continue
        if mid:
            message_ids.add(mid)
            if payload.get("message"):
                projection_ids.add(mid)
        if isinstance(payload.get("conversation"), dict) and event["conversation_id"]:
            conversation_ids.add(event["conversation_id"])
        eligible.append((event, mid))
    messages = {}
    if message_ids:
        found = await rows(
            conn,
            f"SELECT * FROM ag_message m WHERE id=ANY(CAST(:ids AS uuid[])) AND {visible_sql()}",
            ids=list(message_ids),
        )
        messages = {str(message["id"]): message for message in found}
        projected = await messages_projection(
            conn, [message for message in found if message["id"] in projection_ids]
        )
        projections = {message["id"]: message for message in projected}
    conversations = {}
    if conversation_ids:
        found = await rows(
            conn,
            "SELECT * FROM ag_conversation WHERE id=ANY(CAST(:ids AS uuid[])) AND deleted_at IS NULL",
            ids=list(conversation_ids),
        )
        for conv in found:
            conversations[str(conv["id"])] = await conversation_projection(conn, conv)
    result = []
    for event, mid in eligible:
        payload = event["payload"]
        if mid:
            message = messages.get(str(mid))
            if not message or message["conversation_id"] != event["conversation_id"]:
                continue
            if payload.get("message"):
                payload = payload | {"message": projections[str(mid)]}
        if isinstance(payload.get("conversation"), dict) and event["conversation_id"]:
            conv = conversations.get(str(event["conversation_id"]))
            if not conv:
                continue
            payload = payload | {"conversation": conv}
        result.append(dict(event) | {"payload": payload})
    return result


async def assert_message_visible(conn, mid):
    if not await one(conn, f"SELECT id FROM ag_message m WHERE id=:id AND {visible_sql()}", id=mid):
        raise DomainError("MESSAGE_EXPIRED", "消息已到期或删除，请重新发送", 410)


async def assert_cached_visible(conn, payload):
    if payload.get("resource_expired"):
        raise DomainError("MESSAGE_EXPIRED", "原消息内容已到期", 410)
    message = payload.get("message", {})
    mid = (
        message.get("id")
        or payload.get("trigger_message_id")
        or (payload.get("id") if "seq" in payload else None)
    )
    if mid:
        await assert_message_visible(conn, UUID(str(mid)))
    if "body" in payload and payload.get("id"):
        if not await one(
            conn,
            f"SELECT id FROM ag_staff_note n WHERE id=:id AND {deadline_sql('n')}>now()",
            id=UUID(payload["id"]),
        ):
            raise DomainError("MESSAGE_EXPIRED", "原备注内容已到期", 410)
