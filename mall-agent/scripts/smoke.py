"""Real HTTP smoke. Creates only labelled agent demo records; reads mall products, never orders."""

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
from jsonschema import Draft202012Validator, RefResolver

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "docs/api/openapi.json").read_text(encoding="utf-8"))
EVENTS = json.loads((ROOT / "docs/api/sse-events.schema.json").read_text(encoding="utf-8"))
RESOLVER = RefResolver(
    base_uri="https://mall.local/openapi.json",
    referrer=CONTRACT,
    store={"https://mall.local/openapi.json": CONTRACT},
)
EVENT_RESOLVER = RefResolver(
    base_uri="https://mall.local/sse-events.schema.json",
    referrer=EVENTS,
    store={"https://mall.local/openapi.json": CONTRACT},
)


def validate(name, body):
    Draft202012Validator(CONTRACT["components"]["schemas"][name], resolver=RESOLVER).validate(body)


async def main():
    async with httpx.AsyncClient(timeout=10, trust_env=False) as health:
        mode = (await health.get("http://127.0.0.1:8099/readyz")).json()["capabilities"]["dialog"]
    live = mode == "LIVE"
    evidence = {
        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "provider": "openai_responses" if live else "fixture",
        "checks": [],
    }

    def passed(name):
        evidence["checks"].append(name)
        print("PASS", name)

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8201", timeout=httpx.Timeout(130, connect=10), trust_env=False
    ) as client:
        login = (
            await client.post(
                "/mall-portal/sso/login",
                data={
                    "username": os.environ.get("MALL_DEMO_USER", "member"),
                    "password": os.environ.get("MALL_DEMO_PASSWORD", "MallDemo2026!"),
                },
            )
        ).json()
        assert login["code"] == 200, "Mall demo login failed"
        token = login["data"]["tokenHead"] + login["data"]["token"]
        client.headers["Authorization"] = token
        prefix = "/mall-agent/v1/member"

        async def call(method, path, body=None, key=None, expected=200, schema=None):
            headers = {"Idempotency-Key": str(key or uuid4())} if method == "POST" else {}
            response = await client.request(method, path, json=body, headers=headers)
            assert response.status_code == expected, f"{method} {path}: HTTP {response.status_code}"
            data = response.json()
            if schema:
                validate(schema, data)
            return data["data"]

        me = await call("GET", prefix + "/me", schema="memberMeResponse")
        assert me["kind"] == "MEMBER"
        passed("real_member_identity")
        unauthorized = await client.get(
            prefix + "/me", headers={"Authorization": "Bearer invalid-test-token"}
        )
        assert unauthorized.status_code == 401
        passed("invalid_token_denied")
        unsigned = await client.get("/mall-portal/agent-bridge/v1/identity")
        assert unsigned.status_code == 403
        passed("unsigned_bridge_denied")
        hidden = await client.get("/mall-agent/internal/runtime-auth/test/products")
        assert hidden.status_code != 200 or hidden.json().get("code") != "OK"
        passed("internal_broker_not_routed")

        if "--verify-persistence" in sys.argv:
            evidence_path = ROOT / "deployment/.run/agent-smoke.json"
            previous = json.loads(evidence_path.read_text(encoding="utf-8"))
            restored = await call(
                "GET",
                prefix + f"/conversations/{previous['conversation_id']}",
                schema="getConversationResponse",
            )
            assert len(restored["recent_messages"]) >= 2
            assert any(m["sender_kind"] == "ASSISTANT" and m["text"] for m in restored["recent_messages"])
            previous["checks"].append("history_preserved_after_api_restart")
            evidence_path.write_text(json.dumps(previous, ensure_ascii=False, indent=2), encoding="utf-8")
            passed("history_preserved_after_api_restart")
            return

        key = uuid4()
        body = {"title": "架构闭环验收", "entry_context": {"kind": "GENERAL"}}
        conversation = await call(
            "POST", prefix + "/conversations", body, key, 201, "createConversationResponse"
        )
        replay = await call("POST", prefix + "/conversations", body, key, 201)
        assert replay["id"] == conversation["id"]
        passed("create_idempotency")
        cid = conversation["id"]
        await call("GET", prefix + "/conversations", schema="listConversationsResponse")
        snapshot = await call("GET", prefix + f"/conversations/{cid}", schema="getConversationResponse")
        payload = {
            "client_message_id": str(uuid4()),
            "text": "架构闭环验收😀：请调用商品查询工具查询手机，列出真实商品名称、商品ID和价格，不超过200字。",
            "attachment_ids": [],
        }
        key = uuid4()
        sent = await call(
            "POST", prefix + f"/conversations/{cid}/messages", payload, key, 202, "sendMemberMessageResponse"
        )
        duplicate = await call("POST", prefix + f"/conversations/{cid}/messages", payload, key, 202)
        assert sent["message"]["id"] == duplicate["message"]["id"]
        assert sent["run"]["id"] == duplicate["run"]["id"]
        passed("send_idempotency")

        deadline = time.monotonic() + 125
        events = []
        async with client.stream(
            "GET", prefix + f"/conversations/{cid}/events", params={"after": snapshot["event_cursor"]}
        ) as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    event = json.loads(line[6:])
                    Draft202012Validator(EVENTS, resolver=EVENT_RESOLVER).validate(event)
                    events.append(event)
                    if event["type"] == "message.completed":
                        assert event["payload"]["message"]["status"] == "COMPLETED", (
                            "Generation did not complete"
                        )
                        break
                assert time.monotonic() < deadline, "SSE timeout"
        ids = [int(e["event_id"]) for e in events]
        assert ids == sorted(set(ids))
        assert any(e["type"] == "message.delta" for e in events)
        passed("schema_valid_persistent_sse")
        run = await call("GET", prefix + f"/runs/{sent['run']['id']}", schema="getRunResponse")
        assert run["state"] == "COMPLETED"
        snapshot = await call("GET", prefix + f"/conversations/{cid}", schema="getConversationResponse")
        assert len(snapshot["recent_messages"]) == 2
        assert bool(snapshot["recent_messages"][-1]["text"])
        assert ("架构联调测试回复" not in snapshot["recent_messages"][-1]["text"]) == live
        await call("GET", prefix + f"/conversations/{cid}/messages", schema="listMessagesResponse")
        passed("persistent_history_and_live_model" if live else "persistent_history_and_fixture_label")
        async with client.stream(
            "GET", prefix + f"/conversations/{cid}/events", params={"after": str(ids[-2])}
        ) as response:
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    assert json.loads(line[6:])["event_id"] == str(ids[-1])
                    break
        passed("sse_resume_after_cursor")

        payload = {"client_message_id": str(uuid4()), "text": "停止验收", "attachment_ids": []}
        stop_sent = await call("POST", prefix + f"/conversations/{cid}/messages", payload, expected=202)
        run = await call("GET", prefix + f"/runs/{stop_sent['run']['id']}")
        for _ in range(3):
            response = await client.post(
                prefix + f"/runs/{run['id']}/stop",
                json={"expected_version": run["version"]},
                headers={"Idempotency-Key": str(uuid4())},
            )
            if response.status_code == 202:
                break
            assert response.status_code == 409 and response.json()["code"] == "VERSION_CONFLICT"
            run = await call("GET", prefix + f"/runs/{run['id']}")
        assert response.status_code == 202
        for _ in range(40):
            current = await call("GET", prefix + f"/runs/{run['id']}")
            if current["state"] == "STOPPED":
                break
            await asyncio.sleep(0.2)
        assert current["state"] == "STOPPED"
        passed("stop_reaches_terminal_state")
        evidence["conversation_id"] = cid
        evidence["run_id"] = sent["run"]["id"]
        evidence["event_count"] = len(events)
        evidence["result"] = "passed"
    path = ROOT / "deployment/.run/agent-smoke.json"
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Evidence:", path)


if __name__ == "__main__":
    asyncio.run(main())
