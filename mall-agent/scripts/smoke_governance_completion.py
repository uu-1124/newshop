"""Local gateway acceptance, dedicated expired-message fixture, no model calls."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

import httpx
from expansion_client import ADMIN, MEMBER, login
from expansion_client import api as gateway_api

from app.infrastructure.postgres.database import Database, execute, one
from app.runtime import run

ROOT = Path(__file__).resolve().parents[2]


async def api(client, method, path, body=None, key=None, status=200, params=None):
    if params:
        path += "?" + urlencode(params)
    return await gateway_api(client, path, method, body, key, expected=status)


async def main():
    config = json.loads((ROOT / "deployment/.run/agent-env.json").read_text())
    db = Database(config["databaseUrl"])
    state = {"checked_at": datetime.now(UTC).isoformat(), "checks": []}
    path = ROOT / "deployment/.run/completion-governance-smoke-20261113.json"
    async with (
        httpx.AsyncClient(base_url="http://127.0.0.1:8201", trust_env=False, timeout=60) as staff,
        httpx.AsyncClient(base_url="http://127.0.0.1:8201", trust_env=False, timeout=40) as buyer,
    ):
        await login(staff, True)
        await login(buyer, username="agent-lifecycle-6c72a0f5")
        original = await api(staff, "GET", ADMIN + "/settings")
        current = original
        cid = None

        def passed(name):
            state["checks"].append(name)
            path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            print("PASS", name, flush=True)

        async def wait_job(jid):
            for _ in range(100):
                job = await api(staff, "GET", ADMIN + "/jobs/" + jid)
                if job["state"] in {"FAILED", "CANCELLED"}:
                    raise AssertionError("Gateway task failed: " + str(job["error_code"]))
                if job["state"] == "SUCCEEDED":
                    return job
                await asyncio.sleep(1)
            raise AssertionError("Gateway task not completed")

        try:
            me = await api(staff, "GET", ADMIN + "/me")
            assert {
                "agent:settings:read",
                "agent:settings:write",
                "agent:statistics:read",
                "agent:retention:preview",
                "agent:retention:execute",
                "agent:usage:read",
                "agent:action:reconcile",
            } <= set(me["permissions"])
            passed("fresh_governance_permissions_and_versioned_settings")
            changed = original["value"] | {
                "max_input_characters": min(9000, original["value"]["max_input_characters"])
            }
            body = {"value": changed, "expected_version": original["version"]}
            key = str(uuid4())
            current = await api(staff, "PUT", ADMIN + "/settings", body, key=key)
            assert await api(staff, "PUT", ADMIN + "/settings", body, key=key) == current
            conv = await api(
                buyer,
                "POST",
                MEMBER + "/conversations",
                {"title": "[TEST ONLY] governance expiry", "entry_context": {"kind": "GENERAL"}},
                status=201,
            )
            cid = conv["id"]
            response = await buyer.post(
                MEMBER + f"/conversations/{cid}/messages",
                headers={"Idempotency-Key": str(uuid4())},
                json={
                    "client_message_id": str(uuid4()),
                    "text": "x" * (changed["max_input_characters"] + 1),
                    "attachment_ids": [],
                },
            )
            assert response.status_code == 422 and response.json()["code"] == "INPUT_TOO_LONG"
            passed("setting_applied_to_real_member_message_and_idempotent_replay")
            mid = uuid4()
            async with db.transaction() as conn:
                convrow = await one(
                    conn, "SELECT * FROM ag_conversation WHERE id=CAST(:id AS uuid) FOR UPDATE", id=cid
                )
                await execute(
                    conn,
                    """INSERT INTO ag_message(id,conversation_id,seq,sender_kind,sender_id,status,text_content,created_at,retention_until)
                    VALUES(:id,CAST(:cid AS uuid),:seq,'MEMBER',:member,'COMPLETED','[TEST ONLY] expired fixture',now()-interval '100 days',now()-interval '10 days')""",
                    id=mid,
                    cid=cid,
                    seq=convrow["next_message_seq"],
                    member=convrow["member_id"],
                )
                await execute(
                    conn,
                    "UPDATE ag_conversation SET next_message_seq=next_message_seq+1 WHERE id=CAST(:id AS uuid)",
                    id=cid,
                )
            snapshot = await api(buyer, "GET", MEMBER + "/conversations/" + cid)
            assert not snapshot["recent_messages"]
            preview = await api(
                staff,
                "POST",
                ADMIN + "/retention/preview",
                {"proposed_settings": current["value"]},
                status=202,
            )
            result = await wait_job(preview["id"])
            assert result["result"]["messages"] >= 1
            state["preview_job_id"] = preview["id"]
            job = await api(
                staff,
                "POST",
                ADMIN + "/retention/execute",
                {"preview_job_id": preview["id"], "expected_settings_version": current["version"]},
                status=202,
            )
            finished = await wait_job(job["id"])
            state["execution_job_id"] = job["id"]
            assert finished["result"]["messages"] >= 1
            async with db.transaction() as conn:
                row = await one(conn, "SELECT text_content,deleted_at FROM ag_message WHERE id=:id", id=mid)
                assert row["deleted_at"] and row["text_content"] == ""
                assert await one(
                    conn,
                    "SELECT resource_id FROM ag_erasure_tombstone WHERE resource_type='MESSAGE' AND resource_id=:id",
                    id=str(mid),
                )
            passed("live_retention_preview_fixed_cutoff_execution_tombstone_and_immediate_expiry_filter")
            stats = await api(
                staff,
                "GET",
                ADMIN + "/statistics",
                params={
                    "from": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
                    "to": datetime.now(UTC).isoformat(),
                },
            )
            assert stats["business"]["requests"] > 0 and stats["p95_business_ms"] is not None
            state["business_p95_ms"] = stats["p95_business_ms"]
            state["business_requests"] = stats["business"]["requests"]
            assert "items" in await api(staff, "GET", ADMIN + "/model-usage/unknown")
            assert "items" in await api(staff, "GET", ADMIN + "/actions/unresolved")
            passed("real_request_statistics_unknown_usage_and_unresolved_action_endpoints")
        finally:
            if current["value"] != original["value"]:
                latest = await api(staff, "GET", ADMIN + "/settings")
                restored = await api(
                    staff,
                    "PUT",
                    ADMIN + "/settings",
                    {"value": original["value"], "expected_version": latest["version"]},
                )
                state["restored_settings_version"] = restored["version"]
            if cid:
                snapshot = await api(buyer, "GET", MEMBER + "/conversations/" + cid)
                await api(
                    buyer,
                    "DELETE",
                    MEMBER + "/conversations/" + cid,
                    {"expected_version": snapshot["conversation"]["version"]},
                    status=200,
                )
                state["test_conversation_deleted"] = True
            await db.engine.dispose()
            path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    run(main())
