"""Shared staff command idempotency; domain audit remains with each service."""

from app.application.expiry import assert_cached_visible
from app.domain.identity import DomainError
from app.infrastructure.postgres.database import digest, execute, one


async def staff_dedupe(conn, principal, scope, key, body):
    await execute(
        conn,
        "SELECT pg_advisory_xact_lock(hashtextextended(:key,0))",
        key=f"{scope}:STAFF:{principal.id}:{key}",
    )
    record = await one(
        conn,
        "SELECT * FROM ag_request_dedupe WHERE scope=:scope AND actor_kind='STAFF' AND actor_id=:actor AND idempotency_key=:key",
        scope=scope,
        actor=principal.id,
        key=key,
    )
    if record and record["request_hash"] != digest(body):
        raise DomainError("IDEMPOTENCY_CONFLICT", "请求编号已用于不同内容", 409)
    if record:
        await assert_cached_visible(conn, record["response_ref"])
    return record["response_ref"] if record else None
