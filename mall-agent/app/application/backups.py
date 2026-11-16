"""Registered backup commands and public state; no paths, secrets or shell inputs."""

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import Field

from app.api.dto import Input
from app.application.commands import staff_dedupe
from app.application.governance import governance_audit
from app.application.jobs import job_view
from app.domain.capabilities import JobType
from app.domain.identity import DomainError
from app.infrastructure.postgres.database import digest, encoded, execute, one, rows
from app.infrastructure.staff_auth import require_permission

POLICY_KEY = "agent-backup-policy-v1"
RECORD_PREFIX = "agent-backup-record:"
STATE_KEY = "agent-backup-executor-v1"


class BackupPolicy(Input):
    enabled: bool = False
    daily_hour_utc: int = Field(default=19, ge=0, le=23, strict=True)
    restore_weekday_utc: int = Field(default=5, ge=0, le=6, strict=True)
    retention_days: int = Field(default=7, ge=7, le=90, strict=True)
    weekly_retention_weeks: int = Field(default=4, ge=1, le=12, strict=True)
    journal_interval_minutes: int = Field(default=15, ge=5, le=60, strict=True)
    note: str = Field(default="演示环境每日一致备份、每周隔离恢复", min_length=1, max_length=500)


class BackupParameters(Input):
    mode: Literal["CREATE", "VERIFY", "RESTORE"]
    backup_record_id: UUID | None = None
    policy_version: int | None = Field(default=None, ge=1, strict=True)
    scheduled: bool = False
    note: str = Field(min_length=1, max_length=500)


def validate_parameters(parameters):
    try:
        value = BackupParameters.model_validate(parameters)
        if (value.mode == "CREATE") == (value.backup_record_id is not None):
            raise ValueError()
        if value.scheduled and value.policy_version is None:
            raise ValueError()
        if not value.note.strip():
            raise ValueError()
        return value.model_dump(mode="json")
    except ValueError:
        raise DomainError("VALIDATION_ERROR", "备份任务只接受已登记编号及固定操作", 422) from None


async def read_backup_policy(conn):
    record = await one(conn, "SELECT value,version FROM ag_setting WHERE key=:key", key=POLICY_KEY)
    return {
        "value": BackupPolicy.model_validate(record["value"]).model_dump(mode="json")
        if record
        else BackupPolicy().model_dump(mode="json"),
        "version": record["version"] if record else 1,
    }


async def put_state(conn, key, value, actor=0):
    await execute(
        conn,
        """INSERT INTO ag_setting(key,value,updated_by) VALUES(:key,CAST(:value AS jsonb),:actor)
        ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_by=EXCLUDED.updated_by,
        version=ag_setting.version+1,updated_at=now()""",
        key=key,
        value=encoded(value),
        actor=actor,
    )


async def backup_alerts(conn):
    policy = await read_backup_policy(conn)
    state = await one(conn, "SELECT value,updated_at FROM ag_setting WHERE key=:key", key=STATE_KEY)
    now, alerts = datetime.now(UTC), []
    if not state or state["updated_at"] < now - timedelta(seconds=90):
        alerts.append(
            {"code": "BACKUP_EXECUTOR_MISSING", "resource": "BACKUP", "severity": "ERROR", "count": 1}
        )
    value = state["value"] if state else {}
    if value.get("last_error"):
        alerts.append(
            {"code": "BACKUP_OPERATION_FAILED", "resource": "BACKUP", "severity": "ERROR", "count": 1}
        )
    if value.get("maintenance_stuck"):
        alerts.append(
            {"code": "MAINTENANCE_REQUIRES_RECOVERY", "resource": "BACKUP", "severity": "ERROR", "count": 1}
        )
    if not policy["value"]["enabled"]:
        return alerts
    value = state["value"] if state else {}
    for field, hours, code in [
        ("last_backup_at", 26, "BACKUP_STALE"),
        ("last_restore_at", 192, "RESTORE_VERIFICATION_STALE"),
        ("last_journal_at", policy["value"]["journal_interval_minutes"] / 60 * 2, "ERASURE_JOURNAL_STALE"),
    ]:
        date = datetime.fromisoformat(value[field]) if value.get(field) else None
        if not date or date < now - timedelta(hours=hours):
            alerts.append({"code": code, "resource": "BACKUP", "severity": "ERROR", "count": 1})
    return alerts


