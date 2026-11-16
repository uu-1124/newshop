"""Dedicated 600-message live memory fixture; no cart, order or payment writes."""

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from expansion_client import MEMBER, api, login

from app.infrastructure.postgres.database import Database, execute, one
from app.runtime import run

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "deployment/.run/completion-cumulative-memory-live-20261113.json"


async def main():
    if OUTPUT.exists():
        raise RuntimeError("Recorded memory fixture exists; inspect its run and resume explicitly")
    cfg = json.loads((ROOT / "deployment/.run/agent-env.json").read_text(encoding="utf-8"))
    db = Database(cfg["databaseUrl"])
    state = {
        "created_at": datetime.now(UTC).isoformat(),
        "fixture": "DEDICATED_TEST_ONLY",
        "business_execute": False,
        "result": "IN_PROGRESS",
        "cases": [],
    }

    def save():
        OUTPUT.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    try:
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8201", trust_env=False, timeout=30) as client:
            await login(client, username="agent-lifecycle-6c72a0f5")
            conv = await api(
                client,
                MEMBER + "/conversations",
                "POST",
                {"title": "[TEST ONLY] cumulative memory 600", "entry_context": {"kind": "GENERAL"}},
                expected=201,
            )
            cid = conv["id"]
            state["conversation_id"] = cid
            save()
            async with db.transaction() as conn:
                owned = await one(
                    conn,
                    "SELECT member_id FROM ag_conversation WHERE id=CAST(:id AS uuid) AND title=:title FOR UPDATE",
                    id=cid,
                    title="[TEST ONLY] cumulative memory 600",
                )
                assert owned["member_id"] == 22
                await execute(
                    conn,
                    """INSERT INTO ag_message(id,conversation_id,seq,sender_kind,sender_id,status,text_content,retention_until)
                    SELECT gen_random_uuid(),CAST(:cid AS uuid),seq,'MEMBER',:member,'COMPLETED',
                    CASE seq WHEN 1 THEN '请记住，我偏好黑色。预算不超过1800元。' ELSE '这是一条专用演示历史闲聊记录'||seq::text END,
                    now()+interval '1 day' FROM generate_series(1,600) seq""",
                    cid=cid,
                    member=owned["member_id"],
                )
                await execute(
                    conn,
                    "UPDATE ag_conversation SET next_message_seq=601 WHERE id=CAST(:cid AS uuid)",
                    cid=cid,
                )
            for name, question in [
                (
                    "EARLY",
                    "请仅复述我最早告诉你的颜色偏好和预算，不推荐或查询商品，不引用店铺政策。记不清的部分请说不知道。",
                ),
                ("UPDATE", "我的预算现在改为1500元，颜色偏好继续保留。请复述更新后的两项要求，不推荐商品。"),
            ]:
                record = {"case": name, "question": question}
                state["cases"].append(record)
                sent = await api(
                    client,
                    MEMBER + "/conversations/" + cid + "/messages",
                    "POST",
                    {"client_message_id": str(uuid4()), "text": question, "attachment_ids": []},
                    expected=202,
                )
                record["run_id"] = sent["run"]["id"]
                save()
                started = time.monotonic()
                while time.monotonic() - started < 220:
                    snap = await api(client, MEMBER + "/conversations/" + cid)
                    if not snap["active_run"]:
                        break
                    await asyncio.sleep(2)
                else:
                    raise RuntimeError("Memory run remains live; do not restart")
                message = next(
                    m for m in reversed(snap["recent_messages"]) if m["sender_kind"] == "ASSISTANT"
                )
                record.update(
                    answer=message["text"],
                    status=message["status"],
                    elapsed_seconds=round(time.monotonic() - started, 2),
                )
                async with db.transaction(snapshot=True) as conn:
                    summary = await one(
                        conn,
                        "SELECT summary,through_seq,memory_epoch,retention_until FROM ag_context_summary WHERE conversation_id=CAST(:cid AS uuid)",
                        cid=cid,
                    )
                    record["summary"] = dict(summary) if summary else None
                save()
                assert record["status"] == "COMPLETED" and "黑色" in record["answer"]
                expected = "1800" if name == "EARLY" else "1500"
                assert expected in record["answer"], (
                    "Expected member requirement missing; retain failed answer for inspection"
                )
                if name == "UPDATE":
                    assert "1800" not in record["answer"]
                record["parameter_check"] = "PASSED_PENDING_HUMAN_REVIEW"
                save()
                print("RESULT", name, record["elapsed_seconds"], record["answer"], flush=True)
            state["result"] = "LATEST_REQUIREMENTS_PASSED_PENDING_REVIEW"
            snap = await api(client, MEMBER + "/conversations/" + cid)
            await api(
                client,
                MEMBER + "/conversations/" + cid,
                "DELETE",
                {"expected_version": snap["conversation"]["version"]},
            )
            state["conversation_deleted"] = True
            save()
    finally:
        await db.engine.dispose()


if __name__ == "__main__":
    run(main())
