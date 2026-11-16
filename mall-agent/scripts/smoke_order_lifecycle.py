"""Explicit local demo lifecycle acceptance; never invokes a real payment/refund provider.

Uses a dedicated labelled member, existing real SKU and gateway. All mutation keys and
business identifiers are saved before a replay. Model calls happen only with --models.
"""

import argparse
import asyncio
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pymysql
from expansion_client import ADMIN, MEMBER, api, login, wait_run
from PIL import Image

from app.runtime import run

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "deployment/.run/order-lifecycle-smoke.json"


def mysql():
    source = json.loads((ROOT / "deployment/config/portal.yaml").read_text(encoding="utf-8"))["spring"][
        "datasource"
    ]
    return pymysql.connect(
        host="127.0.0.1",
        port=13306,
        user=source["username"],
        password=source["password"],
        database="mall",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


async def main(models=False, cleanup=False):
    state = (
        json.loads(STATE.read_text(encoding="utf-8"))
        if STATE.exists()
        else {"created_at": datetime.now(UTC).isoformat(), "checks": [], "keys": {}, "actions": {}}
    )

    def save():
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def key(name):
        if name not in state["keys"]:
            state["keys"][name] = str(uuid4())
            save()
        return state["keys"][name]

    def passed(name):
        if name not in state["checks"]:
            state["checks"].append(name)
        save()
        print("PASS", name, flush=True)

    if "buyer" not in state:
        with mysql() as db, db.cursor() as q:
            buyer = "agent-lifecycle-" + uuid4().hex[:8]
            q.execute(
                "INSERT INTO ums_member(username,password,nickname,status,integration,create_time) SELECT %s,password,'[TEST ONLY] lifecycle buyer',1,0,NOW() FROM ums_member WHERE username='member' LIMIT 1",
                (buyer,),
            )
            assert q.rowcount == 1
            state.update(buyer=buyer, member_id=str(q.lastrowid))
        save()
    async with (
        httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=60, trust_env=False) as buyer,
        httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=60, trust_env=False) as staff,
        httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=40, trust_env=False) as stranger,
    ):
        await login(buyer, username=state["buyer"])
        await login(staff, True)
        await login(stranger, username="agent-conversation-bd63b203")

        async def legacy(client, path, body=None, params=None, method="POST"):
            response = await client.request(method, path, json=body, params=params)
            result = response.json()
            assert response.status_code == 200 and result.get("code") == 200, (
                path,
                response.status_code,
                result.get("code"),
            )
            return result.get("data")

        if cleanup:
            if state.get("conversation_id"):
                response = await buyer.get(MEMBER + "/conversations/" + state["conversation_id"])
                if response.status_code == 200:
                    await api(
                        buyer,
                        MEMBER + "/conversations/" + state["conversation_id"],
                        "DELETE",
                        {"expected_version": response.json()["data"]["conversation"]["version"]},
                    )
            # A simulated paid order consumed only the dedicated fixture's purchased quantity.
            # Restore those exact SKU resources once, retaining order/return/audit evidence.
            if state.get("fulfilled_order_id") and not state.get("demo_stock_restored"):
                with mysql() as db, db.cursor() as q:
                    db.begin()
                    q.execute(
                        "SELECT id,member_id,note FROM oms_order WHERE id=%s FOR UPDATE",
                        (state["fulfilled_order_id"],),
                    )
                    order = q.fetchone()
                    assert order and str(order["member_id"]) == state["member_id"]
                    marker = "[TEST ONLY] lifecycle resources restored"
                    if order["note"] != marker:
                        q.execute(
                            "SELECT product_sku_id,SUM(product_quantity) quantity FROM oms_order_item WHERE order_id=%s GROUP BY product_sku_id ORDER BY product_sku_id",
                            (order["id"],),
                        )
                        for item in q.fetchall():
                            q.execute(
                                "UPDATE pms_sku_stock SET stock=stock+%s WHERE id=%s",
                                (item["quantity"], item["product_sku_id"]),
                            )
                        q.execute("UPDATE oms_order SET note=%s WHERE id=%s", (marker, order["id"]))
                    db.commit()
                state["demo_stock_restored"] = True
            state["test_conversation_deleted"] = True
            save()
            passed("dedicated_demo_cleanup_and_exact_stock_restoration")
            return

        if "conversation_id" not in state:
            conv = await api(
                buyer,
                MEMBER + "/conversations",
                "POST",
                {"title": "[TEST ONLY] 订单售后直购验收", "entry_context": {"kind": "GENERAL"}},
                key("conversation"),
                201,
            )
            state["conversation_id"] = conv["id"]
            save()
        cid = state["conversation_id"]
        if "address_id" not in state:
            addresses = await legacy(buyer, "/mall-portal/member/address/list", method="GET")
            if not addresses:
                await legacy(
                    buyer,
                    "/mall-portal/member/address/add",
                    {
                        "name": "演示收货人",
                        "phoneNumber": "13800000000",
                        "defaultStatus": 0,
                        "postCode": "000000",
                        "province": "演示省",
                        "city": "演示市",
                        "region": "演示区",
                        "detailAddress": "[TEST ONLY] 不寄送实物",
                    },
                )
                addresses = await legacy(buyer, "/mall-portal/member/address/list", method="GET")
            state["address_id"] = str(addresses[0]["id"])
            product = await api(buyer, MEMBER + "/products/45")
            sku = next(s for s in product["sku_options"] if s["available_quantity"] >= 3)
            state.update(product_id=product["id"], sku_id=sku["id"])
            with mysql() as db, db.cursor() as q:
                q.execute("SELECT stock,lock_stock FROM pms_sku_stock WHERE id=%s", (state["sku_id"],))
                state["stock_before"] = q.fetchone()
            save()
        selection = {
            "action_type": "CREATE_ORDER",
            "source": "DIRECT",
            "items": [{"product_id": state["product_id"], "sku_id": state["sku_id"], "quantity": 2}],
        }
        options = await api(buyer, MEMBER + "/checkout-options", "POST", selection, key("options"))
        assert options["addresses"] and any(a["id"] == state["address_id"] for a in options["addresses"])
        assert not await legacy(buyer, "/mall-portal/cart/list", method="GET")

        async def prepare(name, parameters):
            if name in state["actions"]:
                return await api(buyer, MEMBER + "/actions/" + state["actions"][name])
            snap = await api(buyer, MEMBER + "/conversations/" + cid)
            action = await api(
                buyer,
                MEMBER + f"/conversations/{cid}/actions",
                "POST",
                {"expected_conversation_version": snap["conversation"]["version"], "parameters": parameters},
                key("prepare:" + name),
                201,
            )
            state["actions"][name] = action["id"]
            save()
            return action

        async def confirm(name, action):
            if action["state"] == "PENDING_CONFIRMATION":
                action = await api(
                    buyer,
                    MEMBER + f"/actions/{action['id']}/confirm",
                    "POST",
                    {"expected_version": action["version"]},
                    key("confirm:" + name),
                    202,
                )
            for _ in range(90):
                if action["state"] not in {"EXECUTING", "UNKNOWN"}:
                    break
                await asyncio.sleep(1)
                action = await api(buyer, MEMBER + "/actions/" + action["id"])
            assert action["state"] == "SUCCEEDED", (name, action["state"], action["error_code"])
            return action

        params = selection | {"address_id": state["address_id"], "coupon_id": None, "use_points": 0}
        order_action = await confirm("direct-cancel", await prepare("direct-cancel", params))
        oid = order_action["result"]["business_id"]
        state["cancelled_order_id"] = oid
        save()
        cancel = await confirm(
            "cancel-order", await prepare("cancel-order", {"action_type": "CANCEL_ORDER", "order_id": oid})
        )
        assert cancel["result"]["order"]["status"] == 4
        passed("direct_original_checkout_without_cart_and_confirmed_cancel")
        fulfilled = await confirm("direct-return", await prepare("direct-return", params))
        oid = fulfilled["result"]["business_id"]
        state["fulfilled_order_id"] = oid
        save()
        current = await api(buyer, MEMBER + "/orders/" + oid)
        if current["status"] == 0:
            await legacy(buyer, "/mall-portal/order/paySuccess", params={"orderId": oid, "payType": 1})
        current = await api(buyer, MEMBER + "/orders/" + oid)
        if current["status"] == 1:
            await legacy(
                staff,
                "/mall-admin/order/update/delivery",
                [{"orderId": int(oid), "deliveryCompany": "演示快递", "deliverySn": "DEMO-" + oid}],
            )
        current = await api(buyer, MEMBER + "/orders/" + oid)
        if current["status"] == 2:
            await legacy(buyer, "/mall-portal/order/confirmReceiveOrder", params={"orderId": oid})
        current = await api(buyer, MEMBER + "/orders/" + oid)
        assert current["status"] == 3 and current["lines"]
        assert (await stranger.get(MEMBER + "/orders/" + oid)).status_code == 404
        page = await api(buyer, MEMBER + "/orders?limit=1")
        assert len(page["items"]) == 1 and page["has_more"]
        second = await api(buyer, MEMBER + "/orders?limit=1&cursor=" + page["next_cursor"])
        assert second["items"][0]["id"] != page["items"][0]["id"]
        passed("member_order_pagination_ownership_and_simulated_payment_fulfillment")

        if "attachment_id" not in state:
            image = io.BytesIO()
            Image.new("RGB", (100, 70), "blue").save(image, format="PNG")
            response = await buyer.post(
                MEMBER + "/attachments",
                headers={"Idempotency-Key": key("proof")},
                data={"purpose": "CHAT_IMAGE"},
                files={"file": ("synthetic-return-proof.png", image.getvalue(), "image/png")},
            )
            assert response.status_code == 201
            state["attachment_id"] = response.json()["data"]["id"]
            save()
        line = current["lines"][0]
        eligibility = await api(
            buyer, MEMBER + f"/orders/{oid}/items/{line['order_item_id']}/return-eligibility"
        )
        if "return" not in state["actions"]:
            assert eligibility["eligible"] and eligibility["reason_codes"]
        action = await confirm(
            "return",
            await prepare(
                "return",
                {
                    "action_type": "CREATE_RETURN",
                    "order_id": oid,
                    "order_item_id": line["order_item_id"],
                    "quantity": 1,
                    "reason_code": eligibility["reason_codes"][0]["code"],
                    "description": "[TEST ONLY] 演示退货，不寄送实物",
                    "attachment_ids": [state["attachment_id"]],
                },
            ),
        )
        rid = action["result"]["business_id"]
        state["return_id"] = rid
        save()
        returned = await api(staff, ADMIN + "/returns/" + rid)
        if returned["stage"] == "REQUESTED":
            premature = await staff.post(
                ADMIN + f"/returns/{rid}/simulate-refund",
                headers={"Idempotency-Key": str(uuid4())},
                json={"expected_version": returned["version"], "note": "[TEST ONLY] stage guard"},
            )
            assert premature.status_code == 409
            address = (await api(staff, ADMIN + "/return-addresses"))["items"][0]
            returned = await api(
                staff,
                ADMIN + f"/returns/{rid}/review",
                "POST",
                {
                    "expected_version": returned["version"],
                    "decision": "APPROVE",
                    "approved_amount": returned["requested_amount"],
                    "company_address_id": address["id"],
                    "note": "[TEST ONLY] 批准演示申请",
                },
                key("review"),
            )
        if returned["stage"] == "APPROVED":
            returned = await api(
                staff,
                ADMIN + f"/returns/{rid}/receive",
                "POST",
                {"expected_version": returned["version"], "note": "[TEST ONLY] 演示收到退货"},
                key("receive"),
            )
        if returned["stage"] == "GOODS_RECEIVED":
            state["refund_request"] = {
                "expected_version": returned["version"],
                "note": "[TEST ONLY] 独立演示退款，无真实资金",
            }
            save()
            returned = await api(
                staff,
                ADMIN + f"/returns/{rid}/simulate-refund",
                "POST",
                state["refund_request"],
                key("refund"),
            )
        replay = await api(
            staff, ADMIN + f"/returns/{rid}/simulate-refund", "POST", state["refund_request"], key("refund")
        )
        assert replay == returned and returned["stage"] == "SIMULATED_REFUNDED"
        assert (
            await staff.get(ADMIN + "/attachments/" + state["attachment_id"] + "/content")
        ).status_code == 200
        assert (
            await stranger.get(MEMBER + "/attachments/" + state["attachment_id"] + "/content")
        ).status_code == 404
        passed("item_return_proof_review_receive_independent_demo_refund_and_replay")

        log = await api(staff, ADMIN + f"/orders/{oid}/logistics")
        if not log["shipment_id"]:
            log = await api(
                staff,
                ADMIN + f"/orders/{oid}/logistics",
                "PUT",
                {"expected_version": log["version"], "carrier": "演示快递", "tracking_no": "DEMO-" + oid},
                key("shipment"),
            )
        if not log["nodes"]:
            state["node_request"] = {
                "expected_shipment_version": log["version"],
                "occurred_at": datetime.now(UTC).isoformat(),
                "location": "演示分拨中心",
                "description": "[TEST ONLY] 演示运输节点",
            }
            save()
            await api(
                staff,
                ADMIN + f"/shipments/{log['shipment_id']}/events",
                "POST",
                state["node_request"],
                key("node"),
                201,
            )
        log = await api(buyer, MEMBER + f"/orders/{oid}/logistics")
        assert log["nodes"] and log["is_simulated"]
        assert (await stranger.get(MEMBER + f"/orders/{oid}/logistics")).status_code == 404
        passed("staff_versioned_demo_logistics_visible_only_to_order_owner")

        if models and not state.get("model_queries_completed"):
            for label, text in [
                ("ORDER", f"请查询我的订单{current['order_sn']}当前状态和演示物流，不执行任何操作。"),
                ("RETURN", f"请查询我的订单{current['order_sn']}的退货申请处理结果，明确说明是演示退款。"),
            ]:
                if label in state.get("model_answers", {}):
                    continue
                sent = await api(
                    buyer,
                    MEMBER + f"/conversations/{cid}/messages",
                    "POST",
                    {"client_message_id": key("model:" + label), "text": text, "attachment_ids": []},
                    key("model:" + label),
                    202,
                )
                run_id = sent["run"]["id"]
                original = await api(buyer, MEMBER + "/runs/" + run_id)
                if original["state"] in {"FAILED", "INTERRUPTED"}:
                    retried = await api(buyer, MEMBER + f"/runs/{run_id}/retry", "POST", {"expected_version": original["version"]}, key("model-retry:"+label), 202)
                    state.setdefault("retried_model_runs", {})[label] = retried["id"]
                    save()
                answer = await wait_run(buyer, cid)
                state.setdefault("model_answers", {})[label] = {
                    "message_id": answer["id"],
                    "text": answer["text"],
                }
                save()
            state["model_queries_completed"] = True
            state["paid_usage"] = "TWO_EXPLICIT_READONLY_ORDER_RETURN_DIALOGS"
            passed("real_order_and_return_chat_nodes_with_current_member_queries")
        state["verified_at"] = datetime.now(UTC).isoformat()
        state["real_financial_channels_called"] = False
        save()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    arguments = parser.parse_args()
    run(main(arguments.models, arguments.cleanup))