class Backups:
    def __init__(self, db, jobs):
        self.db, self.jobs = db, jobs

    async def listing(self, principal, before=None, limit=20):
        require_permission(principal, "agent:backup:verify")
        async with self.db.transaction(snapshot=True) as conn:
            items = await rows(
                conn,
                """SELECT key,value FROM ag_setting WHERE key LIKE 'agent-backup-record:%'
                AND (CAST(:before AS text) IS NULL OR key<:before) ORDER BY key DESC LIMIT :limit""",
                before=RECORD_PREFIX + str(before) if before else None,
                limit=limit + 1,
            )
            policy = await read_backup_policy(conn)
            state = await one(conn, "SELECT value FROM ag_setting WHERE key=:key", key=STATE_KEY)
            alerts = await backup_alerts(conn)
        more = len(items) > limit
        return {
            "items": [item["value"] for item in items[:limit]],
            "has_more": more,
            "next_before": items[limit - 1]["value"]["id"] if more else None,
            "policy": policy,
            "executor": state["value"] if state else None,
            "alerts": alerts,
        }

    async def update_policy(self, principal, key, value, expected):
        require_permission(principal, "agent:backup:verify")
        require_permission(principal, "agent:settings:write")
        value = BackupPolicy.model_validate(value).model_dump(mode="json")
        body = {"value": value, "expected_version": expected}
        async with self.db.transaction() as conn:
            old = await staff_dedupe(conn, principal, "updateBackupPolicy", key, body)
            if old:
                return old
            await execute(conn, "SELECT pg_advisory_xact_lock(hashtextextended('agent-backup-policy',0))")
            policy = await read_backup_policy(conn)
            if policy["version"] != expected:
                raise DomainError("VERSION_CONFLICT", "备份策略已变化，请刷新", 409)
            await execute(
                conn,
                "INSERT INTO ag_setting(key,value,updated_by) VALUES(:key,CAST(:value AS jsonb),:actor) ON CONFLICT DO NOTHING",
                key=POLICY_KEY,
                value=encoded(policy["value"]),
                actor=principal.id,
            )
            await put_state(conn, POLICY_KEY, value, principal.id)
            result = {"value": value, "version": expected + 1}
            await self.cache(conn, principal, "updateBackupPolicy", key, body, result)
            await governance_audit(
                conn, principal, "updateBackupPolicy", POLICY_KEY, key, {"before": policy, "after": result}
            )
            return result

    async def request(
        self, principal, key, mode, record_id, note, expected_policy=None, maintenance_confirmed=False
    ):
        require_permission(principal, "agent:backup:verify")
        if not note.strip():
            raise DomainError("VALIDATION_ERROR", "请填写操作说明", 422)
        if mode == "CREATE" and not maintenance_confirmed:
            raise DomainError("MAINTENANCE_CONFIRMATION_REQUIRED", "一致备份需要明确确认维护窗口", 409)
        body = {
            "mode": mode,
            "backup_record_id": str(record_id) if record_id else None,
            "note": note,
            "policy_version": expected_policy,
            "maintenance_confirmed": maintenance_confirmed,
        }
        scope = "createBackup" if mode == "CREATE" else "verifyBackup"
        async with self.db.transaction() as conn:
            cached = await staff_dedupe(conn, principal, scope, key, body)
            if cached:
                return cached
            if mode == "CREATE":
                policy = await read_backup_policy(conn)
                if expected_policy != policy["version"]:
                    raise DomainError("VERSION_CONFLICT", "请刷新备份策略", 409)
            else:
                record = await one(
                    conn,
                    "SELECT value FROM ag_setting WHERE key=:key FOR UPDATE",
                    key=RECORD_PREFIX + str(record_id),
                )
                if not record or record["value"].get("state") != "AVAILABLE":
                    raise DomainError("NOT_FOUND", "登记备份不存在或不可用", 404)
            state = await one(conn, "SELECT updated_at FROM ag_setting WHERE key=:key", key=STATE_KEY)
            if not state or state["updated_at"] < datetime.now(UTC) - timedelta(seconds=90):
                raise DomainError("BACKUP_EXECUTOR_UNAVAILABLE", "备份执行器当前不可用", 503)
            params = {
                "mode": mode,
                "backup_record_id": str(record_id) if record_id else None,
                "policy_version": expected_policy,
                "scheduled": False,
                "note": note,
            }
            job = await self.jobs.enqueue_in(
                conn,
                JobType.BACKUP_VERIFY,
                f"backup-command:{principal.id}:{key}",
                params,
                principal.id,
                max_attempts=1,
            )
            result = job_view(job)
            await self.cache(conn, principal, scope, key, body, result)
            await governance_audit(
                conn,
                principal,
                scope,
                str(record_id or job["id"]),
                key,
                {"job_id": str(job["id"]), "mode": mode, "note": note},
            )
            return result

    @staticmethod
    async def cache(conn, principal, scope, key, body, result):
        await execute(
            conn,
            """INSERT INTO ag_request_dedupe(scope,actor_kind,actor_id,idempotency_key,request_hash,response_ref,expires_at)
            VALUES(:scope,'STAFF',:actor,:key,:hash,CAST(:result AS jsonb),now()+interval '7 days')""",
            scope=scope,
            actor=principal.id,
            key=key,
            hash=digest(body),
            result=encoded(result),
        )


def register_backups(registry, executor=None):
    async def unavailable(lease):
        raise DomainError("BACKUP_EXECUTOR_REQUIRED", "备份恢复仅由独立执行器处理", 503)

    def typed(kind):
        def validate(parameters):
            result = validate_parameters(parameters)
            if (kind == JobType.RESTORE_VERIFY) != (result["mode"] == "RESTORE"):
                raise DomainError("VALIDATION_ERROR", "备份任务类型不匹配", 422)
            return result

        return validate

    for kind in (JobType.BACKUP_VERIFY, JobType.RESTORE_VERIFY):
        registry.register(
            kind,
            executor or unavailable,
            permission="agent:backup:verify",
            validator=typed(kind),
            timeout=900,
            lane="backups",
        )
