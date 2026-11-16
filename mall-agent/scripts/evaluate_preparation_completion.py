"""Bounded live preparation acceptance; never confirms cards or changes cart/orders."""

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from expansion_client import MEMBER, api, login

from app.application.cart import CartRead, validated_cart_page
from app.infrastructure.java_bridge import JavaBridge
from app.infrastructure.postgres.database import Database, rows
from app.runtime import run
from app.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "deployment/.run/completion-preparation-eval-20261113.json"


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--case", choices=["DIRECT", "ADD_CART", "RETURN", "CART_READ", "CART_ORDER", "CANCEL"]
    )
    parser.add_argument("--retry-failed", action="store_true")
    arguments = parser.parse_args()
    state = (
        json.loads(OUTPUT.read_text(encoding="utf-8"))
        if OUTPUT.exists()
        else {
            "started_at": datetime.now(UTC).isoformat(),
            "cases": [],
            "result": "IN_PROGRESS",
            "confirms_sent": 0,
            "real_payment_or_refund_calls": False,
        }
    )
    config = json.loads((ROOT / "deployment/.run/agent-env.json").read_text(encoding="utf-8"))
    db = Database(config["databaseUrl"])

    def save():
        OUTPUT.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    cases = [
        (
            "DIRECT",
            "请直接购买商品45的规格239，数量1。收货地址选地址20，不使用优惠券，不使用积分。请读取该商品和结算选项后准备订单确认卡，不要确认或执行下单。",
            "CREATE_ORDER",
        ),
        ("ADD_CART", "请读取商品45的规格239，数量1，为我准备加购确认卡。不要确认加购。", "ADD_CART"),
        (
            "RETURN",
            "请读取我的订单116详情，并核验其中可退明细的退货资格。我只退剩余1件；使用该明细资格返回的第一个有效退货原因，说明是演示验收，没有图片凭证。请准备退货申请确认卡，不确认申请，不审核或退款。",
            "CREATE_RETURN",
        ),
        ("CART_READ", "请读取我当前购物车里已有的商品，告诉我数量和可用性。不要加购或下单。", None),
        ("CART_ORDER", "", "CREATE_ORDER"),
        ("CANCEL", "", "CANCEL_ORDER"),
    ]
    try:
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8201", trust_env=False, timeout=30) as client:
            await login(client, username="agent-lifecycle-6c72a0f5")
            for case_id, question, action_type in cases:
                if arguments.case and case_id != arguments.case:
                    continue
                previous = next((c for c in reversed(state["cases"]) if c["case_id"] == case_id), None)
                if previous:
                    if previous.get("review") == "PASSED" and previous.get("conversation_deleted"):
                        continue
                    if not arguments.retry_failed or previous.get("review") != "FAILED":
                        raise RuntimeError(
                            "A recorded preparation case requires review/resume; do not issue a duplicate run"
                        )
                    original_run = await api(client, MEMBER + "/runs/" + previous["run_id"])
                    if original_run["state"] not in {"FAILED", "INTERRUPTED", "CANCELLED", "COMPLETED"}:
                        raise RuntimeError("Prior run still active; refuse duplicate retry")
                if case_id == "RETURN":
                    order = await api(client, MEMBER + "/orders/116")
                    remaining = next(line for line in order["lines"] if line["returnable_quantity"] > 0)
                    question = (
                        f"请读取我的完整订单号{order['order_sn']}，订单内部编号116，目标明细内部编号{remaining['order_item_id']}。"
                        "核验该明细退货资格。我只退剩余1件，选择资格返回的第一个有效退货原因，说明为演示验收，没有图片凭证。"
                        "请准备退货申请确认卡，不确认申请，不审核或退款。"
                    )
                elif case_id == "CART_ORDER":
                    await login(client, username="member")
                    bridge = JavaBridge(
                        Settings(
                            _env_file=None,
                            database_url=config["databaseUrl"],
                            bridge_secret=config["bridgeSecret"],
                            broker_secret=config["brokerSecret"],
                            cursor_secret=config["cursorSecret"],
                        )
                    )
                    try:
                        page = validated_cart_page(
                            await bridge.cart(client.headers["Authorization"], CartRead()), CartRead()
                        )
                    finally:
                        await bridge.client.aclose()
                    item = next(item for item in page["items"] if item["available"])
                    selected = {
                        "action_type": "CREATE_ORDER",
                        "source": "CART",
                        "cart_item_ids": [item["id"]],
                    }
                    options = await api(client, MEMBER + "/checkout-options", "POST", selected)
                    address = options["addresses"][0]["id"]
                    question = (
                        f"请读取我的购物车，选择条目编号{item['id']}，保留该条目的现有数量{item['quantity']}。"
                        f"用已有地址{address}，不使用优惠券，不使用积分。请读取结算选项后准备购物车订单确认卡，"
                        "只准备，不确认下单，不加购，不支付。"
                    )
                elif case_id == "CANCEL":
                    await login(client, username="member")
                    unpaid = await api(client, MEMBER + "/orders?status=0&limit=3")
                    cancellation_target = unpaid["items"][0]
                    question = (
                        f"请读取我的完整订单号{cancellation_target['order_sn']}，内部编号{cancellation_target['id']}。"
                        "核对它仍为未付款后，准备该订单的取消确认卡，必须显示完整订单号和金额；只准备，不确认取消，不支付。"
                    )
                conv = await api(
                    client,
                    MEMBER + "/conversations",
                    "POST",
                    {
                        "title": "[TEST ONLY] preparation acceptance " + case_id,
                        "entry_context": {"kind": "GENERAL"},
                    },
                    expected=201,
                )
                record = {
                    "case_id": case_id,
                    "question": question,
                    "conversation_id": conv["id"],
                    "review": "PENDING",
                }
                state["cases"].append(record)
                save()
                sent = await api(
                    client,
                    MEMBER + "/conversations/" + conv["id"] + "/messages",
                    "POST",
                    {"client_message_id": str(uuid4()), "text": question, "attachment_ids": []},
                    expected=202,
                )
                record["run_id"] = sent["run"]["id"]
                save()
                started = time.monotonic()
                while time.monotonic() - started < 240:
                    snapshot = await api(client, MEMBER + "/conversations/" + conv["id"])
                    if not snapshot["active_run"]:
                        break
                    await asyncio.sleep(2)
                else:
                    raise RuntimeError("Preparation run remains active; inspect this run before proceeding")
                messages = [m for m in snapshot["recent_messages"] if m["sender_kind"] == "ASSISTANT"]
                message = messages[-1]
                record.update(
                    answer=message["text"],
                    status=message["status"],
                    elapsed_seconds=round(time.monotonic() - started, 2),
                    actions=snapshot["pending_actions"],
                    review="PENDING",
                )
                async with db.transaction(snapshot=True) as conn:
                    calls = await rows(
                        conn,
                        "SELECT id,status,error_code FROM ag_model_call WHERE run_id=CAST(:id AS uuid) ORDER BY created_at",
                        id=record["run_id"],
                    )
                    record["model_calls"] = [
                        {"id": str(c["id"]), "status": c["status"], "error_code": c["error_code"]}
                        for c in calls
                    ]
                save()
                assert message["status"] == "COMPLETED", (
                    "Model preparation did not complete; retained for inspection"
                )
                actions = snapshot["pending_actions"]
                if action_type:
                    assert len(actions) == 1 and actions[0]["action_type"] == action_type, (
                        "Expected preparation card missing; retained for inspection"
                    )
                    parameters = actions[0]["parameters"]
                    if action_type in {"ADD_CART", "CREATE_ORDER"} and case_id != "CART_ORDER":
                        assert parameters["items"] == [{"product_id": "45", "sku_id": "239", "quantity": 1}]
                    if action_type == "CREATE_ORDER" and case_id != "CART_ORDER":
                        assert (
                            parameters["source"] == "DIRECT"
                            and parameters["address_id"] == "20"
                            and parameters["coupon_id"] is None
                            and parameters["use_points"] == 0
                        )
                    if case_id == "CART_ORDER":
                        assert parameters["source"] == "CART" and parameters["cart_item_ids"] == [item["id"]]
                        assert (
                            parameters["address_id"] == address
                            and parameters["coupon_id"] is None
                            and parameters["use_points"] == 0
                        )
                    if action_type == "CREATE_RETURN":
                        assert (
                            parameters["order_id"] == "116"
                            and parameters["quantity"] == 1
                            and not parameters["attachment_ids"]
                        )
                    if case_id == "CANCEL":
                        assert parameters == {
                            "action_type": "CANCEL_ORDER",
                            "order_id": cancellation_target["id"],
                        }
                        assert (
                            actions[0]["quote"]["cancellation"]["order_sn"] == cancellation_target["order_sn"]
                        )
                else:
                    assert not actions
                record["review"] = "PASSED"
                record["review_method"] = (
                    "Exact parameters/state assertions; human wording and checkpoint tool path review still required"
                )
                save()
                await api(
                    client,
                    MEMBER + "/conversations/" + conv["id"],
                    "DELETE",
                    {"expected_version": snapshot["conversation"]["version"]},
                )
                record["conversation_deleted"] = True
                save()
                print("PASS", case_id, record["elapsed_seconds"], flush=True)
            latest = {case["case_id"]: case for case in state["cases"]}
            state["result"] = (
                "PARAMETERS_PASSED_PENDING_HUMAN_REVIEW"
                if all(latest.get(case_id, {}).get("review") == "PASSED" for case_id, _, _ in cases)
                else "IN_PROGRESS_RETAINED_ATTEMPTS"
            )
            save()
    finally:
        await db.engine.dispose()


if __name__ == "__main__":
    run(main())
