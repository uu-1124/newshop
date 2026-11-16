"""Controlled UI/SSE fixture: real persistence and APIs, synthetic stream writer.

No supplier model is invoked. New labelled conversations receive a normal buyer
message while waiting for staff, then an explicitly synthetic RUNNING record is
installed after withdrawing the queue. The regular generation worker cannot claim
it; chunks use Runs.append and real outbox/SSE. Existing business rows are untouched.
"""

import argparse
import asyncio
import hashlib
import json
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
from expansion_client import ADMIN, MEMBER, api, login

from app.application.runs import Runs
from app.infrastructure.postgres.database import Database, append_events, encoded, execute, one
from app.runtime import run

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "deployment/.run/completion-stream-ui-fixtures-20261114.json"
sys.path.insert(0, str(ROOT / "deployment"))


def save(state):
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def baseline():
    from backup_data import pg_connection
    from smoke_retention_protection import business_snapshot

    result = {"business": business_snapshot()}
    with pg_connection(ROOT, True) as conn:
        result["model_calls"] = conn.execute("SELECT count(*) n FROM ag_model_call").fetchone()["n"]
        result["policies"] = {
            row["key"]: {
                "version": row["version"],
                "hash": hashlib.sha256(encoded(row["value"]).encode()).hexdigest(),
            }
            for row in conn.execute(
                "SELECT key,value,version FROM ag_setting WHERE key IN ('agent-runtime-v1','agent-backup-policy-v1') ORDER BY key"
            )
        }
    return result


async def events(response):
    async for line in response.aiter_lines():
        if line.startswith("data: "):
            yield json.loads(line[6:])


