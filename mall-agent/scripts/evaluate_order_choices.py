"""Real two-order selection and ambiguity; only labelled seed/cleanup execute.

Model turns prepare cancellation cards but never confirm. All business writes
use authenticated demo APIs. SQL is read-only, including fixture fingerprints.
"""

import argparse
import asyncio
import hashlib
import json
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from expansion_client import MEMBER, api, login
from smoke_action_deadlines import stocks
from smoke_order_lifecycle import mysql
from smoke_stream_ui import baseline

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "deployment/.run/completion-order-choices-live-20261114.json"
sys.path.insert(0, str(ROOT / "deployment"))


def save(state):
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def key(state, name):
    state.setdefault("keys", {}).setdefault(name, str(uuid4()))
    save(state)
    return state["keys"][name]


def original():
    result = baseline()
    result.pop("model_calls")
    return result


def fingerprint_rows(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()


def seeded_order_facts(state):
    with mysql() as db, db.cursor() as q:
        q.execute("START TRANSACTION READ ONLY")
        ids = [row["id"] for row in state["orders"]]
        assert len(ids) == 2 and ids[0] != ids[1]
        q.execute("SELECT * FROM oms_order WHERE id IN (%s,%s) ORDER BY id", ids)
        rows = q.fetchall()
        assert len(rows) == 2 and all(row["member_id"] == 22 for row in rows)
        result = {"orders": fingerprint_rows(rows)}
        q.execute("SELECT * FROM oms_order_item WHERE order_id IN (%s,%s) ORDER BY id", ids)
        result["items"] = fingerprint_rows(q.fetchall())
        db.rollback()
        return result


async def new_conversation(client, state, name, entry=None):
    case = state.setdefault("conversations", {}).setdefault(name, {})
    if not case.get("id"):
        result = await api(
            client,
            MEMBER + "/conversations",
            "POST",
            {"title": "[TEST ONLY] 两订单目标 " + name, "entry_context": entry or {"kind": "GENERAL"}},
            key(state, name + ":create"),
            201,
        )
        case["id"] = result["id"]
        save(state)
    return case["id"]


async def explicit_operation(client, state, name, params):
    cid = await new_conversation(client, state, name)
    case = state["conversations"][name]
    if not case.get("action_id"):
        snapshot = await api(client, MEMBER + "/conversations/" + cid)
        result = await api(
            client,
            MEMBER + f"/conversations/{cid}/actions",
            "POST",
            {"expected_conversation_version": snapshot["conversation"]["version"], "parameters": params},
            key(state, name + ":prepare"),
            201,
        )
        case["action_id"] = result["id"]
        save(state)
    current = await api(client, MEMBER + "/actions/" + case["action_id"])
    if current["state"] == "PENDING_CONFIRMATION":
        current = await api(
            client,
            MEMBER + f"/actions/{current['id']}/confirm",
            "POST",
            {"expected_version": current["version"]},
            key(state, name + ":confirm"),
            202,
        )
    for _ in range(90):
        if current["state"] not in {"UNKNOWN", "EXECUTING"}:
            break
        await asyncio.sleep(1)
        current = await api(client, MEMBER + "/actions/" + case["action_id"])
    assert current["state"] == "SUCCEEDED", "Keep original operation; do not retry execution"
    return current


async def seed(client, state):
    from backup_data import pg_connection

    if "baseline" not in state:
        state["baseline"], state["stocks_before"] = original(), stocks()
        with pg_connection(ROOT, True) as conn:
            calls = [dict(row) for row in conn.execute("SELECT * FROM ag_model_call ORDER BY id")]
            state["historical_calls"] = {
                "count": len(calls),
                "sha256": fingerprint_rows(calls),
                "ids": [str(row["id"]) for row in calls],
            }
        save(state)
    state.setdefault("orders", [])
    for i, (sku, quantity) in enumerate([("240", 1), ("242", 2)]):
        if len(state["orders"]) > i:
            continue
        current = await explicit_operation(
            client,
            state,
            "seed" + str(i),
            {
                "action_type": "CREATE_ORDER",
                "source": "DIRECT",
                "items": [{"product_id": "45", "sku_id": sku, "quantity": quantity}],
                "address_id": "20",
                "coupon_id": None,
                "use_points": 0,
            },
        )
        order = await api(client, MEMBER + "/orders/" + current["result"]["business_id"])
        assert order["status"] == 0
        state["orders"].append(
            {k: order[k] for k in ["id", "order_sn", "pay_amount"]} | {"sku_id": sku, "quantity": quantity}
        )
        save(state)
    page = await api(client, MEMBER + "/orders?status=0&limit=20")
    assert not page["has_more"] and {row["id"] for row in page["items"]} == {
        row["id"] for row in state["orders"]
    }, "The other-order scenario requires exactly these two pending orders"
    state["seeded_orders_fingerprint"] = seeded_order_facts(state)
    assert original() == state["baseline"]
    state["seeded"] = True
    save(state)
    print(json.dumps({"seeded": True, "orders": state["orders"]}, ensure_ascii=False))


def turn_inputs(state, case):
    first, second = state["orders"]
    if case.startswith("ENTRY"):
        return [
            ("请重新读取这笔订单并准备取消确认卡。这里只准备，不点击确认，不执行取消或付款。", first["id"]),
            ("订单目标还没决定了，撤回这次选择，先澄清，不要准备新卡。", None),
        ]
    return [
        (
            f"读取本人完整订单号{first['order_sn']}，内部编号{first['id']}，核对待付款后准备取消确认卡。只准备，不点击确认，不执行取消或付款。",
            first["id"],
        ),
        (
            "刚才那笔不再选，改为另一笔未付款订单。先读取本人未付款订单列表和目标详情，再准备新的取消确认卡。只准备，不点击确认，不执行取消或付款。",
            second["id"],
        ),
        (
            "再改为鸢尾紫256G那笔订单。重新读取本人订单列表与这笔详情，核对后准备取消卡；只准备，不确认不执行。",
            first["id"],
        ),
        ("这两笔我还没决定取消哪笔，先澄清，不要准备新卡，不执行任何取消。", None),
        (
            f"现在明确选完整订单号{second['order_sn']}，订单编号{second['id']}。重新核对并准备取消卡，不确认、不执行取消。",
            second["id"],
        ),
        ("刚才的订单先不要取消了，撤回本次选择，先不办理。不要准备取消卡。", None),
        ("取消刚才这笔，先准备卡不执行；如果目标不明确请先问我。", None),
    ]


async def model_turn(client, state, args):
    from backup_data import pg_connection

    assert state.get("seeded") and not state.get("cleaned")
    entry = {"kind": "ORDER", "order_id": state["orders"][0]["id"]} if args.case.startswith("ENTRY") else None
    cid = await new_conversation(client, state, args.case, entry)
    case = state["conversations"][args.case]
    turns = case.setdefault("turns", [])
    while len(turns) <= args.turn:
        question, wanted = turn_inputs(state, args.case)[len(turns)]
        turns.append({"question": question, "expected_order_id": wanted, "key": str(uuid4())})
    assert all(t.get("review") == "PASSED" for t in turns[: args.turn])
    turn = turns[args.turn]
    if turn.get("review") == "PASSED":
        print("PASS existing turn; no new model request")
        return
    if not turn.get("run_id"):
        with pg_connection(ROOT, True) as conn:
            used = conn.execute(
                "SELECT coalesce(sum(CASE WHEN state='SETTLED' THEN settled_units ELSE reserved_units END),0) n FROM ag_quota_reservation WHERE bucket_key='model-requests:DIALOG' AND state<>'RELEASED' AND period_start=date_trunc('day',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
            ).fetchone()["n"]
            assert used <= 180, "Do not raise, clear or bypass the original 200 daily request cap"
        save(state)
        sent = await api(
            client,
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
            run = await api(client, MEMBER + "/runs/" + turn["run_id"])
            if run["state"] not in {"QUEUED", "RUNNING", "STOP_REQUESTED"}:
                break
            await asyncio.sleep(2)
    snapshot = await api(client, MEMBER + "/conversations/" + cid)
    turn.update(run=run, snapshot=snapshot, finished_at=datetime.now(UTC).isoformat())
    with pg_connection(ROOT, True) as conn:
        turn["calls"] = [
            dict(row)
            for row in conn.execute(
                "SELECT capability,status FROM ag_model_call WHERE run_id=%s ORDER BY created_at,id",
                (turn["run_id"],),
            )
        ]
        turn["dispatches"] = conn.execute(
            "SELECT count(*) n FROM ag_action_dispatch d JOIN ag_action a ON a.id=d.action_id WHERE a.conversation_id=%s",
            (cid,),
        ).fetchone()["n"]
    save(state)
    assert run["state"] == "COMPLETED", "Retain a terminal failure; never silently resend"
    assert turn["dispatches"] == 0
    pending = snapshot["pending_actions"]
    if turn["expected_order_id"]:
        expected = next(order for order in state["orders"] if order["id"] == turn["expected_order_id"])
        assert len(pending) == 1 and pending[0]["parameters"] == {
            "action_type": "CANCEL_ORDER",
            "order_id": expected["id"],
        }, "The exact member-selected order is required"
        current = pending[0]
        assert current["quote"]["cancellation"] == {
            k: expected[k] for k in ["id", "order_sn", "pay_amount"]
        } | {"status": 0}
        assert current["quote"]["rule_version"] == "CANCEL_PENDING_V2"
        turn["action"] = current
    else:
        assert not pending, "Unresolved or revoked member target cannot create another pending card"
    for old in turns[: args.turn]:
        if not old.get("action"):
            continue
        current = await api(client, MEMBER + "/actions/" + old["action"]["id"])
        assert (
            current["state"] in {"STALE", "EXPIRED"} and current["parameters"] == old["action"]["parameters"]
        )
    assert seeded_order_facts(state) == state["seeded_orders_fingerprint"]
    assert original() == state["baseline"]
    turn["review"] = "PASSED"
    save(state)
    print(
        json.dumps(
            {
                "case": args.case,
                "turn": args.turn,
                "review": "PASSED",
                "target": turn["expected_order_id"],
                "calls": len(turn["calls"]),
                "pending": len(pending),
            },
            ensure_ascii=False,
        )
    )


async def cleanup(client, state):
    from backup_data import pg_connection

    assert original() == state["baseline"]
    for i, order in enumerate(state["orders"]):
        current = await api(client, MEMBER + "/orders/" + order["id"])
        if current["status"] == 0:
            await explicit_operation(
                client, state, "cleanup" + str(i), {"action_type": "CANCEL_ORDER", "order_id": order["id"]}
            )
        assert (await api(client, MEMBER + "/orders/" + order["id"]))["status"] == 4
    for case in state["conversations"].values():
        if case.get("deleted"):
            continue
        cid = case["id"]
        snapshot = await api(client, MEMBER + "/conversations/" + cid)
        assert not snapshot["active_run"]
        await api(
            client,
            MEMBER + "/conversations/" + cid,
            "DELETE",
            {"expected_version": snapshot["conversation"]["version"]},
        )
        assert (await client.get(MEMBER + "/conversations/" + cid)).status_code == 404
        case["deleted"] = True
        save(state)
    assert stocks() == state["stocks_before"] and original() == state["baseline"]
    with pg_connection(ROOT, True) as conn:
        calls = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM ag_model_call WHERE id=ANY(%s::uuid[]) ORDER BY id",
                (state["historical_calls"]["ids"],),
            )
        ]
        assert len(calls) == state["historical_calls"]["count"]
        assert fingerprint_rows(calls) == state["historical_calls"]["sha256"]
        state["historical_calls_unchanged"] = True
    state["cleaned"] = True
    state["cleaned_at"] = datetime.now(UTC).isoformat()
    save(state)
    print(
        "PASS dedicated pending orders cancelled, inventory locks restored, owned conversations deleted, original facts unchanged"
    )


