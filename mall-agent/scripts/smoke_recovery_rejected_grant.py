"""Bounded live recovery fault on one dedicated ADD_CART action; no model/payment.

Only this script's new labelled PG action is changed for fault injection. Java
business writes use authenticated APIs; MySQL is read-only for fingerprints.
Secrets, grants, cipher envelopes and raw driver errors are never printed.
"""

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
from expansion_client import ADMIN, MEMBER, api, login
from pydantic import SecretStr
from smoke_order_lifecycle import mysql

from app.infrastructure.action_security import ActionSecurity

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "deployment/.run/completion-rejected-grant-live-20261114.json"
sys.path.insert(0, str(ROOT / "deployment"))


def save(state):
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def business_fingerprint(state):
    with mysql() as db, db.cursor() as q:
        q.execute("START TRANSACTION READ ONLY")
        q.execute("SELECT * FROM oms_agent_operation WHERE operation_no=%s", (state["action_id"],))
        operation = q.fetchall()
        assert len(operation) == 1 and operation[0]["state"] == "SUCCEEDED"
        q.execute("SELECT * FROM pms_sku_stock WHERE id=239")
        stock = q.fetchall()
        q.execute(
            "SELECT * FROM oms_cart_item WHERE member_id=22 AND product_sku_id=239 AND delete_status=0 ORDER BY id"
        )
        cart = q.fetchall()
        db.rollback()
    return {
        name: hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()
        for name, rows in (("operation", operation), ("stock", stock), ("cart", cart))
    }


def row_state(state):
    from backup_data import pg_connection

    with pg_connection(ROOT, True) as conn:
        row = conn.execute(
            "SELECT a.state,a.version,d.state dispatch_state,d.last_error_code,d.attempts,d.fencing_token FROM ag_action a JOIN ag_action_dispatch d ON d.action_id=a.id WHERE a.id=%s AND a.member_id=22",
            (state["action_id"],),
        ).fetchone()
        return dict(row)


