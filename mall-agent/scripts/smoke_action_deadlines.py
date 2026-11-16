"""Four-card live acceptance using authenticated APIs and scoped historical fixtures.

No supplier calls or payment/refund requests. Only a new demo seed order is
executed and later cancelled. Existing business rows are read-only. Direct PG
writes are restricted to new labelled cards' deadlines and one legacy snapshot.
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from expansion_client import ADMIN, MEMBER, api, login
from smoke_order_lifecycle import mysql
from smoke_stream_ui import baseline

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "deployment/.run/completion-action-deadlines-live-20261114.json"
KINDS = ["ADD_CART", "CREATE_ORDER", "CANCEL_ORDER", "CREATE_RETURN"]
sys.path.insert(0, str(ROOT / "deployment"))


def save(state):
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def stocks():
    with mysql() as db, db.cursor() as q:
        q.execute("START TRANSACTION READ ONLY")
        q.execute("SELECT id,stock,lock_stock FROM pms_sku_stock WHERE id IN (239,240,241,242) ORDER BY id")
        result = q.fetchall()
        db.rollback()
        return result


def key(state, name):
    state.setdefault("keys", {}).setdefault(name, str(uuid4()))
    save(state)
    return state["keys"][name]


async def snapshot(buyer, case):
    return await api(buyer, MEMBER + "/conversations/" + case["conversation_id"])


async def card(buyer, state, name, parameters):
    case = state.setdefault("cases", {}).setdefault(name, {})
    if not case.get("conversation_id"):
        title = "[TEST ONLY] 卡片安全 " + name
        result = await api(
            buyer,
            MEMBER + "/conversations",
            "POST",
            {"title": title, "entry_context": {"kind": "GENERAL"}},
            key(state, name + ":conversation"),
            201,
        )
        case.update(conversation_id=result["id"], title=title)
        save(state)
    if not case.get("action_id"):
        current = await snapshot(buyer, case)
        result = await api(
            buyer,
            MEMBER + f"/conversations/{case['conversation_id']}/actions",
            "POST",
            {"expected_conversation_version": current["conversation"]["version"], "parameters": parameters},
            key(state, name + ":prepare"),
            201,
        )
        case.update(action_id=result["id"], parameters=parameters)
        save(state)
    return case


async def action(buyer, case):
    return await api(buyer, MEMBER + "/actions/" + case["action_id"])


async def confirm(buyer, state, name):
    case = state["cases"][name]
    current = await action(buyer, case)
    if current["state"] == "PENDING_CONFIRMATION":
        current = await api(
            buyer,
            MEMBER + f"/actions/{current['id']}/confirm",
            "POST",
            {"expected_version": current["version"]},
            key(state, name + ":confirm"),
            202,
        )
    for _ in range(90):
        if current["state"] not in {"EXECUTING", "UNKNOWN"}:
            break
        await asyncio.sleep(1)
        current = await action(buyer, case)
    assert current["state"] == "SUCCEEDED", "Demo operation did not complete; preserve its original ID"
    return current


async def self_profile(staff):
    me = await api(staff, ADMIN + "/me")
    page = await api(staff, ADMIN + "/staff?limit=100")
    return next(row for row in page["items"] if row["id"] == me["id"])


async def run(args):
    from backup_data import pg_connection

    state = (
        json.loads(STATE.read_text(encoding="utf-8"))
        if STATE.exists()
        else {"created_at": datetime.now(UTC).isoformat(), "cases": {}, "keys": {}, "state": "preparing"}
    )
    if state.get("state") == "cleaned":
        print("PASS previously cleaned fixture; no new business executed")
        return
    if "baseline" not in state:
        state["baseline"], state["stocks_before"] = baseline(), stocks()
        save(state)
    async with (
        httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=60, trust_env=False) as buyer,
        httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=60, trust_env=False) as staff,
    ):
        await login(buyer, username="agent-lifecycle-6c72a0f5")
        await login(staff, True)
        if args.mode == "prepare":
            order_parameters = {
                "action_type": "CREATE_ORDER",
                "source": "DIRECT",
                "items": [{"product_id": "45", "sku_id": "240", "quantity": 1}],
                "address_id": "20",
                "coupon_id": None,
                "use_points": 0,
            }
            await card(buyer, state, "seed", order_parameters)
            if not state.get("seed_order_id"):
                result = await confirm(buyer, state, "seed")
                state["seed_order_id"] = result["result"]["business_id"]
                save(state)
            eligibility = await api(buyer, MEMBER + "/orders/116/items/156/return-eligibility")
            assert eligibility["eligible"] and eligibility["remaining_quantity"] >= 1
            parameters = {
                "ADD_CART": {"action_type": "ADD_CART", "items": order_parameters["items"]},
                "CREATE_ORDER": order_parameters,
                "CANCEL_ORDER": {"action_type": "CANCEL_ORDER", "order_id": state["seed_order_id"]},
                "CREATE_RETURN": {
                    "action_type": "CREATE_RETURN",
                    "order_id": "116",
                    "order_item_id": "156",
                    "quantity": 1,
                    "reason_code": eligibility["reason_codes"][0]["code"],
                    "description": "[TEST ONLY] 确认卡失效验收，不提交退货",
                    "attachment_ids": [],
                },
            }
            for kind in KINDS:
                for transition in ["expiry", "cancel", "handoff"]:
                    name = kind + ":" + transition
                    case = await card(buyer, state, name, parameters[kind])
                    if transition == "cancel" and not case.get("cancelled"):
                        current = await action(buyer, case)
                        result = await api(
                            buyer,
                            MEMBER + f"/actions/{current['id']}/cancel",
                            "POST",
                            {"expected_version": current["version"]},
                            key(state, name + ":cancel"),
                        )
                        assert result["state"] == "CANCELLED"
                        case["cancelled"] = True
                        save(state)
            case = await card(buyer, state, "CANCEL_ORDER:legacy", parameters["CANCEL_ORDER"])
            if not case.get("historical_fixture"):
                with pg_connection(ROOT) as conn:
                    row = conn.execute(
                        "SELECT member_id,quote_snapshot FROM ag_action WHERE id=%s AND conversation_id=%s AND state='PENDING_CONFIRMATION' FOR UPDATE",
                        (case["action_id"], case["conversation_id"]),
                    ).fetchone()
                    assert row and row["member_id"] == 22
                    quote = row["quote_snapshot"]
                    quote["rule_version"] = "CANCEL_PENDING_V1"
                    quote.pop("cancellation", None)
                    from psycopg.types.json import Jsonb

                    conn.execute(
                        "UPDATE ag_action SET quote_snapshot=%s WHERE id=%s AND conversation_id=%s AND member_id=22",
                        (Jsonb(quote), case["action_id"], case["conversation_id"]),
                    )
                case["historical_fixture"] = (
                    "Only the new card projection models V1; original Java quote is unchanged"
                )
                save(state)
            state["state"] = "prepared"
        elif args.mode == "expire":
            cases = [state["cases"][kind + ":expiry"] for kind in KINDS]
            ids = [case["action_id"] for case in cases]
            with pg_connection(ROOT) as conn:
                rows = conn.execute(
                    "SELECT id,conversation_id,member_id FROM ag_action WHERE id=ANY(%s::uuid[]) AND state='PENDING_CONFIRMATION' FOR UPDATE",
                    (ids,),
                ).fetchall()
                assert len(rows) == 4 and all(row["member_id"] == 22 for row in rows)
                assert {str(row["conversation_id"]) for row in rows} == {
                    case["conversation_id"] for case in cases
                }
                changed = conn.execute(
                    "UPDATE ag_action SET expires_at=LEAST(expires_at,clock_timestamp()+interval '6 seconds') WHERE id=ANY(%s::uuid[]) AND member_id=22 RETURNING id,expires_at",
                    (ids,),
                ).fetchall()
                state["deadline_fixture"] = [dict(row) for row in changed]
        elif args.mode == "handoff":
            # Earlier batch cards may have expired during page verification.
            # Prepare a fresh valid card rather than extending an old deadline.
            name = args.kind + ":takeover"
            case = await card(buyer, state, name, state["cases"][args.kind + ":handoff"]["parameters"])
            before = await action(buyer, case)
            assert before["state"] == "PENDING_CONFIRMATION"
            case["before_handoff"] = {k: before[k] for k in ("id", "version", "state", "expires_at")}
            case["before_handoff_checked_at"] = datetime.now(UTC).isoformat()
            save(state)
            current = await snapshot(buyer, case)
            if not case.get("handoff_id"):
                h = await api(
                    buyer,
                    MEMBER + f"/conversations/{case['conversation_id']}/handoff",
                    "POST",
                    {
                        "expected_conversation_version": current["conversation"]["version"],
                        "reason": "[TEST ONLY] 四卡接管验收",
                    },
                    key(state, name + ":handoff"),
                    201,
                )
                case["handoff_id"] = h["id"]
                save(state)
            else:
                h = (await snapshot(buyer, case))["handoff"]
            profile = await self_profile(staff)
            state.setdefault("staff_presence_before", profile["presence"])
            save(state)
            await api(
                staff,
                ADMIN + "/presence",
                "PUT",
                {"expected_version": profile["version"], "presence": "ONLINE"},
            )
            if h["state"] == "WAITING":
                h = await api(
                    staff,
                    ADMIN + f"/handoffs/{h['id']}/claim",
                    "POST",
                    {"expected_version": h["version"]},
                    key(state, name + ":claim"),
                )
            case["claimed"] = h
            case["claimed_snapshot"] = await snapshot(buyer, case)
            assert case["claimed_snapshot"]["conversation"]["state"] == "HUMAN_ACTIVE"
            assert (await action(buyer, case))["state"] == "STALE"
        elif args.mode == "close":
            case = state["cases"].get(args.kind + ":takeover", state["cases"][args.kind + ":handoff"])
            h = (await snapshot(buyer, case))["handoff"]
            if h["state"] == "CLAIMED":
                case["closed"] = await api(
                    staff,
                    ADMIN + f"/handoffs/{h['id']}/close",
                    "POST",
                    {"expected_version": h["version"], "reason": "[TEST ONLY] 页面验收后关闭"},
                )
        elif args.mode in {"legacy", "legacy-reject"}:
            case = await card(
                buyer,
                state,
                "CANCEL_ORDER:legacy-api" if args.mode == "legacy-reject" else "CANCEL_ORDER:legacy-current",
                state["cases"]["CANCEL_ORDER:legacy"]["parameters"],
            )
            before = await action(buyer, case)
            assert before["state"] == "PENDING_CONFIRMATION"
            case["before_legacy_fixture"] = {k: before[k] for k in ("id", "version", "state", "expires_at")}
            with pg_connection(ROOT) as conn:
                row = conn.execute(
                    "SELECT quote_snapshot FROM ag_action WHERE id=%s AND conversation_id=%s AND member_id=22 AND state='PENDING_CONFIRMATION' FOR UPDATE",
                    (case["action_id"], case["conversation_id"]),
                ).fetchone()
                assert row
                quote = row["quote_snapshot"]
                quote["rule_version"] = "CANCEL_PENDING_V1"
                quote.pop("cancellation", None)
                from psycopg.types.json import Jsonb

                conn.execute(
                    "UPDATE ag_action SET quote_snapshot=%s WHERE id=%s AND conversation_id=%s AND member_id=22",
                    (Jsonb(quote), case["action_id"], case["conversation_id"]),
                )
            case["historical_fixture"] = True
            save(state)
            if args.mode == "legacy-reject":
                current = await action(buyer, case)
                assert current["state"] == "PENDING_CONFIRMATION"
                response = await buyer.post(
                    MEMBER + f"/actions/{current['id']}/confirm",
                    headers={"Idempotency-Key": key(state, "legacy-api:reject")},
                    json={"expected_version": current["version"]},
                )
                data = response.json()
                assert response.status_code == 409 and data.get("code") == "ACTION_STATE_CONFLICT"
                assert data.get("message") == "此旧确认卡缺少可信订单号或金额，请重新读取订单并准备取消卡"
                state["legacy_before_expiry_rejection"] = {
                    "http": response.status_code,
                    "code": data["code"],
                    "message": data["message"],
                    "checked_at": datetime.now(UTC).isoformat(),
                    "expires_at": current["expires_at"],
                    "state": current["state"],
                }
        elif args.mode == "verify":
            state["rejections"] = {}
            for name, case in state["cases"].items():
                if name == "seed" or name == "cleanup-cancel":
                    continue
                current = await action(buyer, case)
                response = await buyer.post(
                    MEMBER + f"/actions/{current['id']}/confirm",
                    headers={"Idempotency-Key": key(state, name + ":reject")},
                    json={"expected_version": current["version"]},
                )
                data = response.json()
                assert response.status_code == 409 and data.get("code") in {
                    "ACTION_STATE_CONFLICT",
                    "VERSION_CONFLICT",
                }
                state["rejections"][name] = {
                    "http": response.status_code,
                    "code": data["code"],
                    "state": current["state"],
                }
                save(state)
            with pg_connection(ROOT, True) as conn:
                ids = [
                    case["action_id"]
                    for name, case in state["cases"].items()
                    if name not in {"seed", "cleanup-cancel"}
                ]
                dispatches = conn.execute(
                    "SELECT count(*) n FROM ag_action_dispatch WHERE action_id=ANY(%s::uuid[])", (ids,)
                ).fetchone()["n"]
                audits = conn.execute(
                    "SELECT count(*) n FROM ag_audit_log WHERE operation='confirmAction' AND resource_id=ANY(%s)",
                    (ids,),
                ).fetchone()["n"]
                assert dispatches == 0 and audits == 0
                state["invalid_card_dispatches"], state["invalid_card_confirm_audits"] = dispatches, audits
            current = baseline()
            assert current == state["baseline"]
            state["baseline_unchanged_before_cleanup"] = True
        elif args.mode == "cleanup":
            order = await api(buyer, MEMBER + "/orders/" + state["seed_order_id"])
            if order["status"] == 0:
                await card(
                    buyer,
                    state,
                    "cleanup-cancel",
                    {"action_type": "CANCEL_ORDER", "order_id": state["seed_order_id"]},
                )
                await confirm(buyer, state, "cleanup-cancel")
            assert (await api(buyer, MEMBER + "/orders/" + state["seed_order_id"]))["status"] == 4
            for case in state["cases"].values():
                response = await buyer.get(MEMBER + "/conversations/" + case["conversation_id"])
                if response.status_code == 404:
                    continue
                snap = response.json()["data"]
                h = snap.get("handoff")
                if h and h["state"] == "CLAIMED":
                    await api(
                        staff,
                        ADMIN + f"/handoffs/{h['id']}/close",
                        "POST",
                        {"expected_version": h["version"], "reason": "[TEST ONLY] 清理验收会话"},
                    )
                    snap = await snapshot(buyer, case)
                await api(
                    buyer,
                    MEMBER + "/conversations/" + case["conversation_id"],
                    "DELETE",
                    {"expected_version": snap["conversation"]["version"]},
                )
                assert (
                    await buyer.get(MEMBER + "/conversations/" + case["conversation_id"])
                ).status_code == 404
            if "staff_presence_before" in state:
                profile = await self_profile(staff)
                await api(
                    staff,
                    ADMIN + "/presence",
                    "PUT",
                    {"expected_version": profile["version"], "presence": state["staff_presence_before"]},
                )
            state["stocks_after"] = stocks()
            assert state["stocks_after"] == state["stocks_before"]
            assert baseline() == state["baseline"]
            state["state"] = "cleaned"
            state["cleaned_at"] = datetime.now(UTC).isoformat()
        save(state)
        print(
            json.dumps(
                {
                    "mode": args.mode,
                    "state": state["state"],
                    "cases": len(state["cases"]),
                    "kind": args.kind,
                    "invalid_dispatches": state.get("invalid_card_dispatches"),
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode",
        choices=["prepare", "expire", "handoff", "close", "legacy", "legacy-reject", "verify", "cleanup"],
    )
    parser.add_argument("--kind", choices=KINDS)
    args = parser.parse_args()
    if args.mode in {"handoff", "close"} and args.kind is None:
        parser.error("--kind is required for a handoff transition")
    try:
        asyncio.run(run(args))
    except Exception as error:
        print("ACCEPTANCE_FAILED type=" + type(error).__name__)
        raise SystemExit(1)
