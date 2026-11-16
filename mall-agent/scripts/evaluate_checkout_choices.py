"""Resumable real-model checkout revisions. Prepare only, no business confirm.

Seed/cleanup use authenticated demo APIs; SQL access is read-only. Existing
business/member/cart rows are fingerprinted and never edited by this fixture.
"""

import argparse
import asyncio
import hashlib
import json
import sys
import traceback
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
from expansion_client import MEMBER, api, login
from smoke_order_lifecycle import mysql
from smoke_stream_ui import baseline

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "deployment/.run/completion-checkout-choices-live-20261114.json"
sys.path.insert(0, str(ROOT / "deployment"))


def save(state):
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def key(state, name):
    state.setdefault("keys", {}).setdefault(name, str(uuid4()))
    save(state)
    return state["keys"][name]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def original(state=None):
    result = baseline()
    result.pop("model_calls")
    with mysql() as db, db.cursor() as q:
        q.execute("START TRANSACTION READ ONLY")
        queries = {
            "member19": ("SELECT * FROM ums_member WHERE id=19", ()),
            "stocks": ("SELECT * FROM pms_sku_stock WHERE id IN (239,240,241,242) ORDER BY id", ()),
            "member_orders": ("SELECT * FROM oms_order WHERE member_id=19 ORDER BY id", ()),
        }
        for name, (sql, args) in queries.items():
            q.execute(sql, args)
            result[name] = digest(q.fetchall())
        if state and state.get("original_cart_ids"):
            q.execute(
                "SELECT * FROM oms_cart_item WHERE id IN ("
                + ",".join(["%s"] * len(state["original_cart_ids"]))
                + ") ORDER BY id",
                state["original_cart_ids"],
            )
            result["original_cart_rows"] = digest(q.fetchall())
        db.rollback()
    return result


async def legacy(client, path, body=None, *, params=None, method="POST"):
    response = await client.request(method, path, json=body, params=params)
    value = response.json()
    assert response.status_code == 200 and value.get("code") == 200, (
        path,
        response.status_code,
        value.get("code"),
    )
    return value.get("data")


async def options(buyer, parameters):
    selection = {
        k: v for k, v in parameters.items() if k in {"action_type", "source", "items", "cart_item_ids"}
    }
    return await api(buyer, MEMBER + "/checkout-options", "POST", selection)


def direct(state, address=None, coupon=None, points=0):
    return {
        "action_type": "CREATE_ORDER",
        "source": "DIRECT",
        "items": [{"product_id": "45", "sku_id": "239", "quantity": 1}],
        "address_id": address or state["first_address"],
        "coupon_id": coupon,
        "use_points": points,
    }


def turns(state, case):
    addr1, addr2 = state["first_address"], state["second_address"]
    coupon1, coupon2 = [row["id"] for row in state["coupons"]]
    if case.startswith("DIRECT"):
        return [
            (
                f"我要OPPO Reno8鸢尾紫128G一台，直接购买。使用地址{addr1}、不用优惠券和积分。读取真实规格和本次结算选项，准备订单确认卡；只准备，不点击确认，不下单不付款。",
                direct(state),
            ),
            (
                f"刚才那款规格和数量不变，地址改用{addr2}，优惠券改用{coupon1}，使用100积分。重新查询结算选项并准备新订单卡，只准备，不确认不执行。",
                direct(state, addr2, coupon1, 100),
            ),
            (
                f"地址保持用地址{addr2}，优惠券改为{coupon2}，积分从100改成200。原来商品规格数量不变，重新准备订单确认卡，不确认不下单。",
                direct(state, addr2, coupon2, 200),
            ),
            (
                f"仍用地址{addr2}，最后不使用优惠券，不使用积分，原商品规格和数量不变。重新读取选项并准备订单卡，不确认不下单。",
                direct(state, addr2),
            ),
            ("旧卡不要使用，地址和优惠券暂时没决定，先向我澄清，不要准备新卡，不执行任何业务。", None),
        ]
    first, second = state["cart_rows"]

    def selected(ids, points=0):
        return {
            "action_type": "CREATE_ORDER",
            "source": "CART",
            "cart_item_ids": ids,
            "address_id": addr2,
            "coupon_id": None,
            "use_points": points,
        }

    return [
        (
            f"我要结算购物车条目{first['id']}，只选这一行。请读取本人购物车及本次结算选项，用地址{addr2}，不用优惠券和积分，准备订单卡。只准备，不确认不下单。",
            selected([first["id"]]),
        ),
        (
            f"旧卡不要用了，改选购物车里Reno8晴空蓝256G那行，只结算它。仍用地址{addr2}，不用券和积分。重新读取购物车和结算选项后准备订单卡，不确认不下单。",
            selected([second["id"]]),
        ),
        (
            f"仍结算购物车条目{second['id']}，请重新读购物车，按现在两件商品准备新的订单确认卡。地址{addr2}，不用券，使用100积分。不要确认或下单。",
            selected([second["id"]], 100),
        ),
        ("购物车到底买哪一行还没决定，旧卡不用了，先澄清，不要准备新卡，不下单。", None),
    ]


