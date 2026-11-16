from uuid import UUID

from fastapi import Depends, Header, Query
from starlette.requests import Request

from app.api.dependencies import idempotency, ok, services
from app.api.dto import VersionInput
from app.application.jobs import job_view
from app.infrastructure.postgres.database import one, rows
from app.infrastructure.staff_auth import require_permission


async def staff(request: Request, authorization: str = Header(default="")):
    context = await services(request).extensions.staff_identity.require().authenticate(authorization)
    return context.principal


def register_admin_routes(app):
    prefix = "/mall-agent/v1/admin"

    @app.get(prefix + "/audit-logs", operation_id="listAuditLogs")
    async def audit_logs(
        request: Request,
        principal=Depends(staff),
        limit: int = Query(20, ge=1, le=100),
        cursor: str | None = Query(None, max_length=2048),
        request_id: UUID | None = None,
        resource_id: str = Query("", max_length=120),
    ):
        require_permission(principal, "agent:audit:read")
        svc = services(request)
        scope = f"audit:{principal.id}:{request_id}:{resource_id}:" + ",".join(sorted(principal.permissions))
        position = svc.cursors.decode(cursor, scope) if cursor else None
        async with svc.db.transaction(snapshot=True) as conn:
            records = await rows(
                conn,
                """SELECT * FROM ag_audit_log WHERE
                ((actor_kind='STAFF' AND actor_id=:actor) OR :broad) AND (CAST(:request AS uuid) IS NULL OR request_id=CAST(:request AS uuid))
                AND (:resource='' OR resource_id=:resource)
                AND (CAST(:date AS timestamptz) IS NULL OR (created_at,id)<(CAST(:date AS timestamptz),CAST(:id AS uuid)))
                ORDER BY created_at DESC,id DESC LIMIT :limit""",
                actor=principal.id,
                broad="agent:conversation:history:all" in principal.permissions,
                request=str(request_id) if request_id else None,
                resource=resource_id,
                date=position[0] if position else None,
                id=position[1] if position else None,
                limit=limit + 1,
            )
        more = len(records) > limit
        page = records[:limit]
        return ok(
            request,
            {
                "items": [
                    {
                        "id": str(row["id"]),
                        "actor_kind": row["actor_kind"],
                        "actor_id": str(row["actor_id"]) if row["actor_id"] and row["actor_id"] > 0 else None,
                        "operation": row["operation"],
                        "resource_type": row["resource_type"],
                        "resource_id": row["resource_id"],
                        "request_id": str(row["request_id"]),
                        "outcome": row["outcome"],
                        "summary": f"{row['operation']} / {row['outcome']}",
                        "created_at": row["created_at"],
                    }
                    for row in page
                ],
                "has_more": more,
                "next_cursor": svc.cursors.encode(
                    [page[-1]["created_at"].isoformat(), str(page[-1]["id"])], scope
                )
                if more
                else None,
            },
        )

    @app.get(prefix + "/me", operation_id="adminMe")
    async def me(request: Request, principal=Depends(staff)):
        svc = services(request)
        if svc.extensions.handoffs.bound and "agent:presence:write" in principal.permissions:
            await svc.extensions.handoffs.require().ensure_profile(principal)
        return ok(
            request,
            {
                "id": str(principal.id),
                "kind": "STAFF",
                "display_name": principal.display_name,
                "permissions": sorted(principal.permissions),
                "token_valid_until": None,
            },
        )

    @app.get(prefix + "/jobs", operation_id="listJobs")
    async def listing(
        request: Request,
        limit: int = Query(20, ge=1, le=100),
        cursor: str | None = Query(None, max_length=2048),
        principal=Depends(staff),
    ):
        require_permission(principal, "agent:job:read")
        svc = services(request)
        scope = f"jobs:{principal.id}:" + ",".join(sorted(principal.permissions))
        position = svc.cursors.decode(cursor, scope) if cursor else None
        kinds = [
            kind.value
            for kind, permission in svc.job_handlers.permissions.items()
            if permission in principal.permissions
        ]
        async with svc.db.transaction(snapshot=True) as conn:
            clock = await one(conn, "SELECT last_event_id FROM ag_event_clock WHERE singleton_id=1")
            items = await rows(
                conn,
                """SELECT * FROM ag_job WHERE (created_by=:owner OR created_by IS NULL)
                AND job_type=ANY(:types) AND (CAST(:date AS timestamptz) IS NULL OR (created_at,id)<(CAST(:date AS timestamptz),CAST(:id AS uuid)))
                ORDER BY created_at DESC,id DESC LIMIT :limit""",
                owner=principal.id,
                types=kinds,
                date=position[0] if position else None,
                id=position[1] if position else None,
                limit=limit + 1,
            )
        more = len(items) > limit
        items = items[:limit]
        next_cursor = (
            svc.cursors.encode([items[-1]["created_at"].isoformat(), str(items[-1]["id"])], scope)
            if more
            else None
        )
        return ok(
            request,
            {
                "items": [job_view(item) for item in items],
                "next_cursor": next_cursor,
                "has_more": more,
                "event_cursor": str(clock["last_event_id"]),
            },
        )

    @app.get(prefix + "/jobs/{job_id}", operation_id="getJob")
    async def get(job_id: UUID, request: Request, principal=Depends(staff)):
        return ok(request, await services(request).jobs.get(principal, job_id))

    @app.post(prefix + "/jobs/{job_id}/retry", status_code=202, operation_id="retryJob")
    async def retry(
        job_id: UUID, body: VersionInput, request: Request, principal=Depends(staff), key=Depends(idempotency)
    ):
        return ok(request, await services(request).jobs.retry(principal, job_id, body.expected_version, key))
