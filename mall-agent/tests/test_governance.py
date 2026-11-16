import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from test_runtime import settings_for

from app.api.admin import staff
from app.application.governance import POLICY_KEY, Governance, PublicPolicy
from app.bootstrap import Services
from app.domain.identity import DomainError, Principal
from app.infrastructure.model_quota import reserve, settle
from app.infrastructure.postgres.database import encoded, execute, one, rows
from app.main import create_app


def operator():
    return Principal(
        1200000000 + uuid4().int % 100000000,
        "governance test",
        "STAFF",
        frozenset(
            {
                "agent:settings:read",
                "agent:settings:write",
                "agent:statistics:read",
                "agent:usage:read",
                "agent:usage:write",
            }
        ),
    )


@pytest.mark.anyio
async def test_settings_permissions_version_history_idempotency_and_shortening_preview(integration_config):
    svc = Services(settings_for(integration_config).model_copy(update={"governance_enabled": True}))
    app = create_app(svc.conversations.settings)
    app.state.services = svc
    current = operator()
    identity = replace(current, permissions=frozenset())
    app.dependency_overrides[staff] = lambda: identity
    try:
        async with svc.db.transaction() as conn:
            await execute(
                conn,
                "DELETE FROM ag_setting WHERE key=:key OR key LIKE 'agent-policy-history:%'",
                key=POLICY_KEY,
            )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/mall-agent/v1/admin/settings")).status_code == 403
            identity = current
            response = await client.get("/mall-agent/v1/admin/settings")
            assert response.status_code == 200 and response.json()["data"]["version"] == 1
            value = response.json()["data"]["value"]
            value["max_input_characters"] = 9000
            body = {"value": value, "expected_version": 1}
            key = str(uuid4())
            first = await client.put(
                "/mall-agent/v1/admin/settings", json=body, headers={"Idempotency-Key": key}
            )
            assert first.status_code == 200 and first.json()["data"]["version"] == 2, first.text
            replay = await client.put(
                "/mall-agent/v1/admin/settings", json=body, headers={"Idempotency-Key": key}
            )
            assert replay.json()["data"] == first.json()["data"]
            assert (
                await client.put(
                    "/mall-agent/v1/admin/settings", json=body, headers={"Idempotency-Key": str(uuid4())}
                )
            ).status_code == 409
            lower = {"value": value | {"message_retention_days": 30}, "expected_version": 2}
            rejected = await client.put(
                "/mall-agent/v1/admin/settings", json=lower, headers={"Idempotency-Key": str(uuid4())}
            )
            assert rejected.status_code == 409 and rejected.json()["code"] == "RETENTION_PREVIEW_REQUIRED"
            invalid = {"value": value | {"arbitrary_shell": "bad"}, "expected_version": 2}
            assert (
                await client.put(
                    "/mall-agent/v1/admin/settings", json=invalid, headers={"Idempotency-Key": str(uuid4())}
                )
            ).status_code == 422
        async with svc.db.transaction() as conn:
            history = await one(conn, "SELECT value FROM ag_setting WHERE key='agent-policy-history:2'")
            assert history["value"]["before"]["version"] == 1 and history["value"]["after"]["version"] == 2
    finally:
        async with svc.db.transaction() as conn:
            await execute(
                conn,
                "DELETE FROM ag_setting WHERE key=:key OR key LIKE 'agent-policy-history:%'",
                key=POLICY_KEY,
            )
        await svc.close()


