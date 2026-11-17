import time
from uuid import UUID

from fastapi import Depends, Header, Query, Request
from fastapi.responses import Response, StreamingResponse
from python_multipart.exceptions import MultipartParseError
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from app.api.dependencies import idempotency, member, ok, services
from app.api.dto import (
    ConversationCreate,
    ConversationRename,
    FeedbackInput,
    MessageInput,
    ReadInput,
    SpeechInput,
    TranscriptionInput,
    VersionInput,
)
from app.application.attachments import MAX_IMAGE_BYTES
from app.application.conversations import owned
from app.domain.identity import DomainError
from app.infrastructure.postgres.database import encoded, one, rows


def register_member_routes(app, settings):
    prefix = "/mall-agent/v1/member"

    @app.post(prefix + "/audio/transcriptions", operation_id="transcribeAudio")
    async def transcribe(
        request: Request, body: TranscriptionInput, principal=Depends(member), key=Depends(idempotency)
    ):
        return ok(request, await services(request).media.transcribe(principal, body.attachment_id, key))

    @app.post(prefix + "/messages/{message_id}/speech", operation_id="synthesizeSpeech")
    async def speech(
        message_id: UUID,
        request: Request,
        body: SpeechInput,
        principal=Depends(member),
        key=Depends(idempotency),
    ):
        return ok(
            request,
            await services(request).media.speech(
                principal, message_id, key, body.model_dump(exclude_none=True)
            ),
        )

    @app.get(prefix + "/messages/{message_id}/feedback", operation_id="readFeedback")
    async def read_feedback(message_id: UUID, request: Request, principal=Depends(member)):
        return ok(request, await services(request).feedback.get(principal, message_id))

    @app.post(prefix + "/messages/{message_id}/feedback", operation_id="submitFeedback")
    async def feedback(
        message_id: UUID,
        request: Request,
        body: FeedbackInput,
        principal=Depends(member),
        key=Depends(idempotency),
    ):
        return ok(
            request,
            await services(request).feedback.submit(principal, message_id, key, body.model_dump(mode="json")),
        )

    @app.post(prefix + "/attachments", status_code=201, operation_id="uploadMemberAttachment")
    async def upload(request: Request, principal=Depends(member), key=Depends(idempotency)):
        services(request).attachments.require_enabled()
        content_type = request.headers.get("content-type", "")
        if not content_type.lower().startswith("multipart/form-data;"):
            raise DomainError("UNSUPPORTED_MEDIA_TYPE", "请使用附件上传表单", 415)
        declared = request.headers.get("content-length")
        limit = MAX_IMAGE_BYTES + 65536
        if declared and (not declared.isdigit() or int(declared) > limit):
            raise DomainError("ATTACHMENT_TOO_LARGE", "附件大小不能超过5MB", 413)

        async def limited_stream():
            total = 0
            async for chunk in request.stream():
                total += len(chunk)
                if total > limit:
                    raise DomainError("ATTACHMENT_TOO_LARGE", "附件大小不能超过5MB", 413)
                yield chunk

        parser = MultiPartParser(
            request.headers, limited_stream(), max_files=1, max_fields=1, max_part_size=2048
        )
        form = None
        try:
            form = await parser.parse()
            if (
                set(form) != {"purpose", "file"}
                or len(form.getlist("purpose")) != 1
                or len(form.getlist("file")) != 1
            ):
                raise DomainError("VALIDATION_ERROR", "上传表单字段不符合约定", 422)
            file, purpose = form["file"], form["purpose"]
            if not isinstance(file, UploadFile) or not isinstance(purpose, str):
                raise DomainError("VALIDATION_ERROR", "上传表单字段不符合约定", 422)
            raw = await file.read(MAX_IMAGE_BYTES + 1)
            if purpose == "RECORDING":
                return ok(
                    request,
                    await services(request).media.upload(principal, key, file.filename or "录音.wav", raw),
                )
            return ok(
                request,
                await services(request).attachments.upload(
                    principal, key, purpose, file.filename or "图片", raw
                ),
            )
        except (MultiPartException, MultipartParseError):
            raise DomainError("VALIDATION_ERROR", "图片上传表单无法解析", 422) from None
        finally:
            if form:
                await form.close()
            else:
                for staged in parser._files_to_close_on_error:
                    staged.close()

    @app.get(prefix + "/attachments/{attachment_id}/content", operation_id="readMemberAttachment")
    async def attachment_content(attachment_id: UUID, request: Request, principal=Depends(member)):
        async with services(request).db.transaction() as conn:
            item = await one(conn, "SELECT purpose FROM ag_attachment WHERE id=:id", id=attachment_id)
        if item and item["purpose"] in ("RECORDING", "SPEECH"):
            content = await services(request).media.read(principal, attachment_id)
            return Response(
                content,
                media_type="audio/wav",
                headers={
                    "Cache-Control": "private, no-store",
                    "X-Content-Type-Options": "nosniff",
                    "Content-Disposition": 'inline; filename="audio.wav"',
                },
            )
        content = await services(request).attachments.read(principal, attachment_id)
        return Response(
            content,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": 'inline; filename="image.jpg"',
            },
        )

    @app.get(prefix + "/me", operation_id="memberMe")
    async def me(request: Request, principal=Depends(member)):
        return ok(
            request,
            {
                "id": str(principal.id),
                "kind": principal.kind,
                "display_name": principal.display_name,
                "permissions": [],
                "token_valid_until": None,
            },
        )

    @app.post(prefix + "/conversations", status_code=201, operation_id="createConversation")
    async def create(
        request: Request, body: ConversationCreate, principal=Depends(member), key=Depends(idempotency)
    ):
        return ok(
            request,
            await services(request).conversations.create(
                principal,
                key,
                body.model_dump(mode="json", exclude_none=True),
                request.headers["Authorization"],
            ),
        )

    @app.get(prefix + "/conversations", operation_id="listConversations")
    async def listing(
        request: Request,
        principal=Depends(member),
        limit: int = Query(20, ge=1, le=100),
        cursor: str | None = Query(None, max_length=2048),
        query: str = Query("", max_length=100),
    ):
        return ok(request, await services(request).conversations.list(principal, limit, cursor, query))

    @app.get(prefix + "/conversations/{conversation_id}", operation_id="getConversation")
    async def snapshot(conversation_id: UUID, request: Request, principal=Depends(member)):
        return ok(request, await services(request).conversations.snapshot(principal, conversation_id))

    @app.patch(prefix + "/conversations/{conversation_id}", operation_id="renameConversation")
    async def rename(
        conversation_id: UUID,
        request: Request,
        body: ConversationRename,
        principal=Depends(member),
        key=Depends(idempotency),
    ):
        return ok(
            request,
            await services(request).conversations.rename(
                principal, conversation_id, key, body.model_dump(mode="json")
            ),
        )

    @app.delete(prefix + "/conversations/{conversation_id}", operation_id="deleteConversation")
    async def delete(
        conversation_id: UUID,
        request: Request,
        body: VersionInput,
        principal=Depends(member),
        key=Depends(idempotency),
    ):
        return ok(
            request,
            await services(request).conversations.delete(
                principal, conversation_id, key, body.model_dump(mode="json")
            ),
        )

    @app.post(prefix + "/conversations/{conversation_id}/read", operation_id="updateReadCursor")
    async def mark_read(
        conversation_id: UUID,
        request: Request,
        body: ReadInput,
        principal=Depends(member),
        key=Depends(idempotency),
    ):
        return ok(
            request,
            await services(request).conversations.mark_read(
                principal, conversation_id, key, body.model_dump(mode="json")
            ),
        )

    @app.post(
        prefix + "/conversations/{conversation_id}/messages",
        status_code=202,
        operation_id="sendMemberMessage",
    )
    async def send(
        conversation_id: UUID,
        request: Request,
        body: MessageInput,
        principal=Depends(member),
        key=Depends(idempotency),
    ):
        return ok(
            request,
            await services(request).conversations.send(
                principal,
                request.headers["Authorization"],
                conversation_id,
                key,
                body.model_dump(mode="json"),
            ),
        )

    @app.get(prefix + "/conversations/{conversation_id}/messages", operation_id="listMessages")
    async def messages(
        conversation_id: UUID,
        request: Request,
        principal=Depends(member),
        limit: int = Query(20, ge=1, le=100),
        cursor: str | None = Query(None, max_length=2048),
        before_seq: str | None = Query(None, pattern=r"^[0-9]{1,19}$"),
    ):
        if cursor and before_seq:
            raise DomainError("INVALID_CURSOR", "不能同时指定两种分页位置", 400)
        svc = services(request)
        before = int(before_seq) if before_seq else 9223372036854775807
        if cursor:
            before = svc.cursors.decode(cursor, f"messages:{principal.id}:{conversation_id}")[0]
        return ok(request, await svc.conversations.messages(principal, conversation_id, limit, before))

    @app.get(prefix + "/runs/{run_id}", operation_id="getRun")
    async def run(run_id: UUID, request: Request, principal=Depends(member)):
        return ok(request, await services(request).runs.get(principal, run_id))

    @app.post(prefix + "/runs/{run_id}/stop", status_code=202, operation_id="stopRun")
    async def stop(
        run_id: UUID,
        request: Request,
        body: VersionInput,
        principal=Depends(member),
        key=Depends(idempotency),
    ):
        return ok(request, await services(request).runs.stop(principal, run_id, key, body.model_dump()))

    @app.post(prefix + "/runs/{run_id}/retry", status_code=202, operation_id="retryRun")
    async def retry(
        run_id: UUID,
        request: Request,
        body: VersionInput,
        principal=Depends(member),
        key=Depends(idempotency),
    ):
        return ok(
            request,
            await services(request).conversations.retry_run(
                principal, request.headers["Authorization"], run_id, key, body.model_dump(mode="json")
            ),
        )

    @app.get(prefix + "/conversations/{conversation_id}/events", operation_id="memberEvents")
    async def events(
        conversation_id: UUID,
        request: Request,
        principal=Depends(member),
        after: str | None = Query(None, pattern=r"^[0-9]{1,19}$"),
        last_event_id: str | None = Header(None, pattern=r"^[0-9]{1,19}$"),
    ):
        if after and last_event_id and after != last_event_id:
            raise DomainError("CURSOR_MISMATCH", "重连游标不一致", 400)
        cursor = int(after or last_event_id or "0")
        svc = services(request)

        async def batch(position):
            async with svc.db.transaction(snapshot=True) as conn:
                await owned(conn, conversation_id, principal.id)
                clock = await one(conn, "SELECT * FROM ag_event_clock WHERE singleton_id=1")
                if position < clock["replay_floor"]:
                    raise DomainError("EVENT_CURSOR_EXPIRED", "请重新同步会话", 410)
                if position > clock["last_event_id"]:
                    raise DomainError("INVALID_CURSOR", "事件游标超过当前水位", 400)
                candidates = await rows(
                    conn,
                    """SELECT * FROM ag_event_outbox WHERE conversation_id=:conversation_id
                    AND visibility='CONVERSATION' AND event_id>:after ORDER BY event_id LIMIT 100""",
                    conversation_id=conversation_id,
                    after=position,
                )
                from app.application.expiry import filter_events

                return await filter_events(conn, candidates), (
                    candidates[-1]["event_id"] if candidates else position
                )

        initial, watermark = await batch(cursor)
        from app.application.governance import read_policy

        async with svc.db.transaction() as conn:
            heartbeat_seconds = (await read_policy(conn))["value"]["heartbeat_seconds"]

        async def stream():
            nonlocal cursor, watermark
            pending, checked, heartbeat = initial, time.monotonic(), time.monotonic()
            notification_version = 0
            try:
                while not await request.is_disconnected():
                    if time.monotonic() - checked >= 30:
                        await svc.bridge.identity(request.headers["Authorization"])
                        checked = time.monotonic()
                    for event in pending:
                        cursor = event["event_id"]
                        envelope = {
                            "event_id": str(cursor),
                            "type": event["event_type"],
                            "conversation_id": str(conversation_id),
                            "conversation_version": event["conversation_version"],
                            "control_epoch": event["control_epoch"],
                            "occurred_at": event["created_at"],
                            "payload": event["payload"],
                        }
                        yield f"id: {cursor}\nevent: {event['event_type']}\ndata: {encoded(envelope)}\n\n"
                    cursor = watermark
                    if time.monotonic() - heartbeat >= heartbeat_seconds:
                        yield ": heartbeat\n\n"
                        heartbeat = time.monotonic()
                    notification_version = await svc.notifications.wait(notification_version)
                    pending, watermark = await batch(cursor)
            except DomainError:
                # Closing forces a fresh authenticated HTTP request/snapshot; no hidden infinite stream.
                return

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
