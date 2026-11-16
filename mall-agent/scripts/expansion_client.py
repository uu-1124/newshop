"""Local demo acceptance helper. Never print or persist credentials or raw business data."""

import os
from uuid import uuid4

ADMIN = "/mall-agent/v1/admin"
MEMBER = "/mall-agent/v1/member"


async def login(client, staff=False, username=None):
    username = username or (os.environ.get("MALL_DEMO_ADMIN", "admin") if staff else "member")
    credentials = {
        "username": username,
        "password": os.environ.get("MALL_DEMO_ADMIN_PASSWORD", "MallDemo2026!") if staff else "MallDemo2026!",
    }
    response = await client.post(
        "/mall-admin/admin/login" if staff else "/mall-portal/sso/login",
        **({"json": credentials} if staff else {"data": credentials}),
    )
    data = response.json()
    assert data.get("code") == 200, "Demo login rejected"
    client.headers["Authorization"] = data["data"]["tokenHead"] + data["data"]["token"]


async def api(client, path, method="GET", body=None, key=None, expected=200):
    response = await client.request(
        method, path, json=body, headers={"Idempotency-Key": str(key or uuid4())} if method != "GET" else {}
    )
    data = response.json()
    assert response.status_code == expected and data.get("code") == "OK", (
        path,
        response.status_code,
        data.get("code"),
    )
    return data["data"]


async def wait_run(client, cid, timeout=180):
    import asyncio
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        snap = await api(client, MEMBER + "/conversations/" + cid)
        if not snap["active_run"]:
            messages = [m for m in snap["recent_messages"] if m["sender_kind"] == "ASSISTANT"]
            assert messages and messages[-1]["status"] == "COMPLETED", [
                (m["status"], m["run_id"]) for m in messages
            ]
            return messages[-1]
        await asyncio.sleep(1)
    raise AssertionError("Generation did not complete")


async def wait_job(client, jid, timeout=180):
    import asyncio
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        job = await api(client, ADMIN + "/jobs/" + jid)
        if job["state"] == "SUCCEEDED":
            return job
        assert job["state"] != "FAILED", job["error_code"]
        await asyncio.sleep(1)
    raise AssertionError("Background job did not complete")
