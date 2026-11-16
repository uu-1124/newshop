from datetime import datetime
from uuid import UUID

from fastapi import Depends, Query, Request
from pydantic import Field

from app.api.admin import staff
from app.api.dependencies import idempotency, ok, services
from app.api.dto import Input
from app.application.action_recovery import ActionRecovery
from app.application.governance import PublicPolicy, UsageSettlement


class SettingsInput(Input):
    value: PublicPolicy
    expected_version: int = Field(ge=1, strict=True)
    preview_job_id: UUID | None = None


class RetentionPreviewInput(Input):
    proposed_settings: PublicPolicy


class RetentionExecuteInput(Input):
    preview_job_id: UUID
    expected_settings_version: int = Field(ge=1, strict=True)


class RecoveryInput(Input):
    expected_version: int = Field(ge=1, strict=True)
    expected_dispatch_version: int = Field(ge=1, strict=True)
    reason: str = Field(min_length=1, max_length=500)


def register_governance(app):
    base = "/mall-agent/v1/admin"

    @app.get(base + "/actions/unresolved", operation_id="listUnresolvedActions")
    async def unresolved_actions(
        request: Request,
        before: UUID | None = None,
        limit: int = Query(20, ge=1, le=100),
        principal=Depends(staff),
    ):
        svc = services(request)
        svc.extensions.actions.require()
        return ok(
            request,
            await ActionRecovery(svc.db, svc.conversations.settings).listing(principal, before, limit),
        )

    @app.post(
        base + "/actions/{action_id}/reconcile", status_code=202, operation_id="resumeActionReconciliation"
    )
    async def resume_action(
        action_id: UUID,
        body: RecoveryInput,
        request: Request,
        principal=Depends(staff),
        key=Depends(idempotency),
    ):
        svc = services(request)
        svc.extensions.actions.require()
        return ok(
            request,
            await ActionRecovery(svc.db, svc.conversations.settings).resume(
                principal, action_id, key, body.model_dump(mode="json")
            ),
        )

    @app.post(base + "/retention/preview", status_code=202, operation_id="previewRetention")
    async def preview_retention(
        body: RetentionPreviewInput, request: Request, principal=Depends(staff), key=Depends(idempotency)
    ):
        return ok(
            request,
            await services(request)
            .extensions.retention.require()
            .preview(principal, key, body.proposed_settings.model_dump(mode="json")),
        )

    @app.post(base + "/retention/execute", status_code=202, operation_id="executeRetention")
    async def execute_retention(
        body: RetentionExecuteInput, request: Request, principal=Depends(staff), key=Depends(idempotency)
    ):
        return ok(
            request,
            await services(request)
            .extensions.retention.require()
            .execute(principal, key, body.preview_job_id, body.expected_settings_version),
        )

    @app.get(base + "/model-usage/unknown", operation_id="listUnknownModelUsage")
    async def unknown_usage(
        request: Request,
        before: UUID | None = None,
        limit: int = Query(20, ge=1, le=100),
        principal=Depends(staff),
    ):
        return ok(
            request,
            await services(request).extensions.governance.require().unknown_usage(principal, before, limit),
        )

    @app.post(base + "/model-usage/{call_id}/reconcile", operation_id="reconcileModelUsage")
    async def reconcile_usage(
        call_id: UUID,
        body: UsageSettlement,
        request: Request,
        principal=Depends(staff),
        key=Depends(idempotency),
    ):
        return ok(
            request,
            await services(request)
            .extensions.governance.require()
            .reconcile_usage(principal, call_id, key, body.model_dump(mode="json")),
        )

    @app.get(base + "/settings", operation_id="getSettings")
    async def settings(request: Request, principal=Depends(staff)):
        return ok(request, await services(request).extensions.governance.require().settings(principal))

    @app.put(base + "/settings", operation_id="updateSettings")
    async def update(
        body: SettingsInput, request: Request, principal=Depends(staff), key=Depends(idempotency)
    ):
        return ok(
            request,
            await services(request)
            .extensions.governance.require()
            .update(
                principal,
                key,
                body.value.model_dump(mode="json"),
                body.expected_version,
                body.preview_job_id,
            ),
        )

    @app.get(base + "/statistics", operation_id="getStatistics")
    async def statistics(
        request: Request,
        start: datetime = Query(alias="from"),
        end: datetime = Query(alias="to"),
        capability: str | None = None,
        principal=Depends(staff),
    ):
        return ok(
            request,
            await services(request)
            .extensions.governance.require()
            .statistics(principal, start, end, capability),
        )
