import asyncio
import base64
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from test_actions import Bridge, prepare, setup

from app.application.action_recovery import ActionRecovery
from app.bootstrap import Services
from app.domain.identity import DomainError, Principal
from app.infrastructure.postgres.database import encoded, execute, one
from app.infrastructure.staff_auth import StaffAuthorization
from app.main import create_app


def recovery_staff():
    return Principal(
        2100000000 + uuid4().int % 100000000,
        "test recovery",
        "STAFF",
        frozenset({"agent:action:read", "agent:action:reconcile"}),
    )


async def dispatch(svc, aid):
    async with svc.db.transaction() as conn:
        return dict(await one(conn, "SELECT * FROM ag_action_dispatch WHERE action_id=:id", id=aid))


async def due(svc, aid):
    async with svc.db.transaction() as conn:
        await execute(
            conn,
            "UPDATE ag_action_dispatch SET next_attempt_at=now(),lease_until=now()-interval '1 second' WHERE action_id=:id",
            id=aid,
        )


async def finish_synthetic(svc, member):
    async with svc.db.transaction() as conn:
        await execute(
            conn,
            "UPDATE ag_action SET state='FAILED',error_code='TEST_CLEANUP' WHERE member_id=:member AND state IN ('EXECUTING','UNKNOWN')",
            member=member.id,
        )
        await execute(
            conn,
            "UPDATE ag_action_dispatch SET state='TERMINAL',lease_owner=NULL,lease_until=NULL WHERE action_id IN (SELECT id FROM ag_action WHERE member_id=:member)",
            member=member.id,
        )


async def unknown(svc, member, cid, bridge):
    action = await prepare(svc, member, cid)
    bridge.mode = "lost_response"
    value = await svc.extensions.actions.require().confirm(
        member, "synthetic-only", action["id"], str(uuid4()), {"expected_version": action["version"]}
    )
    assert value["state"] == "UNKNOWN"
    return action


@pytest.mark.anyio
async def test_staff_recovery_renews_only_original_query_grant_without_reexecuting(integration_config):
    svc, member, cid, bridge = await setup(integration_config)
    actions = svc.extensions.actions.require()
    staff = Principal(
        2100000000 + uuid4().int % 100000000,
        "test recovery",
        "STAFF",
        frozenset({"agent:action:read", "agent:action:reconcile"}),
    )
    recovery = ActionRecovery(svc.db, svc.conversations.settings)
    try:
        action = await prepare(svc, member, cid)
        bridge.mode = "lost_response"
        await actions.confirm(
            member, "synthetic-only", action["id"], str(uuid4()), {"expected_version": action["version"]}
        )
        async with svc.db.transaction() as conn:
            await execute(
                conn,
                "UPDATE ag_action_dispatch SET grant_expires_at=now()-interval '1 minute',lease_until=NULL,next_attempt_at=now() WHERE action_id=CAST(:id AS uuid)",
                id=action["id"],
            )
        await actions.reconcile_due()
        item = next(r for r in (await recovery.listing(staff))["items"] if r["action_id"] == action["id"])
        assert item["dispatch_state"] == "MANUAL_REQUIRED"
        body = {
            "expected_version": item["action_version"],
            "expected_dispatch_version": item["dispatch_version"],
            "reason": "隔离测试：供应商响应丢失后仅核对原操作",
        }
        key = str(uuid4())
        with pytest.raises(DomainError) as forbidden:
            await recovery.resume(replace(staff, permissions=frozenset()), UUID(action["id"]), key, body)
        assert forbidden.value.status == 403
        result = await recovery.resume(staff, UUID(action["id"]), key, body)
        assert result["reconciliation_only"] and result["dispatch_state"] == "READY"
        assert await recovery.resume(staff, UUID(action["id"]), key, body) == result
        with pytest.raises(DomainError) as stale:
            await recovery.resume(staff, UUID(action["id"]), str(uuid4()), body)
        assert stale.value.code == "VERSION_CONFLICT"
        await actions.reconcile_due()
        assert (await actions.get(member, action["id"]))["state"] == "SUCCEEDED"
        assert len(bridge.calls) == 1
        assert not any(r["action_id"] == action["id"] for r in (await recovery.listing(staff))["items"])
        async with svc.db.transaction() as conn:
            audit = await one(
                conn,
                "SELECT details FROM ag_audit_log WHERE operation='resumeActionReconciliation' AND resource_id=:id",
                id=action["id"],
            )
            assert audit["details"]["authority"] == "RECONCILE_ONLY"
    finally:
        await svc.close()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["CONFIRMATION_INVALID", "JAVA_UNAVAILABLE"])
