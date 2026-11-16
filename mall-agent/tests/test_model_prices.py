import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.application.governance import PublicPolicy, UsageSettlement
from app.infrastructure.model_metering import actual_units, capture_pricing, estimate, finish_meter
from app.infrastructure.postgres.database import Database, execute, one


def fusheng_prices():
    catalogue = json.loads(
        (Path(__file__).resolve().parents[2] / "deployment/model-prices-20261116.json").read_text(
            encoding="utf-8"
        )
    )
    return [r for r in catalogue["rules"] if r["capability"] == "DIALOG"]


def test_official_base_prices_split_cached_input_without_double_billing():
    prices = fusheng_prices()
    currency, cost = estimate(
        prices,
        {
            "input_tokens": 1000000,
            "output_tokens": 100000,
            "input_tokens_details": {"cached_tokens": 200000, "cache_creation_tokens": 100000},
        },
    )
    # 700k ordinary input + 200k read + 100k write + 100k output; multiplier=1.
    assert currency == "USD" and cost == Decimal("2.670000")
    units = actual_units(
        {"input_tokens": 1000, "output_tokens": 100, "input_tokens_details": {"cached_tokens": 200}}
    )
    assert units["TOKEN"] == 1100 and units["INPUT_TOKEN"] == 1000
    assert units["UNCACHED_INPUT_TOKEN"] == 800 and units["CACHE_WRITE_TOKEN"] == 0


def test_missing_or_invalid_cache_breakdown_keeps_cost_unknown_and_legacy_rates_compatible():
    for usage in [
        {"input_tokens": 100, "output_tokens": 20},
        {"input_tokens": 100, "output_tokens": 20, "input_tokens_details": {"cached_tokens": 101}},
        {"input_tokens": 100, "output_tokens": 20, "input_tokens_details": {"cached_tokens": "NaN"}},
        {"input_tokens": "invalid", "output_tokens": 20, "input_tokens_details": {"cached_tokens": 0}},
    ]:
        assert estimate(fusheng_prices(), usage) == ("USD", None)
    legacy = [{"unit": "INPUT_TOKEN", "currency": "CNY", "price": "0.5", "per_units": 1000000}]
    assert estimate(legacy, {"prompt_tokens": 1000000}) == ("CNY", Decimal("0.500000"))
    assert estimate([legacy[0] | {"unit": "TOKEN"}], {"total_tokens": 1000000})[1] == Decimal("0.500000")


def test_policy_rejects_double_counting_or_incomplete_cache_prices():
    prices = [{k: v for k, v in r.items() if k not in {"provider", "source"}} for r in fusheng_prices()]
    assert PublicPolicy(model_prices=prices).model_prices
    with pytest.raises(ValidationError):
        PublicPolicy(model_prices=prices[:-1])
    for unit in ("TOKEN", "INPUT_TOKEN"):
        with pytest.raises(ValidationError):
            PublicPolicy(model_prices=prices + [prices[0] | {"unit": unit}])
    with pytest.raises(ValidationError):
        PublicPolicy(model_prices=[prices[0] | {"currency": "CNY"}] + prices[1:])


def test_cache_settlement_requires_complete_consistent_supplier_counts():
    base = {"expected_version": 1, "statement_ref": "supplier-invoice", "reason": "verified"}
    for counts in [
        {"cache_read_tokens": 10},
        {"input_tokens": 10, "cache_read_tokens": 10},
        {"input_tokens": 10, "cache_read_tokens": 10, "cache_write_tokens": 1},
    ]:
        with pytest.raises(ValidationError):
            UsageSettlement(**base, **counts)
    parsed = UsageSettlement(
        **base, input_tokens=100, output_tokens=20, cache_read_tokens=10, cache_write_tokens=0
    )
    assert estimate(fusheng_prices(), parsed.model_dump(exclude_none=True))[1] == Decimal("0.000381")


@pytest.mark.anyio
async def test_cache_usage_is_retained_with_immutable_model_bound_price_snapshot(integration_config):
    db = Database(integration_config["databaseUrl"])
    try:
        async with db.transaction() as conn:
            for table in ["ag_model_meter", "ag_quota_reservation"]:
                await execute(
                    conn, f"CREATE TEMP TABLE {table} (LIKE public.{table} INCLUDING DEFAULTS) ON COMMIT DROP"
                )
            pid, call, incompatible = [uuid4() for _ in range(3)]
            prices = [r | {"profile_id": str(pid)} for r in fusheng_prices()]
            policy = {"value": {"model_prices": prices}}
            profile = {"id": pid, "model": "gpt-6.1-sol"}
            await capture_pricing(conn, call, policy, "DIALOG", profile, ("MEMBER", 1))
            await capture_pricing(
                conn, incompatible, policy, "DIALOG", profile | {"model": "different-model"}, ("MEMBER", 1)
            )
            prices[0]["price"] = "999"  # Future policy edits cannot rewrite a call's captured rate.
            usage = {
                "input_tokens": 1000000,
                "output_tokens": 100000,
                "input_tokens_details": {
                    "cached_tokens": 200000,
                    "cache_creation_tokens": 100000,
                    "untrusted_extra": "discard",
                },
            }
            assert await finish_meter(conn, call, usage, True) == Decimal("2.670000")
            saved = await one(conn, "SELECT * FROM ag_model_meter WHERE call_id=:id", id=call)
            assert saved["currency"] == "USD" and saved["usage_state"] == "REPORTED"
            assert saved["usage"]["input_tokens_details"] == {
                "cached_tokens": 200000,
                "cache_creation_tokens": 100000,
            }
            assert saved["pricing"][0]["price"] == "2"
            assert await finish_meter(conn, incompatible, usage, True) is None
            assert (await one(conn, "SELECT pricing FROM ag_model_meter WHERE call_id=:id", id=incompatible))[
                "pricing"
            ] == []
    finally:
        await db.engine.dispose()