async def main(args):
    from backup_data import load_json

    config = load_json(ROOT / "deployment/.run/agent-env.json")
    db = Database(config["databaseUrl"])
    runs = Runs(db, SimpleNamespace(run_lease_seconds=900, provider="fixture", model_timeout_seconds=30))
    state = (
        json.loads(STATE.read_text(encoding="utf-8"))
        if STATE.exists()
        else {"created_at": datetime.now(UTC).isoformat(), "cases": {}}
    )
    if "baseline" not in state:
        state["baseline"] = baseline()
        save(state)
    case = state["cases"].setdefault(args.case, {"keys": {}})

    def key(name):
        if name not in case["keys"]:
            case["keys"][name] = str(uuid4())
            save(state)
        return case["keys"][name]

    async with httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=60, trust_env=False) as buyer:
        await login(buyer, username="agent-lifecycle-6c72a0f5")
        if args.mode == "prepare":
            if not case.get("conversation_id"):
                conv = await api(
                    buyer,
                    MEMBER + "/conversations",
                    "POST",
                    {"title": "[TEST ONLY] 流式界面 " + args.case, "entry_context": {"kind": "GENERAL"}},
                    key("conversation"),
                    201,
                )
                case["conversation_id"] = conv["id"]
                save(state)
            cid = case["conversation_id"]
            snap = await api(buyer, MEMBER + "/conversations/" + cid)
            if not case.get("message_id"):
                if snap["conversation"]["state"] == "AI_ACTIVE":
                    await api(
                        buyer,
                        MEMBER + f"/conversations/{cid}/handoff",
                        "POST",
                        {"expected_conversation_version": snap["conversation"]["version"]},
                        key("handoff"),
                        201,
                    )
                    snap = await api(buyer, MEMBER + "/conversations/" + cid)
                assert snap["conversation"]["state"] == "WAITING_HUMAN"
                assert not snap["conversation"]["waiting_ai_enabled"] and not snap["active_run"]
                sent = await api(
                    buyer,
                    MEMBER + f"/conversations/{cid}/messages",
                    "POST",
                    {
                        "client_message_id": key("message"),
                        "text": "[TEST ONLY] 页面流式同步和停止验证，不调用模型",
                        "attachment_ids": [],
                    },
                    key("message"),
                    202,
                )
                assert sent["run"] is None
                case["message_id"] = sent["message"]["id"]
                save(state)
            snap = await api(buyer, MEMBER + "/conversations/" + cid)
            if snap["conversation"]["state"] == "WAITING_HUMAN":
                await api(
                    buyer,
                    MEMBER + f"/conversations/{cid}/handoff",
                    "DELETE",
                    {"expected_version": snap["handoff"]["version"]},
                    key("withdraw"),
                )
                save(state)
            if not case.get("run_id"):
                rid, mid = uuid4(), uuid4()
                async with db.transaction() as conn:
                    c = await one(
                        conn,
                        "SELECT * FROM ag_conversation WHERE id=CAST(:id AS uuid) AND member_id=22 AND title=:title AND state='AI_ACTIVE' FOR UPDATE",
                        id=cid,
                        title="[TEST ONLY] 流式界面 " + args.case,
                    )
                    assert c
                    assert not await one(
                        conn, "SELECT id FROM ag_run WHERE conversation_id=:cid", cid=c["id"]
                    )
                    r = await one(
                        conn,
                        "INSERT INTO ag_run(id,conversation_id,trigger_message_id,state,conversation_version,memory_epoch,checkpoint_thread_id,lease_owner,lease_until,started_at,control_epoch,config_snapshot) VALUES(:id,:cid,CAST(:trigger AS uuid),'RUNNING',:version,:memory,:thread,'synthetic-ui-stream',now()+interval '15 minutes',now(),:epoch,CAST(:config AS jsonb)) RETURNING *",
                        id=rid,
                        cid=c["id"],
                        trigger=case["message_id"],
                        version=c["version"],
                        memory=c["memory_epoch"],
                        thread="synthetic-ui-stream:" + str(rid),
                        epoch=c["control_epoch"],
                        config=encoded({"synthetic_ui_only": True}),
                    )
                    await execute(
                        conn,
                        "INSERT INTO ag_message(id,conversation_id,seq,sender_kind,sender_id,sender_display_name,run_id,status,text_content,blocks,retention_until) VALUES(:id,:cid,:seq,'ASSISTANT',0,'AI 助手',:run,'GENERATING','','[]'::jsonb,now()+interval '90 days')",
                        id=mid,
                        cid=c["id"],
                        seq=c["next_message_seq"],
                        run=rid,
                    )
                    c = await one(
                        conn,
                        "UPDATE ag_conversation SET next_message_seq=next_message_seq+1,version=version+1,updated_at=now() WHERE id=:id RETURNING *",
                        id=c["id"],
                    )
                    await append_events(
                        conn,
                        c,
                        [
                            ("run.started", {"run_id": str(rid)}),
                            (
                                "message.accepted",
                                {
                                    "message_id": str(mid),
                                    "seq": str(c["next_message_seq"] - 1),
                                    "client_message_id": None,
                                },
                            ),
                        ],
                    )
                case.update(
                    run_id=str(rid),
                    assistant_id=str(mid),
                    claimed={
                        "id": str(rid),
                        "conversation_id": cid,
                        "assistant_id": str(mid),
                        "member_id": 22,
                        "fencing_token": r["fencing_token"],
                        "lease_owner": r["lease_owner"],
                    },
                )
                save(state)
            if not case.get("initial_appended"):
                assert await runs.append(case["claimed"], "第一段：已收到咨询😀。")
                case["initial_appended"] = True
                save(state)
            print("PASS prepared synthetic live stream " + args.case)
        elif args.mode == "replay":
            cid, claimed = case["conversation_id"], case["claimed"]
            snap = await api(buyer, MEMBER + "/conversations/" + cid)
            assert snap["conversation"]["title"] == "[TEST ONLY] 流式界面 " + args.case
            assert snap["active_run"] and snap["active_run"]["id"] == case["run_id"]
            online, missed = "第二段：在线收到😀。", "第三段：断开期间保存，重连补齐。"
            async with asyncio.timeout(30):
                async with buyer.stream(
                    "GET", MEMBER + f"/conversations/{cid}/events", params={"after": snap["event_cursor"]}
                ) as response:
                    assert response.status_code == 200
                    assert await runs.append(claimed, online)
                    async for event in events(response):
                        assert event["conversation_id"] == cid
                        if event["type"] == "message.delta" and event["payload"]["run_id"] == case["run_id"]:
                            assert event["payload"]["text"] == online
                            cursor = event["event_id"]
                            case["online_event"] = event
                            break
                    else:
                        raise AssertionError("No live SSE fragment")
            assert await runs.append(claimed, missed)
            await runs.finish(claimed)
            replayed = []
            async with asyncio.timeout(30):
                async with buyer.stream(
                    "GET", MEMBER + f"/conversations/{cid}/events", headers={"Last-Event-ID": cursor}
                ) as response:
                    assert response.status_code == 200
                    async for event in events(response):
                        assert event["conversation_id"] == cid and int(event["event_id"]) > int(cursor)
                        replayed.append(event)
                        if event["type"] == "message.completed":
                            assert event["payload"]["message"]["run_id"] == case["run_id"]
                            assert event["payload"]["message"]["status"] == "COMPLETED"
                            break
                    else:
                        raise AssertionError("No replayed terminal event")
            chunks = [event["payload"]["text"] for event in replayed if event["type"] == "message.delta"]
            assert chunks == [missed]
            assert [int(event["event_id"]) for event in replayed] == sorted(
                {int(event["event_id"]) for event in replayed}
            )
            final = await api(buyer, MEMBER + "/conversations/" + cid)
            assistant = next(m for m in final["recent_messages"] if m["id"] == case["assistant_id"])
            assert assistant["text"] == "第一段：已收到咨询😀。" + online + missed
            assert not final["active_run"]
            async with db.transaction() as conn:
                count = await one(
                    conn, "SELECT count(*) n FROM ag_run WHERE conversation_id=CAST(:cid AS uuid)", cid=cid
                )
                assert count["n"] == 1
            case.update(replayed_events=replayed, replay_snapshot=final, run_count=1)
            save(state)
            print("PASS real gateway SSE reconnect replay, Unicode and same single run")
        elif args.mode == "claim":
            cid = case["conversation_id"]
            snap = await api(buyer, MEMBER + "/conversations/" + cid)
            assert snap["conversation"]["title"] == "[TEST ONLY] 流式界面 " + args.case
            assert snap["conversation"]["state"] == "WAITING_HUMAN"
            assert not snap["active_run"]
            async with httpx.AsyncClient(
                base_url="http://127.0.0.1:8201", timeout=60, trust_env=False
            ) as staff:
                await login(staff, True)
                me = await api(staff, ADMIN + "/me")

                async def profile():
                    people = (await api(staff, ADMIN + "/staff?limit=100"))["items"]
                    return next(person for person in people if person["id"] == me["id"])

                original = await profile()
                case["staff_presence_before"] = original["presence"]
                save(state)
                await api(
                    staff,
                    ADMIN + "/presence",
                    "PUT",
                    {"expected_version": original["version"], "presence": "ONLINE"},
                )
                try:
                    await api(
                        staff,
                        ADMIN + f"/handoffs/{snap['handoff']['id']}/claim",
                        "POST",
                        {"expected_version": snap["handoff"]["version"]},
                        key("claim"),
                    )
                    await api(
                        staff,
                        ADMIN + f"/conversations/{cid}/messages",
                        "POST",
                        {
                            "client_message_id": key("staff-message"),
                            "text": "[TEST ONLY] 人工已接待，旧 AI 输出已停止。",
                            "attachment_ids": [],
                            "cards": [],
                        },
                        key("staff-message"),
                        201,
                    )
                finally:
                    current = await profile()
                    restored = await api(
                        staff,
                        ADMIN + "/presence",
                        "PUT",
                        {"expected_version": current["version"], "presence": original["presence"]},
                    )
                    assert restored["presence"] == original["presence"]
                    case["staff_presence_restored"] = True
                    save(state)
            final = await api(buyer, MEMBER + "/conversations/" + cid)
            assert final["conversation"]["state"] == "HUMAN_ACTIVE" and not final["active_run"]
            case["human_snapshot"] = final
            save(state)
            print("PASS authenticated staff claim/reply and presence restored")
        elif args.mode in {"append", "finish", "assert-fenced"}:
            claimed = case["claimed"]
            async with db.transaction() as conn:
                c = await one(
                    conn,
                    "SELECT member_id,title FROM ag_conversation WHERE id=CAST(:id AS uuid)",
                    id=case["conversation_id"],
                )
                assert c and c["member_id"] == 22 and c["title"] == "[TEST ONLY] 流式界面 " + args.case
            if args.mode == "append":
                assert await runs.append(claimed, args.text)
                case.setdefault("appended", []).append(args.text)
            elif args.mode == "finish":
                await runs.finish(claimed)
            else:
                assert not await runs.append(claimed, "FENCED_TEXT_MUST_NOT_APPEAR")
                await runs.finish(claimed)
                case["late_append_rejected"] = True
            snap = await api(buyer, MEMBER + "/conversations/" + case["conversation_id"])
            case["snapshot"] = {
                "conversation": snap["conversation"],
                "active_run": snap["active_run"],
                "messages": [
                    {"id": m["id"], "run_id": m["run_id"], "status": m["status"], "text": m["text"]}
                    for m in snap["recent_messages"]
                ],
            }
            assert "FENCED_TEXT_MUST_NOT_APPEAR" not in encoded(case["snapshot"])
            save(state)
            print("PASS " + args.mode + " " + args.case)
        elif args.mode == "cleanup":
            for name, record in state["cases"].items():
                if not record.get("conversation_id"):
                    continue
                response = await buyer.get(MEMBER + "/conversations/" + record["conversation_id"])
                if response.status_code == 200:
                    c = response.json()["data"]["conversation"]
                    await api(
                        buyer,
                        MEMBER + "/conversations/" + record["conversation_id"],
                        "DELETE",
                        {"expected_version": c["version"]},
                    )
                assert (
                    await buyer.get(MEMBER + "/conversations/" + record["conversation_id"])
                ).status_code == 404
                record["deleted"] = True
            save(state)
            print("PASS synthetic stream conversations deleted")
            state["final"] = baseline()
            assert state["final"] == state["baseline"]
            state["cleanup_verified_at"] = datetime.now(UTC).isoformat()
            save(state)
            print("PASS original business, policies and model call total unchanged")
    await db.engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode", choices=["prepare", "append", "finish", "assert-fenced", "claim", "replay", "cleanup"]
    )
    parser.add_argument("--case", default="stop")
    parser.add_argument("--text", default="第二段：继续核对当前信息。")
    try:
        run(main(parser.parse_args()))
    except Exception as error:
        print("STREAM_UI_FAILED type=" + type(error).__name__)
        print("origin_type=" + type(getattr(error, "orig", None)).__name__)
        for frame in traceback.extract_tb(error.__traceback__):
            if frame.filename.replace("\\", "/").startswith(str(ROOT).replace("\\", "/")):
                print(Path(frame.filename).name + ":" + str(frame.lineno) + " " + frame.name)
        raise SystemExit(1)
