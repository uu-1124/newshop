"""Registered task handlers and PostgreSQL leases with fenced result commits."""

from uuid import uuid4

from app.domain.capabilities import JobHandler, JobLease, JobType
from app.domain.identity import DomainError
from app.infrastructure.postgres.database import digest, encoded, execute, one, rows


class JobHandlers:
    def __init__(self):
        self._handlers: dict[JobType, JobHandler] = {}
        self.permissions = {}
        self.validators = {}
        self.timeouts = {}
        self.on_failed = {}
        self.lanes = {}

    def register(
        self,
        kind: JobType,
        handler: JobHandler,
        permission="agent:job:read",
        validator=None,
        timeout=None,
        on_failed=None,
        lane="default",
    ):
        if kind in self._handlers:
            raise ValueError(f"Duplicate job handler: {kind}")
        self._handlers[kind] = handler
        self.permissions[kind] = permission
        self.validators[kind] = validator
        self.timeouts[kind] = timeout
        self.on_failed[kind] = on_failed
        self.lanes[kind] = lane

    def validate(self, kind, parameters):
        if kind not in self._handlers:
            raise DomainError("JOB_HANDLER_NOT_ACTIVE", "该后台任务尚未开放", 409)
        if len(encoded(parameters)) > 4096:
            raise DomainError("VALIDATION_ERROR", "后台任务参数过大", 422)
        validator = self.validators[kind]
        if validator:
            return validator(parameters)
        return parameters

    @property
    def supported(self) -> tuple[JobType, ...]:
        return tuple(sorted(self._handlers))

    async def dispatch(self, lease: JobLease):
        handler = self._handlers.get(lease.kind)
        if handler is None:
            raise DomainError("JOB_HANDLER_NOT_ACTIVE", "该后台任务尚未开放", 409)
        self.validate(lease.kind, lease.parameters)
        return await handler(lease)

    def describe(self):
        return [{"type": kind.value, "active": kind in self._handlers} for kind in JobType]


async def wake_job(conn, job):
    await execute(
        conn,
        """INSERT INTO ag_job_wakeup(id,job_id,job_version,next_attempt_at)
        VALUES (:id,:job,:version,:due) ON CONFLICT (job_id,job_version) DO NOTHING""",
        id=uuid4(),
        job=job["id"],
        version=job["version"],
        due=job["next_attempt_at"],
    )


def job_view(job):
    result = {
        key: job[key]
        for key in (
            "job_type",
            "state",
            "progress",
            "attempts",
            "version",
            "error_code",
            "error_message",
            "created_at",
            "updated_at",
        )
    } | {
        "id": str(job["id"]),
        "result": {
            k: v
            for k, v in (job.get("result") or {}).items()
            if k
            in {
                "plan_id",
                "cutoff",
                "settings_version",
                "settings_sha256",
                "messages",
                "conversations",
                "notes",
                "attachments",
                "protected_attachments",
                "execution_rechecks_protection",
                "attachments_scheduled",
                "conversation_id",
                "context_expiry_cleanup",
                "threads",
            }
        }
        if job["job_type"] in {"RETENTION_PREVIEW", "RETENTION_EXECUTE"} and job.get("result") is not None
        else None,
        "summary": "过期知识索引集合清理"
        if job["parameters"].get("mode") == "GC"
        else {
            "RUNTIME_CHECK": "运行基础检查",
            "KB_PARSE": "知识原文解析",
            "KB_INDEX": "知识索引构建",
            "RETENTION_EXECUTE": "已删除会话资料清理",
            "RETENTION_PREVIEW": "留存影响试算",
            "ATTACHMENT_DELETE": "无保护附件清理",
        }.get(job["job_type"], "后台处理任务"),
    }
    if job["job_type"] in {"BACKUP_VERIFY", "RESTORE_VERIFY"}:
        result["summary"] = {
            "CREATE": "一致备份维护任务",
            "VERIFY": "备份认证与恢复验证排队",
            "RESTORE": "隔离恢复验证",
        }.get(job["parameters"].get("mode"), "受控备份任务")
        result["result"] = (
            {
                k: v
                for k, v in (job.get("result") or {}).items()
                if k
                in {
                    "backup_record_id",
                    "restore_id",
                    "restore_job_id",
                    "authenticated_artifacts",
                    "restore_seconds",
                    "verified_at",
                    "isolation_destroyed",
                    "mode",
                    "verified",
                }
            }
            if job.get("result") is not None
            else None
        )
    return result


