import asyncio
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event
from test_runtime import settings_for

from app.application.governance import POLICY_KEY, PublicPolicy
from app.bootstrap import Services
from app.domain.identity import DomainError
from app.infrastructure.model_quota import model_actor, reserve, settle
from app.infrastructure.postgres.database import encoded, execute, one, ordered_advisory_locks, rows


@pytest.mark.anyio
async def test_batched_multi_bucket_quota_waiters_are_atomic_and_subjects_keep_unknown_usage(
    integration_config,
):
    svc = Services(settings_for(integration_config))
    member = 2100000000 + uuid4().int % 100000000
    counter = [0]

    def count(*args):
        counter[0] += 1

    calls = []
    try:
        await svc.models.setup()
        profile = svc.models.profiles["DIALOG"]
        policy = PublicPolicy(
            quota_limits=[
                {
                    "scope": "MEMBER",
                    "subject_id": str(member),
                    "capability": "DIALOG",
                    "unit": "TOKEN",
                    "daily_limit": "10",
                }
            ]
        ).model_dump(mode="json")
        async with svc.db.transaction() as conn:
            original = await one(conn, "SELECT value,version FROM ag_setting WHERE key=:key", key=POLICY_KEY)
            await execute(
                conn,
                "INSERT INTO ag_setting(key,value,updated_by) VALUES(:key,CAST(:value AS jsonb),0) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
                key=POLICY_KEY,
                value=encoded(policy),
            )
        event.listen(svc.db.engine.sync_engine, "before_cursor_execute", count)
        actor_context = model_actor.set(("MEMBER", member))
        try:
            async with svc.db.transaction() as conn:
                counter[0] = 0
                first = await reserve(conn, "DIALOG", profile, 100000, {"REQUEST": 1, "TOKEN": 2})
                assert counter[0] <= 5, "Reservation round trips must not grow with bucket count"
                calls.append(first)
        finally:
            model_actor.reset(actor_context)
        event.remove(svc.db.engine.sync_engine, "before_cursor_execute", count)

        async def attempt():
            try:
                async with svc.db.transaction() as conn:
                    call = await reserve(conn, "DIALOG", profile, 100000, {"TOKEN": 2}, ("MEMBER", member))
                    calls.append(call)
                    return call
            except DomainError as error:
                return error.code

        results = await asyncio.wait_for(asyncio.gather(*(attempt() for _ in range(12))), 60)
        assert results.count("MODEL_QUOTA_EXCEEDED") == 8 and len(calls) == 5
        async with svc.db.transaction() as conn:
            meters = await rows(
                conn, "SELECT * FROM ag_model_meter WHERE call_id=ANY(CAST(:ids AS uuid[]))", ids=calls
            )
            assert len(meters) == 5 and all(
                m["actor_kind"] == "MEMBER" and m["actor_id"] == member for m in meters
            )
            bucket = await one(
                conn,
                "SELECT count(*) n,sum(reserved_units) units FROM ag_quota_reservation WHERE bucket_key=:key",
                key=f"quota:MEMBER:{member}:DIALOG:TOKEN",
            )
            assert bucket["n"] == 5 and bucket["units"] == Decimal(10)
            groups = await rows(
                conn,
                "SELECT call_id,count(*) n FROM ag_quota_reservation WHERE call_id=ANY(CAST(:ids AS uuid[])) GROUP BY call_id",
                ids=calls,
            )
            assert len(groups) == 5 and len({x["n"] for x in groups}) == 1
            await settle(conn, UUID(first), "SUCCEEDED", {"input_tokens": 0, "output_tokens": 1})
            await settle(conn, UUID(calls[1]), "FAILED", {})
            unknown = await rows(
                conn,
                "SELECT state,settled_units FROM ag_quota_reservation WHERE call_id=:id",
                id=UUID(calls[1]),
            )
            assert unknown and all(x["state"] == "UNKNOWN" and x["settled_units"] is None for x in unknown)
        async with svc.db.transaction() as conn:
            with pytest.raises(DomainError, match="MODEL_QUOTA_EXCEEDED"):
                await reserve(conn, "DIALOG", profile, 100000, {"TOKEN": 2}, ("MEMBER", member))
        async with svc.db.transaction() as conn:
            another = await reserve(conn, "DIALOG", profile, 100000, {"TOKEN": 100}, ("MEMBER", member + 1))
            calls.append(another)
            await settle(conn, UUID(another), "SUCCEEDED", {})
            token = await one(
                conn,
                "SELECT state,settled_units FROM ag_quota_reservation WHERE call_id=:id AND unit_kind='TOKEN' LIMIT 1",
                id=UUID(another),
            )
            assert token["state"] == "UNKNOWN" and token["settled_units"] is None
            # Repeated settlement cannot rewrite units already settled by the first report.
            await settle(conn, UUID(first), "SUCCEEDED", {"input_tokens": 99, "output_tokens": 99})
            token = await one(
                conn,
                "SELECT state,settled_units FROM ag_quota_reservation WHERE call_id=:id AND unit_kind='TOKEN' LIMIT 1",
                id=UUID(first),
            )
            assert token["state"] == "SETTLED" and token["settled_units"] == 1
    finally:
        if event.contains(svc.db.engine.sync_engine, "before_cursor_execute", count):
            event.remove(svc.db.engine.sync_engine, "before_cursor_execute", count)
        async with svc.db.transaction() as conn:
            if "original" in locals() and original:
                await execute(
                    conn,
                    "UPDATE ag_setting SET value=CAST(:value AS jsonb),version=:version WHERE key=:key",
                    value=encoded(original["value"]),
                    version=original["version"],
                    key=POLICY_KEY,
                )
            else:
                await execute(conn, "DELETE FROM ag_setting WHERE key=:key", key=POLICY_KEY)
        await svc.close()


