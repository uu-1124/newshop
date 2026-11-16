"""Real local backup governance acceptance; no model or commerce execution."""

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

from app.runtime import run

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "deployment/.run/completion-backup-governance-live-20261113.json"
BASE = "/mall-agent/v1/admin"


async def main():
    state = {"started_at": datetime.now(UTC).isoformat(), "checks": [], "jobs": []}

    def save():
        OUTPUT.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def passed(check):
        state["checks"].append(check)
        save()
        print("PASS", check, flush=True)

    async with httpx.AsyncClient(base_url="http://127.0.0.1:8201", trust_env=False, timeout=30) as client:
        r = await client.post(
            "/mall-admin/admin/login", json={"username": "admin", "password": "MallDemo2026!"}
        )
        data = r.json()
        assert data.get("code") == 200, "Demo administrator login rejected"
        client.headers["Authorization"] = data["data"]["tokenHead"] + data["data"]["token"]

        async def api(path, method="GET", body=None, key=None):
            r = await client.request(
                method,
                BASE + path,
                json=body,
                headers={"Idempotency-Key": str(key or uuid4())} if method != "GET" else {},
            )
            d = r.json()
            assert r.status_code < 400 and d.get("code") == "OK", (path, r.status_code, d.get("code"))
            return d["data"]

        me = await api("/me")
        assert "agent:backup:verify" in me["permissions"]
        page = await api("/backups")
        assert page["items"] and page["executor"]
        state["initial_backup_ids"] = [r["id"] for r in page["items"]]
        save()
        passed("registered_backups_visible_to_explicit_permission")
        for body, status in [
            ({"backup_record_id": "../../private", "note": "[TEST ONLY] reject path"}, 422),
            ({"backup_record_id": str(uuid4()), "note": "[TEST ONLY] absent"}, 404),
        ]:
            r = await client.post(
                BASE + "/backups/verify", json=body, headers={"Idempotency-Key": str(uuid4())}
            )
            assert r.status_code == status
        passed("arbitrary_path_and_unregistered_backup_rejected")
        r = await client.post(
            BASE + "/backups",
            json={
                "expected_policy_version": page["policy"]["version"],
                "maintenance_confirmed": False,
                "note": "[TEST ONLY] do not pause",
            },
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert r.status_code == 409
        passed("maintenance_requires_explicit_confirmation")
        selected = max(page["items"], key=lambda x: x["created_at"])
        key = str(uuid4())
        body = {
            "backup_record_id": selected["id"],
            "note": "[TEST ONLY] authenticated archive then isolated restore; no business reopen",
        }
        first = await api("/backups/verify", "POST", body, key)
        again = await api("/backups/verify", "POST", body, key)
        assert first == again
        state["verify_job_id"] = first["id"]
        save()
        passed("replayed_verify_request_returns_same_durable_job")
        deadline = time.monotonic() + 900

        async def wait_job(jid):
            while time.monotonic() < deadline:
                job = await api("/jobs/" + jid)
                if job["state"] in {"SUCCEEDED", "FAILED"}:
                    state["jobs"].append(job)
                    save()
                    assert job["state"] == "SUCCEEDED", (job["id"], job["error_code"])
                    return job
                await asyncio.sleep(3)
            raise RuntimeError("Registered backup job did not finish")

        verified = await wait_job(first["id"])
        assert verified["result"]["verified"] is False and verified["result"]["restore_job_id"]
        passed("archive_authentication_is_distinct_from_actual_restore")
        restored = await wait_job(verified["result"]["restore_job_id"])
        assert restored["result"]["verified"] and restored["result"]["isolation_destroyed"]
        state["manual_restore"] = restored["result"]
        passed("independent_executor_restores_and_cleans_owned_isolation")
        page = await api("/backups")
        selected = next(x for x in page["items"] if x["id"] == selected["id"])
        assert selected["last_restore_id"] == restored["result"]["restore_id"]
        passed("public_registry_matches_verified_restore_result")
        state["result"] = "MANUAL_GOVERNANCE_BACKUP_RESTORE_PASSED"
        state["finished_at"] = datetime.now(UTC).isoformat()
        save()


if __name__ == "__main__":
    run(main())
