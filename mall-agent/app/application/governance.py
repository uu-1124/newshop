"""Versioned public policy and permission-scoped operational statistics.

This module never accepts credentials, raw SQL, arbitrary destinations, or shell
commands as settings. Policy consumers read the same stored version.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from app.api.dto import Input
from app.application.commands import staff_dedupe
from app.domain.identity import DomainError
from app.infrastructure.model_metering import actual_units, estimate
from app.infrastructure.postgres.database import digest, encoded, execute, one, rows
from app.infrastructure.staff_auth import require_permission

POLICY_KEY = "agent-runtime-v1"
CAPABILITIES = {"DIALOG", "VISION", "EMBEDDING", "RERANK", "ASR", "TTS"}


class QuotaLimit(Input):
    scope: Literal["GLOBAL", "CAPABILITY", "PROFILE", "MEMBER", "STAFF"]
    subject_id: str | None = Field(default=None, max_length=80)
    capability: Literal["DIALOG", "VISION", "EMBEDDING", "RERANK", "ASR", "TTS"] | None = None
    unit: Literal["REQUEST", "TOKEN", "CHARACTER", "SECOND"]
    daily_limit: str = Field(pattern=r"^[1-9][0-9]{0,13}(?:\.[0-9]{1,6})?$")

    @model_validator(mode="after")
    def valid(self):
        if (self.scope in {"PROFILE", "MEMBER", "STAFF"}) != (self.subject_id is not None):
            raise ValueError("额度主体与范围不匹配")
        if self.scope == "CAPABILITY" and self.capability is None:
            raise ValueError("能力额度须指定能力")
        if self.scope in {"MEMBER", "STAFF"}:
            if not self.subject_id.isdigit() or not 0 < int(self.subject_id) <= 9223372036854775807:
                raise ValueError("额度主体编号无效")
        elif self.scope == "PROFILE":
            UUID(self.subject_id)
        if (
            self.unit in {"REQUEST", "TOKEN", "CHARACTER"}
            and Decimal(self.daily_limit) != Decimal(self.daily_limit).to_integral_value()
        ):
            raise ValueError("次数、令牌与字符限额须为整数")
        return self


class ModelPrice(Input):
    profile_id: UUID | None = None
    capability: Literal["DIALOG", "VISION", "EMBEDDING", "RERANK", "ASR", "TTS"]
    unit: Literal[
        "REQUEST",
        "INPUT_TOKEN",
        "OUTPUT_TOKEN",
        "TOKEN",
        "CHARACTER",
        "SECOND",
        "UNCACHED_INPUT_TOKEN",
        "CACHE_READ_TOKEN",
        "CACHE_WRITE_TOKEN",
    ]
    currency: Literal["CNY", "USD"]
    price: str = Field(pattern=r"^(0|[1-9][0-9]{0,7})(?:\.[0-9]{1,8})?$")
    per_units: int = Field(default=1000000, ge=1, le=1000000000, strict=True)
    model_name: str | None = Field(default=None, min_length=1, max_length=160)
    source_ref: str | None = Field(default=None, min_length=1, max_length=500)


class UsageSettlement(Input):
    expected_version: int = Field(ge=1, strict=True)
    input_tokens: int | None = Field(default=None, ge=0, le=1000000000000, strict=True)
    output_tokens: int | None = Field(default=None, ge=0, le=1000000000000, strict=True)
    total_tokens: int | None = Field(default=None, ge=0, le=1000000000000, strict=True)
    cache_read_tokens: int | None = Field(default=None, ge=0, le=1000000000000, strict=True)
    cache_write_tokens: int | None = Field(default=None, ge=0, le=1000000000000, strict=True)
    characters: int | None = Field(default=None, ge=0, le=1000000000000, strict=True)
    seconds: str | None = Field(default=None, pattern=r"^(0|[1-9][0-9]{0,7})(?:\.[0-9]{1,6})?$")
    statement_ref: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def valid(self):
        if not self.statement_ref.strip() or not self.reason.strip():
            raise ValueError("须填写供应商对账凭据编号与说明")
        if self.total_tokens is not None and self.input_tokens is not None and self.output_tokens is not None:
            if self.total_tokens != self.input_tokens + self.output_tokens:
                raise ValueError("总令牌量与输入输出不一致")
        if self.cache_read_tokens is not None or self.cache_write_tokens is not None:
            if self.input_tokens is None or self.cache_read_tokens is None or self.cache_write_tokens is None:
                raise ValueError("缓存计量须同时提供输入、缓存读取与写入令牌量")
            if self.cache_read_tokens + self.cache_write_tokens > self.input_tokens:
                raise ValueError("缓存令牌量超过输入令牌量")
        return self


class PublicPolicy(Input):
    message_retention_days: int = Field(default=90, ge=1, le=3650, strict=True)
    image_retention_days: int = Field(default=90, ge=1, le=3650, strict=True)
    audio_retention_days: int = Field(default=7, ge=1, le=365, strict=True)
    tts_retention_days: int = Field(default=7, ge=1, le=365, strict=True)
    event_retention_days: int = Field(default=7, ge=1, le=90, strict=True)
    action_ttl_seconds: int = Field(default=300, ge=60, le=900, strict=True)
    heartbeat_seconds: int = Field(default=20, ge=10, le=60, strict=True)
    presence_timeout_seconds: int = Field(default=60, ge=30, le=300, strict=True)
    max_input_characters: int = Field(default=10000, ge=1, le=10000, strict=True)
    max_document_pages: int = Field(default=200, ge=1, le=2000, strict=True)
    quota_limits: list[QuotaLimit] = Field(default_factory=list, max_length=100)
    model_prices: list[ModelPrice] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def valid(self):
        if self.presence_timeout_seconds < self.heartbeat_seconds * 2:
            raise ValueError("在线状态有效期须至少为心跳间隔的两倍")
        identities = [(q.scope, q.subject_id, q.capability, q.unit) for q in self.quota_limits]
        if len(set(identities)) != len(identities):
            raise ValueError("同范围、主体、能力和单位只允许一条额度规则")
        identities = [(p.profile_id, p.capability, p.unit) for p in self.model_prices]
        if len(set(identities)) != len(identities):
            raise ValueError("同配置、能力和单位只允许一条计价规则")
        for profile, capability in {(p.profile_id, p.capability) for p in self.model_prices}:
            rules = [p for p in self.model_prices if p.profile_id == profile and p.capability == capability]
            units = {p.unit for p in rules}
            if (
                len({p.currency for p in rules}) > 1
                or (
                    "TOKEN" in units
                    and units
                    & {
                        "INPUT_TOKEN",
                        "OUTPUT_TOKEN",
                        "UNCACHED_INPUT_TOKEN",
                        "CACHE_READ_TOKEN",
                        "CACHE_WRITE_TOKEN",
                    }
                )
                or (
                    "INPUT_TOKEN" in units
                    and units & {"UNCACHED_INPUT_TOKEN", "CACHE_READ_TOKEN", "CACHE_WRITE_TOKEN"}
                )
            ):
                raise ValueError("同一计价配置不能混币种或重复计算令牌")
            split = {"UNCACHED_INPUT_TOKEN", "CACHE_READ_TOKEN", "CACHE_WRITE_TOKEN"}
            if units & split and not split <= units:
                raise ValueError("缓存计价须同时配置普通输入、缓存读取与写入价格")
        return self


async def read_policy(conn, lock=False):
    if lock:
        await execute(conn, "SELECT pg_advisory_xact_lock(hashtextextended('agent-public-policy',0))")
    row = await one(
        conn, "SELECT * FROM ag_setting WHERE key=:key" + (" FOR UPDATE" if lock else ""), key=POLICY_KEY
    )
    if row is None:
        return {"value": PublicPolicy().model_dump(mode="json"), "version": 1}
    return {
        "value": PublicPolicy.model_validate(row["value"]).model_dump(mode="json"),
        "version": row["version"],
    }


async def governance_audit(conn, principal, operation, resource, key, details=None):
    await execute(
        conn,
        """INSERT INTO ag_audit_log(id,actor_kind,actor_id,operation,resource_type,resource_id,request_id,outcome,details)
        VALUES (:id,'STAFF',:actor,:operation,'GOVERNANCE',:resource,:key,'SUCCEEDED',CAST(:details AS jsonb))""",
        id=uuid4(),
        actor=principal.id,
        operation=operation,
        resource=resource,
        key=key,
        details=encoded(details or {}),
    )


class Governance:
    def __init__(self, db, backups_enabled=False, observability_enabled=False):
        self.db, self.backups_enabled, self.observability_enabled = db, backups_enabled, observability_enabled

    async def settings(self, principal):
        require_permission(principal, "agent:settings:read")
        async with self.db.transaction(snapshot=True) as conn:
            return await read_policy(conn)

    async def update(self, principal, key, value, expected_version, preview_job_id=None):
        require_permission(principal, "agent:settings:write")
        proposed = PublicPolicy.model_validate(value).model_dump(mode="json")
        body = {
            "value": proposed,
            "expected_version": expected_version,
            "preview_job_id": str(preview_job_id) if preview_job_id else None,
        }
        async with self.db.transaction() as conn:
            cached = await staff_dedupe(conn, principal, "updateSettings", key, body)
            if cached:
                return cached
            await execute(conn, "SELECT pg_advisory_xact_lock(hashtextextended('agent-public-policy',0))")
            old = await read_policy(conn, lock=True)
            if old["version"] != expected_version:
                raise DomainError("VERSION_CONFLICT", "设置版本已变化，请刷新", 409)
            for price in proposed["model_prices"]:
                if price["profile_id"]:
                    profile = await one(
                        conn,
                        "SELECT capability,model_name FROM ag_model_profile WHERE id=:id",
                        id=price["profile_id"],
                    )
                    if (
                        not profile
                        or profile["capability"] != price["capability"]
                        or (price.get("model_name") and price["model_name"] != profile["model_name"])
                    ):
                        raise DomainError(
                            "PRICE_PROFILE_MISMATCH", "价格规则与模型配置不匹配，请刷新核对", 422
                        )
            shortening = any(
                proposed[name] < old["value"][name] for name in proposed if name.endswith("_retention_days")
            )
            if shortening:
                preview = await one(
                    conn,
                    "SELECT * FROM ag_job WHERE id=:id AND job_type='RETENTION_PREVIEW' AND state='SUCCEEDED' AND created_by=:actor AND created_at>now()-interval '24 hours'",
                    id=preview_job_id,
                    actor=principal.id,
                )
                if (
                    not preview
                    or preview["parameters"].get("settings_sha256") != digest(proposed)
                    or preview["parameters"].get("settings_version") != expected_version
                ):
                    raise DomainError(
                        "RETENTION_PREVIEW_REQUIRED", "缩短保留期前须完成当前配置的影响试算", 409
                    )
            await execute(
                conn,
                "INSERT INTO ag_setting(key,value,updated_by) VALUES (:key,CAST(:value AS jsonb),:actor) ON CONFLICT DO NOTHING",
                key=POLICY_KEY,
                value=encoded(old["value"]),
                actor=principal.id,
            )
            next_version = expected_version + 1
            # Preserve deadlines already accrued under the previous policy before
            # lengthening it. Expired content must never reappear between sweeps.
            for name, table, where in (
                ("message_retention_days", "ag_message", "deleted_at IS NULL"),
                ("message_retention_days", "ag_staff_note", "true"),
                ("image_retention_days", "ag_attachment", "purpose='CHAT_IMAGE'"),
                ("audio_retention_days", "ag_attachment", "purpose='RECORDING'"),
                ("tts_retention_days", "ag_attachment", "purpose='SPEECH'"),
                ("event_retention_days", "ag_event_outbox", "true"),
            ):
                if proposed[name] > old["value"][name]:
                    await execute(
                        conn,
                        f"""UPDATE {table} SET retention_until=LEAST(retention_until,created_at+make_interval(days=>:days))
                        WHERE {where} AND LEAST(retention_until,created_at+make_interval(days=>:days))<=now()""",
                        days=old["value"][name],
                    )
            await execute(
                conn,
                "UPDATE ag_setting SET value=CAST(:value AS jsonb),version=:version,updated_by=:actor,updated_at=now() WHERE key=:key",
                key=POLICY_KEY,
                value=encoded(proposed),
                version=next_version,
                actor=principal.id,
            )
            await execute(
                conn,
                "INSERT INTO ag_setting(key,value,updated_by) VALUES (:key,CAST(:value AS jsonb),:actor)",
                key=f"agent-policy-history:{next_version}",
                value=encoded({"before": old, "after": {"value": proposed, "version": next_version}}),
                actor=principal.id,
            )
            result = {"value": proposed, "version": next_version}
            await execute(
                conn,
                "INSERT INTO ag_request_dedupe(scope,actor_kind,actor_id,idempotency_key,request_hash,response_ref,expires_at) VALUES ('updateSettings','STAFF',:actor,:key,:hash,CAST(:result AS jsonb),now()+interval '7 days')",
                actor=principal.id,
                key=key,
                hash=digest(body),
                result=encoded(result),
            )
            await governance_audit(
                conn,
                principal,
                "updateSettings",
                POLICY_KEY,
                key,
                {
                    "previous_version": expected_version,
                    "version": next_version,
                    "settings_sha256": digest(proposed),
                },
            )
            return result

    async def statistics(self, principal, start, end, capability=None):
        require_permission(principal, "agent:statistics:read")
        if (
            start.tzinfo is None
            or end.tzinfo is None
            or not start < end
            or end - start > timedelta(days=93)
            or end > datetime.now(UTC) + timedelta(minutes=1)
        ):
            raise DomainError("VALIDATION_ERROR", "请选择带时区、不超过93天的有效统计区间", 422)
        if capability is not None and capability not in CAPABILITIES:
            raise DomainError("VALIDATION_ERROR", "统计能力无效", 422)
        async with self.db.transaction(snapshot=True) as conn:
            totals = await one(
                conn,
                """SELECT count(*) requests,count(*) FILTER (WHERE status<>'SUCCEEDED') errors,
                CASE WHEN count(*) FILTER (WHERE input_tokens IS NULL)=0 THEN sum(input_tokens) END input_tokens,
                CASE WHEN count(*) FILTER (WHERE output_tokens IS NULL)=0 THEN sum(output_tokens) END output_tokens,
                percentile_disc(0.95) WITHIN GROUP(ORDER BY latency_ms) p95_model_ms
                FROM ag_model_call WHERE created_at>=:start AND created_at<:end AND (CAST(:cap AS text) IS NULL OR capability=:cap)""",
                start=start,
                end=end,
                cap=capability,
            )
            runs = await one(
                conn,
                "SELECT count(*) FILTER(WHERE state='RUNNING') active_runs,count(*) FILTER(WHERE state='QUEUED') queued_runs FROM ag_run",
            )
            breakdown = await rows(
                conn,
                """SELECT capability,count(*) requests,count(*) FILTER(WHERE status<>'SUCCEEDED') errors,
                count(*) FILTER(WHERE input_tokens IS NULL OR output_tokens IS NULL) unknown_token_calls,
                COALESCE(sum(input_tokens),0) known_input_tokens,COALESCE(sum(output_tokens),0) known_output_tokens,
                percentile_disc(0.95) WITHIN GROUP(ORDER BY latency_ms) p95_ms
                FROM ag_model_call WHERE created_at>=:start AND created_at<:end AND (CAST(:cap AS text) IS NULL OR capability=:cap) GROUP BY capability ORDER BY capability""",
                start=start,
                end=end,
                cap=capability,
            )
            quotas = await rows(
                conn,
                """SELECT unit_kind,state,count(*) reservations,sum(reserved_units) reserved_units,sum(settled_units) settled_units
                FROM ag_quota_reservation WHERE created_at>=:start AND created_at<:end
                AND ((bucket_key LIKE 'model-requests:%' AND unit_kind='REQUEST')
                    OR (bucket_key LIKE 'quota:GLOBAL:*:*:%' AND unit_kind<>'REQUEST'))
                AND (CAST(:cap AS text) IS NULL OR profile_id IN
                    (SELECT id FROM ag_model_profile WHERE capability=:cap))
                GROUP BY unit_kind,state ORDER BY unit_kind,state""",
                start=start,
                end=end,
                cap=capability,
            )
            business = await one(
                conn,
                """SELECT count(*) requests,count(*) FILTER(WHERE status_code>=500) server_errors,
                count(*) FILTER(WHERE status_code BETWEEN 400 AND 499) rejected_requests,
                percentile_disc(0.95) WITHIN GROUP(ORDER BY duration_ms) p95_ms
                FROM ag_request_metric WHERE created_at>=:start AND created_at<:end AND operation NOT IN ('memberEvents','adminEvents')""",
                start=start,
                end=end,
            )
            costs = await rows(
                conn,
                """SELECT m.currency,count(*) calls,sum(m.estimated_cost) estimated_cost
                FROM ag_model_meter m JOIN ag_model_call c ON c.id=m.call_id
                WHERE c.created_at>=:start AND c.created_at<:end AND m.estimated_cost IS NOT NULL
                AND (CAST(:cap AS text) IS NULL OR c.capability=:cap) GROUP BY m.currency ORDER BY m.currency""",
                start=start,
                end=end,
                cap=capability,
            )
            alerts = await self.alerts_in(conn)
            runtime = None
            if self.observability_enabled:
                from app.application.observability import runtime_snapshot

                snapshot = await runtime_snapshot(conn)
                runtime = snapshot.model_dump(mode="json") if snapshot else None
        priced = sum(row["calls"] for row in costs)
        complete_cost = len(costs) == 1 and priced == totals["requests"]
        return {
            "from": start,
            "to": end,
            **dict(totals),
            **dict(runs),
            "input_tokens": str(totals["input_tokens"]) if totals["input_tokens"] is not None else None,
            "output_tokens": str(totals["output_tokens"]) if totals["output_tokens"] is not None else None,
            "p95_business_ms": business["p95_ms"],
            "business": dict(business),
            "estimated_cost": str(costs[0]["estimated_cost"]) if complete_cost else None,
            "cost_currency": costs[0]["currency"] if complete_cost else None,
            "unpriced_calls": totals["requests"] - priced,
            "costs": [dict(row) | {"estimated_cost": str(row["estimated_cost"])} for row in costs],
            "request_scope": "MODEL_CALLS",
            "capabilities": [
                dict(row)
                | {
                    "known_input_tokens": str(row["known_input_tokens"]),
                    "known_output_tokens": str(row["known_output_tokens"]),
                }
                for row in breakdown
            ],
            "quota_usage": [
                {key: str(value) if isinstance(value, Decimal) else value for key, value in row.items()}
                for row in quotas
            ],
            "alerts": alerts,
            "runtime": runtime,
        }

    async def alerts_in(self, conn):
        alerts = []
        workers = await rows(
            conn,
            "SELECT DISTINCT process_kind FROM ag_worker_heartbeat WHERE last_seen_at>now()-interval '45 seconds' AND state<>'STOPPED'",
        )
        for kind in sorted(
            {"generation", "scheduler", "jobs", "outbox"} - {row["process_kind"] for row in workers}
        ):
            alerts.append(
                {"code": "WORKER_HEARTBEAT_MISSING", "resource": kind, "severity": "ERROR", "count": 1}
            )
        for code, sql in (
            (
                "ACTION_MANUAL_REQUIRED",
                "SELECT count(*) n FROM ag_action_dispatch WHERE state='MANUAL_REQUIRED'",
            ),
            (
                "ACTION_UNKNOWN_OVER_10_MIN",
                "SELECT count(*) n FROM ag_action WHERE state IN ('EXECUTING','UNKNOWN') AND COALESCE(confirmed_at,created_at)<now()-interval '10 minutes'",
            ),
            (
                "MODEL_USAGE_UNKNOWN",
                """SELECT count(*) n FROM (
                SELECT call_id FROM ag_quota_reservation WHERE state='UNKNOWN' AND created_at<now()-interval '10 minutes'
                UNION SELECT call_id FROM ag_model_meter WHERE usage_state IN ('UNKNOWN','PENDING') AND created_at<now()-interval '10 minutes') unresolved""",
            ),
            (
                "FAILED_JOBS",
                "SELECT count(*) n FROM ag_job WHERE state='FAILED' AND updated_at>now()-interval '1 day'",
            ),
        ):
            value = await one(conn, sql)
            if value["n"]:
                alerts.append({"code": code, "resource": "AGENT", "severity": "WARNING", "count": value["n"]})
        urgent = await one(
            conn,
            "SELECT count(*) n FROM ag_action WHERE state IN ('EXECUTING','UNKNOWN') AND COALESCE(confirmed_at,created_at)<now()-interval '1 hour'",
        )
        if urgent["n"]:
            alerts.append(
                {
                    "code": "ACTION_UNKNOWN_OVER_1_HOUR",
                    "resource": "AGENT",
                    "severity": "ERROR",
                    "count": urgent["n"],
                }
            )
        repeated = await rows(
            conn,
            """WITH ordered AS (SELECT job_type,state,row_number() OVER(PARTITION BY job_type ORDER BY created_at DESC,id DESC) pos
            FROM ag_job WHERE created_at>now()-interval '1 day') SELECT job_type FROM ordered WHERE pos<=3 GROUP BY job_type
            HAVING count(*)=3 AND count(*) FILTER(WHERE state='FAILED')=3""",
        )
        for item in repeated:
            alerts.append(
                {
                    "code": "JOB_TYPE_REPEATED_FAILURE",
                    "resource": item["job_type"],
                    "severity": "ERROR",
                    "count": 3,
                }
            )
        if self.backups_enabled:
            from app.application.backups import backup_alerts

            alerts.extend(await backup_alerts(conn))
        if self.observability_enabled:
            from app.application.observability import alerts as runtime_alerts
            from app.application.observability import runtime_snapshot

            alerts.extend(runtime_alerts(await runtime_snapshot(conn)))
        return alerts

    async def unknown_usage(self, principal, before=None, limit=20):
        require_permission(principal, "agent:usage:read")
        async with self.db.transaction(snapshot=True) as conn:
            data = await rows(
                conn,
                """SELECT m.*,c.capability,c.provider_request_id,c.status,c.run_id,c.job_id,c.media_request_id,c.error_code FROM ag_model_meter m
                LEFT JOIN ag_model_call c ON c.id=m.call_id
                WHERE (m.usage_state IN ('PENDING','UNKNOWN') OR EXISTS
                    (SELECT 1 FROM ag_quota_reservation q WHERE q.call_id=m.call_id AND q.state='UNKNOWN'))
                AND (CAST(:before AS uuid) IS NULL OR (m.created_at,m.call_id)<
                    (SELECT created_at,call_id FROM ag_model_meter WHERE call_id=CAST(:before AS uuid)))
                ORDER BY m.created_at DESC,m.call_id DESC LIMIT :limit""",
                before=before,
                limit=limit + 1,
            )
            items = []
            units_by_call = {}
            if data:
                units = await rows(
                    conn,
                    """SELECT call_id,unit_kind,max(reserved_units) reserved_units FROM ag_quota_reservation
                    WHERE call_id=ANY(CAST(:ids AS uuid[])) AND state IN ('RESERVED','UNKNOWN')
                    GROUP BY call_id,unit_kind ORDER BY call_id,unit_kind""",
                    ids=[item["call_id"] for item in data[:limit]],
                )
                for unit in units:
                    units_by_call.setdefault(unit["call_id"], []).append(unit)
            for item in data[:limit]:
                items.append(
                    {
                        "call_id": str(item["call_id"]),
                        "run_id": str(item["run_id"]) if item["run_id"] else None,
                        "job_id": str(item["job_id"]) if item["job_id"] else None,
                        "media_request_id": str(item["media_request_id"])
                        if item["media_request_id"]
                        else None,
                        "actor_kind": item["actor_kind"],
                        "actor_id": str(item["actor_id"]) if item["actor_id"] is not None else None,
                        "error_code": item["error_code"],
                        "capability": item["capability"],
                        "provider_request_id": item["provider_request_id"],
                        "status": item["status"],
                        "usage_state": item["usage_state"],
                        "version": item["version"],
                        "created_at": item["created_at"],
                        "reservations": [
                            {"unit": u["unit_kind"], "reserved_units": str(u["reserved_units"])}
                            for u in units_by_call.get(item["call_id"], [])
                        ],
                    }
                )
        return {
            "items": items,
            "has_more": len(data) > limit,
            "next_before": str(data[limit - 1]["call_id"]) if len(data) > limit else None,
        }

    async def reconcile_usage(self, principal, call_id, key, body):
        from app.infrastructure.model_quota import settle

        require_permission(principal, "agent:usage:write")
        parsed = UsageSettlement.model_validate(body)
        wire = parsed.model_dump(mode="json")
        scope = f"reconcileModelUsage:{call_id}"
        async with self.db.transaction() as conn:
            cached = await staff_dedupe(conn, principal, scope, key, wire)
            if cached:
                return cached
            meter = await one(conn, "SELECT * FROM ag_model_meter WHERE call_id=:id FOR UPDATE", id=call_id)
            if not meter:
                raise DomainError("NOT_FOUND", "调用计量记录不存在", 404)
            if meter["version"] != parsed.expected_version:
                raise DomainError("VERSION_CONFLICT", "计量记录已变化，请刷新", 409)
            pending = await rows(
                conn,
                "SELECT DISTINCT unit_kind FROM ag_quota_reservation WHERE call_id=:id AND state IN ('RESERVED','UNKNOWN')",
                id=call_id,
            )
            call = await one(
                conn,
                "SELECT id,status,error_code,created_at<now()-interval '10 minutes' stale FROM ag_model_call WHERE id=:id",
                id=call_id,
            )
            if call and call["error_code"] == "ADAPTER_IN_FLIGHT":
                if not call["stale"]:
                    raise DomainError("MODEL_CALL_IN_PROGRESS", "调用尚未完成，不允许提前结算", 409)
                await execute(
                    conn,
                    "UPDATE ag_model_call SET error_code='ADAPTER_COMPLETION_MISSING' WHERE id=:id AND error_code='ADAPTER_IN_FLIGHT'",
                    id=call_id,
                )
            if not call:
                reservation = await one(
                    conn,
                    """SELECT q.profile_id,q.profile_version,v.capability,
                    q.created_at<now()-interval '10 minutes' stale FROM ag_quota_reservation q
                    JOIN ag_model_profile_version v ON v.profile_id=q.profile_id AND v.version_no=q.profile_version
                    WHERE q.call_id=:id ORDER BY q.bucket_key LIMIT 1""",
                    id=call_id,
                )
                if not reservation or not reservation["stale"]:
                    raise DomainError("MODEL_CALL_IN_PROGRESS", "调用尚未落账，不允许提前结算", 409)
                # A crashed adapter may never have committed its completion row.
                # Reconcile usage against supplier evidence without asserting success.
                await execute(
                    conn,
                    """INSERT INTO ag_model_call(id,profile_id,profile_version,capability,status,latency_ms,error_code)
                    VALUES(:id,:profile,:version,:cap,'UNKNOWN',0,'ADAPTER_COMPLETION_MISSING')""",
                    id=call_id,
                    profile=reservation["profile_id"],
                    version=reservation["profile_version"],
                    cap=reservation["capability"],
                )
                call = {"id": call_id, "status": "UNKNOWN"}
            if not pending:
                raise DomainError("INVALID_STATE", "该调用没有待核对用量", 409)
            usage = {
                k: v
                for k, v in wire.items()
                if k
                in {
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                    "characters",
                    "seconds",
                    "cache_read_tokens",
                    "cache_write_tokens",
                }
                and v is not None
            }
            if {r["unit_kind"] for r in pending} - set(actual_units(usage)):
                raise DomainError("USAGE_INCOMPLETE", "须填写该调用全部未知单位的实际供应商用量", 422)
            currency, cost = estimate(meter["pricing"], usage)
            await settle(conn, call_id, "SUCCEEDED", usage)
            await execute(
                conn,
                """UPDATE ag_model_meter SET usage=CAST(:usage AS jsonb),usage_state='RECONCILED',
                currency=:currency,estimated_cost=:cost,statement_ref=:ref,reconciled_by=:actor,reconciled_at=now(),
                updated_at=now(),version=version+1 WHERE call_id=:id""",
                id=call_id,
                usage=encoded(usage),
                currency=currency,
                cost=cost,
                ref=parsed.statement_ref,
                actor=principal.id,
            )
            await execute(
                conn,
                """UPDATE ag_model_call SET input_tokens=COALESCE(:input,input_tokens),
                output_tokens=COALESCE(:output,output_tokens),billable_units=COALESCE(:units,billable_units),
                estimated_cost=:cost WHERE id=:id""",
                id=call_id,
                input=parsed.input_tokens,
                output=parsed.output_tokens,
                units=parsed.seconds if parsed.seconds is not None else parsed.characters,
                cost=cost,
            )
            result = {
                "call_id": str(call_id),
                "usage_state": "RECONCILED",
                "version": meter["version"] + 1,
                "estimated_cost": str(cost) if cost is not None else None,
                "currency": currency,
            }
            await execute(
                conn,
                """INSERT INTO ag_request_dedupe(scope,actor_kind,actor_id,idempotency_key,request_hash,response_ref,expires_at)
                VALUES(:scope,'STAFF',:actor,:key,:hash,CAST(:result AS jsonb),now()+interval '7 days')""",
                scope=scope,
                actor=principal.id,
                key=key,
                hash=digest(wire),
                result=encoded(result),
            )
            await governance_audit(
                conn,
                principal,
                "reconcileModelUsage",
                str(call_id),
                key,
                {
                    "statement_ref": parsed.statement_ref,
                    "reason": parsed.reason,
                    "previous_version": meter["version"],
                    "usage": usage,
                    "original_call_status": call["status"],
                },
            )
            return result