async def test_rejected_grant_requires_manual_attention_but_transport_failure_remains_retryable(
    integration_config, failure
):
    svc, member, cid, bridge = await setup(integration_config)
    actions = svc.extensions.actions.require()
    try:
        action = await unknown(svc, member, cid, bridge)
        attempts = []

        async def broken(*args):
            attempts.append(args[0])
            raise DomainError(failure, "synthetic failure", 403 if failure == "CONFIRMATION_INVALID" else 503)

        bridge.reconcile_action = broken
        await due(svc, action["id"])
        await actions.reconcile_due()
        current = await dispatch(svc, action["id"])
        assert current["state"] == ("MANUAL_REQUIRED" if failure == "CONFIRMATION_INVALID" else "RECONCILING")
        assert current["last_error_code"] == (
            "RECONCILE_GRANT_REJECTED" if failure == "CONFIRMATION_INVALID" else "OPERATION_RESULT_PENDING"
        )
        assert current["lease_owner"] is None and current["lease_until"] is None
        assert (await actions.get(member, action["id"]))["state"] == "UNKNOWN"
        await due(svc, action["id"])
        await actions.reconcile_due()
        assert len(attempts) == (1 if failure == "CONFIRMATION_INVALID" else 2)
        assert len(bridge.calls) == 1
    finally:
        await finish_synthetic(svc, member)
        await svc.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "tampering", ["parameters", "confirmed_at", "audit_time", "operation_no", "audit_epoch"]
)
async def test_invalid_original_confirmation_evidence_cannot_renew_grant(integration_config, tampering):
    svc, member, cid, bridge = await setup(integration_config)
    recovery = ActionRecovery(svc.db, svc.conversations.settings)
    staff = recovery_staff()
    try:
        action = await unknown(svc, member, cid, bridge)
        async with svc.db.transaction() as conn:
            await execute(
                conn,
                "UPDATE ag_action_dispatch SET state='MANUAL_REQUIRED',lease_until=NULL WHERE action_id=:id",
                id=action["id"],
            )
            if tampering == "parameters":
                await execute(
                    conn,
                    "UPDATE ag_action SET parameters=jsonb_set(parameters,'{items,0,quantity}','2') WHERE id=:id",
                    id=action["id"],
                )
            elif tampering == "confirmed_at":
                await execute(
                    conn,
                    "UPDATE ag_action SET confirmed_at=confirmed_at+interval '1 second' WHERE id=:id",
                    id=action["id"],
                )
            elif tampering == "operation_no":
                await execute(
                    conn,
                    "UPDATE ag_action SET operation_no=:other WHERE id=:id",
                    id=action["id"],
                    other=uuid4(),
                )
            else:
                field, value = (
                    ("confirmed_at", "bad timestamp") if tampering == "audit_time" else ("control_epoch", 999)
                )
                await execute(
                    conn,
                    "UPDATE ag_audit_log SET details=jsonb_set(details,CAST(:path AS text[]),CAST(:value AS jsonb)) WHERE id=(SELECT confirmation_audit_id FROM ag_action_dispatch WHERE action_id=:id)",
                    id=action["id"],
                    path=[field],
                    value=encoded(value),
                )
        before = await dispatch(svc, action["id"])
        item = next(x for x in (await recovery.listing(staff))["items"] if x["action_id"] == action["id"])
        with pytest.raises(DomainError, match="CONFIRMATION_EVIDENCE_INVALID"):
            await recovery.resume(
                staff,
                UUID(action["id"]),
                str(uuid4()),
                {
                    "expected_version": item["action_version"],
                    "expected_dispatch_version": item["dispatch_version"],
                    "reason": "隔离测试确认依据异常",
                },
            )
        assert await dispatch(svc, action["id"]) == before
        assert len(bridge.calls) == 1
    finally:
        await finish_synthetic(svc, member)
        await svc.close()


