"""Exercise a dedicated demo buyer through the real gateway; cancel test orders afterwards."""

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


async def main():
    from jsonschema import Draft202012Validator

    contract = json.loads((ROOT / "docs/api/openapi.json").read_text(encoding="utf-8"))

    def validate(name, data):
        Draft202012Validator(
            {"$ref": f"#/components/schemas/{name}", "components": contract["components"]}
        ).validate(data)

    run = ROOT / "deployment/.run"
    evidence = {"verified_at": datetime.now(UTC).isoformat(), "checks": []}
    portal = json.loads((ROOT / "deployment/config/portal.yaml").read_text(encoding="utf-8"))["spring"][
        "datasource"
    ]
    mysql = pymysql.connect(
        host="127.0.0.1",
        port=13306,
        user=portal["username"],
        password=portal["password"],
        database="mall",
        autocommit=True,
    )
    username = "agent-order-" + uuid4().hex[:10]
    with mysql.cursor() as q:
        q.execute(
            "INSERT INTO ums_member(username,password,nickname,status,integration,create_time) SELECT %s,password,'[TEST ONLY] order buyer',1,5000,NOW() FROM ums_member WHERE username='member' LIMIT 1",
            (username,),
        )
        assert q.rowcount == 1
        member_id = q.lastrowid
    evidence.update(test_buyer=username, member_id=str(member_id))
    config = json.loads((run / "agent-env.json").read_text(encoding="utf-8"))
    pg_url = config["databaseUrl"].replace("postgresql+psycopg://", "postgresql://")
    headers, order_ids = {}, []
    selected_sku, stock_before = None, None
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=40, trust_env=False) as client:

        async def call(method, path, body=None, key=None, legacy=False, **kwargs):
            response = await client.request(
                method, path, json=body, headers=headers | ({"Idempotency-Key": key} if key else {}), **kwargs
            )
            data = response.json()
            if response.status_code >= 300 or data.get("code") != (200 if legacy else "OK"):
                raise RuntimeError(
                    f"Checkout check rejected {path}: HTTP {response.status_code}, code {data.get('code')}"
                )
            return data.get("data")

        try:
            ready = (await client.get("http://127.0.0.1:8099/readyz")).json()
            assert (
                ready["build"] == "order-0.9"
                and ready["capabilities"]["transactions"] == "ADD_CART_AND_CREATE_ORDER_LIVE"
            )
            login = await call(
                "POST",
                "/mall-portal/sso/login",
                legacy=True,
                data={
                    "username": username,
                    "password": os.environ.get("MALL_DEMO_MEMBER_PASSWORD", "MallDemo2026!"),
                },
            )
            headers["Authorization"] = login["tokenHead"] + login["token"]
            await call(
                "POST",
                "/mall-portal/member/address/add",
                {
                    "name": "演示验收收货人",
                    "phoneNumber": "13800000000",
                    "defaultStatus": 1,
                    "postCode": "000000",
                    "province": "演示省",
                    "city": "演示市",
                    "region": "演示区",
                    "detailAddress": "测试订单不发货",
                },
                legacy=True,
            )
            conv = await call(
                "POST",
                "/mall-agent/v1/member/conversations",
                {"title": "[TEST ONLY] order checkout", "entry_context": {"kind": "GENERAL"}},
                str(uuid4()),
            )
            cid = conv["id"]
            evidence["conversation_id"] = cid
            key = str(uuid4())
            await call(
                "POST",
                f"/mall-agent/v1/member/conversations/{cid}/messages",
                {
                    "client_message_id": key,
                    "text": "我想购买手机，请先查询真实商品，供我选择规格，随后在购物车选择地址和优惠核对订单，不要自动下单。",
                    "attachment_ids": [],
                },
                key,
            )
            for _ in range(100):
                snap = await call("GET", f"/mall-agent/v1/member/conversations/{cid}")
                if not snap["active_run"]:
                    break
                await asyncio.sleep(1)
            else:
                raise RuntimeError("Shopping workflow did not finish")
            products = [
                b["product"] for m in snap["recent_messages"] for b in m["blocks"] if b["type"] == "product"
            ]
            assert products
            product = next(p for p in products if any(s["available_quantity"] >= 2 for s in p["sku_options"]))
            sku = next(s for s in product["sku_options"] if s["available_quantity"] >= 2)
            selected_sku = int(sku["id"])
            with mysql.cursor() as q:
                q.execute("SELECT stock,lock_stock FROM pms_sku_stock WHERE id=%s", (selected_sku,))
                stock_before = q.fetchone()
            evidence["checks"].append("real_gpt_shopping_workflow_returns_java_sku_cards")
            # Use the existing agent ADD_CART flow so no client-supplied price is used.
            add = await call(
                "POST",
                f"/mall-agent/v1/member/conversations/{cid}/actions",
                {
                    "expected_conversation_version": snap["conversation"]["version"],
                    "parameters": {
                        "action_type": "ADD_CART",
                        "items": [{"product_id": product["id"], "sku_id": sku["id"], "quantity": 1}],
                    },
                },
                str(uuid4()),
            )
            add = await call(
                "POST",
                f"/mall-agent/v1/member/actions/{add['id']}/confirm",
                {"expected_version": add["version"]},
                str(uuid4()),
            )
            assert add["state"] == "SUCCEEDED"
            cart = await call("GET", "/mall-portal/cart/list", legacy=True)
            selection = {
                "action_type": "CREATE_ORDER",
                "source": "CART",
                "cart_item_ids": [str(i["id"]) for i in cart],
            }
            options = await call("POST", "/mall-agent/v1/member/checkout-options", selection, str(uuid4()))
            validate("CheckoutOptions", options)
            assert len(options["addresses"]) == 1 and options["available_points"] == 5000
            snap = await call("GET", f"/mall-agent/v1/member/conversations/{cid}")
            action = await call(
                "POST",
                f"/mall-agent/v1/member/conversations/{cid}/actions",
                {
                    "expected_conversation_version": snap["conversation"]["version"],
                    "parameters": selection
                    | {"address_id": options["addresses"][0]["id"], "coupon_id": None, "use_points": 100},
                },
                str(uuid4()),
            )
            validate("Action", action)
            evidence["action_id"] = action["id"]
            with mysql.cursor() as q:
                q.execute("SELECT COUNT(*) FROM oms_order WHERE member_id=%s", (member_id,))
                assert q.fetchone()[0] == 0
                q.execute("SELECT integration FROM ums_member WHERE id=%s", (member_id,))
                assert q.fetchone()[0] == 5000
            with mysql.cursor() as q:
                q.execute("SELECT stock,lock_stock FROM pms_sku_stock WHERE id=%s", (selected_sku,))
                assert q.fetchone() == stock_before
            assert await call("GET", "/mall-portal/cart/list", legacy=True) == cart
            evidence["checks"].append("prepare_keeps_cart_stock_points_and_orders_unchanged")
            key = str(uuid4())
            results = await asyncio.gather(
                *[
                    call(
                        "POST",
                        f"/mall-agent/v1/member/actions/{action['id']}/confirm",
                        {"expected_version": action["version"]},
                        key,
                    )
                    for _ in range(2)
                ]
            )
            result = await call("GET", f"/mall-agent/v1/member/actions/{action['id']}")
            assert result["state"] == "SUCCEEDED", (
                f"Expected order success, observed {result['state']} {result.get('error_code')}"
            )
            validate("Action", result)
            order = result["result"]["order"]
            order_ids.append(order["id"])
            evidence["order_id"] = order["id"]
            assert order["pay_amount"] == action["quote"]["pricing"]["payable"] and order["status"] == 0
            assert all(r["id"] == action["id"] for r in results)
            detail = await call("GET", f"/mall-portal/order/detail/{order['id']}", legacy=True)
            assert detail["status"] == 0
            with mysql.cursor() as q:
                q.execute("SELECT COUNT(*) FROM oms_order WHERE member_id=%s", (member_id,))
                assert q.fetchone()[0] == 1
            with mysql.cursor() as q:
                q.execute("SELECT stock,lock_stock FROM pms_sku_stock WHERE id=%s", (selected_sku,))
                assert q.fetchone() == (stock_before[0], stock_before[1] + 1)
            evidence["checks"].append(
                "duplicate_confirmation_creates_one_original_java_order_with_exact_quote"
            )
            with psycopg.connect(pg_url) as db:
                db.execute(
                    "UPDATE ag_action SET state='UNKNOWN',result=NULL,error_code='TEST_RESPONSE_LOSS',version=version+1 WHERE id=%s",
                    (action["id"],),
                )
                db.execute(
                    "UPDATE ag_action_dispatch SET state='RECONCILING',lease_owner=NULL,lease_until=NULL,next_attempt_at=now(),version=version+1 WHERE action_id=%s",
                    (action["id"],),
                )
            for _ in range(35):
                restored = await call("GET", f"/mall-agent/v1/member/actions/{action['id']}")
                if restored["state"] == "SUCCEEDED":
                    break
                await asyncio.sleep(1)
            assert restored["state"] == "SUCCEEDED" and restored["result"]["business_id"] == order["id"]
            with mysql.cursor() as q:
                q.execute("SELECT COUNT(*) FROM oms_order WHERE member_id=%s", (member_id,))
                assert q.fetchone()[0] == 1
            evidence["checks"].append("real_scheduler_recovers_committed_order_without_reexecution")
        finally:
            # Cancel only this freshly-created dedicated buyer's test orders through the original API.
            # Preserve the order, operation and confirmation evidence; no payment is performed.
            if headers:
                with mysql.cursor() as q:
                    q.execute("SELECT id FROM oms_order WHERE member_id=%s AND status=0", (member_id,))
                    cleanup_ids = [str(row[0]) for row in q.fetchall()]
                for order_id in cleanup_ids:
                    await call(
                        "POST",
                        "/mall-portal/order/cancelUserOrder",
                        legacy=True,
                        params={"orderId": order_id},
                    )
                with mysql.cursor() as q:
                    q.execute("SELECT COUNT(*) FROM oms_order WHERE member_id=%s AND status<>4", (member_id,))
                    assert q.fetchone()[0] == 0
                    q.execute("SELECT integration FROM ums_member WHERE id=%s", (member_id,))
                    assert q.fetchone()[0] == 5000
                if selected_sku is not None and stock_before is not None:
                    with mysql.cursor() as q:
                        q.execute("SELECT stock,lock_stock FROM pms_sku_stock WHERE id=%s", (selected_sku,))
                        assert q.fetchone() == stock_before
                    evidence["test_stock_and_reservation_restored"] = True
                evidence["test_orders_cancelled_and_points_restored"] = True
            (run / "order-smoke.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            mysql.close()
    print("PASS real GPT, existing Java quote/order, duplicate confirmation, recovery and test-order cleanup")


if __name__ == "__main__":
    asyncio.run(main())
