"""Ordered multi-bucket model quota reservations and conservative settlement."""

from contextvars import ContextVar
from decimal import Decimal
from uuid import uuid4

from app.application.governance import read_policy
from app.domain.identity import DomainError
from app.infrastructure.model_metering import actual_units, capture_pricing
from app.infrastructure.postgres.database import encoded, execute, ordered_advisory_locks, rows

model_actor = ContextVar("model_actor", default=("SYSTEM", None))


def normalized_units(values):
    result = {"REQUEST": Decimal(1)}
    for unit, amount in (values or {}).items():
        if unit not in {"REQUEST", "TOKEN", "CHARACTER", "SECOND"}:
            raise DomainError("MODEL_QUOTA_INVALID", "未知模型计量单位", 503)
        value = Decimal(str(amount))
        if not value.is_finite() or not 0 <= value <= Decimal("100000000000000"):
            raise DomainError("MODEL_QUOTA_INVALID", "模型预留量无效", 503)
        result[unit] = value
    result["REQUEST"] = Decimal(1)
    return result


async def reserve(conn, capability, profile, default_limit, requested, subject=None):
    amounts = normalized_units(requested)
    actor_kind, actor_id = subject or model_actor.get()
    policy = await read_policy(conn)
    buckets = {f"model-requests:{capability}": ("REQUEST", Decimal(default_limit))}
    for scope, owner in (
        ("GLOBAL", None),
        ("CAPABILITY", None),
        ("PROFILE", str(profile["id"])),
        (actor_kind, str(actor_id) if actor_id is not None else None),
    ):
        if scope not in {"GLOBAL", "CAPABILITY", "PROFILE", "MEMBER", "STAFF"}:
            continue
        for cap in (capability,) if scope == "CAPABILITY" else (None, capability):
            for unit in amounts:
                buckets[f"quota:{scope}:{owner or '*'}:{cap or '*'}:{unit}"] = (
                    unit,
                    Decimal("99999999999999"),
                )
    for rule in policy["value"]["quota_limits"]:
        scope, unit, cap, owner = rule["scope"], rule["unit"], rule["capability"], rule["subject_id"]
        if unit not in amounts or (cap is not None and cap != capability):
            continue
        if scope == "PROFILE" and owner != str(profile["id"]):
            continue
        if scope in {"MEMBER", "STAFF"} and (actor_kind != scope or str(actor_id) != owner):
            continue
        # Version is intentionally absent: changing active versions does not reset usage.
        key = f"quota:{scope}:{owner or '*'}:{cap or '*'}:{unit}"
        buckets[key] = (unit, Decimal(rule["daily_limit"]))
    await ordered_advisory_locks(conn, buckets)
    usage_by_bucket = {
        row["bucket_key"]: row["units"]
        for row in await rows(
            conn,
            """SELECT bucket_key,sum(CASE WHEN state='SETTLED' THEN settled_units ELSE reserved_units END) units
            FROM ag_quota_reservation WHERE bucket_key=ANY(CAST(:keys AS text[])) AND state<>'RELEASED'
            AND period_start=date_trunc('day',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
            GROUP BY bucket_key""",
            keys=list(buckets),
        )
    }
    for key, (unit, limit) in buckets.items():
        if usage_by_bucket.get(key, Decimal(0)) + amounts[unit] > limit:
            raise DomainError(
                "MODEL_QUOTA_EXCEEDED", "本次模型请求超过当前主体或能力的今日额度，请稍后或联系人工", 429
            )
    call_id = uuid4()
    await capture_pricing(conn, call_id, policy, capability, profile, (actor_kind, actor_id))
    await execute(
        conn,
        """INSERT INTO ag_quota_reservation
        (id,call_id,bucket_key,period_start,unit_kind,reserved_units,profile_id,profile_version,expires_at)
        SELECT r.id,:call,r.key,date_trunc('day',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC',
            r.unit,r.amount,:profile,:version,now()+interval '1 day'
        FROM jsonb_to_recordset(CAST(:reservations AS jsonb)) AS r(id uuid,key text,unit text,amount numeric)""",
        call=call_id,
        profile=profile["id"],
        version=profile["version"],
        reservations=encoded(
            [
                {"id": str(uuid4()), "key": key, "unit": unit, "amount": str(amounts[unit])}
                for key, (unit, _) in buckets.items()
            ]
        ),
    )
    return str(call_id)


async def settle(conn, call_id, status, usage):
    await rows(
        conn,
        "SELECT * FROM ag_quota_reservation WHERE call_id=:call ORDER BY bucket_key FOR UPDATE",
        call=call_id,
    )
    values = actual_units(usage)
    await execute(
        conn,
        """UPDATE ag_quota_reservation SET
        state=CASE WHEN :succeeded AND CAST(:units AS jsonb) ? unit_kind THEN 'SETTLED' ELSE 'UNKNOWN' END,
        settled_units=CASE WHEN :succeeded THEN (CAST(:units AS jsonb)->>unit_kind)::numeric ELSE NULL END,
        updated_at=now(),version=version+1
        WHERE call_id=:call AND state IN ('RESERVED','UNKNOWN')""",
        call=call_id,
        succeeded=status == "SUCCEEDED",
        units=encoded({key: str(value) for key, value in values.items() if value.is_finite() and value >= 0}),
    )
