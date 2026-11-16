from uuid import UUID

from fastapi import Depends, Query, Request
from pydantic import Field

from app.api.admin import staff
from app.api.dependencies import idempotency, ok, services
from app.api.dto import Input
from app.application.backups import BackupPolicy


class VerifyInput(Input):
    backup_record_id: UUID
    note: str = Field(min_length=1, max_length=500)


class CreateInput(Input):
    expected_policy_version: int = Field(ge=1, strict=True)
    maintenance_confirmed: bool
    note: str = Field(min_length=1, max_length=500)


class PolicyInput(Input):
    value: BackupPolicy
    expected_version: int = Field(ge=1, strict=True)


def register_backup_routes(app, settings):
    if not settings.backups_enabled:
        return
    base = "/mall-agent/v1/admin/backups"

    @app.get(base, operation_id="listBackups")
    async def listing(
        request: Request,
        before: UUID | None = None,
        limit: int = Query(20, ge=1, le=100),
        principal=Depends(staff),
    ):
        return ok(
            request, await services(request).extensions.backups.require().listing(principal, before, limit)
        )

    @app.post(base, operation_id="createBackup", status_code=202)
    async def create(body: CreateInput, request: Request, principal=Depends(staff), key=Depends(idempotency)):
        return ok(
            request,
            await services(request)
            .extensions.backups.require()
            .request(
                principal,
                key,
                "CREATE",
                None,
                body.note,
                body.expected_policy_version,
                body.maintenance_confirmed,
            ),
        )

    @app.put(base + "/policy", operation_id="updateBackupPolicy")
    async def policy(body: PolicyInput, request: Request, principal=Depends(staff), key=Depends(idempotency)):
        return ok(
            request,
            await services(request)
            .extensions.backups.require()
            .update_policy(principal, key, body.value.model_dump(mode="json"), body.expected_version),
        )

    @app.post(base + "/verify", operation_id="verifyBackup", status_code=202)
    async def verify(body: VerifyInput, request: Request, principal=Depends(staff), key=Depends(idempotency)):
        return ok(
            request,
            await services(request)
            .extensions.backups.require()
            .request(principal, key, "VERIFY", body.backup_record_id, body.note),
        )