@pytest.mark.anyio
async def test_multi_subject_unit_reservations_are_atomic_shared_across_versions_and_unknown_is_not_released(
    integration_config,
):
    svc = Services(settings_for(integration_config))
    member = 1500000000 + uuid4().int % 100000000
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
                    "daily_limit": "100",
                }
            ]
        ).model_dump(mode="json")
        async with svc.db.transaction() as conn:
            await execute(
                conn,
                "INSERT INTO ag_setting(key,value,updated_by) VALUES(:key,CAST(:value AS jsonb),0) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
                key=POLICY_KEY,
                value=encoded(policy),
            )

        async def attempt():
            try:
                async with svc.db.transaction() as conn:
                    return await reserve(conn, "DIALOG", profile, 100000, {"TOKEN": 60}, ("MEMBER", member))
            except DomainError as error:
                return error.code

        calls = await asyncio.gather(attempt(), attempt())
        assert calls.count("MODEL_QUOTA_EXCEEDED") == 1
        call = next(value for value in calls if value != "MODEL_QUOTA_EXCEEDED")
        async with svc.db.transaction() as conn:
            # A failed multi-bucket reservation must not consume any partial bucket.
            reserved = await rows(
                conn,
                "SELECT DISTINCT call_id FROM ag_quota_reservation WHERE bucket_key=:key",
                key=f"quota:MEMBER:{member}:DIALOG:TOKEN",
            )
            assert len(reserved) == 1
            await settle(conn, UUID(call), "SUCCEEDED", {"input_tokens": 10, "output_tokens": 10})
        async with svc.db.transaction() as conn:
            second = await reserve(conn, "DIALOG", profile, 100000, {"TOKEN": 60}, ("MEMBER", member))
            await settle(conn, UUID(second), "FAILED", {})
        async with svc.db.transaction() as conn:
            unknown = await one(
                conn,
                "SELECT state,settled_units FROM ag_quota_reservation WHERE call_id=:call AND bucket_key=:key",
                call=UUID(second),
                key=f"quota:MEMBER:{member}:DIALOG:TOKEN",
            )
            assert unknown["state"] == "UNKNOWN" and unknown["settled_units"] is None
        async with svc.db.transaction() as conn:
            with pytest.raises(DomainError, match="MODEL_QUOTA_EXCEEDED"):
                await reserve(
                    conn,
                    "DIALOG",
                    profile | {"version": profile["version"] + 1},
                    100000,
                    {"TOKEN": 30},
                    ("MEMBER", member),
                )
        async with svc.db.transaction() as conn:
            # Another member does not consume the tested member's bucket.
            other = await reserve(conn, "DIALOG", profile, 100000, {"TOKEN": 60}, ("MEMBER", member + 1))
            assert other
            await settle(conn, UUID(other), "SUCCEEDED", {})
            units = await one(
                conn,
                "SELECT state FROM ag_quota_reservation WHERE call_id=:call AND unit_kind='TOKEN' LIMIT 1",
                call=UUID(other),
            )
            assert units["state"] == "UNKNOWN"  # Missing provider usage is not charged as zero.
    finally:
        async with svc.db.transaction() as conn:
            await execute(conn, "DELETE FROM ag_setting WHERE key=:key", key=POLICY_KEY)
        await svc.close()


@pytest.mark.anyio
async def test_statistics_reports_real_units_missing_usage_latency_and_alerts_without_secrets(
    integration_config,
):
    svc = Services(settings_for(integration_config))
    who = operator()
    try:
        await svc.models.setup()
        profile = svc.models.profiles["DIALOG"]
        start = datetime.now(UTC)
        call = uuid4()
        async with svc.db.transaction() as conn:
            await execute(
                conn,
                """INSERT INTO ag_model_call(id,profile_id,profile_version,capability,status,input_tokens,output_tokens,latency_ms)
                VALUES(:id,:profile,:version,'DIALOG','SUCCEEDED',11,22,1234)""",
                id=call,
                profile=profile["id"],
                version=profile["version"],
            )
            await execute(
                conn,
                """INSERT INTO ag_request_metric(id,request_id,operation,method,status_code,duration_ms)
                VALUES(:id,:request,'getProduct','GET',200,42),(:id2,:request2,'getProduct','GET',503,180)""",
                id=uuid4(),
                request=uuid4(),
                id2=uuid4(),
                request2=uuid4(),
            )
        service = Governance(svc.db)
        end = datetime.now(UTC) + timedelta(seconds=1)
        result = await service.statistics(who, start, end, "DIALOG")
        assert result["requests"] == 1 and result["input_tokens"] == "11" and result["output_tokens"] == "22"
        assert result["p95_model_ms"] == 1234 and result["estimated_cost"] is None
        assert result["request_scope"] == "MODEL_CALLS" and result["p95_business_ms"] == 180
        assert result["business"] == {
            "requests": 2,
            "server_errors": 1,
            "rejected_requests": 0,
            "p95_ms": 180,
        }
        assert result["capabilities"][0]["known_input_tokens"] == "11"
        with pytest.raises(DomainError):
            await service.statistics(replace(who, permissions=frozenset()), start, end)
        with pytest.raises(DomainError):
            await service.statistics(who, start, end + timedelta(days=100))
    finally:
        await svc.close()


