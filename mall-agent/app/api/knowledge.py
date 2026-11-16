from uuid import UUID

from fastapi import Depends, Query, Request
from fastapi.responses import Response
from python_multipart.exceptions import MultipartParseError
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from app.api.admin import staff
from app.api.dependencies import idempotency, member, ok, services
from app.api.dto import VersionInput
from app.api.knowledge_dto import (
    KBEnabledInput,
    KBInput,
    KBPreviewUpdate,
    KBPublishInput,
    KBUpdate,
    KBVersionInput,
)
from app.domain.capabilities import JobType
from app.domain.identity import DomainError
from app.infrastructure.knowledge_files import MAX_BYTES
from app.infrastructure.staff_auth import require_permission


def knowledge(request):
    return services(request).extensions.knowledge.require()


def register_knowledge_routes(app):
    prefix = "/mall-agent/v1/admin"

    @app.get(prefix + "/kb/documents", operation_id="listKBDocuments")
    async def listing(
        request: Request,
        principal=Depends(staff),
        limit: int = Query(20, ge=1, le=100),
        cursor: str | None = Query(None, max_length=2048),
        query: str = Query("", max_length=200),
        category: str = Query("", max_length=80),
    ):
        return ok(request, await knowledge(request).listing(principal, limit, cursor, query, category))

    @app.post(prefix + "/kb/documents", status_code=201, operation_id="createKBDocument")
    async def create(body: KBInput, request: Request, principal=Depends(staff), key=Depends(idempotency)):
        return ok(request, await knowledge(request).create(principal, key, body.model_dump(mode="json")))

    @app.get(prefix + "/kb/documents/{document_id}", operation_id="getKBDocument")
    async def get_document(document_id: UUID, request: Request, principal=Depends(staff)):
        return ok(request, await knowledge(request).get_document(principal, document_id))

    @app.patch(prefix + "/kb/documents/{document_id}", operation_id="updateKBDocument")
    async def update(
        document_id: UUID,
        body: KBUpdate,
        request: Request,
        principal=Depends(staff),
        key=Depends(idempotency),
    ):
        return ok(
            request,
            await knowledge(request).update(principal, document_id, key, body.model_dump(mode="json")),
        )

    @app.get(prefix + "/kb/documents/{document_id}/versions", operation_id="listKBVersions")
    async def versions(
        document_id: UUID,
        request: Request,
        principal=Depends(staff),
        limit: int = Query(20, ge=1, le=100),
        cursor: str | None = Query(None, max_length=2048),
    ):
        return ok(
            request, await knowledge(request).listing(principal, limit, cursor, document_id=document_id)
        )

    @app.post(
        prefix + "/kb/documents/{document_id}/versions", status_code=201, operation_id="createKBVersion"
    )
    async def create_version(
        document_id: UUID,
        body: KBVersionInput,
        request: Request,
        principal=Depends(staff),
        key=Depends(idempotency),
    ):
        return ok(
            request,
            await knowledge(request).create_version(
                principal, document_id, key, body.model_dump(mode="json")
            ),
        )

    @app.get(prefix + "/kb/versions/{version_id}", operation_id="getKBVersion")
    async def get_version(version_id: UUID, request: Request, principal=Depends(staff)):
        return ok(request, await knowledge(request).get_version(principal, version_id))

    @app.post(prefix + "/kb/versions/{version_id}/parse", status_code=202, operation_id="parseKBVersion")
    async def parse(
        version_id: UUID,
        body: VersionInput,
        request: Request,
        principal=Depends(staff),
        key=Depends(idempotency),
    ):
        return ok(
            request,
            await knowledge(request).enqueue(
                principal, version_id, key, body.expected_version, JobType.KB_PARSE
            ),
        )

    @app.get(prefix + "/kb/versions/{version_id}/preview", operation_id="previewKBVersion")
    async def preview(version_id: UUID, request: Request, principal=Depends(staff)):
        return ok(request, await knowledge(request).preview(principal, version_id))

    @app.patch(prefix + "/kb/versions/{version_id}/preview", operation_id="editKBPreview")
    async def edit_preview(
        version_id: UUID,
        body: KBPreviewUpdate,
        request: Request,
        principal=Depends(staff),
        key=Depends(idempotency),
    ):
        return ok(
            request,
            await knowledge(request).edit_preview(principal, version_id, key, body.model_dump(mode="json")),
        )

    @app.post(prefix + "/kb/versions/{version_id}/build", status_code=202, operation_id="buildKBVersion")
    async def build(
        version_id: UUID,
        body: VersionInput,
        request: Request,
        principal=Depends(staff),
        key=Depends(idempotency),
    ):
        return ok(
            request,
            await knowledge(request).enqueue(
                principal, version_id, key, body.expected_version, JobType.KB_INDEX
            ),
        )

    @app.post(prefix + "/kb/documents/{document_id}/publish", operation_id="publishKBVersion")
    async def publish(
        document_id: UUID,
        body: KBPublishInput,
        request: Request,
        principal=Depends(staff),
        key=Depends(idempotency),
    ):
        return ok(
            request,
            await knowledge(request).publish(principal, document_id, key, body.model_dump(mode="json")),
        )

    @app.put(prefix + "/kb/documents/{document_id}/enabled", operation_id="setKBEnabled")
    async def enabled(
        document_id: UUID,
        body: KBEnabledInput,
        request: Request,
        principal=Depends(staff),
        key=Depends(idempotency),
    ):
        return ok(
            request,
            await knowledge(request).enable(principal, document_id, key, body.model_dump(mode="json")),
        )

    @app.post(prefix + "/attachments", status_code=201, operation_id="uploadStaffAttachment")
    async def upload(request: Request, principal=Depends(staff), key=Depends(idempotency)):
        require_permission(principal, "agent:attachment:upload")
        if not request.headers.get("content-type", "").lower().startswith("multipart/form-data;"):
            raise DomainError("UNSUPPORTED_MEDIA_TYPE", "请使用文件上传表单", 415)
        declared = request.headers.get("content-length")
        limit = MAX_BYTES + 65536
        if declared and (not declared.isdigit() or int(declared) > limit):
            raise DomainError("ATTACHMENT_TOO_LARGE", "知识原件不能超过20MB", 413)

        async def limited_stream():
            total = 0
            async for chunk in request.stream():
                total += len(chunk)
                if total > limit:
                    raise DomainError("ATTACHMENT_TOO_LARGE", "知识原件不能超过20MB", 413)
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
            if purpose == "CHAT_IMAGE":
                return ok(
                    request,
                    await services(request).attachments.upload(
                        principal, key, purpose, file.filename or "图片", await file.read(5 * 1024 * 1024 + 1)
                    ),
                )
            knowledge(request)
            require_permission(principal, "agent:kb:edit")
            return ok(
                request,
                await services(request).knowledge_runtime.files.upload(
                    principal, key, purpose, file.filename or "source.txt", await file.read(MAX_BYTES + 1)
                ),
            )
        except (MultiPartException, MultipartParseError):
            raise DomainError("VALIDATION_ERROR", "知识上传表单无法解析", 422) from None
        finally:
            if form:
                await form.close()
            else:
                for staged in parser._files_to_close_on_error:
                    staged.close()

    @app.get(prefix + "/attachments/{attachment_id}/content", operation_id="readStaffAttachment")
    async def content(attachment_id: UUID, request: Request, principal=Depends(staff)):
        from app.infrastructure.postgres.database import one

        svc = services(request)
        require_permission(principal, "agent:attachment:read")
        async with svc.db.transaction(snapshot=True) as conn:
            item = await one(conn, "SELECT purpose FROM ag_attachment WHERE id=:id", id=attachment_id)
        if item and item["purpose"] == "CHAT_IMAGE":
            raw, mime = await svc.attachments.read(principal, attachment_id), "image/jpeg"
        else:
            knowledge(request)
            raw, mime = await svc.knowledge_runtime.files.read(principal, attachment_id)
        return Response(
            raw,
            media_type=mime,
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": 'attachment; filename="source"',
            },
        )

    @app.get("/mall-agent/v1/member/citations/{version_id}/chunks/{chunk_id}", operation_id="readCitation")
    async def citation(version_id: UUID, chunk_id: UUID, request: Request, principal=Depends(member)):
        return ok(
            request,
            await services(request)
            .extensions.retrieval.require()
            .read_citation(principal, version_id, chunk_id, request.headers["Authorization"]),
        )