async def seed(buyer, staff, state):
    if "original_cart_ids" not in state:
        with mysql() as db, db.cursor() as q:
            q.execute("START TRANSACTION READ ONLY")
            q.execute("SELECT id FROM oms_cart_item WHERE member_id=19 ORDER BY id")
            state["original_cart_ids"] = [r["id"] for r in q.fetchall()]
            db.rollback()
        state["baseline"] = original(state)
        save(state)
    addresses = await legacy(buyer, "/mall-portal/member/address/list", method="GET")
    if not state.get("first_address"):
        assert len(addresses) == 1
        state["first_address"] = str(addresses[0]["id"])
        state["original_address_ids"] = [str(a["id"]) for a in addresses]
        state["original_addresses_hash"] = digest(addresses)
        save(state)
    if not state.get("second_address"):
        label = "[TEST ONLY] 多轮结算地址 " + state["tag"]
        existing = [a for a in addresses if a.get("detailAddress") == label]
        if not existing:
            await legacy(
                buyer,
                "/mall-portal/member/address/add",
                {
                    "name": "演示改选收货人",
                    "phoneNumber": "13800000000",
                    "defaultStatus": 0,
                    "postCode": "000000",
                    "province": "演示省",
                    "city": "演示市",
                    "region": "演示区",
                    "detailAddress": label,
                },
            )
            addresses = await legacy(buyer, "/mall-portal/member/address/list", method="GET")
            existing = [a for a in addresses if a.get("detailAddress") == label]
        assert len(existing) == 1
        state["second_address"] = str(existing[0]["id"])
        save(state)
    state.setdefault("coupons", [])
    for n, amount in enumerate([10, 20]):
        if len(state["coupons"]) > n:
            continue
        name = f"[TEST ONLY] 多轮结算{state['tag']}-{n}"
        page = await legacy(
            staff,
            "/mall-admin/coupon/list",
            params={"name": name, "pageSize": 20, "pageNum": 1},
            method="GET",
        )
        existing = [a for a in page["list"] if a["name"] == name]
        if not existing:
            now = datetime.now(UTC)
            body = {
                "type": 0,
                "name": name,
                "platform": 0,
                "amount": str(amount),
                "perLimit": 1,
                "minPoint": "0.00",
                "startTime": int((now - timedelta(hours=1)).timestamp() * 1000),
                "endTime": int((now + timedelta(days=2)).timestamp() * 1000),
                "enableTime": int((now - timedelta(hours=1)).timestamp() * 1000),
                "useType": 2,
                "note": "仅演示验收，清理后过期；不执行订单",
                "publishCount": 1,
                "memberLevel": 0,
                "productRelationList": [
                    {"productId": 45, "productName": "OPPO Reno8", "productSn": "TEST-CHOICE"}
                ],
                "productCategoryRelationList": [],
            }
            await legacy(staff, "/mall-admin/coupon/create", body)
            page = await legacy(
                staff,
                "/mall-admin/coupon/list",
                params={"name": name, "pageSize": 20, "pageNum": 1},
                method="GET",
            )
            existing = [a for a in page["list"] if a["name"] == name]
        assert len(existing) == 1
        coupon = existing[0]
        histories = await legacy(buyer, "/mall-portal/member/coupon/listHistory", method="GET")
        if not any(str(h["couponId"]) == str(coupon["id"]) for h in histories):
            await legacy(buyer, "/mall-portal/member/coupon/add/" + str(coupon["id"]))
        state["coupons"].append({"id": str(coupon["id"]), "name": name, "amount": str(amount)})
        save(state)
    state.setdefault("cart_rows", [])
    for sku in [240, 242]:
        if any(row["productSkuId"] == sku for row in state["cart_rows"]):
            continue
        cart = await legacy(buyer, "/mall-portal/cart/list", method="GET")
        found = [r for r in cart if r["productSkuId"] == sku]
        assert len(found) <= 1
        if not found:
            await legacy(
                buyer, "/mall-portal/cart/add", {"productId": 45, "productSkuId": sku, "quantity": 1}
            )
            cart = await legacy(buyer, "/mall-portal/cart/list", method="GET")
            found = [r for r in cart if r["productSkuId"] == sku]
        assert len(found) == 1 and found[0]["id"] not in state["original_cart_ids"]
        state["cart_rows"].append({"id": str(found[0]["id"]), "productSkuId": sku})
        save(state)
    choices = await options(buyer, direct(state))
    assert choices["available_points"] == 5000
    assert {r["id"] for r in state["coupons"]} <= {r["id"] for r in choices["coupons"] if r["available"]}
    assert {state["first_address"], state["second_address"]} <= {r["id"] for r in choices["addresses"]}
    assert original(state) == state["baseline"]
    state["seeded"] = True
    save(state)


