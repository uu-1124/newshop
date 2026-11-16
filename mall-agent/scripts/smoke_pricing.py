"""Verify an actual V1 -> V2 package upgrade using one dedicated demo buyer.

Run prepare-legacy while the old portal is active, switch the package, then run verify.
Creates demo records through the real gateway and cancels every test order afterwards.
No model call or payment is performed. Existing product prices and rules are read only.
"""

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pymysql

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "deployment/.run/pricing-upgrade-smoke.json"
API = "/mall-agent/v1/member"


def connect_mysql():
    config = json.loads((ROOT / "deployment/config/portal.yaml").read_text(encoding="utf-8"))
    source = config["spring"]["datasource"]
    return pymysql.connect(
        host="127.0.0.1",
        port=13306,
        user=source["username"],
        password=source["password"],
        database="mall",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def save(state):
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def main(phase):
    mysql = connect_mysql()
    headers = {}
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=40, trust_env=False) as client:

        async def call(method, path, body=None, *, legacy=False, key=None, **kwargs):
            response = await client.request(
                method, path, json=body, headers=headers | ({"Idempotency-Key": key} if key else {}), **kwargs
            )
            result = response.json()
            if response.status_code >= 300 or result.get("code") != (200 if legacy else "OK"):
                raise RuntimeError(
                    f"Pricing check rejected {path}: HTTP {response.status_code}, code {result.get('code')}"
                )
            return result.get("data") if legacy else result["data"]

        async def login():
            result = await call(
                "POST",
                "/mall-portal/sso/login",
                legacy=True,
                data={
                    "username": state["test_buyer"],
                    "password": os.environ.get("MALL_DEMO_MEMBER_PASSWORD", "MallDemo2026!"),
                },
            )
            headers["Authorization"] = result["tokenHead"] + result["token"]

        async def prepare(parameters):
            snapshot = await call("GET", f"{API}/conversations/{state['conversation_id']}")
            return await call(
                "POST",
                f"{API}/conversations/{state['conversation_id']}/actions",
                {
                    "expected_conversation_version": snapshot["conversation"]["version"],
                    "parameters": parameters,
                },
                key=str(uuid4()),
            )

        async def add_cart():
            action = await prepare(
                {
                    "action_type": "ADD_CART",
                    "items": [{"product_id": state["product_id"], "sku_id": state["sku_id"], "quantity": 3}],
                }
            )
            result = await call(
                "POST",
                f"{API}/actions/{action['id']}/confirm",
                {"expected_version": action["version"]},
                key=str(uuid4()),
            )
            assert result["state"] == "SUCCEEDED"
            cart = await call("GET", "/mall-portal/cart/list", legacy=True)
            assert len(cart) == 1 and cart[0]["quantity"] == 3
            return [str(cart[0]["id"])]

        def facts():
            with mysql.cursor() as query:
                query.execute("SELECT stock,lock_stock FROM pms_sku_stock WHERE id=%s", (state["sku_id"],))
                stock = query.fetchone()
                query.execute("SELECT integration FROM ums_member WHERE id=%s", (state["member_id"],))
                return stock, query.fetchone()["integration"]

        def order_count():
            with mysql.cursor() as query:
                query.execute("SELECT COUNT(*) n FROM oms_order WHERE member_id=%s", (state["member_id"],))
                return query.fetchone()["n"]

        async def check_order(order_id, quote, *, active=True):
            detail = await call("GET", f"/mall-portal/order/detail/{order_id}", legacy=True)
            items = detail["orderItemList"]
            assert detail["status"] == (0 if active else 4) and len(items) >= 2
            assert sum(item["productQuantity"] for item in items) == 3
            for name, pricing in (
                ("promotionAmount", "promotion_discount"),
                ("couponAmount", "coupon_discount"),
                ("integrationAmount", "points_discount"),
                ("realAmount", "payable"),
            ):
                total = sum(Decimal(str(item[name])) * item["productQuantity"] for item in items)
                assert total == Decimal(quote["pricing"][pricing]), (name, total)
            assert Decimal(str(detail["payAmount"])) == Decimal(quote["pricing"]["payable"])
            assert all(Decimal(str(item["realAmount"])) >= 0 for item in items)
            if active:
                stock, points = facts()
                assert stock == state["stock_before"] | {
                    "lock_stock": state["stock_before"]["lock_stock"] + 3
                }
                assert points == 4900
            recorded = {item["id"] for item in state["orders"]}
            if str(order_id) not in recorded:
                state["orders"].append(
                    {"id": str(order_id), "payable": quote["pricing"]["payable"], "detail_rows": len(items)}
                )
            save(state)

        async def cleanup():
            with mysql.cursor() as query:
                query.execute(
                    "SELECT id FROM ums_member WHERE id=%s AND username=%s",
                    (state["member_id"], state["test_buyer"]),
                )
                assert query.fetchone(), "Dedicated buyer identity must match before cleanup"
                query.execute(
                    "SELECT id FROM oms_order WHERE member_id=%s AND status=0", (state["member_id"],)
                )
                pending = query.fetchall()
            for order in pending:
                await call(
                    "POST", "/mall-portal/order/cancelUserOrder", legacy=True, params={"orderId": order["id"]}
                )
            with mysql.cursor() as query:
                query.execute(
                    "SELECT COUNT(*) n FROM oms_order WHERE member_id=%s AND status<>4", (state["member_id"],)
                )
                assert query.fetchone()["n"] == 0
            stock, points = facts()
            assert stock == state["stock_before"] and points == 5000
            state["test_orders_cancelled"] = True
            state["stock_and_points_restored"] = True
            save(state)

        try:
            if phase == "prepare-legacy":
                assert not state, (
                    "Upgrade evidence already exists; preserve it and use a fresh run deliberately"
                )
                state = {
                    "started_at": datetime.now(UTC).isoformat(),
                    "test_buyer": "agent-pricing-" + uuid4().hex[:10],
                    "checks": [],
                    "orders": [],
                    "paid_models_called": False,
                    "payment_executed": False,
                }
                with mysql.cursor() as query:
                    query.execute(
                        "SELECT s.id sku_id,s.product_id,s.stock,s.lock_stock,s.price FROM pms_sku_stock s "
                        "JOIN pms_product p ON p.id=s.product_id WHERE p.publish_status=1 AND p.delete_status=0 "
                        "AND p.promotion_type=0 AND s.price>=10 AND s.stock-s.lock_stock>=3 ORDER BY s.id LIMIT 1"
                    )
                    selected = query.fetchone()
                    assert selected, "An existing nonpromotional SKU with three available units is required"
                    query.execute(
                        "INSERT INTO ums_member(username,password,nickname,status,integration,create_time) "
                        "SELECT %s,password,'[TEST ONLY] pricing upgrade',1,5000,NOW() "
                        "FROM ums_member WHERE username='member' LIMIT 1",
                        (state["test_buyer"],),
                    )
                    assert query.rowcount == 1
                    state.update(
                        member_id=str(query.lastrowid),
                        sku_id=str(selected["sku_id"]),
                        product_id=str(selected["product_id"]),
                        unit_price=str(selected["price"]),
                        stock_before={name: selected[name] for name in ("stock", "lock_stock")},
                    )
                save(state)
                await login()
                await call(
                    "POST",
                    "/mall-portal/member/address/add",
                    {
                        "name": "金额验收买家",
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
                conversation = await call(
                    "POST",
                    f"{API}/conversations",
                    {"title": "[TEST ONLY] V1到V2金额验收", "entry_context": {"kind": "GENERAL"}},
                    key=str(uuid4()),
                )
                state["conversation_id"] = conversation["id"]
                cart_ids = await add_cart()
                selection = {"action_type": "CREATE_ORDER", "source": "CART", "cart_item_ids": cart_ids}
                options = await call("POST", f"{API}/checkout-options", selection, key=str(uuid4()))
                state["parameters"] = selection | {
                    "address_id": options["addresses"][0]["id"],
                    "coupon_id": None,
                    "use_points": 0,
                }
                legacy = await prepare(state["parameters"])
                assert legacy["quote"]["rule_version"] == "ORDER_CHECKOUT_V1"
                assert order_count() == 0 and facts() == (state["stock_before"], 5000)
                state["legacy_action"] = legacy
                state["checks"].append("actual_v1_package_prepared_readonly_quote")
                save(state)
                print("PASS legacy quote prepared; switch portal package before running verify")
                return

            assert state.get("legacy_action") and not state.get("verified_at"), (
                "An unfinished legacy preparation is required"
            )
            await login()
            legacy = state["legacy_action"]
            legacy_checked = "actual_v1_quote_rejected_by_v2_without_order_or_resource_changes"
            if legacy_checked in state["checks"]:
                rejected = await call("GET", f"{API}/actions/{legacy['id']}")
            else:
                rejected = await call(
                    "POST",
                    f"{API}/actions/{legacy['id']}/confirm",
                    {"expected_version": legacy["version"]},
                    key=str(uuid4()),
                )
                assert order_count() == 0 and facts() == (state["stock_before"], 5000)
                state["checks"].append(legacy_checked)
            assert rejected["state"] == "FAILED" and rejected["error_code"] == "QUOTE_CHANGED", rejected
            save(state)
            try:
                if state.get("successful_action_id"):
                    state["resumed_at"] = datetime.now(UTC).isoformat()
                    fresh = await call("GET", f"{API}/actions/{state['successful_action_id']}")
                    assert fresh["state"] == "SUCCEEDED", "Resume must query the original operation"
                else:
                    fresh = await prepare(state["parameters"] | {"use_points": 100})
                quote = fresh["quote"]
                assert quote["rule_version"] == "ORDER_CHECKOUT_CENTS_V2" and len(quote["lines"]) == 1
                assert quote["pricing"]["points_discount"] == "1.00"
                assert Decimal(quote["pricing"]["payable"]) == Decimal(state["unit_price"]) * 3 - 1
                if not state.get("successful_action_id"):
                    assert order_count() == 0 and facts() == (state["stock_before"], 5000)
                    key = str(uuid4())
                    replies = await asyncio.gather(
                        *[
                            call(
                                "POST",
                                f"{API}/actions/{fresh['id']}/confirm",
                                {"expected_version": fresh["version"]},
                                key=key,
                            )
                            for _ in range(2)
                        ]
                    )
                    assert all(reply["id"] == fresh["id"] for reply in replies)
                result = await call("GET", f"{API}/actions/{fresh['id']}")
                assert result["state"] == "SUCCEEDED"
                order_id = result["result"]["business_id"]
                state["successful_action_id"] = fresh["id"]
                detail = await call("GET", f"/mall-portal/order/detail/{order_id}", legacy=True)
                await check_order(order_id, quote, active=detail["status"] == 0)
                allocation_checked = "v2_exact_points_allocation_duplicate_confirm_and_real_order_detail"
                if allocation_checked not in state["checks"]:
                    assert order_count() == 1
                    state["checks"].append(allocation_checked)
                recovery_checked = "live_scheduler_recovers_same_order_without_reexecution"
                if recovery_checked not in state["checks"]:
                    config = json.loads((ROOT / "deployment/.run/agent-env.json").read_text(encoding="utf-8"))
                    pg_url = config["databaseUrl"].replace("postgresql+psycopg://", "postgresql://")
                    with psycopg.connect(pg_url) as database:
                        changed = database.execute(
                            "UPDATE ag_action SET state='UNKNOWN',result=NULL,error_code='TEST_RESPONSE_LOSS',version=version+1 "
                            "WHERE id=%s AND member_id=%s AND state='SUCCEEDED'",
                            (fresh["id"], state["member_id"]),
                        )
                        assert changed.rowcount == 1
                        database.execute(
                            "UPDATE ag_action_dispatch SET state='RECONCILING',lease_owner=NULL,lease_until=NULL,"
                            "next_attempt_at=now(),version=version+1 WHERE action_id=%s",
                            (fresh["id"],),
                        )
                    for _ in range(40):
                        restored = await call("GET", f"{API}/actions/{fresh['id']}")
                        if restored["state"] == "SUCCEEDED":
                            break
                        await asyncio.sleep(1)
                    assert restored["state"] == "SUCCEEDED" and restored["result"]["business_id"] == order_id
                    assert order_count() == 1
                    state["checks"].append(recovery_checked)
                await cleanup()
                with mysql.cursor() as query:
                    query.execute(
                        "SELECT id,status FROM oms_order WHERE member_id=%s AND id<>%s",
                        (state["member_id"], order_id),
                    )
                    original_orders = query.fetchall()
                assert len(original_orders) <= 1, "Resume must not create a duplicate storefront order"
                if original_orders:
                    normal_id = str(original_orders[0]["id"])
                    await check_order(normal_id, quote, active=False)
                else:
                    cart_ids = await add_cart()
                    normal = await call(
                        "POST",
                        "/mall-portal/order/generateOrder",
                        {
                            "cartIds": cart_ids,
                            "memberReceiveAddressId": state["parameters"]["address_id"],
                            "couponId": None,
                            "useIntegration": 100,
                            "payType": 0,
                        },
                        legacy=True,
                    )
                    await check_order(str(normal["order"]["id"]), quote)
                assert order_count() == 2
                normal_checked = "original_storefront_order_writer_uses_same_exact_allocation"
                if normal_checked not in state["checks"]:
                    state["checks"].append(normal_checked)
            finally:
                await cleanup()
            historical = await call("GET", f"{API}/actions/{state['successful_action_id']}")
            assert historical["state"] == "SUCCEEDED"
            state["checks"].append("cancel_restores_resources_and_historical_action_remains_successful")
            state["verified_at"] = datetime.now(UTC).isoformat()
            save(state)
            print(
                "PASS V1 upgrade rejection, V2 exact cents, duplicate confirmation, recovery, original checkout and cleanup"
            )
        finally:
            mysql.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare-legacy", "verify"))
    asyncio.run(main(parser.parse_args().phase))
