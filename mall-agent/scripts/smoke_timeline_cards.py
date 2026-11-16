"""Dedicated demo UI fixtures and read-only fingerprints; no production SQL writes.

All business changes use existing authenticated gateway APIs. A private test product
keeps the original SKU/order/return fixture unchanged. No paid model is called.
"""

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from expansion_client import ADMIN, MEMBER, api, login
from smoke_order_lifecycle import mysql

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "deployment/.run/completion-timeline-cards-fixture-20261114.json"
sys.path.insert(0, str(ROOT / "deployment"))


def save(state):
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def fingerprint(items):
    return {
        "rows": len(items),
        "sha256": hashlib.sha256(json.dumps(items, sort_keys=True, default=str).encode()).hexdigest(),
    }


def snapshot(state, name):
    from backup_data import pg_connection

    result = {"checked_at": datetime.now(UTC).isoformat(), "mysql": {}}
    with mysql() as db, db.cursor() as cursor:
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        cursor.execute("START TRANSACTION READ ONLY")
        selections = {
            "original_order": ("SELECT * FROM oms_order WHERE id=116", ()),
            "original_shipment": ("SELECT * FROM oms_agent_shipment WHERE order_id=116", ()),
            "original_nodes": (
                "SELECT e.* FROM oms_agent_shipment_event e JOIN oms_agent_shipment s ON s.id=e.shipment_id WHERE s.order_id=116 ORDER BY e.id",
                (),
            ),
            "original_return": ("SELECT * FROM oms_agent_return WHERE return_id=38", ()),
            "original_stock": ("SELECT * FROM pms_sku_stock WHERE id=239", ()),
            "staff_commands": ("SELECT * FROM oms_agent_staff_command ORDER BY staff_id,command_key", ()),
            "business_audit": ("SELECT * FROM oms_agent_business_audit ORDER BY id", ()),
        }
        if state.get("shipment_id"):
            selections["test_shipment"] = (
                "SELECT * FROM oms_agent_shipment WHERE id=%s",
                (state["shipment_id"],),
            )
            selections["test_nodes"] = (
                "SELECT * FROM oms_agent_shipment_event WHERE shipment_id=%s ORDER BY id",
                (state["shipment_id"],),
            )
        for key, (query, parameters) in selections.items():
            cursor.execute(query, parameters)
            result["mysql"][key] = fingerprint(cursor.fetchall())
        if state.get("shipment_id"):
            cursor.execute(
                "SELECT id,occurred_at,version FROM oms_agent_shipment_event WHERE shipment_id=%s ORDER BY occurred_at,id",
                (state["shipment_id"],),
            )
            result["node_times"] = cursor.fetchall()
        db.rollback()
    with pg_connection(ROOT, True) as conn:
        result["pg_read_only"] = conn.execute("SHOW transaction_read_only").fetchone()[
            "transaction_read_only"
        ]
        result["logistics_audit"] = fingerprint(
            [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM ag_audit_log WHERE operation='agent:logistics:write' ORDER BY id"
                )
            ]
        )
        result["logistics_http"] = [
            dict(row)
            for row in conn.execute(
                "SELECT operation,status_code,created_at FROM ag_request_metric "
                "WHERE operation IN ('adminCreateLogisticsNode','adminEditLogisticsNode') "
                "AND created_at>=%s ORDER BY created_at",
                (state["created_at"],),
            )
        ]
    state.setdefault("snapshots", {})[name] = result
    save(state)
    return result


async def legacy(client, path, body=None, params=None, method="POST"):
    response = await client.request(method, path, json=body, params=params)
    value = response.json()
    assert response.status_code == 200 and value.get("code") == 200, (
        path,
        response.status_code,
        value.get("code"),
    )
    return value.get("data")