async def verify(client, state):
    from backup_data import pg_connection, preflight

    assert state.get("cleaned") and original() == state["baseline"]
    assert stocks() == state["stocks_before"]
    for case in state["conversations"].values():
        assert case.get("deleted")
        assert (await client.get(MEMBER + "/conversations/" + case["id"])).status_code == 404
    for order in state["orders"]:
        assert (await api(client, MEMBER + "/orders/" + order["id"]))["status"] == 4
    with pg_connection(ROOT, True) as conn:
        cids = [case["id"] for case in state["conversations"].values()]
        model_cids = [case["id"] for case in state["conversations"].values() if "turns" in case]
        result = {
            "checked_at": datetime.now(UTC).isoformat(),
            "cleaned": True,
            "original_business_stocks_policies_unchanged": True,
            "historical_calls_unchanged": state["historical_calls_unchanged"],
            "active": preflight(conn),
            "model_confirmation_requests": state["model_confirmation_requests"],
            "model_conversation_dispatches": conn.execute(
                "SELECT count(*) n FROM ag_action_dispatch d JOIN ag_action a ON a.id=d.action_id WHERE a.conversation_id=ANY(%s::uuid[])",
                (model_cids,),
            ).fetchone()["n"],
            "seed_cleanup_dispatches": conn.execute(
                "SELECT a.action_type,a.state,count(*) n FROM ag_action_dispatch d JOIN ag_action a ON a.id=d.action_id WHERE a.conversation_id=ANY(%s::uuid[]) GROUP BY a.action_type,a.state ORDER BY a.action_type,a.state",
                (cids,),
            ).fetchall(),
            "cases": {
                name: [
                    {
                        "run_id": turn.get("run_id"),
                        "review": turn.get("review"),
                        "state": turn.get("run", {}).get("state"),
                        "target": turn["expected_order_id"],
                        "calls": len(turn.get("calls", [])),
                    }
                    for turn in case["turns"]
                ]
                for name, case in state["conversations"].items()
                if "turns" in case
            },
            "calls": conn.execute(
                "SELECT c.capability,c.status,count(*) n FROM ag_model_call c JOIN ag_run r ON r.id=c.run_id WHERE r.conversation_id=ANY(%s::uuid[]) GROUP BY c.capability,c.status ORDER BY c.capability,c.status",
                (model_cids,),
            ).fetchall(),
            "new_unlinked_calls": conn.execute(
                "SELECT count(*) n FROM ag_model_call WHERE created_at>=%s AND run_id IS NULL AND job_id IS NULL AND media_request_id IS NULL",
                (state["created_at"],),
            ).fetchone()["n"],
            "counts": conn.execute(
                "SELECT (SELECT count(*) FROM ag_model_call) calls,(SELECT count(*) FROM ag_model_meter WHERE usage_state IN ('UNKNOWN','PENDING')) unknown_meters,(SELECT count(*) FROM ag_kb_document WHERE enabled AND active_version_id IS NOT NULL) active_knowledge,(SELECT version_num FROM alembic_version) migration,(SELECT jsonb_array_length(value->'model_prices') FROM ag_setting WHERE key='agent-runtime-v1') price_rules"
            ).fetchone(),
            "workers": conn.execute(
                "SELECT process_kind,count(*) FILTER(WHERE last_seen_at>now()-interval '45 seconds' AND state<>'STOPPED') fresh FROM ag_worker_heartbeat GROUP BY process_kind ORDER BY process_kind"
            ).fetchall(),
        }
        assert result["model_conversation_dispatches"] == 0
        assert result["new_unlinked_calls"] == 0
        assert all(row["fresh"] > 0 for row in result["workers"])
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8099", timeout=30, trust_env=False) as health:
        for name in ("livez", "readyz"):
            response = await health.get("/" + name)
            result[name] = {"http": response.status_code}
            assert response.status_code == 200
            if name == "readyz":
                result[name]["dependencies"] = {
                    key: value.get("status") if isinstance(value, dict) else value
                    for key, value in response.json().get("dependencies", {}).items()
                }
    output = (
        ROOT
        / "deployment/.run"
        / (
            "completion-order-choices-final-state-20261114.json"
            if "-live-final-" in STATE.name
            else "completion-order-choices-initial-final-state-20261114.json"
        )
    )
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


