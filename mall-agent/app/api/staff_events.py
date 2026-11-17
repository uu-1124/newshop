import time
from uuid import UUID

from fastapi import Depends, Header, Query, Request
from fastapi.responses import StreamingResponse

from app.api.admin import staff
from app.api.dependencies import idempotency, ok, services
from app.api.dto import ReadInput
from app.application.expiry import filter_events
from app.application.handoffs import visible_conversation
from app.domain.identity import DomainError
from app.infrastructure.postgres.database import encoded, one, rows
from app.infrastructure.staff_auth import require_permission
from app.infrastructure.stream_observation import observe_stream_domain_end


async def staff_event_batch(svc, position, current, conversation_id=None):
    async with svc.db.transaction(snapshot=True) as conn:
        if conversation_id:
            await visible_conversation(conn, current, conversation_id)
        clock = await one(conn, "SELECT * FROM ag_event_clock WHERE singleton_id=1")
        if position < clock["replay_floor"]:
            raise DomainError("EVENT_CURSOR_EXPIRED", "请重新同步工作台", 410)
        if position > clock["last_event_id"]:
            raise DomainError("INVALID_CURSOR", "事件位置超过当前水位", 400)
        candidates = await rows(
            conn,
            """SELECT * FROM ag_event_outbox WHERE event_id>:position
            AND (CAST(:cid AS uuid) IS NULL OR conversation_id=CAST(:cid AS uuid)) ORDER BY event_id LIMIT 100""",
            position=position,
            cid=str(conversation_id) if conversation_id else None,
        )
        visible, conversations = [], {}
        for event in candidates:
            if event["visibility"] == "PERMISSION":
                if event["required_permission"] in current.permissions:
                    visible.append(event)
                continue
            if "agent:conversation:read" not in current.permissions:
                continue
            cid = event["conversation_id"]
            if cid not in conversations:
                try:
                    await visible_conversation(conn, current, cid)
                    conversations[cid] = True
                except DomainError:
                    conversations[cid] = False
            if not conversations[cid]:
                continue
            if (
                event["visibility"] == "STAFF_ONLY"
                and event["required_permission"]
                and event["required_permission"] not in current.permissions
            ):
                continue
            visible.append(event)
        return await filter_events(conn, visible), candidates[-1]["event_id"] if candidates else position


def register_staff_events(app):
    prefix = "/mall-agent/v1/admin"

    @app.post(prefix + "/conversations/{conversation_id}/read", operation_id="adminReadCursor")
    async def read(
        conversation_id: UUID,
        body: ReadInput,
        request: Request,
        principal=Depends(staff),
        key=Depends(idempotency),
    ):
        return ok(
            request,
            await services(request)
            .extensions.handoffs.require()
            .mark_read(principal, conversation_id, key, body.model_dump(mode="json")),
        )

    @app.get(prefix + "/events", operation_id="adminEvents")
    async def events(
        request: Request,
        principal=Depends(staff),
        conversation_id: UUID | None = None,
        after: str | None = Query(None, pattern=r"^[0-9]{1,19}$"),
        last_event_id: str | None = Header(None, pattern=r"^[0-9]{1,19}$"),
    ):
        require_permission(principal, "agent:events:subscribe")
        if after and last_event_id and after != last_event_id:
            raise DomainError("CURSOR_MISMATCH", "事件重连位置不一致", 400)
        if "agent:conversation:read" not in principal.permissions and not any(
            p in principal.permissions for p in ("agent:kb:read", "agent:job:read", "agent:audit:read")
        ):
            raise DomainError("FORBIDDEN", "当前员工没有事件读取权限", 403)
        svc = services(request)
        cursor = int(after or last_event_id or "0")

        initial, watermark = await staff_event_batch(svc, cursor, principal, conversation_id)
        from app.application.governance import read_policy

        async with svc.db.transaction() as conn:
            heartbeat_seconds = (await read_policy(conn))["value"]["heartbeat_seconds"]

        async def stream():
            nonlocal cursor, watermark
            pending = initial
            checked = heartbeat = time.monotonic()
            notification = 0
            current = principal
            try:
                while not await request.is_disconnected():
                    if time.monotonic() - checked >= 10:
                        current = (
                            await svc.extensions.staff_identity.require().authenticate(
                                request.headers["Authorization"]
                            )
                        ).principal
                        require_permission(current, "agent:events:subscribe")
                        checked = time.monotonic()
                    for event in pending:
                        payload = {
                            "event_id": str(event["event_id"]),
                            "type": event["event_type"],
                            "conversation_id": str(event["conversation_id"])
                            if event["conversation_id"]
                            else None,
                            "conversation_version": event["conversation_version"],
                            "control_epoch": event["control_epoch"],
                            "occurred_at": event["created_at"],
                            "payload": event["payload"],
                        }
                        yield f"id: {event['event_id']}\nevent: {event['event_type']}\ndata: {encoded(payload)}\n\n"
                    cursor = watermark
                    if time.monotonic() - heartbeat >= heartbeat_seconds:
                        yield ": heartbeat\n\n"
                        heartbeat = time.monotonic()
                    notification = await svc.notifications.wait(notification)
                    pending, watermark = await staff_event_batch(svc, cursor, current, conversation_id)
            except DomainError as error:
                observe_stream_domain_end(request, "STAFF_EVENTS", error)
                return

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