class Jobs:
    def __init__(self, db, settings, handlers, lane="default"):
        self.db, self.settings, self.handlers = db, settings, handlers
        self.lane = lane

    async def enqueue(self, kind, key, parameters, created_by=None, max_attempts=3):
        async with self.db.transaction() as conn:
            return await self.enqueue_in(conn, kind, key, parameters, created_by, max_attempts)

    async def enqueue_in(self, conn, kind, key, parameters, created_by=None, max_attempts=3):
        parameters = self.handlers.validate(kind, parameters)
        await execute(conn, "SELECT pg_advisory_xact_lock(hashtextextended(:key,0))", key=f"job:{kind}:{key}")
        old = await one(
            conn, "SELECT * FROM ag_job WHERE job_type=:type AND dedupe_key=:key", type=kind, key=key
        )
        if old:
            if digest(old["parameters"]) != digest(parameters) or old["created_by"] != created_by:
                raise DomainError("IDEMPOTENCY_CONFLICT", "后台任务编号已用于不同请求", 409)
            return old
        job = await one(
            conn,
            """INSERT INTO ag_job(id,job_type,dedupe_key,parameters,created_by,max_attempts)
            VALUES (:id,:type,:key,CAST(:parameters AS jsonb),:owner,:max) RETURNING *""",
            id=uuid4(),
            type=kind,
            key=key,
            parameters=encoded(parameters),
            owner=created_by,
            max=max_attempts,
        )
        await wake_job(conn, job)
        return job

    async def claim(self, owner, job_id=None):
        types = [kind.value for kind in self.handlers.supported if self.handlers.lanes[kind] == self.lane]
        if not types:
            return None
        async with self.db.transaction() as conn:
            job = await one(
                conn,
                """SELECT * FROM ag_job WHERE state='QUEUED' AND next_attempt_at<=now()
                AND attempts<max_attempts AND job_type=ANY(:types)
                AND (CAST(:id AS uuid) IS NULL OR id=CAST(:id AS uuid))
                ORDER BY next_attempt_at,id FOR UPDATE SKIP LOCKED LIMIT 1""",
                types=types,
                id=str(job_id) if job_id else None,
            )
            if not job:
                return None
            return await one(
                conn,
                """UPDATE ag_job SET state='RUNNING', attempts=attempts+1,
                lease_owner=:owner,lease_until=now()+:secs*interval '1 second',fencing_token=fencing_token+1,
                updated_at=now(),version=version+1,error_code=NULL,error_message=NULL WHERE id=:id RETURNING *""",
                id=job["id"],
                owner=owner,
                secs=self.settings.job_lease_seconds,
            )

    async def heartbeat(self, job):
        async with self.db.transaction() as conn:
            return bool(
                await one(
                    conn,
                    """UPDATE ag_job SET lease_until=now()+:secs*interval '1 second'
                WHERE id=:id AND state='RUNNING' AND lease_owner=:owner AND fencing_token=:fence
                AND lease_until>now() RETURNING id""",
                    id=job["id"],
                    owner=job["lease_owner"],
                    fence=job["fencing_token"],
                    secs=self.settings.job_lease_seconds,
                )
            )

    async def finish(self, job, result=None, error=None, retryable=True):
        if result is not None and len(encoded(result)) > 16000:
            result, error, retryable = None, "JOB_RESULT_TOO_LARGE", False
        async with self.db.transaction() as conn:
            current = await one(
                conn,
                """SELECT * FROM ag_job WHERE id=:id AND state='RUNNING'
                AND lease_owner=:owner AND fencing_token=:fence AND lease_until>now() FOR UPDATE""",
                id=job["id"],
                owner=job["lease_owner"],
                fence=job["fencing_token"],
            )
            if not current:
                raise DomainError("LEASE_LOST", "后台任务租约已失效", 409)
            retry = bool(error and retryable and current["attempts"] < current["max_attempts"])
            state = "QUEUED" if retry else "FAILED" if error else "SUCCEEDED"
            updated = await one(
                conn,
                """UPDATE ag_job SET state=CAST(:state AS varchar), result=CAST(:result AS jsonb),
                progress=CASE WHEN CAST(:state AS varchar)='SUCCEEDED' THEN 100 ELSE progress END,
                lease_owner=NULL,lease_until=NULL,error_code=:error,error_message=:message,
                next_attempt_at=now()+:delay*interval '1 second',updated_at=now(),version=version+1
                WHERE id=:id RETURNING *""",
                id=current["id"],
                state=state,
                result=encoded(result) if result is not None else None,
                error=error,
                message="后台处理失败，可查看任务状态" if error else None,
                delay=min(60, self.settings.job_retry_base_seconds * 2 ** (current["attempts"] - 1)),
            )
            if retry:
                await wake_job(conn, updated)
            elif state == "FAILED" and self.handlers.on_failed.get(JobType(current["job_type"])):
                await self.handlers.on_failed[JobType(current["job_type"])](conn, updated)
            return updated

    async def recover_expired(self):
        async with self.db.transaction() as conn:
            expired = await rows(
                conn,
                """SELECT * FROM ag_job WHERE state='RUNNING' AND lease_until<=now()
                ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 50""",
            )
            for job in expired:
                updated = await one(
                    conn,
                    """UPDATE ag_job SET state=CASE WHEN attempts<max_attempts THEN 'QUEUED' ELSE 'FAILED' END,
                    fencing_token=fencing_token+1,lease_owner=NULL,lease_until=NULL,error_code='JOB_LEASE_EXPIRED',
                    error_message='工作进程中断',next_attempt_at=now()+:delay*interval '1 second',updated_at=now(),version=version+1
                    WHERE id=:id RETURNING *""",
                    id=job["id"],
                    delay=self.settings.job_retry_base_seconds,
                )
                if updated["state"] == "QUEUED":
                    await wake_job(conn, updated)
                elif self.handlers.on_failed.get(JobType(job["job_type"])):
                    await self.handlers.on_failed[JobType(job["job_type"])](conn, updated)
            return len(expired)

    def authorize(self, principal, job):
        from app.infrastructure.staff_auth import require_permission

        require_permission(principal, "agent:job:read")
        kind = JobType(job["job_type"])
        if kind not in self.handlers.permissions or (
            job["created_by"] is not None and job["created_by"] != principal.id
        ):
            raise DomainError("NOT_FOUND", "后台任务不存在", 404)
        require_permission(principal, self.handlers.permissions[kind])

    async def get(self, principal, job_id):
        async with self.db.transaction() as conn:
            job = await one(conn, "SELECT * FROM ag_job WHERE id=:id", id=job_id)
            if not job:
                raise DomainError("NOT_FOUND", "后台任务不存在", 404)
            self.authorize(principal, job)
            return job_view(job)

    async def retry(self, principal, job_id, expected, key):
        from app.infrastructure.staff_auth import require_permission

        require_permission(principal, "agent:job:retry")
        scope = f"retryJob:{job_id}"
        body = {"expected_version": expected}
        async with self.db.transaction() as conn:
            await execute(
                conn,
                "SELECT pg_advisory_xact_lock(hashtextextended(:key,0))",
                key=f"{scope}:{principal.id}:{key}",
            )
            job = await one(conn, "SELECT * FROM ag_job WHERE id=:id FOR UPDATE", id=job_id)
            if not job:
                raise DomainError("NOT_FOUND", "后台任务不存在", 404)
            self.authorize(principal, job)
            previous = await one(
                conn,
                """SELECT * FROM ag_request_dedupe WHERE scope=:scope
                AND actor_kind='STAFF' AND actor_id=:actor AND idempotency_key=:key""",
                scope=scope,
                actor=principal.id,
                key=key,
            )
            if previous:
                if previous["request_hash"] != digest(body):
                    raise DomainError("IDEMPOTENCY_CONFLICT", "请求编号已使用", 409)
                return previous["response_ref"]
            if job["version"] != expected:
                raise DomainError("VERSION_CONFLICT", "任务状态已变化", 409)
            if job["state"] != "FAILED" or job["attempts"] >= 10:
                raise DomainError("INVALID_STATE", "该任务当前不能重试", 409)
            self.handlers.validate(JobType(job["job_type"]), job["parameters"])
            job = await one(
                conn,
                """UPDATE ag_job SET state='QUEUED',max_attempts=LEAST(10,attempts+3),
                next_attempt_at=now(),fencing_token=fencing_token+1,updated_at=now(),version=version+1
                WHERE id=:id RETURNING *""",
                id=job_id,
            )
            await wake_job(conn, job)
            view = job_view(job)
            await execute(
                conn,
                """INSERT INTO ag_request_dedupe(scope,actor_kind,actor_id,idempotency_key,request_hash,response_ref,expires_at)
                VALUES (:scope,'STAFF',:actor,:key,:hash,CAST(:view AS jsonb),now()+interval '7 days')""",
                scope=scope,
                actor=principal.id,
                key=key,
                hash=digest(body),
                view=encoded(view),
            )
            return view
