"""Verify deployed staff authorization and the real durable task runtime.

Creates labelled diagnostic jobs and one temporary staff/role fixture. It never
calls order, payment, inventory or after-sale mutation APIs. No credentials output.
"""

import asyncio
import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from prepare_staff import prepare

from app.application.jobs import Jobs
from app.application.runtime_check import runtime_handlers
from app.domain.capabilities import JobType
from app.infrastructure.java_bridge import sign
from app.infrastructure.postgres.database import Database, execute, one
from app.runtime import run
from app.settings import Settings


async def main():
    root = Path(__file__).resolve().parents[2]
    cfg = json.loads((root / "deployment/.run/agent-env.json").read_text())
    runtime = json.loads((root / "deployment/.run/runtime-env.json").read_text())
    settings = Settings(
        database_url=cfg["databaseUrl"],
        bridge_secret=cfg["bridgeSecret"],
        broker_secret=cfg["brokerSecret"],
        cursor_secret=cfg["cursorSecret"],
    )
    db = Database(cfg["databaseUrl"])
    jobs = Jobs(db, settings, runtime_handlers(db, settings))
    evidence = {"time": datetime.now(UTC).isoformat(), "build": settings.build, "checks": []}

    def passed(name):
        evidence["checks"].append(name)
        print("PASS", name, flush=True)

    async with httpx.AsyncClient(base_url="http://127.0.0.1:8201", trust_env=False, timeout=30) as client:

        async def mall(method, path, **kwargs):
            response = await client.request(method, "/mall-admin" + path, **kwargs)
            body = response.json()
            assert body.get("code") == 200, f"Staff fixture request rejected: {path}"
            return body.get("data")

        info = await prepare(client)
        login = await mall(
            "POST",
            "/admin/login",
            json={
                "username": os.environ.get("MALL_DEMO_ADMIN", "admin"),
                "password": os.environ.get("MALL_DEMO_ADMIN_PASSWORD", "MallDemo2026!"),
            },
        )
        admin_token = login["tokenHead"] + login["token"]
        client.headers["Authorization"] = admin_token
        me = await client.get("/mall-agent/v1/admin/me")
        assert me.status_code == 200 and me.json()["data"]["kind"] == "STAFF"
        principal = me.json()["data"]
        assert {"agent:job:read", "agent:job:retry"} <= set(principal["permissions"])
        passed("real_staff_identity_and_explicit_permissions")
        response = await client.get("/mall-admin/agent-bridge/v1/identity")
        assert response.status_code == 403
        passed("unsigned_staff_bridge_denied")
        target = "/mall-admin/agent-bridge/v1/identity"
        timestamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        nonce = str(uuid4())
        headers = {
            "X-Agent-Timestamp": timestamp,
            "X-Agent-Nonce": nonce,
            "X-Agent-Signature": sign("GET", target, b"", timestamp, nonce, runtime["staffBridgeSecret"]),
        }
        assert (await client.get(target, headers=headers)).status_code == 200
        assert (await client.get(target, headers=headers)).status_code == 403
        passed("signed_staff_bridge_nonce_replay_denied")
        member_login = (
            await client.post(
                "/mall-portal/sso/login", data={"username": "member", "password": "MallDemo2026!"}
            )
        ).json()
        member_token = member_login["data"]["tokenHead"] + member_login["data"]["token"]
        member = await client.get("/mall-agent/v1/admin/me", headers={"Authorization": member_token})
        assert member.status_code == 401 or member.json().get("code") == 401
        passed("member_token_cannot_enter_staff_domain")

        role_id, staff_id = None, None
        suffix = secrets.token_hex(5)
        username, password = "agent-verify-" + suffix, secrets.token_urlsafe(24)
        try:
            await mall(
                "POST",
                "/role/create",
                json={
                    "name": "Agent verify " + suffix,
                    "description": "Temporary runtime acceptance fixture",
                    "status": 1,
                    "sort": 999,
                },
            )
            role_id = next(
                r["id"] for r in await mall("GET", "/role/listAll") if r["name"] == "Agent verify " + suffix
            )
            await mall(
                "POST",
                "/role/allocResource",
                data={"roleId": role_id, "resourceIds": info["resources"]["agent:job:read"]},
            )
            staff = await mall(
                "POST",
                "/admin/register",
                json={
                    "username": username,
                    "password": password,
                    "nickName": "Runtime acceptance",
                    "note": "Temporary agent runtime verification",
                },
            )
            staff_id = staff["id"]
            await mall("POST", "/admin/role/update", data={"adminId": staff_id, "roleIds": role_id})
            staff_login = await mall(
                "POST", "/admin/login", json={"username": username, "password": password}
            )
            staff_headers = {"Authorization": staff_login["tokenHead"] + staff_login["token"]}
            assert (await client.get("/mall-agent/v1/admin/jobs", headers=staff_headers)).status_code == 200
            # Keep the same token while changing DB permissions and role/account status.
            await mall(
                "POST",
                "/role/allocResource",
                data={"roleId": role_id, "resourceIds": info["resources"]["agent:job:retry"]},
            )
            assert (await client.get("/mall-agent/v1/admin/jobs", headers=staff_headers)).status_code == 403
            passed("permission_revocation_applies_to_existing_token")
            await mall(
                "POST",
                "/role/allocResource",
                data={"roleId": role_id, "resourceIds": info["resources"]["agent:job:read"]},
            )
            await mall("POST", f"/role/updateStatus/{role_id}", data={"status": 0})
            assert (await client.get("/mall-agent/v1/admin/jobs", headers=staff_headers)).status_code == 403
            passed("disabled_role_immediately_loses_permissions")
            await mall("POST", f"/role/updateStatus/{role_id}", data={"status": 1})
            await mall("POST", f"/admin/updateStatus/{staff_id}", data={"status": 0})
            denied = await client.get("/mall-agent/v1/admin/me", headers=staff_headers)
            assert denied.status_code == 401 or denied.json().get("code") == 401
            passed("disabled_staff_immediately_rejected")
        finally:
            if staff_id:
                await mall("POST", f"/admin/delete/{staff_id}")
            if role_id:
                await mall("POST", "/role/delete", data={"ids": role_id})

        job = await jobs.enqueue(
            JobType.RUNTIME_CHECK, "runtime-smoke:" + str(uuid4()), {}, int(principal["id"])
        )
        for _ in range(80):
            response = await client.get(f"/mall-agent/v1/admin/jobs/{job['id']}")
            assert response.status_code == 200
            view = response.json()["data"]
            if view["state"] in {"SUCCEEDED", "FAILED"}:
                break
            await asyncio.sleep(0.5)
        assert view["state"] == "SUCCEEDED", "Live task worker failed to complete"
        passed("pg_enqueue_live_consumer_success")
        async with db.transaction() as conn:
            record = await one(conn, "SELECT * FROM ag_job WHERE id=:id", id=job["id"])
            assert record["result"]["migration"] == "0003"
            assert {"jobs", "outbox", "scheduler", "generation"} <= set(record["result"]["workers"])
        # Labelled failed fixture exercises the public retry API without installing a fake handler.
        failed_id = uuid4()
        async with db.transaction() as conn:
            await execute(
                conn,
                """INSERT INTO ag_job(id,job_type,dedupe_key,parameters,state,attempts,max_attempts,created_by,error_code)
                VALUES (:id,'RUNTIME_CHECK',:key,'{"schema_version":"runtime-check-v1"}'::jsonb,'FAILED',1,1,:actor,'ACCEPTANCE_SIMULATED_FAILURE')""",
                id=failed_id,
                key="runtime-retry-smoke:" + str(failed_id),
                actor=int(principal["id"]),
            )
        key = str(uuid4())
        payload = {"expected_version": 1}
        first = await client.post(
            f"/mall-agent/v1/admin/jobs/{failed_id}/retry", json=payload, headers={"Idempotency-Key": key}
        )
        second = await client.post(
            f"/mall-agent/v1/admin/jobs/{failed_id}/retry", json=payload, headers={"Idempotency-Key": key}
        )
        assert first.status_code == second.status_code == 202
        assert first.json()["data"] == second.json()["data"]
        for _ in range(80):
            view = (await client.get(f"/mall-agent/v1/admin/jobs/{failed_id}")).json()["data"]
            if view["state"] in {"SUCCEEDED", "FAILED"}:
                break
            await asyncio.sleep(0.5)
        assert view["state"] == "SUCCEEDED" and view["attempts"] == 2
        passed("public_staff_retry_idempotency_then_live_consumer_success")
        # Both task generations must have actually passed publisher confirmation.
        for _ in range(40):
            async with db.transaction() as conn:
                pending = await one(
                    conn,
                    "SELECT count(*) AS n FROM ag_job_wakeup WHERE job_id IN (:a,:b) AND published_at IS NULL",
                    a=job["id"],
                    b=failed_id,
                )
            if pending["n"] == 0:
                break
            await asyncio.sleep(0.5)
        assert pending["n"] == 0
        passed("transactional_wakeups_confirmed_by_real_rabbit")
        page = await client.get("/mall-agent/v1/admin/jobs", params={"limit": 1})
        assert page.status_code == 200 and page.json()["data"]["has_more"]
        assert (
            await client.get(
                "/mall-agent/v1/admin/jobs", params={"limit": 1, "cursor": page.json()["data"]["next_cursor"]}
            )
        ).status_code == 200
        passed("staff_job_cursor_pagination")
        evidence.update(result="passed", job_ids=[str(job["id"]), str(failed_id)])
        (root / "deployment/.run/runtime-smoke.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    await db.engine.dispose()


if __name__ == "__main__":
    run(main())
