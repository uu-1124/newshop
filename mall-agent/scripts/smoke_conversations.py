"""Real gateway conversation acceptance using a dedicated demo buyer; no checkout."""

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pymysql

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "deployment/.run/conversation-management-smoke.json"
API = "/mall-agent/v1/member"


def save(state):
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def main(phase):
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    if phase == "prepare" and not state:
        source = json.loads((ROOT / "deployment/config/portal.yaml").read_text(encoding="utf-8"))["spring"][
            "datasource"
        ]
        with pymysql.connect(
            host="127.0.0.1",
            port=13306,
            user=source["username"],
            password=source["password"],
            database="mall",
            autocommit=True,
        ) as mysql:
            username = "agent-conversation-" + uuid4().hex[:8]
            with mysql.cursor() as query:
                query.execute(
                    "INSERT INTO ums_member(username,password,nickname,status,integration,create_time) SELECT %s,password,'[TEST ONLY] conversation buyer',1,0,NOW() FROM ums_member WHERE username='member' LIMIT 1",
                    (username,),
                )
                assert query.rowcount == 1
                member_id = str(query.lastrowid)
        state = {
            "started_at": datetime.now(UTC).isoformat(),
            "test_buyer": username,
            "member_id": member_id,
            "conversations": [],
            "checks": [],
            "payment_executed": False,
        }
        save(state)
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=40, trust_env=False) as client:
        response = await client.post(
            "/mall-portal/sso/login",
            data={
                "username": state["test_buyer"],
                "password": os.environ.get("MALL_DEMO_MEMBER_PASSWORD", "MallDemo2026!"),
            },
        )
        data = response.json()["data"]
        headers = {"Authorization": data["tokenHead"] + data["token"]}

        async def call(method, path, body=None, *, key=None, expected=200, **kwargs):
            result = await client.request(
                method,
                path,
                json=body,
                headers=headers | ({"Idempotency-Key": str(key or uuid4())} if method != "GET" else {}),
                **kwargs,
            )
            payload = result.json()
            assert result.status_code == expected and payload.get("code") == "OK", (
                path,
                result.status_code,
                payload.get("code"),
            )
            return payload["data"]

        if phase == "prepare":
            if state.get("failed_run_id"):
                print("Existing browser fixture is ready; continue verification using the saved buyer")
                return
            prefix = "[TEST ONLY] 会话扩展 "
            for number in range(len(state["conversations"]), 23):
                conv = await call(
                    "POST",
                    API + "/conversations",
                    {"title": prefix + str(number + 1), "entry_context": {"kind": "GENERAL"}},
                    expected=201,
                )
                state["conversations"].append(conv["id"])
                save(state)
            page = await call("GET", API + "/conversations", params={"limit": 20, "query": prefix})
            assert len(page["items"]) == 20 and page["has_more"] and page["next_cursor"]
            second = await client.get(
                API + "/conversations",
                params={"limit": 20, "query": prefix, "cursor": page["next_cursor"]},
                headers=headers,
            )
            assert second.status_code == 200
            page2 = second.json()["data"]
            assert len(page2["items"]) == 3 and not page2["has_more"]
            assert len({item["id"] for item in page["items"] + page2["items"]}) == 23
            state["checks"].append("real_gateway_scoped_search_and_two_page_conversations")
            target = state["conversations"][-1]
            snapshot = await call("GET", f"{API}/conversations/{target}")
            renamed = await call(
                "PATCH",
                f"{API}/conversations/{target}",
                {
                    "title": "[TEST ONLY] 会话功能验收",
                    "expected_version": snapshot["conversation"]["version"],
                },
            )
            assert renamed["control_epoch"] == snapshot["conversation"]["control_epoch"]
            state["checks"].append("rename_keeps_control_epoch")
            # A labelled terminal failure fixture cannot be claimed by generation.
            # Only the explicit retry button can create a fresh paid generation run.
            config = json.loads((ROOT / "deployment/.run/agent-env.json").read_text(encoding="utf-8"))
            with psycopg.connect(
                config["databaseUrl"].replace("postgresql+psycopg://", "postgresql://")
            ) as pg:
                assert pg.execute(
                    "SELECT id FROM ag_conversation WHERE id=%s AND member_id=%s",
                    (target, state["member_id"]),
                ).fetchone()
                mid, rid, assistant = uuid4(), uuid4(), uuid4()
                pg.execute(
                    "INSERT INTO ag_message(id,conversation_id,seq,sender_kind,sender_id,client_message_id,text_content,status,retention_until) VALUES (%s,%s,1,'MEMBER',%s,%s,'请只回复：会话重试验收通过。','COMPLETED',now()+interval '1 day')",
                    (mid, target, state["member_id"], uuid4()),
                )
                pg.execute(
                    "INSERT INTO ag_run(id,conversation_id,trigger_message_id,state,conversation_version,memory_epoch,control_epoch,checkpoint_thread_id,error_code,finished_at) VALUES (%s,%s,%s,'FAILED',%s,1,%s,%s,'TEST_ONLY_FAILURE',now())",
                    (
                        rid,
                        target,
                        mid,
                        renamed["version"],
                        renamed["control_epoch"],
                        f"member:{state['member_id']}:run:{rid}",
                    ),
                )
                pg.execute(
                    "INSERT INTO ag_message(id,conversation_id,seq,sender_kind,sender_id,text_content,status,run_id,retention_until) VALUES (%s,%s,2,'ASSISTANT',0,'[TEST ONLY] 故障演练保留的部分回复','FAILED',%s,now()+interval '1 day')",
                    (assistant, target, rid),
                )
                pg.execute(
                    "UPDATE ag_conversation SET next_message_seq=3,version=version+1,updated_at=now() WHERE id=%s",
                    (target,),
                )
            state.update(
                target_conversation_id=target, failed_run_id=str(rid), browser_retry_will_call_real_model=True
            )
            snapshot = await call("GET", f"{API}/conversations/{target}")
            assert snapshot["conversation"]["unread_count"] == 1
            state["checks"].append("real_read_projection_counts_unread_assistant_reply")
            save(state)
            print(
                "PASS prepared 23 test conversations and a terminal retry fixture; browser uses dedicated test buyer"
            )
            print("Test buyer:", state["test_buyer"])
            return
        config = json.loads((ROOT / "deployment/.run/agent-env.json").read_text(encoding="utf-8"))
        with psycopg.connect(config["databaseUrl"].replace("postgresql+psycopg://", "postgresql://")) as pg:
            retried = pg.execute(
                "SELECT id,state,attempt,trigger_message_id FROM ag_run WHERE retry_of_run_id=%s",
                (state["failed_run_id"],),
            ).fetchall()
            assert len(retried) == 1 and retried[0][1] == "COMPLETED" and retried[0][2] == 2
            count = pg.execute(
                "SELECT COUNT(*) FROM ag_message WHERE conversation_id=%s AND sender_kind='MEMBER'",
                (state["target_conversation_id"],),
            ).fetchone()[0]
            assert count == 1
        state["checks"].append("browser_retry_completed_new_attempt_without_duplicate_user_message")
        for cid in state["conversations"]:
            result = await client.get(f"{API}/conversations/{cid}", headers=headers)
            if result.status_code == 404:
                continue
            conv = result.json()["data"]["conversation"]
            await call("DELETE", f"{API}/conversations/{cid}", {"expected_version": conv["version"]})
        assert not (await call("GET", API + "/conversations"))["items"]
        state["checks"].append("test_conversations_deleted_and_hidden")
        state["verified_at"] = datetime.now(UTC).isoformat()
        state["paid_model_retry_performed"] = True
        save(state)
        print("PASS runtime retry and all test conversations deleted; cleanup jobs remain durable")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "verify"))
    asyncio.run(main(parser.parse_args().phase))
