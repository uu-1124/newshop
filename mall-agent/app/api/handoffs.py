from typing import Literal
from uuid import UUID

from fastapi import Depends, Query, Request

from app.api.admin import staff
from app.api.dependencies import idempotency, member, ok, services
from app.api.dto import VersionInput
from app.api.handoff_dto import (
    CloseInput,
    Empty,
    HandoffInput,
    NoteInput,
    PresenceInput,
    StaffMessageInput,
    StaffProfileInput,
    TransferInput,
    WaitingAIInput,
)


def service(request):
    return services(request).extensions.handoffs.require()


def register_handoff_routes(app):
    member_base = "/mall-agent/v1/member/conversations/{conversation_id}"
    admin = "/mall-agent/v1/admin"

    @app.post(member_base + "/handoff", status_code=201, operation_id="requestHandoff")
    async def request_handoff(
        conversation_id: UUID,
        body: HandoffInput,
        request: Request,
        principal=Depends(member),
        key=Depends(idempotency),
    ):
        return ok(
            request,
            await service(request).request(principal, conversation_id, key, body.model_dump(mode="json")),
        )

    @app.delete(member_base + "/handoff", operation_id="withdrawHandoff")
    async def withdraw(
        conversation_id: UUID,
        body: VersionInput,
        request: Request,
        principal=Depends(member),
        key=Depends(idempotency),
    ):
        return ok(
            request,
            await service(request).waiting(
                principal, conversation_id, key, body.model_dump(mode="json"), withdraw=True
            ),
        )

    @app.post(member_base + "/waiting-ai-mode", operation_id="setWaitingAI")
    async def waiting(
        conversation_id: UUID,
        body: WaitingAIInput,
        request: Request,
        principal=Depends(member),
        key=Depends(idempotency),
    ):
        return ok(
            request,
            await service(request).waiting(principal, conversation_id, key, body.model_dump(mode="json")),
        )

    @app.get(admin + "/conversations", operation_id="adminListConversations")
    async def conversations(
        request: Request,
        bucket: Literal["WAITING", "MINE", "HISTORY"] = "WAITING",
        query: str = Query("", max_length=100),
        limit: int = Query(20, ge=1, le=100),
        cursor: str | None = Query(None, max_length=2048),
        principal=Depends(staff),
    ):
        return ok(request, await service(request).list(principal, bucket, query, limit, cursor))

    @app.get(admin + "/conversations/{conversation_id}", operation_id="adminConversationSnapshot")
    async def snapshot(conversation_id: UUID, request: Request, principal=Depends(staff)):
        return ok(request, await service(request).snapshot(principal, conversation_id))

    @app.get(admin + "/conversations/{conversation_id}/messages", operation_id="adminListMessages")
    async def history(
        conversation_id: UUID,
        request: Request,
        limit: int = Query(20, ge=1, le=100),
        cursor: str | None = Query(None, max_length=2048),
        principal=Depends(staff),
    ):
        return ok(request, await service(request).history(principal, conversation_id, limit, cursor))

    @app.post(
        admin + "/conversations/{conversation_id}/messages", status_code=201, operation_id="sendStaffMessage"
    )
    async def send(
        conversation_id: UUID,
        body: StaffMessageInput,
        request: Request,
        principal=Depends(staff),
        key=Depends(idempotency),
    ):
        return ok(
            request,
            await service(request).send(
                principal,
                conversation_id,
                key,
                body.model_dump(mode="json"),
                token=request.headers.get("authorization", ""),
            ),
        )

    @app.get(admin + "/conversations/{conversation_id}/context", operation_id="getStaffBusinessContext")
    async def context(
        conversation_id: UUID,
        request: Request,
        product_id: str | None = Query(None, pattern=r"^[1-9][0-9]{0,18}$"),
        order_id: str | None = Query(None, pattern=r"^[1-9][0-9]{0,18}$"),
        principal=Depends(staff),
    ):
        cards = ([{"kind": "PRODUCT", "id": product_id}] if product_id else []) + (
            [{"kind": "ORDER", "id": order_id}] if order_id else []
        )
        result, _ = await service(request).context(
            principal, conversation_id, request.headers.get("authorization", ""), cards or None
        )
        return ok(request, result)

    @app.get(admin + "/conversations/{conversation_id}/notes", operation_id="listStaffNotes")
    async def notes(
        conversation_id: UUID,
        request: Request,
        limit: int = Query(20, ge=1, le=100),
        cursor: str | None = Query(None, max_length=2048),
        principal=Depends(staff),
    ):
        return ok(
            request, await service(request).history(principal, conversation_id, limit, cursor, notes=True)
        )

    @app.post(
        admin + "/conversations/{conversation_id}/notes", status_code=201, operation_id="createStaffNote"
    )
    async def note(
        conversation_id: UUID,
        body: NoteInput,
        request: Request,
        principal=Depends(staff),
        key=Depends(idempotency),
    ):
        return ok(
            request,
            await service(request).send(
                principal, conversation_id, key, body.model_dump(mode="json"), note=True
            ),
        )

    @app.get(admin + "/handoffs", operation_id="listHandoffs")
    async def queue(
        request: Request,
        limit: int = Query(20, ge=1, le=100),
        cursor: str | None = Query(None, max_length=2048),
        principal=Depends(staff),
    ):
        return ok(request, await service(request).list(principal, "WAITING", "", limit, cursor, queue=True))

    @app.post(admin + "/handoffs/{handoff_id}/claim", operation_id="claimHandoff")
    async def claim(
        handoff_id: UUID,
        body: VersionInput,
        request: Request,
        principal=Depends(staff),
        key=Depends(idempotency),
    ):
        return ok(
            request,
            await service(request).command(principal, handoff_id, key, body.model_dump(mode="json"), "claim"),
        )

    @app.post(admin + "/handoffs/{handoff_id}/transfer", operation_id="transferHandoff")
    async def transfer(
        handoff_id: UUID,
        body: TransferInput,
        request: Request,
        principal=Depends(staff),
        key=Depends(idempotency),
    ):
        return ok(
            request,
            await service(request).command(
                principal,
                handoff_id,
                key,
                body.model_dump(mode="json"),
                "transfer",
                request.headers.get("authorization", ""),
            ),
        )

    @app.post(admin + "/handoffs/{handoff_id}/close", operation_id="closeHandoff")
    async def close(
        handoff_id: UUID,
        body: CloseInput,
        request: Request,
        principal=Depends(staff),
        key=Depends(idempotency),
    ):
        return ok(
            request,
            await service(request).command(principal, handoff_id, key, body.model_dump(mode="json"), "close"),
        )

    @app.get(admin + "/staff", operation_id="listStaff")
    async def staff_list(
        request: Request,
        limit: int = Query(20, ge=1, le=100),
        cursor: str | None = Query(None, max_length=2048),
        principal=Depends(staff),
    ):
        return ok(request, await service(request).list_staff(principal, limit, cursor))

    @app.put(admin + "/presence", operation_id="setPresence")
    async def presence(
        body: PresenceInput, request: Request, principal=Depends(staff), key=Depends(idempotency)
    ):
        return ok(request, await service(request).set_presence(principal, key, body.model_dump(mode="json")))

    @app.post(admin + "/presence/heartbeat", operation_id="presenceHeartbeat")
    async def heartbeat(body: Empty, request: Request, principal=Depends(staff), key=Depends(idempotency)):
        return ok(request, await service(request).set_presence(principal, key, {}, heartbeat=True))

    @app.patch(admin + "/staff/{staff_id}", operation_id="updateStaffProfile")
    async def profile(
        staff_id: str,
        body: StaffProfileInput,
        request: Request,
        principal=Depends(staff),
        key=Depends(idempotency),
    ):
        from app.domain.identity import DomainError

        if not staff_id.isdigit() or not 0 < int(staff_id) <= 9223372036854775807:
            raise DomainError("VALIDATION_ERROR", "员工编号不正确", 422)
        return ok(
            request,
            await service(request).update_profile(
                principal, int(staff_id), key, body.model_dump(mode="json")
            ),
        )