async def run_turn(buyer, state, args):
    from backup_data import pg_connection

    assert state.get("seeded") and not state.get("cleaned")
    case = state["cases"].setdefault(args.case, {"turns": []})
    if not case.get("conversation_id"):
        result = await api(
            buyer,
            MEMBER + "/conversations",
            "POST",
            {"title": "[TEST ONLY] 真实结算改选 " + args.case, "entry_context": {"kind": "GENERAL"}},
            key(state, args.case + ":create"),
            201,
        )
        case["conversation_id"] = result["id"]
        save(state)
    while len(case["turns"]) <= args.turn:
        question, expected = turns(state, args.case)[len(case["turns"])]
        case["turns"].append({"question": question, "expected": expected, "key": str(uuid4())})
    assert all(t.get("review") == "PASSED" for t in case["turns"][: args.turn])
    turn = case["turns"][args.turn]
    if turn.get("review") == "PASSED":
        print("PASS existing turn; no new supplier request")
        return
    cid = case["conversation_id"]
    if (
        args.case == "CART_FIXED"
        and args.turn == 0
        and not case.get("quantity_reset")
        and not turn.get("run_id")
    ):
        await legacy(
            buyer,
            "/mall-portal/cart/update/quantity",
            params={"id": state["cart_rows"][1]["id"], "quantity": 1},
            method="GET",
        )
        case["quantity_reset"] = True
        save(state)
    if args.case.startswith("CART") and args.turn == 2 and not case.get("quantity_changed"):
        await legacy(
            buyer,
            "/mall-portal/cart/update/quantity",
            params={"id": state["cart_rows"][1]["id"], "quantity": 2},
            method="GET",
        )
        case["quantity_changed"] = True
        save(state)
    if not turn.get("run_id"):
        with pg_connection(ROOT, True) as conn:
            used = conn.execute(
                "SELECT coalesce(sum(CASE WHEN state='SETTLED' THEN settled_units ELSE reserved_units END),0) n FROM ag_quota_reservation WHERE bucket_key='model-requests:DIALOG' AND state<>'RELEASED' AND period_start=date_trunc('day',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
            ).fetchone()["n"]
            assert used <= 180, "Keep existing daily cap; do not clear or raise usage"
        save(state)
        sent = await api(
            buyer,
            MEMBER + f"/conversations/{cid}/messages",
            "POST",
            {"client_message_id": turn["key"], "text": turn["question"], "attachment_ids": []},
            turn["key"],
            202,
        )
        turn.update(run_id=sent["run"]["id"], started_at=datetime.now(UTC).isoformat())
        save(state)
    async with asyncio.timeout(240):
        while True:
            current = await api(buyer, MEMBER + "/runs/" + turn["run_id"])
            if current["state"] not in {"QUEUED", "RUNNING", "STOP_REQUESTED"}:
                break
            await asyncio.sleep(2)
    snapshot = await api(buyer, MEMBER + "/conversations/" + cid)
    turn.update(run=current, snapshot=snapshot, finished_at=datetime.now(UTC).isoformat())
    with pg_connection(ROOT, True) as conn:
        turn["calls"] = [
            dict(r)
            for r in conn.execute(
                "SELECT capability,status,run_id,job_id FROM ag_model_call WHERE run_id=%s ORDER BY created_at,id",
                (turn["run_id"],),
            )
        ]
        turn["dispatches"] = conn.execute(
            "SELECT count(*) n FROM ag_action_dispatch d JOIN ag_action a ON a.id=d.action_id WHERE a.conversation_id=%s",
            (cid,),
        ).fetchone()["n"]
    save(state)
    assert current["state"] == "COMPLETED", "Preserve failed run; never silently resend"
    assert turn["dispatches"] == 0
    pending = snapshot["pending_actions"]
    if turn["expected"] is None:
        assert not pending, "Unresolved latest choices must clarify"
    else:
        assert len(pending) == 1 and pending[0]["parameters"] == turn["expected"], (
            "Card must match exact member choices"
        )
        action = pending[0]
        turn["action"] = action
        for line in action["quote"]["lines"]:
            assert "Reno8" in line["name"], "Confirmation requires the current product name"
            attributes = json.loads(line["specification"])
            assert isinstance(attributes, list) and attributes, (
                "Confirmation requires current SKU specifications"
            )
        assert sum(Decimal(row["line_payable"]) for row in action["quote"]["lines"]) == Decimal(
            action["quote"]["pricing"]["payable"]
        )
        assert (
            Decimal(action["quote"]["pricing"]["points_discount"])
            == Decimal(turn["expected"]["use_points"]) / 100
        )
        coupon = turn["expected"]["coupon_id"]
        discount = next((Decimal(c["amount"]) for c in state["coupons"] if c["id"] == coupon), Decimal(0))
        assert Decimal(action["quote"]["pricing"]["coupon_discount"]) == discount
        if args.case.startswith("CART") and args.turn == 2:
            assert action["quote"]["lines"][0]["quantity"] == 2
    for prior in case["turns"][: args.turn]:
        if prior.get("action"):
            old = await api(buyer, MEMBER + "/actions/" + prior["action"]["id"])
            assert old["state"] in {"STALE", "EXPIRED"} and old["parameters"] == prior["action"]["parameters"]
    assert original(state) == state["baseline"]
    turn["review"] = "PASSED"
    save(state)
    print(
        json.dumps(
            {
                "case": args.case,
                "turn": args.turn,
                "review": "PASSED",
                "model_calls": len(turn["calls"]),
                "pending": len(pending),
            },
            ensure_ascii=False,
        )
    )


