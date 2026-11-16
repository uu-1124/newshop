"""Resumable real-model multi-turn preparation; no business confirmation is sent."""

import argparse
import asyncio
import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from expansion_client import MEMBER, api, login
from smoke_retention_protection import business_snapshot

from app.runtime import run

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "deployment/.run/completion-preparation-revisions-live-20261114.json"
sys.path.insert(0, str(ROOT / "deployment"))


def save(state):
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def baseline():
    from backup_data import pg_connection
    from smoke_order_lifecycle import mysql

    result = {"business": business_snapshot()}
    with mysql() as db, db.cursor() as cursor:
        cursor.execute("START TRANSACTION READ ONLY")
        cursor.execute("SELECT * FROM pms_sku_stock WHERE id=240")
        result["second_sku_hash"] = hashlib.sha256(
            json.dumps(cursor.fetchall(), sort_keys=True, default=str).encode()
        ).hexdigest()
        db.rollback()
    with pg_connection(ROOT, True) as conn:
        result["policy_versions"] = {
            row["key"]: row["version"]
            for row in conn.execute(
                "SELECT key,version FROM ag_setting WHERE key IN ('agent-runtime-v1','agent-backup-policy-v1')"
            )
        }
    return result


def cancellation_fingerprints(orders):
    from smoke_order_lifecycle import mysql

    with mysql() as db, db.cursor() as cursor:
        cursor.execute("START TRANSACTION READ ONLY")
        cursor.execute(
            "SELECT * FROM oms_order WHERE id IN (%s,%s) ORDER BY id", tuple(order["id"] for order in orders)
        )
        value = hashlib.sha256(
            json.dumps(cursor.fetchall(), sort_keys=True, default=str).encode()
        ).hexdigest()
        db.rollback()
        return value


async def initialize(client, case, state):
    if case["kind"] == "CANCEL":
        orders = (await api(client, MEMBER + "/orders?status=0&limit=3"))["items"]
        assert orders
        first = orders[0]
        second = orders[1] if len(orders) > 1 else first
        case["orders"] = [{k: row[k] for k in ("id", "order_sn", "pay_amount")} for row in [first, second]]
        case["order_fingerprints"] = cancellation_fingerprints(case["orders"])
        questions = [
            f"读取本人完整订单号{order['order_sn']}，内部编号{order['id']}，核对未付款后准备取消确认卡。只准备，不点击确认，不取消、不付款。"
            for order in [first, second]
        ]
        questions[1] = (
            "改为另一笔选择，旧卡不再使用。"
            if first["id"] != second["id"]
            else "重新核对刚才这笔订单，旧卡不再使用。"
        ) + questions[1]
        case["turns"] = [
            {"question": q, "expected": {"action_type": "CANCEL_ORDER", "order_id": order["id"]}}
            for q, order in zip(questions, [first, second])
        ]
    elif case["kind"] == "RETURN":
        order = await api(client, MEMBER + "/orders/116")
        eligibility = await api(client, MEMBER + "/orders/116/items/156/return-eligibility")
        assert eligibility["eligible"] and eligibility["remaining_quantity"] >= 1
        reasons = eligibility["reason_codes"]
        assert reasons
        selected = [reasons[0], reasons[1] if len(reasons) > 1 else reasons[0]]
        case["turns"] = [
            {
                "question": (
                    ("修改原因和说明，旧卡不要使用。" if index else "")
                    + f"读取本人完整订单号{order['order_sn']}，内部编号116，明细156，核验资格。"
                    + f"只退1件，选择原因编号{reason['code']}，说明填写演示修改{index + 1}，不提供图片。"
                    + "请准备退货申请确认卡，不提交、不审核、不退款。"
                ),
                "expected": {
                    "action_type": "CREATE_RETURN",
                    "order_id": "116",
                    "order_item_id": "156",
                    "quantity": 1,
                    "reason_code": reason["code"],
                    "attachment_ids": [],
                },
            }
            for index, reason in enumerate(selected)
        ]
    else:
        action = "ADD_CART" if case["kind"] == "ADD_CART" else "CREATE_ORDER"
        case["turns"] = []
        for index, (sku, quantity) in enumerate([("239", 1), ("240", 2)]):
            question = (
                "修改刚才的选择，旧卡不要使用。" if index else ""
            ) + f"读取商品45，改用规格{sku}，数量{quantity}。"
            parameters = {
                "action_type": action,
                "items": [{"product_id": "45", "sku_id": sku, "quantity": quantity}],
            }
            if action == "CREATE_ORDER":
                question += (
                    "收货地址使用已有地址20，不使用优惠券，不使用积分。重新读取本次规格和数量的结算选项。"
                )
                parameters.update(source="DIRECT", address_id="20", coupon_id=None, use_points=0)
            question += (
                "请准备"
                + ("加购" if action == "ADD_CART" else "订单")
                + "确认卡，只准备，不点击确认，不执行业务或付款。"
            )
            case["turns"].append({"question": question, "expected": parameters})
    save(state)