@pytest.mark.anyio
async def test_recovery_lease_and_permission_rechecks_fence_old_results_and_renew_only_original_claims(
    integration_config,
):
    svc, member, cid, bridge = await setup(integration_config)
    actions = svc.extensions.actions.require()
    recovery = ActionRecovery(svc.db, svc.conversations.settings)
    staff = recovery_staff()
    entered, release = asyncio.Event(), asyncio.Event()
    try:
        action = await unknown(svc, member, cid, bridge)
        original = await dispatch(svc, action["id"])
        first_grant = json.loads(
            base64.urlsafe_b64decode(
                actions.security.decrypt(original["reconcile_grant_ciphertext"]).split(".")[0] + "=="
            )
        )
        reconcile = bridge.reconcile_action

        async def waiting(*args):
            entered.set()
            await release.wait()
            return await reconcile(*args)

        bridge.reconcile_action = waiting
        await due(svc, action["id"])
        task = asyncio.create_task(actions.reconcile_due())
        await asyncio.wait_for(entered.wait(), 15)
        item = next(x for x in (await recovery.listing(staff))["items"] if x["action_id"] == action["id"])
        body = {
            "expected_version": item["action_version"],
            "expected_dispatch_version": item["dispatch_version"],
            "reason": "隔离测试旧租约围栏",
        }
        key = str(uuid4())
        with pytest.raises(DomainError, match="ACTION_RECONCILING"):
            await recovery.resume(staff, UUID(action["id"]), key, body)
        async with svc.db.transaction() as conn:
            await execute(
                conn,
                "UPDATE ag_action_dispatch SET lease_until=now()-interval '1 second' WHERE action_id=:id",
                id=action["id"],
            )
        await recovery.resume(staff, UUID(action["id"]), key, body)
        renewed = await dispatch(svc, action["id"])
        new_grant = json.loads(
            base64.urlsafe_b64decode(
                actions.security.decrypt(renewed["reconcile_grant_ciphertext"]).split(".")[0] + "=="
            )
        )
        for field in (
            "operation_no",
            "action_id",
            "member_id",
            "action_type",
            "parameters_hash",
            "control_epoch",
            "quote_id",
            "confirmation_audit_id",
            "confirmed_at",
        ):
            assert new_grant[field] == first_grant[field]
        assert new_grant["purpose"] == "reconcile_only"
        with pytest.raises(DomainError, match="FORBIDDEN"):
            await recovery.resume(replace(staff, permissions=frozenset()), UUID(action["id"]), key, body)
        release.set()
        await task
        assert (await actions.get(member, action["id"]))["state"] == "UNKNOWN"
        assert await dispatch(svc, action["id"]) == renewed
        bridge.reconcile_action = reconcile
        await actions.reconcile_due()
        assert (await actions.get(member, action["id"]))["state"] == "SUCCEEDED"
        assert len(bridge.calls) == 1
    finally:
        release.set()
        if "task" in locals():
            await task
        await finish_synthetic(svc, member)
        await svc.close()


@pytest.mark.anyio
@pytest.mark.parametrize("phase,expected", [("before_business", "FAILED"), ("after_commit", "SUCCEEDED")])
async def test_actual_confirmation_process_exit_recovers_without_execution_replay(
    integration_config, tmp_path, phase, expected
):
    svc, member, cid, bridge = await setup(integration_config)
    fresh = None
    try:
        action = await prepare(svc, member, cid)
        ledger = tmp_path / "synthetic-business-ledger.json"
        payload = {
            "config": dict(integration_config),
            "envelope": svc.conversations.settings.action_encryption_key.get_secret_value(),
            "member": member.id,
            "action": action["id"],
            "phase": phase,
            "ledger": str(ledger),
        }
        process = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, str(Path(__file__).with_name("recovery_process_worker.py"))],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=45,
        )
        assert process.returncode == 73
        recorded = json.loads(ledger.read_text(encoding="utf-8"))
        assert recorded["execute_attempts"] == 1 and recorded["phase"] == phase
        await due(svc, action["id"])
        cfg = svc.conversations.settings
        await svc.close()
        fresh = Services(cfg)
        new_actions = fresh.extensions.actions.require()
        resumed_bridge = Bridge(member)
        if recorded.get("business"):
            resumed_bridge.results[action["id"]] = recorded["business"]
        new_actions.bridge = resumed_bridge
        await asyncio.gather(new_actions.reconcile_due(), new_actions.reconcile_due())
        result = await new_actions.get(member, action["id"])
        assert result["state"] == expected and not resumed_bridge.calls
        if expected == "FAILED":
            assert result["error_code"] == "RECOVERY_NOT_EXECUTED"
        assert (await dispatch(fresh, action["id"]))["state"] == "TERMINAL"
        assert json.loads(ledger.read_text(encoding="utf-8"))["execute_attempts"] == 1
    finally:
        await finish_synthetic(fresh or svc, member)
        await (fresh or svc).close()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["expired", "undecryptable"])
