"""Frozen pricing and actual supplier units. Unknown cost is never recorded as zero."""

from decimal import ROUND_HALF_UP, Decimal

from app.infrastructure.postgres.database import encoded, execute, one, rows


def actual_units(usage):
    values = {"REQUEST": Decimal(1)}
    for unit, key, alternative in (
        ("INPUT_TOKEN", "input_tokens", "prompt_tokens"),
        ("OUTPUT_TOKEN", "output_tokens", "completion_tokens"),
        ("CHARACTER", "characters", "characters"),
        ("SECOND", "seconds", "seconds"),
    ):
        raw = usage.get(key, usage.get(alternative))
        if raw is not None:
            value = Decimal(str(raw))
            if value.is_finite() and value >= 0:
                values[unit] = value
    if "INPUT_TOKEN" in values and "OUTPUT_TOKEN" in values:
        values["TOKEN"] = values["INPUT_TOKEN"] + values["OUTPUT_TOKEN"]
    elif usage.get("total_tokens") is not None:
        value = Decimal(str(usage["total_tokens"]))
        if value.is_finite() and value >= 0:
            values["TOKEN"] = value
    return values


def estimate(pricing, usage):
    if not pricing:
        return None, None
    units = actual_units(usage)
    if any(rule["unit"] not in units for rule in pricing):
        return pricing[0]["currency"], None
    cost = sum(
        (units[rule["unit"]] * Decimal(rule["price"]) / Decimal(rule["per_units"]) for rule in pricing),
        Decimal(0),
    )
    return pricing[0]["currency"], cost.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


async def capture_pricing(conn, call_id, policy, capability, profile, actor):
    all_rules = policy["value"]["model_prices"]
    exact = [r for r in all_rules if r["capability"] == capability and r["profile_id"] == str(profile["id"])]
    rules = exact or [r for r in all_rules if r["capability"] == capability and r["profile_id"] is None]
    await execute(
        conn,
        """INSERT INTO ag_model_meter(call_id,actor_kind,actor_id,pricing)
        VALUES(:id,:kind,:actor,CAST(:pricing AS jsonb))""",
        id=call_id,
        kind=actor[0],
        actor=actor[1],
        pricing=encoded(rules),
    )


async def finish_meter(conn, call_id, usage, known):
    meter = await one(conn, "SELECT * FROM ag_model_meter WHERE call_id=:id FOR UPDATE", id=call_id)
    if not meter:
        return None
    required = await rows(
        conn, "SELECT DISTINCT unit_kind FROM ag_quota_reservation WHERE call_id=:id", id=call_id
    )
    values = actual_units(usage)
    known = known and not ({r["unit_kind"] for r in required} - set(values))
    usage = {
        k: v
        for k, v in usage.items()
        if k
        in {
            "input_tokens",
            "output_tokens",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "characters",
            "seconds",
        }
    }
    currency, cost = estimate(meter["pricing"], usage)
    await execute(
        conn,
        """UPDATE ag_model_meter SET usage=CAST(:usage AS jsonb),usage_state=:state,
        currency=:currency,estimated_cost=:cost,version=version+1,updated_at=now() WHERE call_id=:id""",
        id=call_id,
        usage=encoded(usage),
        state="REPORTED" if known else "UNKNOWN",
        currency=currency,
        cost=cost,
    )
    return cost