async def prepare(state, cancel_only=False, next_cancel=False):
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
        if "original_baseline" not in state.get("snapshots", {}):
            snapshot(state, "original_baseline")
        if not state.get("product_id"):
            with mysql() as db, db.cursor() as q:
                q.execute("SELECT id FROM pms_product WHERE product_sn=%s", (state["product_sn"],))
                existing = q.fetchall()
            assert len(existing) <= 1
            if not existing:
                source = await legacy(staff, "/mall-admin/product/updateInfo/45", method="GET")
                product = {
                    k: source[k]
                    for k in ("brandId", "productCategoryId", "productAttributeCategoryId", "unit")
                    if k in source
                }
                product.update(
                    name="[TEST ONLY] 物流时间与取消卡验收 " + state["product_sn"],
                    productSn=state["product_sn"],
                    subTitle="仅供演示验收，不寄送实物",
                    deleteStatus=0,
                    publishStatus=1,
                    verifyStatus=1,
                    previewStatus=0,
                    newStatus=0,
                    recommandStatus=0,
                    price="10.01",
                    originalPrice="10.01",
                    stock=8,
                    lowStock=0,
                    sale=0,
                    promotionType=0,
                    giftPoint=0,
                    giftGrowth=0,
                    usePointLimit=0,
                    weight="0.01",
                    productLadderList=[],
                    productFullReductionList=[],
                    memberPriceList=[],
                    productAttributeValueList=[],
                    subjectProductRelationList=[],
                    prefrenceAreaProductRelationList=[],
                    skuStockList=[
                        {
                            "skuCode": state["product_sn"],
                            "price": "10.01",
                            "stock": 8,
                            "lockStock": 0,
                            "lowStock": 0,
                            "sale": 0,
                            "spData": "[]",
                        }
                    ],
                )
                await legacy(staff, "/mall-admin/product/create", product)
            with mysql() as db, db.cursor() as q:
                q.execute("SELECT id FROM pms_product WHERE product_sn=%s", (state["product_sn"],))
                state["product_id"] = str(q.fetchone()["id"])
                q.execute("SELECT id FROM pms_sku_stock WHERE product_id=%s", (state["product_id"],))
                stock = q.fetchall()
                assert len(stock) == 1
                state["sku_id"] = str(stock[0]["id"])
            save(state)
        if not state.get("conversation_id"):
            conv = await api(
                buyer,
                MEMBER + "/conversations",
                "POST",
                {
                    "title": "[TEST ONLY] 时间与取消卡 " + state["product_sn"],
                    "entry_context": {"kind": "GENERAL"},
                },
                key("conversation"),
                201,
            )
            state["conversation_id"] = conv["id"]
            save(state)
        cid = state["conversation_id"]

        async def action(name, parameters, confirm=False):
            if name not in state["actions"]:
                current = await api(buyer, MEMBER + "/conversations/" + cid)
                value = await api(
                    buyer,
                    MEMBER + f"/conversations/{cid}/actions",
                    "POST",
                    {
                        "expected_conversation_version": current["conversation"]["version"],
                        "parameters": parameters,
                    },
                    key("prepare:" + name),
                    201,
                )
                state["actions"][name] = value["id"]
                save(state)
            value = await api(buyer, MEMBER + "/actions/" + state["actions"][name])
            if confirm and value["state"] == "PENDING_CONFIRMATION":
                value = await api(
                    buyer,
                    MEMBER + f"/actions/{value['id']}/confirm",
                    "POST",
                    {"expected_version": value["version"]},
                    key("confirm:" + name),
                    202,
                )
            for _ in range(90):
                if value["state"] not in {"EXECUTING", "UNKNOWN"}:
                    break
                await asyncio.sleep(1)
                value = await api(buyer, MEMBER + "/actions/" + value["id"])
            if confirm:
                assert value["state"] == "SUCCEEDED", (name, value["state"], value.get("error_code"))
            return value

        parameters = {
            "action_type": "CREATE_ORDER",
            "source": "DIRECT",
            "items": [{"product_id": state["product_id"], "sku_id": state["sku_id"], "quantity": 1}],
            "address_id": "20",
            "coupon_id": None,
            "use_points": 0,
        }
        if not cancel_only:
            created = await action("timeline-order", parameters, True)
            oid = state["timeline_order_id"] = created["result"]["business_id"]
            save(state)
            order = await api(buyer, MEMBER + "/orders/" + oid)
            if order["status"] == 0:
                await legacy(buyer, "/mall-portal/order/paySuccess", params={"orderId": oid, "payType": 1})
            order = await api(buyer, MEMBER + "/orders/" + oid)
            if order["status"] == 1:
                await legacy(
                    staff,
                    "/mall-admin/order/update/delivery",
                    [{"orderId": int(oid), "deliveryCompany": "演示快递", "deliverySn": "TEST-" + oid}],
                )
            order = await api(buyer, MEMBER + "/orders/" + oid)
            assert order["status"] == 2
            logistics = await api(staff, ADMIN + f"/orders/{oid}/logistics")
            if not logistics["shipment_id"]:
                logistics = await api(
                    staff,
                    ADMIN + f"/orders/{oid}/logistics",
                    "PUT",
                    {
                        "expected_version": logistics["version"],
                        "carrier": "演示快递",
                        "tracking_no": "TEST-" + oid,
                    },
                    key("shipment"),
                )
            state["shipment_id"] = logistics["shipment_id"]
            save(state)
            with mysql() as db, db.cursor() as q:
                q.execute("SELECT delivery_time FROM oms_order WHERE id=%s", (oid,))
                shipped = (
                    q.fetchone()["delivery_time"].replace(tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(UTC)
                )
            until = shipped + timedelta(seconds=3)
            if datetime.now(UTC) < until:
                await asyncio.sleep((until - datetime.now(UTC)).total_seconds())
            for i in range(3):
                if any(n["location"] == f"演示节点{i + 1}" for n in logistics["nodes"]):
                    continue
                occurred = shipped + timedelta(seconds=i, microseconds=123456 + i * 111111)
                await api(
                    staff,
                    ADMIN + f"/shipments/{logistics['shipment_id']}/events",
                    "POST",
                    {
                        "expected_shipment_version": logistics["version"],
                        "occurred_at": occurred.isoformat(),
                        "location": f"演示节点{i + 1}",
                        "description": f"[TEST ONLY] 时间边界节点{i + 1}",
                    },
                    key(f"node:{i}"),
                    201,
                )
                logistics = await api(staff, ADMIN + f"/orders/{oid}/logistics")
            state["nodes"] = logistics["nodes"]
            save(state)
        case = "cancel-ui2" if next_cancel else "cancel"
        created = await action(case + "-order", parameters, True)
        state.setdefault("cancel_order_ids", [])
        for oid in (state.get("cancel_order_id"), created["result"]["business_id"]):
            if oid and oid not in state["cancel_order_ids"]:
                state["cancel_order_ids"].append(oid)
        state["cancel_order_id"] = created["result"]["business_id"]
        save(state)
        cancellation = await action(
            case + "-card", {"action_type": "CANCEL_ORDER", "order_id": state["cancel_order_id"]}
        )
        state["cancellation_card"] = {
            k: cancellation[k] for k in ("id", "state", "version", "expires_at", "quote")
        }
        save(state)
        snapshot(state, "prepared")
        print(
            json.dumps(
                {
                    k: state[k]
                    for k in (
                        "product_id",
                        "sku_id",
                        "conversation_id",
                        "timeline_order_id",
                        "shipment_id",
                        "cancel_order_id",
                        "cancellation_card",
                    )
                    if k in state
                },
                ensure_ascii=False,
                default=str,
            )
        )


async def verify_cancel_replays(state):
    from backup_data import pg_connection

    def facts():
        with mysql() as db, db.cursor() as q:
            q.execute("START TRANSACTION READ ONLY")
            q.execute("SELECT * FROM pms_sku_stock WHERE id=%s", (state["sku_id"],))
            stock = q.fetchone()
            q.execute(
                "SELECT * FROM oms_agent_operation WHERE member_id=22 AND action_type='CANCEL_ORDER' AND business_id IN (%s,%s) ORDER BY operation_no",
                tuple(state["cancel_order_ids"]),
            )
            operations = q.fetchall()
            result = {"stock": fingerprint([stock]), "operations": fingerprint(operations)}
            assert stock["stock"] == 7 and stock["lock_stock"] == 0
            assert len(operations) == 2 and all(o["state"] == "SUCCEEDED" for o in operations)
            db.rollback()
            return result

    before = facts()
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=60, trust_env=False) as buyer:
        await login(buyer, username="agent-lifecycle-6c72a0f5")
        for name, aid in state["actions"].items():
            if not name.endswith("-card"):
                continue
            current = await api(buyer, MEMBER + "/actions/" + aid)
            assert current["state"] == "SUCCEEDED" and current["result"]["order"]["status"] == 4
            with pg_connection(ROOT, True) as conn:
                rows = list(
                    conn.execute(
                        "SELECT idempotency_key FROM ag_request_dedupe WHERE actor_kind='MEMBER' AND actor_id=22 AND scope=%s",
                        ("confirmAction:" + aid,),
                    )
                )
            assert len(rows) == 1
            for _ in range(2):
                repeated = await api(
                    buyer,
                    MEMBER + f"/actions/{aid}/confirm",
                    "POST",
                    {"expected_version": 1},
                    rows[0]["idempotency_key"],
                    202,
                )
                assert repeated["state"] == "SUCCEEDED"
            response = await buyer.post(
                MEMBER + f"/actions/{aid}/confirm",
                json={"expected_version": current["version"]},
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert response.status_code == 409 and response.json()["code"] == "ACTION_STATE_CONFLICT"
    assert facts() == before
    state["cancellation_replay_verified"] = {
        "orders": state["cancel_order_ids"],
        "successful_operations": 2,
        "same_key_replays": 4,
        "fresh_key_terminal_rejections": 2,
        "stock": 7,
        "lock_stock": 0,
        "business_fingerprints_unchanged": True,
    }
    save(state)
    print(json.dumps(state["cancellation_replay_verified"]))


async def cleanup(state):
    async with (
        httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=60, trust_env=False) as buyer,
        httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=60, trust_env=False) as staff,
    ):
        await login(buyer, username="agent-lifecycle-6c72a0f5")
        await login(staff, True)
        for oid in state.get("cancel_order_ids", [state.get("cancel_order_id")]):
            if not oid:
                continue
            order = await api(buyer, MEMBER + "/orders/" + oid)
            if order["status"] == 0:
                await legacy(buyer, "/mall-portal/order/cancelUserOrder", params={"orderId": oid})
            assert (await api(buyer, MEMBER + "/orders/" + oid))["status"] == 4
        state["final_cancel_cards"] = {}
        for name, aid in state["actions"].items():
            if name.endswith("-card"):
                value = await api(buyer, MEMBER + "/actions/" + aid)
                state["final_cancel_cards"][name] = {k: value[k] for k in ("id", "state", "result")}
        product = await legacy(staff, "/mall-admin/product/updateInfo/" + state["product_id"], method="GET")
        assert product["productSn"] == state["product_sn"] and product["name"].startswith("[TEST ONLY]")
        await legacy(
            staff,
            "/mall-admin/product/update/publishStatus",
            params={"ids": state["product_id"], "publishStatus": 0},
        )
        await legacy(
            staff,
            "/mall-admin/product/update/deleteStatus",
            params={"ids": state["product_id"], "deleteStatus": 1},
        )
        current = await buyer.get(MEMBER + "/conversations/" + state["conversation_id"])
        if current.status_code == 200:
            await api(
                buyer,
                MEMBER + "/conversations/" + state["conversation_id"],
                "DELETE",
                {"expected_version": current.json()["data"]["conversation"]["version"]},
            )
        assert (await buyer.get(MEMBER + "/conversations/" + state["conversation_id"])).status_code == 404
        state["cleanup"] = {
            "conversation_deleted": True,
            "test_product_hidden": True,
            "cancel_orders_cancelled": True,
            "business_evidence_retained": True,
            "original_inventory_not_written": True,
            "real_financial_channels_called": False,
        }
        save(state)
        snapshot(state, "after_cleanup")
        print(json.dumps(state["cleanup"]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode", choices=["prepare", "cancel", "next-cancel", "snapshot", "verify-cancel", "cleanup"]
    )
    parser.add_argument("--name", default="current")
    args = parser.parse_args()
    state = (
        json.loads(STATE.read_text(encoding="utf-8"))
        if STATE.exists()
        else {
            "created_at": datetime.now(UTC).isoformat(),
            "product_sn": "UI-TIME-" + uuid4().hex[:10],
            "keys": {},
            "actions": {},
        }
    )
    save(state)
    try:
        if args.mode == "snapshot":
            print(json.dumps(snapshot(state, args.name), ensure_ascii=False, default=str))
        elif args.mode == "verify-cancel":
            asyncio.run(verify_cancel_replays(state))
        elif args.mode == "cleanup":
            asyncio.run(cleanup(state))
        else:
            asyncio.run(prepare(state, args.mode != "prepare", args.mode == "next-cancel"))
    except Exception as error:
        print("FIXTURE_FAILED type=" + type(error).__name__)
        raise SystemExit(1)