async def cleanup(buyer, staff, state):
    for case in state["cases"].values():
        if case.get("deleted"):
            continue
        cid = case["conversation_id"]
        snapshot = await api(buyer, MEMBER + "/conversations/" + cid)
        assert not snapshot["active_run"]
        await api(
            buyer,
            MEMBER + "/conversations/" + cid,
            "DELETE",
            {"expected_version": snapshot["conversation"]["version"]},
        )
        assert (await buyer.get(MEMBER + "/conversations/" + cid)).status_code == 404
        case["deleted"] = True
        save(state)
    cart = await legacy(buyer, "/mall-portal/cart/list", method="GET")
    own = {r["id"] for r in state.get("cart_rows", [])}
    selected = [str(r["id"]) for r in cart if str(r["id"]) in own]
    if selected:
        await legacy(buyer, "/mall-portal/cart/delete", params=[("ids", v) for v in selected])
    addresses = await legacy(buyer, "/mall-portal/member/address/list", method="GET")
    if any(str(a["id"]) == state.get("second_address") for a in addresses):
        await legacy(buyer, "/mall-portal/member/address/delete/" + state["second_address"])
    addresses = await legacy(buyer, "/mall-portal/member/address/list", method="GET")
    assert digest(addresses) == state["original_addresses_hash"]
    for coupon in state.get("coupons", []):
        value = await legacy(staff, "/mall-admin/coupon/" + coupon["id"], method="GET")
        assert value["name"] == coupon["name"] and value["useCount"] == 0
        value.update(endTime=int((datetime.now(UTC) - timedelta(seconds=2)).timestamp() * 1000), count=0)
        await legacy(staff, "/mall-admin/coupon/update/" + coupon["id"], value)
    assert original(state) == state["baseline"]
    assert not (await options(buyer, direct(state)))["coupons"]
    state["cleaned"] = True
    state["cleaned_at"] = datetime.now(UTC).isoformat()
    save(state)
    print(
        "PASS conversations deleted, owned cart rows removed, test address deleted, new coupons expired, original facts unchanged"
    )


async def main(args):
    state = (
        json.loads(STATE.read_text(encoding="utf-8"))
        if STATE.exists()
        else {
            "created_at": datetime.now(UTC).isoformat(),
            "tag": uuid4().hex[:8],
            "cases": {},
            "keys": {},
            "confirm_requests": 0,
        }
    )
    save(state)
    async with (
        httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=60, trust_env=False) as buyer,
        httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=60, trust_env=False) as staff,
    ):
        await login(buyer, username="agent-pricing-68455e784b")
        await login(staff, True)
        if args.mode == "seed":
            await seed(buyer, staff, state)
            print(
                json.dumps(
                    {
                        "seeded": True,
                        "addresses": [state["first_address"], state["second_address"]],
                        "coupon_ids": [c["id"] for c in state["coupons"]],
                        "cart_rows": state["cart_rows"],
                    },
                    ensure_ascii=False,
                )
            )
        elif args.mode == "run":
            await run_turn(buyer, state, args)
        else:
            await cleanup(buyer, staff, state)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["seed", "run", "cleanup"])
    parser.add_argument("--case", default="DIRECT", choices=["DIRECT", "DIRECT_FIXED", "CART", "CART_FIXED"])
    parser.add_argument("--turn", type=int, choices=range(5), default=0)
    args = parser.parse_args()
    try:
        asyncio.run(main(args))
    except Exception as error:
        print("ACCEPTANCE_STOP type=" + type(error).__name__)
        print(
            "LOCATION",
            [(Path(f.filename).name, f.lineno) for f in traceback.extract_tb(error.__traceback__)[-3:]],
        )
        raise SystemExit(1) from None
