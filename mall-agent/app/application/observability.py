"""Validated operational snapshots and alerts. Never expose source rows or credentials."""

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.infrastructure.postgres.database import one

STATE_KEY = "agent-observability-v1"


class Sample(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: Literal["UP", "DOWN", "DISABLED"]
    latency_ms: int = Field(ge=0, le=300000)
    error_code: str | None = Field(default=None, pattern=r"^[A-Z0-9_]{1,80}$")


class Cloud(BaseModel):
    model_config = ConfigDict(extra="forbid")
    memory_total_bytes: int = Field(gt=0)
    memory_available_bytes: int = Field(ge=0)
    disk_total_bytes: int = Field(gt=0)
    disk_free_bytes: int = Field(ge=0)
    swap_used_bytes: int = Field(ge=0)
    containers_running: int = Field(ge=0)
    containers_expected: int = Field(ge=1)
    containers_unhealthy: int = Field(ge=0)
    containers_oom: int = Field(ge=0)
    container_restarts: int = Field(ge=0)


class Postgres(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connections: int = Field(ge=0)
    max_connections: int = Field(gt=0)
    lock_waiters: int = Field(ge=0)
    longest_transaction_seconds: int = Field(ge=0)
    active_slots: int = Field(ge=0, le=3)
    queued_runs: int = Field(ge=0)
    oldest_queued_seconds: int = Field(ge=0)
    queued_jobs: int = Field(ge=0)
    oldest_job_seconds: int = Field(ge=0)
    job_wakeups_pending: int = Field(ge=0)
    event_notifications_pending: int = Field(ge=0)
    event_clock: int = Field(ge=0)
    event_replay_floor: int = Field(ge=0)
    event_clock_violations: int = Field(ge=0)
    pool_wait_p95_ms: int = Field(ge=0)
    pool_saturated_workers: int = Field(ge=0)
    sse_cursor_expired_5m: int = Field(ge=0)
    sse_invalid_cursor_5m: int = Field(ge=0)
    first_text_samples: int = Field(ge=0)
    first_text_p95_ms: int | None = Field(default=None, ge=0)
    queue_p95_ms: int | None = Field(default=None, ge=0)
    total_p95_ms: int | None = Field(default=None, ge=0)


class Outbox(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pending_due: int = Field(ge=0)
    overdue_60_seconds: int = Field(ge=0)
    oldest_due_seconds: int = Field(ge=0)
    retrying: int = Field(ge=0)


class RuntimeSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    checked_at: datetime
    duration_ms: int = Field(ge=0)
    dependencies: dict[str, Sample]
    cloud: Cloud | None = None
    postgres: Postgres | None = None
    java_outbox: Outbox | None = None


DEPENDENCIES = {
    "postgres",
    "portal_bridge",
    "admin_bridge",
    "gateway",
    "private_objects",
    "milvus",
    "redis",
    "rabbitmq",
    "cloud",
}


def validate_snapshot(value):
    result = RuntimeSnapshot.model_validate(value)
    if result.checked_at.tzinfo is None or set(result.dependencies) != DEPENDENCIES:
        raise ValueError("Operational snapshot has unregistered dependencies")
    return result


async def runtime_snapshot(conn):
    record = await one(conn, "SELECT value FROM ag_setting WHERE key=:key", key=STATE_KEY)
    if not record:
        return None
    try:
        return validate_snapshot(record["value"])
    except ValueError:
        return None


def fresh(snapshot, now=None):
    now = now or datetime.now(UTC)
    return bool(snapshot and now - timedelta(seconds=90) <= snapshot.checked_at <= now + timedelta(seconds=5))


def alerts(snapshot, now=None):
    if not fresh(snapshot, now):
        return [{"code": "OBSERVABILITY_STALE", "resource": "MONITOR", "severity": "ERROR", "count": 1}]
    result = []

    def add(code, resource, count=1, severity="WARNING"):
        if count:
            result.append({"code": code, "resource": resource, "severity": severity, "count": count})

    for name, sample in snapshot.dependencies.items():
        if sample.state == "DOWN":
            add("DEPENDENCY_UNAVAILABLE", name, severity="ERROR")
    cloud = snapshot.cloud
    if cloud:
        if cloud.memory_available_bytes / cloud.memory_total_bytes < 0.15:
            add("CLOUD_MEMORY_PRESSURE", "CLOUD")
        if cloud.disk_free_bytes / cloud.disk_total_bytes < 0.20:
            add("CLOUD_DISK_PRESSURE", "CLOUD", severity="ERROR")
        add("CONTAINER_UNHEALTHY", "CLOUD", cloud.containers_unhealthy, severity="ERROR")
        add(
            "CONTAINER_NOT_RUNNING",
            "CLOUD",
            max(0, cloud.containers_expected - cloud.containers_running),
            severity="ERROR",
        )
        add("CONTAINER_OOM_RECORDED", "CLOUD", cloud.containers_oom, severity="ERROR")
        add("CONTAINER_RESTART_RECORDED", "CLOUD", cloud.container_restarts)
    pg = snapshot.postgres
    if pg:
        if pg.connections / pg.max_connections >= 0.8:
            add("POSTGRES_CONNECTION_PRESSURE", "POSTGRES")
        add("POSTGRES_LOCK_WAIT", "POSTGRES", pg.lock_waiters)
        if pg.longest_transaction_seconds >= 30:
            add("POSTGRES_LONG_TRANSACTION", "POSTGRES")
        if pg.pool_wait_p95_ms >= 1000:
            add("POSTGRES_POOL_WAIT", "POSTGRES")
        add("POSTGRES_POOL_SATURATED", "POSTGRES", pg.pool_saturated_workers)
        if pg.oldest_queued_seconds >= 60:
            add("GENERATION_QUEUE_OVER_60_SECONDS", "GENERATION", pg.queued_runs)
        if pg.oldest_job_seconds >= 120:
            add("JOB_QUEUE_OVER_120_SECONDS", "JOBS", pg.queued_jobs)
        add("EVENT_CLOCK_INCONSISTENT", "SSE", pg.event_clock_violations, severity="ERROR")
        if pg.sse_cursor_expired_5m >= 3:
            add("SSE_REPLAY_EXPIRED_BURST", "SSE", pg.sse_cursor_expired_5m)
        if pg.sse_invalid_cursor_5m >= 3:
            add("SSE_INVALID_CURSOR_BURST", "SSE", pg.sse_invalid_cursor_5m)
        if pg.job_wakeups_pending >= 100:
            add("JOB_NOTIFICATION_BACKLOG", "RABBITMQ", pg.job_wakeups_pending)
        if pg.event_notifications_pending >= 100:
            add("EVENT_NOTIFICATION_BACKLOG", "SSE", pg.event_notifications_pending)
    if snapshot.java_outbox:
        add("JAVA_OUTBOX_OVERDUE", "JAVA_OUTBOX", snapshot.java_outbox.overdue_60_seconds, severity="ERROR")
        add("JAVA_OUTBOX_RETRYING", "JAVA_OUTBOX", snapshot.java_outbox.retrying)
    return result


def readiness(snapshot, settings):
    if not fresh(snapshot):
        return False, {"monitor": "STALE"}
    required = {"postgres", "portal_bridge", "gateway"}
    if settings.staff_bridge_secret.get_secret_value():
        required.add("admin_bridge")
    if settings.attachments_enabled:
        required.add("private_objects")
    if settings.knowledge_enabled:
        required.add("milvus")
    states = {name: item.state for name, item in snapshot.dependencies.items()}
    return all(states[name] == "UP" for name in required), states