async def main(args):
    state = (
        json.loads(STATE.read_text(encoding="utf-8"))
        if STATE.exists()
        else {
            "created_at": datetime.now(UTC).isoformat(),
            "conversations": {},
            "keys": {},
            "model_confirmation_requests": 0,
        }
    )
    assert args.mode == "verify" or not state.get("cleaned"), "Never resurrect cleaned acceptance data"
    save(state)
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=60, trust_env=False) as client:
        await login(client, username="agent-lifecycle-6c72a0f5")
        if args.mode == "seed":
            await seed(client, state)
        elif args.mode == "run":
            await model_turn(client, state, args)
        elif args.mode == "verify":
            await verify(client, state)
        else:
            await cleanup(client, state)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["seed", "run", "cleanup", "verify"])
    parser.add_argument(
        "--case", choices=["GENERAL", "GENERAL_FIXED", "ENTRY", "ENTRY_FIXED"], default="GENERAL"
    )
    parser.add_argument("--turn", type=int, choices=range(7), default=0)
    parser.add_argument("--attempt", choices=["initial", "final"], default="initial")
    args = parser.parse_args()
    if args.attempt == "final":
        STATE = ROOT / "deployment/.run/completion-order-choices-live-final-20261114.json"
    try:
        asyncio.run(main(args))
    except Exception as error:
        if STATE.exists():
            stopped = json.loads(STATE.read_text(encoding="utf-8"))
            stopped.setdefault("observations", []).append(
                {
                    "at": datetime.now(UTC).isoformat(),
                    "mode": args.mode,
                    "case": args.case,
                    "turn": args.turn,
                    "error_type": type(error).__name__,
                }
            )
            if args.mode == "run":
                turns = stopped.get("conversations", {}).get(args.case, {}).get("turns", [])
                if len(turns) > args.turn:
                    failed = turns[args.turn]
                    failed["review"] = (
                        "FAILED_ACCEPTANCE"
                        if failed.get("run", {}).get("state")
                        in {"COMPLETED", "FAILED", "STOPPED", "INTERRUPTED"}
                        else "OBSERVATION_INTERRUPTED"
                    )
            save(stopped)
        print("ACCEPTANCE_STOP type=" + type(error).__name__)
        print(
            "LOCATION",
            [
                (Path(frame.filename).name, frame.lineno)
                for frame in traceback.extract_tb(error.__traceback__)[-3:]
            ],
        )
        raise SystemExit(1) from None