async def main(args):
    from backup_data import pg_connection

    state = (
        json.loads(STATE.read_text(encoding="utf-8"))
        if STATE.exists()
        else {
            "created_at": datetime.now(UTC).isoformat(),
            "cases": {},
            "baseline": baseline(),
            "valid_confirmation_requests_sent": 0,
        }
    )
    state.setdefault("valid_confirmation_requests_sent", state.pop("confirmation_requests_sent", 0))
    case = state["cases"].setdefault(args.case, {"kind": args.case, "keys": {}})

    def key(name):
        if name not in case["keys"]:
            case["keys"][name] = str(uuid4())
            save(state)
        return case["keys"][name]

    async with httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=45, trust_env=False) as client:
        await login(client, username="member" if args.case == "CANCEL" else "agent-lifecycle-6c72a0f5")
        if args.mode == "cleanup":
            for name, record in state["cases"].items():
                await login(client, username="member" if name == "CANCEL" else "agent-lifecycle-6c72a0f5")
                cid = record.get("conversation_id")
                if not cid:
                    continue
                response = await client.get(MEMBER + "/conversations/" + cid)
                if response.status_code == 200:
                    snapshot = response.json()["data"]
                    assert not snapshot["active_run"], "Do not delete an active real-model turn"
                    await api(
                        client,
                        MEMBER + "/conversations/" + cid,
                        "DELETE",
                        {"expected_version": snapshot["conversation"]["version"]},
                    )
                assert (await client.get(MEMBER + "/conversations/" + cid)).status_code == 404
                record["deleted"] = True
                if record.get("order_fingerprints"):
                    assert cancellation_fingerprints(record["orders"]) == record["order_fingerprints"]
                save(state)
            assert baseline() == state["baseline"]
            state["cleanup_verified_at"] = datetime.now(UTC).isoformat()
            save(state)
            print("PASS dedicated conversations deleted; original business/stock/policies unchanged")
            return
        if args.mode == "check-old":
            original = case["turns"][0]["action"]
            current = await api(client, MEMBER + "/actions/" + original["id"])
            assert current["state"] in {"STALE", "EXPIRED"}
            state["rejected_confirmation_request_attempts"] = (
                state.get("rejected_confirmation_request_attempts", 0) + 1
            )
            save(state)
            response = await client.post(
                MEMBER + "/actions/" + original["id"] + "/confirm",
                headers={"Idempotency-Key": key("old-rejected-confirm")},
                json={"expected_version": current["version"]},
            )
            assert response.status_code == 409 and response.json()["code"] == "ACTION_STATE_CONFLICT"
            with pg_connection(ROOT, True) as conn:
                count = conn.execute(
                    "SELECT count(*) n FROM ag_action_dispatch d JOIN ag_action a ON a.id=d.action_id WHERE a.conversation_id=%s",
                    (case["conversation_id"],),
                ).fetchone()["n"]
                assert count == 0
            case["old_confirmation_rejected"] = {
                "http": 409,
                "code": response.json()["code"],
                "dispatches": 0,
            }
            assert baseline() == state["baseline"]
            save(state)
            print("PASS old card rejected by authenticated API; no dispatch " + args.case)
            return
        if "turns" not in case:
            await initialize(client, case, state)
        if not case.get("conversation_id"):
            conversation = await api(
                client,
                MEMBER + "/conversations",
                "POST",
                {"title": "[TEST ONLY] 多轮确认卡 " + args.case, "entry_context": {"kind": "GENERAL"}},
                key("conversation"),
                201,
            )
            case["conversation_id"] = conversation["id"]
            save(state)
        cid = case["conversation_id"]
        if args.mode in {"refresh-card", "prepare-api"}:
            turn = case["turns"][-1]
            if args.mode == "prepare-api":
                assert (
                    args.case == "CANCEL" and turn.get("run", {}).get("error_code") == "MODEL_QUOTA_EXCEEDED"
                )
                parameters = turn["expected"]
            else:
                assert turn.get("review") == "PASSED"
                parameters = turn["action"]["parameters"]
            snapshot = await api(client, MEMBER + "/conversations/" + cid)
            assert not snapshot["active_run"]
            prepared = await api(
                client,
                MEMBER + f"/conversations/{cid}/actions",
                "POST",
                {
                    "expected_conversation_version": snapshot["conversation"]["version"],
                    "parameters": parameters,
                },
                key("ui-refresh" if args.mode == "refresh-card" else "ui-api-prepare"),
                201,
            )
            assert prepared["state"] == "PENDING_CONFIRMATION"
            case["ui_action"] = prepared
            case["ui_action_origin"] = args.mode
            save(state)
            print("PASS authenticated UI preparation " + args.case + "; origin=" + args.mode)
            return
        if args.turn == 2 and len(case["turns"]) == 2:
            assert all(t.get("review") == "PASSED" for t in case["turns"])
            if args.case == "ADD_CART":
                case["turns"].append(
                    {
                        "question": "刚才那款手机改回128G，颜色保持鸢尾紫，数量改为1。请重新读取商品规格并准备加购确认卡，不确认、不执行。",
                        "expected": {
                            "action_type": "ADD_CART",
                            "items": [{"product_id": "45", "sku_id": "239", "quantity": 1}],
                        },
                    }
                )
            elif args.case == "DIRECT":
                case["turns"].append(
                    {
                        "question": "旧订单卡先不要使用。不要用100积分，积分先别选，请先问我需要怎样选择积分。现在不要准备确认卡，不下单、不付款。",
                        "expected": None,
                    }
                )
            else:
                raise ValueError("No third turn defined for this case")
            save(state)
        assert args.turn < len(case["turns"])
        assert all(t.get("review") == "PASSED" for t in case["turns"][: args.turn])
        turn = case["turns"][args.turn]
        if turn.get("review") == "PASSED":
            print("PASS already verified " + args.case + " turn " + str(args.turn))
            return
        if not turn.get("run_id"):
            before = await api(client, MEMBER + "/conversations/" + cid)
            assert not before["active_run"]
            prior = [t["action"] for t in case["turns"][: args.turn] if t.get("action")]
            sent = await api(
                client,
                MEMBER + f"/conversations/{cid}/messages",
                "POST",
                {
                    "client_message_id": key("turn-" + str(args.turn)),
                    "text": turn["question"],
                    "attachment_ids": [],
                },
                key("turn-" + str(args.turn)),
                202,
            )
            turn.update(run_id=sent["run"]["id"], started_at=datetime.now(UTC).isoformat(), review="RUNNING")
            save(state)
            for original in prior:
                current = await api(client, MEMBER + "/actions/" + original["id"])
                assert current["state"] in {"STALE", "EXPIRED"}
                assert current["parameters"] == original["parameters"]
            turn["old_cards_revoked_before_new_result"] = True
            save(state)
        started = time.monotonic()
        while time.monotonic() - started < 240:
            current_run = await api(client, MEMBER + "/runs/" + turn["run_id"])
            if current_run["state"] not in {"QUEUED", "RUNNING", "STOP_REQUESTED"}:
                break
            await asyncio.sleep(2)
        else:
            print("WAIT existing run retained; resume this same case/turn")
            raise SystemExit(2)
        snapshot = await api(client, MEMBER + "/conversations/" + cid)
        turn.update(run=current_run, snapshot=snapshot, finished_at=datetime.now(UTC).isoformat())
        with pg_connection(ROOT, True) as conn:
            turn["model_calls"] = [
                dict(row)
                for row in conn.execute(
                    "SELECT id,capability,status FROM ag_model_call WHERE run_id=%s ORDER BY created_at,id",
                    (turn["run_id"],),
                )
            ]
            count = conn.execute(
                "SELECT count(*) n FROM ag_action_dispatch d JOIN ag_action a ON a.id=d.action_id WHERE a.conversation_id=%s",
                (cid,),
            ).fetchone()["n"]
            assert count == 0
        save(state)
        if current_run["state"] != "COMPLETED":
            turn.update(review="FAILED", failure_reason=current_run.get("error_code"))
            save(state)
            raise AssertionError("Real run did not complete; retained for diagnosis")
        cards = snapshot["pending_actions"]
        if turn["expected"] is None:
            assert not cards, "Negative or unresolved latest choices cannot produce a confirmation card"
            turn.update(
                review="PASSED",
                review_method="real model completed clarification with no pending card, prior cards revoked and zero dispatch",
            )
            assert baseline() == state["baseline"]
            save(state)
            print("PASS real-model clarification without a card " + args.case + " turn " + str(args.turn))
            return
        assert len(cards) == 1 and cards[0]["state"] == "PENDING_CONFIRMATION", (
            "Expected current confirmation card missing"
        )
        action = cards[0]
        assert all(action["parameters"].get(k) == v for k, v in turn["expected"].items()), (
            "Prepared parameters do not match latest member choices"
        )
        assert turn["model_calls"]
        turn.update(
            action=action,
            review="PASSED",
            review_method="exact latest parameters, original card revocation, real model ledger; no execution",
        )
        assert baseline() == state["baseline"]
        if case.get("order_fingerprints"):
            assert cancellation_fingerprints(case["orders"]) == case["order_fingerprints"]
        save(state)
        print(
            "PASS real-model "
            + args.case
            + " turn "
            + str(args.turn)
            + "; exact card, old cards revoked, no dispatch"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["run", "refresh-card", "prepare-api", "check-old", "cleanup"])
    parser.add_argument("--case", choices=["ADD_CART", "DIRECT", "RETURN", "CANCEL"], default="ADD_CART")
    parser.add_argument("--turn", type=int, choices=[0, 1, 2], default=0)
    try:
        run(main(parser.parse_args()))
    except Exception as error:
        print("PREPARATION_REVISION_FAILED type=" + type(error).__name__)
        raise SystemExit(1)