@pytest.mark.anyio
async def test_usage_reconciliation_uses_frozen_prices_actual_units_permissions_version_and_idempotency(
    integration_config,
):
    svc = Services(settings_for(integration_config))
    who = operator()
    service = Governance(svc.db)
    try:
        await svc.models.setup()
        policy = PublicPolicy(
            model_prices=[
                {
                    "capability": "DIALOG",
                    "unit": "TOKEN",
                    "price": "1",
                    "per_units": 1000000,
                    "currency": "CNY",
                }
            ]
        )
        async with svc.db.transaction() as conn:
            await execute(conn, "DELETE FROM ag_setting WHERE key=:key", key=POLICY_KEY)
            await execute(
                conn,
                "INSERT INTO ag_setting(key,value,updated_by) VALUES(:key,CAST(:value AS jsonb),:actor)",
                key=POLICY_KEY,
                value=encoded(policy.model_dump(mode="json")),
                actor=who.id,
            )
        record = svc.models.recorder("DIALOG")
        call = await record({"stage": "reserve", "reservation_units": {"TOKEN": 100}})
        # Prices changing while IO is in flight must not reprice that call.
        async with svc.db.transaction() as conn:
            await execute(
                conn,
                "UPDATE ag_setting SET value=jsonb_set(value,'{model_prices,0,price}','\"999\"'::jsonb) WHERE key=:key",
                key=POLICY_KEY,
            )
        await record(
            {
                "call_id": call,
                "status": "UNKNOWN",
                "latency_ms": 123,
                "usage": {},
                "provider_request_id": "TEST_ONLY_SUPPLIER",
            }
        )
        page = await service.unknown_usage(who)
        item = next(i for i in page["items"] if i["call_id"] == call)
        assert {r["unit"] for r in item["reservations"]} == {"REQUEST", "TOKEN"}
        body = {
            "expected_version": item["version"],
            "input_tokens": 11,
            "output_tokens": 22,
            "statement_ref": "TEST_ONLY_STATEMENT_1",
            "reason": "隔离测试供应商用量核对",
        }
        key = str(uuid4())
        with pytest.raises(DomainError) as denied:
            await service.reconcile_usage(replace(who, permissions=frozenset()), UUID(call), key, body)
        assert denied.value.status == 403
        with pytest.raises(DomainError) as incomplete:
            await service.reconcile_usage(who, UUID(call), str(uuid4()), body | {"output_tokens": None})
        assert incomplete.value.code == "USAGE_INCOMPLETE"
        result = await service.reconcile_usage(who, UUID(call), key, body)
        assert result["estimated_cost"] == "0.000033" and result["currency"] == "CNY"
        assert await service.reconcile_usage(who, UUID(call), key, body) == result
        with pytest.raises(DomainError) as stale:
            await service.reconcile_usage(who, UUID(call), str(uuid4()), body)
        assert stale.value.code == "VERSION_CONFLICT"
        async with svc.db.transaction() as conn:
            original = await one(
                conn, "SELECT status,input_tokens,output_tokens FROM ag_model_call WHERE id=:id", id=call
            )
            assert (
                original["status"] == "UNKNOWN"
                and original["input_tokens"] == 11
                and original["output_tokens"] == 22
            )
            states = await rows(
                conn,
                "SELECT state,unit_kind,settled_units FROM ag_quota_reservation WHERE call_id=:id",
                id=call,
            )
            assert all(
                r["state"] == "SETTLED" and r["settled_units"] == (33 if r["unit_kind"] == "TOKEN" else 1)
                for r in states
            )
            assert (
                await one(
                    conn,
                    "SELECT count(*) n FROM ag_audit_log WHERE operation='reconcileModelUsage' AND resource_id=:id",
                    id=call,
                )
            )["n"] == 1
        assert not any(i["call_id"] == call for i in (await service.unknown_usage(who))["items"])
    finally:
        async with svc.db.transaction() as conn:
            await execute(conn, "DELETE FROM ag_setting WHERE key=:key", key=POLICY_KEY)
        await svc.close()


@pytest.mark.anyio
async def test_abandoned_meter_is_not_settled_early_and_can_be_audited_without_claiming_model_success(
    integration_config,
):
    svc = Services(settings_for(integration_config))
    who = operator()
    service = Governance(svc.db)
    try:
        await svc.models.setup()
        record = svc.models.recorder("DIALOG")
        call = await record({"stage": "reserve", "reservation_units": {"TOKEN": 100}})
        body = {
            "expected_version": 1,
            "input_tokens": 10,
            "output_tokens": 3,
            "statement_ref": "TEST_ONLY_ABANDONED",
            "reason": "供应商对账记录证实用量，进程未落完成记录",
        }
        with pytest.raises(DomainError) as early:
            await service.reconcile_usage(who, UUID(call), str(uuid4()), body)
        assert early.value.code == "MODEL_CALL_IN_PROGRESS"
        async with svc.db.transaction() as conn:
            await execute(
                conn,
                "UPDATE ag_quota_reservation SET created_at=now()-interval '11 minutes' WHERE call_id=:id",
                id=call,
            )
            await execute(
                conn,
                "UPDATE ag_model_meter SET created_at=now()-interval '11 minutes' WHERE call_id=:id",
                id=call,
            )
            await execute(
                conn,
                "UPDATE ag_model_call SET created_at=now()-interval '11 minutes' WHERE id=:id",
                id=call,
            )
        result = await service.reconcile_usage(who, UUID(call), str(uuid4()), body)
        assert result["usage_state"] == "RECONCILED"
        async with svc.db.transaction() as conn:
            original = await one(
                conn, "SELECT status,error_code,input_tokens FROM ag_model_call WHERE id=:id", id=call
            )
            assert (
                original["status"] == "UNKNOWN"
                and original["error_code"] == "ADAPTER_COMPLETION_MISSING"
                and original["input_tokens"] == 10
            )
        await record(
            {
                "call_id": call,
                "status": "SUCCEEDED",
                "usage": {"input_tokens": 999, "output_tokens": 999},
                "latency_ms": 2,
            }
        )
        async with svc.db.transaction() as conn:
            final = await one(conn, "SELECT status,input_tokens FROM ag_model_call WHERE id=:id", id=call)
            assert final["status"] == "UNKNOWN" and final["input_tokens"] == 10
    finally:
        await svc.close()