@pytest.mark.anyio
async def test_frozen_profiles_and_prompts_load_in_two_queries_and_keep_version_hash_checks(
    integration_config,
):
    import copy
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from app.infrastructure.model_ledger import ModelLedger

    svc = Services(settings_for(integration_config))
    counter = [0]

    def count(*args):
        counter[0] += 1

    try:
        await svc.models.setup()
        snapshot = await svc.models.capture()
        event.listen(svc.db.engine.sync_engine, "before_cursor_execute", count)
        frozen = await svc.models.for_snapshot(snapshot)
        assert counter[0] == 2
        event.remove(svc.db.engine.sync_engine, "before_cursor_execute", count)
        assert set(frozen.records) == set(snapshot["model_profiles"])
        assert frozen.fact_prompt_id == snapshot["fact_prompt_version_id"]
        bad = copy.deepcopy(snapshot)
        bad["model_profiles"]["DIALOG"]["model"] = "wrong-model"
        with pytest.raises(DomainError, match="MODEL_CONFIG_UNAVAILABLE"):
            await svc.models.for_snapshot(bad)
        bad = copy.deepcopy(snapshot)
        bad["model_profiles"]["DIALOG"]["version"] = 999999
        with pytest.raises(DomainError, match="MODEL_CONFIG_UNAVAILABLE"):
            await svc.models.for_snapshot(bad)
        bad = copy.deepcopy(snapshot)
        bad["routing_prompt_version_id"] = bad["prompt_version_id"]
        with pytest.raises(DomainError, match="PROMPT_VERSION_UNAVAILABLE"):
            await svc.models.for_snapshot(bad)
        pid = snapshot["fact_prompt_version_id"]
        async with svc.db.transaction() as conn:
            await execute(
                conn,
                "UPDATE ag_prompt_version SET content=content||' corrupted' WHERE id=CAST(:id AS uuid)",
                id=pid,
            )

            @asynccontextmanager
            async def transaction(snapshot=False):
                yield conn

            bound = ModelLedger(SimpleNamespace(transaction=transaction), svc.models.base_settings)
            with pytest.raises(DomainError, match="PROMPT_VERSION_UNAVAILABLE"):
                await bound.for_snapshot(snapshot)
            await conn.rollback()
        assert (await svc.models.for_snapshot(snapshot)).prompts["fact-selector"] == frozen.prompts[
            "fact-selector"
        ]
    finally:
        if event.contains(svc.db.engine.sync_engine, "before_cursor_execute", count):
            event.remove(svc.db.engine.sync_engine, "before_cursor_execute", count)
        await svc.close()


@pytest.mark.anyio
async def test_server_side_lock_chain_blocks_then_completes_reverse_order_callers(integration_config):
    svc = Services(settings_for(integration_config))
    prefix = "lock-batch-" + str(uuid4())
    keys = [prefix + ":a", prefix + ":b", prefix + ":c"]
    acquired = asyncio.Event()
    release = asyncio.Event()
    entered = []

    async def holder():
        async with svc.db.transaction() as conn:
            await ordered_advisory_locks(conn, list(reversed(keys)))
            entered.append("holder")
            acquired.set()
            await release.wait()

    async def follower():
        await acquired.wait()
        async with svc.db.transaction() as conn:
            await ordered_advisory_locks(conn, keys + keys)
            entered.append("follower")

    first = asyncio.create_task(holder())
    second = asyncio.create_task(follower())
    try:
        await asyncio.wait_for(acquired.wait(), 15)
        await asyncio.sleep(0.3)
        assert entered == ["holder"] and not second.done()
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 30)
        assert entered == ["holder", "follower"]
    finally:
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)
        await svc.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "capability,requested,usage,expected",
    [
        ("ASR", {"SECOND": "1.25"}, {"seconds": "0.75"}, {"SECOND": Decimal("0.75")}),
        ("TTS", {"CHARACTER": 100}, {"characters": 18}, {"CHARACTER": Decimal(18)}),
    ],
)
async def test_batched_media_units_preserve_precision_and_staff_context(
    integration_config, capability, requested, usage, expected
):
    from app.infrastructure.model_metering import finish_meter

    svc = Services(settings_for(integration_config))
    staff = 2300000000 + uuid4().int % 100000000
    actor_context = model_actor.set(("STAFF", staff))
    try:
        await svc.models.setup()
        async with svc.db.transaction() as conn:
            call = UUID(await reserve(conn, capability, svc.models.profiles[capability], 100000, requested))
            await finish_meter(conn, call, usage, True)
            await settle(conn, call, "SUCCEEDED", usage)
            meter = await one(
                conn, "SELECT actor_kind,actor_id,usage_state FROM ag_model_meter WHERE call_id=:id", id=call
            )
            assert (
                meter["actor_kind"] == "STAFF"
                and meter["actor_id"] == staff
                and meter["usage_state"] == "REPORTED"
            )
            reservations = await rows(
                conn,
                "SELECT unit_kind,state,settled_units FROM ag_quota_reservation WHERE call_id=:id",
                id=call,
            )
            assert reservations and {r["unit_kind"] for r in reservations} == {"REQUEST", *expected}
            for row in reservations:
                assert row["state"] == "SETTLED"
                assert row["settled_units"] == ({"REQUEST": Decimal(1)} | expected)[row["unit_kind"]]
            await conn.rollback()
    finally:
        model_actor.reset(actor_context)
        await svc.close()
