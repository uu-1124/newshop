"""Dedicated demo order: actual broker publish/consume, no payment or model call."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from expansion_client import MEMBER, api, login
from smoke_order_lifecycle import mysql

from app.runtime import run

ROOT = Path(__file__).resolve().parents[2]


async def main():
    output = ROOT / "deployment/.run/completion-business-outbox-smoke-20261113.json"
    state = (
        json.loads(output.read_text(encoding="utf-8"))
        if output.exists()
        else {"started_at": datetime.now(UTC).isoformat(), "checks": []}
    )
    if state.get("result") == "passed":
        print("PASS previously recorded dedicated outbox acceptance; no new business executed")
        return

    def save():
        output.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def passed(check):
        if check not in state["checks"]:
            state["checks"].append(check)
        save()
        print("PASS", check, flush=True)

    with mysql() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT stock,lock_stock FROM pms_sku_stock WHERE id=239")
            before = cur.fetchone()
            if "stock_before" not in state:
                # The previous attempt committed its one-unit reservation before
                # returning HTTP 202. Persist the original lock baseline on resume.
                state["stock_before"] = {
                    "stock": before["stock"],
                    "lock_stock": before["lock_stock"] - 1
                    if state.get("conversation_id")
                    else before["lock_stock"],
                }
                save()
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8201", trust_env=False, timeout=60) as client:
        await login(client, username="agent-lifecycle-6c72a0f5")
        conv = (
            await api(client, MEMBER + "/conversations/" + state["conversation_id"])
            if state.get("conversation_id")
            else await api(
                client,
                MEMBER + "/conversations",
                "POST",
                {"title": "[TEST ONLY] business outbox", "entry_context": {"kind": "GENERAL"}},
                expected=201,
            )
        )
        cid = state.get("conversation_id") or conv["id"]
        state["conversation_id"] = cid
        save()
        params = {
            "action_type": "CREATE_ORDER",
            "source": "DIRECT",
            "items": [{"product_id": "45", "sku_id": "239", "quantity": 1}],
            "address_id": "20",
            "coupon_id": None,
            "use_points": 0,
        }
        cards = [
            b["action"]
            for m in conv.get("recent_messages", [])
            for b in m["blocks"]
            if b["type"] == "action" and b["action"]["action_type"] == "CREATE_ORDER"
        ]
        if len(cards) > 1:
            raise RuntimeError("Dedicated acceptance conversation has ambiguous create actions")
        card = (
            cards[0]
            if cards
            else await api(
                client,
                MEMBER + f"/conversations/{cid}/actions",
                "POST",
                {"expected_conversation_version": conv["version"], "parameters": params},
                expected=201,
            )
        )
        state["create_action_id"] = card["id"]
        state.setdefault("confirmation_key", str(uuid4()))
        save()
        result = await api(client, MEMBER + f"/actions/{card['id']}")
        if result["state"] == "PENDING_CONFIRMATION":
            result = await api(
                client,
                MEMBER + f"/actions/{card['id']}/confirm",
                "POST",
                {"expected_version": card["version"]},
                key=state["confirmation_key"],
                expected=202,
            )
        for _ in range(30):
            if result["state"] in {"SUCCEEDED", "FAILED"}:
                break
            await asyncio.sleep(1)
            result = await api(client, MEMBER + f"/actions/{card['id']}")
        assert result["state"] == "SUCCEEDED"
        oid = result["result"]["business_id"]
        state["order_id"] = oid
        state["order_sn"] = result["result"]["order"]["order_sn"]
        save()
        snap = await api(client, MEMBER + "/conversations/" + cid)
        cancel = await api(
            client,
            MEMBER + f"/conversations/{cid}/actions",
            "POST",
            {
                "expected_conversation_version": snap["conversation"]["version"],
                "parameters": {"action_type": "CANCEL_ORDER", "order_id": oid},
            },
            expected=201,
        )
        assert cancel["quote"]["cancellation"]["order_sn"] == state["order_sn"]
        assert cancel["quote"]["cancellation"]["pay_amount"] == result["result"]["order"]["pay_amount"]
        state["cancel_action_id"] = cancel["id"]
        passed("new_order_and_outbox_committed_together_full_order_number_and_amount_in_cancel_card")
        with mysql() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT state FROM oms_agent_business_outbox WHERE order_id=%s", (oid,))
                assert cur.fetchone()["state"] == "PENDING"
                # Accelerate only this dedicated acceptance event; normal timeout settings stay unchanged.
                cur.execute(
                    "UPDATE oms_agent_business_outbox SET due_at=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND,next_attempt_at=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND WHERE order_id=%s",
                    (oid,),
                )
            conn.commit()
        for _ in range(45):
            with mysql() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT state,attempts,last_error_code FROM oms_agent_business_outbox WHERE order_id=%s",
                        (oid,),
                    )
                    row = cur.fetchone()
            if row["state"] == "CONSUMED":
                break
            await asyncio.sleep(1)
        assert row["state"] == "CONSUMED", row
        state["publication_attempts"] = row["attempts"]
        order = await api(client, MEMBER + f"/orders/{oid}")
        assert order["status"] == 4
        passed("actual_broker_confirmed_publication_and_transactional_consumer_closed_only_test_order")
        with mysql() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT stock,lock_stock FROM pms_sku_stock WHERE id=239")
                assert cur.fetchone() == state["stock_before"]
                cur.execute(
                    "SELECT published_at,consumed_at FROM oms_agent_business_outbox WHERE order_id=%s", (oid,)
                )
                assert cur.fetchone()["consumed_at"]
        passed("stock_and_lock_exactly_restored_after_timeout_cancel")
        snap = await api(client, MEMBER + "/conversations/" + cid)
        await api(
            client,
            MEMBER + "/conversations/" + cid,
            "DELETE",
            {"expected_version": snap["conversation"]["version"]},
            expected=200,
        )
        state["conversation_deleted"] = True
        state["result"] = "passed"
        save()


if __name__ == "__main__":
    run(main())