async def test_local_unusable_grant_never_reaches_business_bridge(integration_config, failure):
    svc, member, cid, bridge = await setup(integration_config)
    try:
        action = await unknown(svc, member, cid, bridge)
        attempts = []

        async def unexpected(*args):
            attempts.append(args[0])
            raise AssertionError("An unusable grant must not be sent")

        bridge.reconcile_action = unexpected
        await due(svc, action["id"])
        async with svc.db.transaction() as conn:
            if failure == "expired":
                await execute(
                    conn,
                    "UPDATE ag_action_dispatch SET grant_expires_at=now()-interval '1 second' WHERE action_id=:id",
                    id=action["id"],
                )
            else:
                await execute(
                    conn,
                    "UPDATE ag_action_dispatch SET reconcile_grant_ciphertext=:broken WHERE action_id=:id",
                    id=action["id"],
                    broken=b"synthetic-undecipherable",
                )
        await svc.extensions.actions.require().reconcile_due()
        current = await dispatch(svc, action["id"])
        assert current["state"] == "MANUAL_REQUIRED"
        assert current["last_error_code"] == (
            "RECONCILE_GRANT_EXPIRED" if failure == "expired" else "RECONCILE_GRANT_UNAVAILABLE"
        )
        assert not attempts and len(bridge.calls) == 1
    finally:
        await finish_synthetic(svc, member)
        await svc.close()


@pytest.mark.anyio
async def test_actual_recovery_http_reauthenticates_before_cached_response_and_denies_expired_or_revoked_staff(
    integration_config,
):
    svc, member, cid, bridge = await setup(integration_config)
    staff = recovery_staff()

    class CurrentIdentity:
        principal = staff
        expired = False
        calls = 0

        async def staff_identity(self, token):
            self.calls += 1
            if self.expired:
                raise DomainError("AUTH_EXPIRED", "员工登录已过期", 401)
            return self.principal

    identity = CurrentIdentity()
    svc.extensions.staff_identity.implementation = StaffAuthorization(identity)
    app = create_app(svc.conversations.settings)
    app.state.services = svc
    try:
        action = await unknown(svc, member, cid, bridge)
        await due(svc, action["id"])
        async with svc.db.transaction() as conn:
            await execute(
                conn,
                "UPDATE ag_action_dispatch SET state='MANUAL_REQUIRED',lease_owner=NULL,lease_until=NULL WHERE action_id=:id",
                id=action["id"],
            )
        current = await dispatch(svc, action["id"])
        value = await svc.extensions.actions.require().get(member, action["id"])
        body = {
            "expected_version": value["version"],
            "expected_dispatch_version": current["version"],
            "reason": "隔离HTTP重新鉴权验证",
        }
        key = str(uuid4())
        path = f"/mall-agent/v1/admin/actions/{action['id']}/reconcile"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://isolated",
            headers={"Authorization": "Bearer synthetic-staff", "Idempotency-Key": key},
        ) as client:
            response = await client.post(path, json=body)
            assert response.status_code == 202 and response.json()["data"]["reconciliation_only"]
            renewed = await dispatch(svc, action["id"])
            identity.principal = replace(staff, permissions=frozenset())
            response = await client.post(path, json=body)
            assert response.status_code == 403
            assert (await client.get("/mall-agent/v1/admin/actions/unresolved")).status_code == 403
            identity.expired = True
            assert (await client.post(path, json=body)).status_code == 401
            assert await dispatch(svc, action["id"]) == renewed
        assert identity.calls == 4 and len(bridge.calls) == 1
        async with svc.db.transaction() as conn:
            count = await one(
                conn,
                "SELECT count(*) n FROM ag_audit_log WHERE operation='resumeActionReconciliation' AND resource_id=:id",
                id=action["id"],
            )
            assert count["n"] == 1
    finally:
        await finish_synthetic(svc, member)
        await svc.close()
