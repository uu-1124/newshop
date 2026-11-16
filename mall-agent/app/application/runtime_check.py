"""A real, non-mutating diagnostic job used to verify the generic task runtime."""

from pydantic import BaseModel, ConfigDict

from app.application.jobs import JobHandlers
from app.domain.capabilities import JobType
from app.domain.identity import DomainError
from app.infrastructure.postgres.database import one, rows


class CheckParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "runtime-check-v1"


def runtime_handlers(db, settings):
    from app.application.conversation_cleanup import register_conversation_cleanup
    from app.infrastructure.model_compatibility import register_model_checks

    registry = JobHandlers()
    retention = None
    if settings.retention_enabled:
        from app.application.retention import register_retention

        retention = register_retention(registry, db, settings)
    register_conversation_cleanup(registry, db, settings, retention)
    register_model_checks(registry, db, settings)
    if settings.backups_enabled:
        from app.application.backups import register_backups

        register_backups(registry)

    def validate(parameters):
        try:
            model = CheckParameters.model_validate(parameters)
            if model.schema_version != "runtime-check-v1":
                raise ValueError()
            return model.model_dump()
        except ValueError:
            raise DomainError("VALIDATION_ERROR", "运行检查参数无效", 422) from None

    async def check(lease):
        async with db.transaction(snapshot=True) as conn:
            version = await one(conn, "SELECT version_num FROM alembic_version")
            workers = await rows(
                conn,
                """SELECT DISTINCT process_kind FROM ag_worker_heartbeat
                WHERE last_seen_at>now()-interval '20 seconds' AND state<>'STOPPED' ORDER BY process_kind""",
            )
        if version["version_num"] != "0011":
            raise DomainError("MIGRATION_REQUIRED", "迁移版本不匹配", 503)
        return {
            "schema_version": "runtime-check-v1",
            "migration": version["version_num"],
            "workers": [item["process_kind"] for item in workers],
            "build": settings.build,
        }

    registry.register(JobType.RUNTIME_CHECK, check, validator=validate)
    return registry
