"""Exercise the real demo gateway. No credentials or bearer tokens are persisted.

Creates clearly marked synthetic conversations and a temporary reception account.
The temporary account is disabled and reception presences are restored afterwards.
"""

import asyncio
import json
import os
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

BASE = "http://127.0.0.1:8201"
ROOT = Path(__file__).resolve().parents[2]


async def main():
    evidence = {"verified_at": datetime.now(UTC).isoformat(), "checks": []}
    temporary_id = None
    initial_presence = None
    current_handoff = None
    conv_id = None
    member_headers, staff_headers, target_headers = {}, {}, {}
    async with httpx.AsyncClient(base_url=BASE, timeout=20, trust_env=False) as client:

        async def call(method, path, headers=None, body=None, key=None, legacy=False, **kwargs):
            request_headers = dict(headers or {})
            if key:
                request_headers["Idempotency-Key"] = key
            response = await client.request(method, path, headers=request_headers, json=body, **kwargs)
            result = response.json()
            if not response.is_success or result.get("code") != (200 if legacy else "OK"):
                raise RuntimeError(
                    f"Smoke rejected at {path}: status={response.status_code}, code={result.get('code')}"
                )
            return result["data"]

        async def staff_login(username, password):
            login = await call(
                "POST",
                "/mall-admin/admin/login",
                body={"username": username, "password": password},
                legacy=True,
            )
            return {"Authorization": login["tokenHead"] + login["token"]}

        async def staff_self(headers):
            me = await call("GET", "/mall-agent/v1/admin/me", headers=headers)
            page = await call("GET", "/mall-agent/v1/admin/staff?limit=100", headers=headers)
            return next(s for s in page["items"] if s["id"] == me["id"])

        async def mode(headers, value):
            row = await staff_self(headers)
            return await call(
                "PUT",
                "/mall-agent/v1/admin/presence",
                headers=headers,
                key=str(uuid4()),
                body={"expected_version": row["version"], "presence": value},
            )

        async def snapshot():
            return await call("GET", f"/mall-agent/v1/member/conversations/{conv_id}", headers=member_headers)

        async def member_send(text):
            key = str(uuid4())
            return await call(
                "POST",
                f"/mall-agent/v1/member/conversations/{conv_id}/messages",
                headers=member_headers,
                key=key,
                body={"client_message_id": key, "text": text, "attachment_ids": []},
            )

        async def request_handoff():
            for _ in range(4):
                snap = await snapshot()
                response = await client.post(
                    f"/mall-agent/v1/member/conversations/{conv_id}/handoff",
                    headers=member_headers | {"Idempotency-Key": str(uuid4())},
                    json={
                        "expected_conversation_version": snap["conversation"]["version"],
                        "reason": "[TEST ONLY] reception verification",
                    },
                )
                result = response.json()
                if response.status_code == 201:
                    return result["data"]
                if result.get("code") != "VERSION_CONFLICT":
                    raise RuntimeError(f"Handoff rejected: {result.get('code')}")
            raise RuntimeError("Conversation version kept changing")

        try:
            staff_headers = await staff_login(
                os.environ.get("MALL_DEMO_ADMIN", "admin"),
                os.environ.get("MALL_DEMO_ADMIN_PASSWORD", "MallDemo2026!"),
            )
            original = await staff_self(staff_headers)
            initial_presence = original["presence"]
            await mode(staff_headers, "ONLINE")
            login = await call(
                "POST",
                "/mall-portal/sso/login",
                legacy=True,
                data={
                    "username": "member",
                    "password": os.environ.get("MALL_DEMO_MEMBER_PASSWORD", "MallDemo2026!"),
                },
            )
            member_headers = {"Authorization": login["tokenHead"] + login["token"]}
            role = json.loads((ROOT / "deployment/.run/reception-role.json").read_text(encoding="utf-8"))
            username = "handoff_test_" + uuid4().hex[:8]
            password = secrets.token_urlsafe(24) + "aA1!"
            account = await call(
                "POST",
                "/mall-admin/admin/register",
                headers=staff_headers,
                legacy=True,
                body={
                    "username": username,
                    "password": password,
                    "nickName": "[TEST ONLY] transfer target",
                    "email": username + "@example.invalid",
                    "note": "Synthetic handoff verification; disabled after test",
                },
            )
            temporary_id = account["id"]
            await call(
                "POST",
                "/mall-admin/admin/role/update",
                headers=staff_headers,
                legacy=True,
                data={"adminId": temporary_id, "roleIds": str(role["role_id"])},
            )
            target_headers = await staff_login(username, password)
            await mode(target_headers, "ONLINE")
            evidence["checks"].append("fresh_staff_permissions_and_target_profile")

            conv = await call(
                "POST",
                "/mall-agent/v1/member/conversations",
                headers=member_headers,
                key=str(uuid4()),
                body={"title": "[TEST ONLY] human reception", "entry_context": {"kind": "GENERAL"}},
            )
            conv_id = conv["id"]
            sent = await member_send("[TEST ONLY] 请简单介绍商城咨询的用途。")
            old_run = sent["run"]["id"]
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                snap = await snapshot()
                if snap["active_run"] and snap["active_run"]["state"] == "RUNNING":
                    break
                await asyncio.sleep(0.2)
            else:
                raise RuntimeError("Generation did not start before the takeover check")
            current_handoff = await request_handoff()
            await asyncio.sleep(1)
            snap = await snapshot()
            assert snap["active_run"] is None
            assert (
                snap["conversation"]["state"] == "WAITING_HUMAN"
                and not snap["conversation"]["waiting_ai_enabled"]
            )
            run = await call("GET", f"/mall-agent/v1/member/runs/{old_run}", headers=member_headers)
            assert run["state"] == "STOPPED"
            assert (await member_send("[TEST ONLY] 我会等待客服。"))["run"] is None
            evidence["checks"].append("real_running_generation_fenced_and_waiting_member_message_has_no_run")

            current_handoff = await call(
                "POST",
                f"/mall-agent/v1/admin/handoffs/{current_handoff['id']}/claim",
                headers=staff_headers,
                key=str(uuid4()),
                body={"expected_version": current_handoff["version"]},
            )
            snap = await snapshot()
            assert snap["conversation"]["state"] == "HUMAN_ACTIVE"
            assert snap["conversation"]["assigned_staff"]["id"] == original["id"]
            key = str(uuid4())
            body = {
                "client_message_id": key,
                "text": "[TEST ONLY] 您好，人工客服已接入。",
                "attachment_ids": [],
                "cards": [],
            }
            first = await call(
                "POST",
                f"/mall-agent/v1/admin/conversations/{conv_id}/messages",
                headers=staff_headers,
                key=key,
                body=body,
            )
            replay = await call(
                "POST",
                f"/mall-agent/v1/admin/conversations/{conv_id}/messages",
                headers=staff_headers,
                key=key,
                body=body,
            )
            assert first["id"] == replay["id"] and first["sender_kind"] == "STAFF"
            note_key = str(uuid4())
            await call(
                "POST",
                f"/mall-agent/v1/admin/conversations/{conv_id}/notes",
                headers=staff_headers,
                key=note_key,
                body={"client_note_id": note_key, "body": "[TEST ONLY] INTERNAL_NOTE_MUST_STAY_PRIVATE"},
            )
            snap = await snapshot()
            assert "INTERNAL_NOTE_MUST_STAY_PRIVATE" not in json.dumps(snap)
            assert (await member_send("[TEST ONLY] 谢谢，人工回复已收到。"))["run"] is None
            evidence["checks"].append("claim_staff_reply_idempotency_member_reply_and_private_note")

            # Subscribe using a current snapshot cursor, then deliver a real staff event.
            cursor = (await snapshot())["event_cursor"]
            async with client.stream(
                "GET",
                f"/mall-agent/v1/member/conversations/{conv_id}/events?after={cursor}",
                headers=member_headers,
            ) as stream:
                assert stream.status_code == 200
                key = str(uuid4())
                response = await call(
                    "POST",
                    f"/mall-agent/v1/admin/conversations/{conv_id}/messages",
                    headers=staff_headers,
                    key=key,
                    body={
                        "client_message_id": key,
                        "text": "[TEST ONLY] 实时通知验证。",
                        "attachment_ids": [],
                        "cards": [],
                    },
                )
                async for line in stream.aiter_lines():
                    if line.startswith("data: "):
                        event = json.loads(line[6:])
                        assert "INTERNAL_NOTE_MUST_STAY_PRIVATE" not in line
                        if (
                            event["type"] == "message.completed"
                            and event["payload"]["message"]["id"] == response["id"]
                        ):
                            break
            evidence["checks"].append("real_authenticated_member_sse_delivers_staff_message")

            await call(
                "POST",
                f"/mall-admin/admin/updateStatus/{temporary_id}",
                headers=staff_headers,
                legacy=True,
                data={"status": 0},
            )
            transfer_body = {
                "expected_version": current_handoff["version"],
                "target_staff_id": str(temporary_id),
                "reason": "[TEST ONLY] test transfer",
            }
            rejected = await client.post(
                f"/mall-agent/v1/admin/handoffs/{current_handoff['id']}/transfer",
                headers=staff_headers | {"Idempotency-Key": str(uuid4())},
                json=transfer_body,
            )
            assert rejected.status_code == 403
            await call(
                "POST",
                f"/mall-admin/admin/updateStatus/{temporary_id}",
                headers=staff_headers,
                legacy=True,
                data={"status": 1},
            )
            target_headers = await staff_login(username, password)
            await mode(target_headers, "ONLINE")
            current_handoff = await call(
                "POST",
                f"/mall-agent/v1/admin/handoffs/{current_handoff['id']}/transfer",
                headers=staff_headers,
                key=str(uuid4()),
                body=transfer_body,
            )
            snap = await snapshot()
            assert snap["conversation"]["assigned_staff"]["id"] == str(temporary_id)
            old_owner = await client.get(
                f"/mall-agent/v1/admin/conversations/{conv_id}", headers=staff_headers
            )
            assert old_owner.status_code == 404
            current_handoff = await call(
                "POST",
                f"/mall-agent/v1/admin/handoffs/{current_handoff['id']}/close",
                headers=target_headers,
                key=str(uuid4()),
                body={
                    "expected_version": current_handoff["version"],
                    "reason": "[TEST ONLY] verification complete",
                },
            )
            snap = await snapshot()
            assert current_handoff["state"] == "CLOSED" and snap["conversation"]["state"] == "CLOSED"
            evidence["checks"].append(
                "disabled_target_rejected_fresh_then_transfer_old_owner_blocked_and_close"
            )
            evidence.update(
                {
                    "conversation_id": conv_id,
                    "old_run_id": old_run,
                    "handoff_id": current_handoff["id"],
                    "final_state": "CLOSED",
                }
            )
        finally:
            if conv_id and member_headers:
                try:
                    snap = await snapshot()
                    handoff = snap["handoff"]
                    if handoff and handoff["state"] == "WAITING":
                        await call(
                            "DELETE",
                            f"/mall-agent/v1/member/conversations/{conv_id}/handoff",
                            headers=member_headers,
                            key=str(uuid4()),
                            body={"expected_version": handoff["version"]},
                        )
                    elif handoff and handoff["state"] == "CLAIMED":
                        owner = target_headers if handoff["staff_id"] == str(temporary_id) else staff_headers
                        await call(
                            "POST",
                            f"/mall-agent/v1/admin/handoffs/{handoff['id']}/close",
                            headers=owner,
                            key=str(uuid4()),
                            body={"expected_version": handoff["version"], "reason": "[TEST ONLY] cleanup"},
                        )
                except Exception:
                    evidence["cleanup_error"] = "reception_cleanup_requires_review"
            if temporary_id:
                try:
                    await mode(target_headers, "OFFLINE")
                finally:
                    await call(
                        "POST",
                        f"/mall-admin/admin/updateStatus/{temporary_id}",
                        headers=staff_headers,
                        legacy=True,
                        data={"status": 0},
                    )
                    evidence["temporary_staff_disabled"] = True
            if initial_presence and staff_headers:
                await mode(staff_headers, initial_presence)
            (ROOT / "deployment/.run/handoff-smoke.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    print(
        "PASS real gateway human reception: "
        + str(len(evidence["checks"]))
        + " groups; temporary staff disabled; test conversation closed"
    )


if __name__ == "__main__":
    asyncio.run(main())