async def main(mode):
    from backup_data import pg_connection

    state = (
        json.loads(STATE.read_text(encoding="utf-8"))
        if STATE.exists()
        else {"created_at": datetime.now(UTC).isoformat(), "keys": {}}
    )

    def key(name):
        if name not in state["keys"]:
            state["keys"][name] = str(uuid4())
            save(state)
        return state["keys"][name]

    async with (
        httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=60, trust_env=False) as buyer,
        httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=60, trust_env=False) as staff,
    ):
        await login(buyer, username="agent-lifecycle-6c72a0f5")
        await login(staff, True)
        if mode == "prepare":
            if not state.get("conversation_id"):
                cart = await buyer.get("/mall-portal/cart/list")
                assert cart.json().get("code") == 200
                state["cart_before"] = cart.json()["data"]
                assert not any(str(r["productSkuId"]) == "239" for r in state["cart_before"])
                conv = await api(
                    buyer,
                    MEMBER + "/conversations",
                    "POST",
                    {"title": "[TEST ONLY] 核对凭据拒绝恢复", "entry_context": {"kind": "GENERAL"}},
                    key("conversation"),
                    201,
                )
                state["conversation_id"] = conv["id"]
                save(state)
            if not state.get("action_id"):
                snap = await api(buyer, MEMBER + "/conversations/" + state["conversation_id"])
                action = await api(
                    buyer,
                    MEMBER + f"/conversations/{state['conversation_id']}/actions",
                    "POST",
                    {
                        "expected_conversation_version": snap["conversation"]["version"],
                        "parameters": {
                            "action_type": "ADD_CART",
                            "items": [{"product_id": "45", "sku_id": "239", "quantity": 1}],
                        },
                    },
                    key("prepare"),
                    201,
                )
                state["action_id"] = action["id"]
                save(state)
            action = await api(buyer, MEMBER + "/actions/" + state["action_id"])
            if action["state"] == "PENDING_CONFIRMATION":
                action = await api(
                    buyer,
                    MEMBER + f"/actions/{state['action_id']}/confirm",
                    "POST",
                    {"expected_version": action["version"]},
                    key("confirm"),
                    202,
                )
            assert action["state"] == "SUCCEEDED"
            state["business_before"] = business_fingerprint(state)
            save(state)
            print("PASS dedicated ADD_CART committed once")
        elif mode == "inject":
            assert not state.get("injected_at"), "Fault already injected; use verify/resume"
            assert business_fingerprint(state) == state["business_before"]
            config = json.loads((ROOT / "deployment/.run/action-env.json").read_text(encoding="utf-8"))
            security = ActionSecurity(
                SimpleNamespace(
                    confirmation_secret=SecretStr(config["confirmationSecret"]),
                    reconciliation_secret=SecretStr(config["reconciliationSecret"]),
                    action_encryption_key=SecretStr(config["encryptionKey"]),
                )
            )
            with pg_connection(ROOT) as conn:
                ref = conn.execute(
                    "SELECT a.*,c.title,c.member_id conversation_member,d.reconcile_grant_ciphertext FROM ag_action a JOIN ag_conversation c ON c.id=a.conversation_id JOIN ag_action_dispatch d ON d.action_id=a.id WHERE a.id=%s AND a.member_id=22 FOR UPDATE OF a,d",
                    (state["action_id"],),
                ).fetchone()
                assert (
                    str(ref["conversation_id"]) == state["conversation_id"]
                    and ref["title"] == "[TEST ONLY] 核对凭据拒绝恢复"
                    and ref["conversation_member"] == 22
                    and ref["state"] == "SUCCEEDED"
                )
                grant = security.decrypt(ref["reconcile_grant_ciphertext"])
                payload, signature = grant.split(".")
                bad = payload + "." + ("A" if signature[0] != "A" else "B") + signature[1:]
                ciphertext = security.envelope.encrypt(bad.encode())
                conn.execute(
                    "UPDATE ag_action SET state='UNKNOWN',result=NULL,error_code='TEST_RESPONSE_LOSS',version=version+1 WHERE id=%s",
                    (state["action_id"],),
                )
                conn.execute(
                    "UPDATE ag_action_dispatch SET state='RECONCILING',reconcile_grant_ciphertext=%s,lease_owner=NULL,lease_until=NULL,next_attempt_at=now(),version=version+1 WHERE action_id=%s",
                    (ciphertext, state["action_id"]),
                )
            state["injected_at"] = datetime.now(UTC).isoformat()
            save(state)
            for _ in range(45):
                status = row_state(state)
                if status["dispatch_state"] == "MANUAL_REQUIRED":
                    break
                await asyncio.sleep(1)
            assert (
                status["state"] == "UNKNOWN"
                and status["last_error_code"] == "RECONCILE_GRANT_REJECTED"
                and status["dispatch_state"] == "MANUAL_REQUIRED"
            )
            state["rejected_status"] = status
            assert business_fingerprint(state) == state["business_before"]
            save(state)
            print("PASS actual Java invalid receipt retained UNKNOWN and stopped automatic retries")
        elif mode == "resume":
            assert state.get("rejected_status")
            items = (await api(staff, ADMIN + "/actions/unresolved"))["items"]
            item = next(r for r in items if r["action_id"] == state["action_id"])
            assert item["dispatch_state"] == "MANUAL_REQUIRED"
            body = {
                "expected_version": item["action_version"],
                "expected_dispatch_version": item["dispatch_version"],
                "reason": "[TEST ONLY] 恢复原编号核对，不重复加购",
            }
            await api(
                staff, ADMIN + f"/actions/{state['action_id']}/reconcile", "POST", body, key("resume"), 202
            )
            for _ in range(45):
                action = await api(buyer, MEMBER + "/actions/" + state["action_id"])
                if action["state"] == "SUCCEEDED":
                    break
                await asyncio.sleep(1)
            assert action["state"] == "SUCCEEDED"
            assert business_fingerprint(state) == state["business_before"]
            state["resumed_status"] = row_state(state)
            state["business_after"] = business_fingerprint(state)
            state["recovery_verified"] = True
            save(state)
            print(
                "PASS authenticated staff renewal recovered same Java result without changing business/cart/stock"
            )
        elif mode == "cleanup":
            assert state.get("recovery_verified"), "Resolve the original action before cleanup"
            cart = (await buyer.get("/mall-portal/cart/list")).json()["data"]
            added = [r for r in cart if str(r["productSkuId"]) == "239"]
            if added:
                assert len(added) == 1 and added[0]["quantity"] == 1
                response = await buyer.post("/mall-portal/cart/delete", params={"ids": added[0]["id"]})
                assert response.json()["code"] == 200
            assert (await buyer.get("/mall-portal/cart/list")).json()["data"] == state["cart_before"]
            response = await buyer.get(MEMBER + "/conversations/" + state["conversation_id"])
            if response.status_code == 200:
                await api(
                    buyer,
                    MEMBER + "/conversations/" + state["conversation_id"],
                    "DELETE",
                    {"expected_version": response.json()["data"]["conversation"]["version"]},
                    key("delete"),
                )
            assert (await buyer.get(MEMBER + "/conversations/" + state["conversation_id"])).status_code == 404
            state["cleanup"] = {
                "cart_restored": True,
                "conversation_deleted": True,
                "business_ledger_retained": True,
                "paid_model_calls": 0,
                "real_payment_or_refund": False,
            }
            save(state)
            print("PASS scoped cart and conversation cleanup")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["prepare", "inject", "resume", "cleanup"])
    try:
        asyncio.run(main(parser.parse_args().mode))
    except Exception as error:
        print("RECOVERY_ACCEPTANCE_FAILED type=" + type(error).__name__)
        raise SystemExit(1)
