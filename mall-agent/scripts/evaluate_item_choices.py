"""Resumable real-model member item choices; preparation only, never confirmation."""

import argparse
import asyncio
import json
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from expansion_client import MEMBER, api, login
from smoke_stream_ui import baseline

from app.runtime import run

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "deployment/.run/completion-item-choices-live-20261114.json"
sys.path.insert(0, str(ROOT / "deployment"))


def save(state):
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def unchanged():
    value = baseline()
    value.pop("model_calls")
    return value


def turns(case):
    if case.startswith("ADD_CART"):
        return [
            (
                "我要OPPO Reno8，鸢尾紫128GB一台，先查真实规格并准备加购确认卡。只准备，不点击确认，不执行业务。",
                "239",
                1,
            ),
            (
                "刚才那款手机容量改成256G，颜色保持鸢尾紫，数量改成两件。重新查询后准备新的加购卡，不确认不执行。",
                "240",
                2,
            ),
            ("旧卡不要用了。规格和数量我还没决定，请先问清楚，不要准备确认卡。", None, None),
            ("现在选定OPPO Reno8鸢尾紫128G一台，重新读商品并准备加购卡，只准备，不确认不执行。", "239", 1),
        ]
    return [
        (
            "读取商品45，选择规格239，数量1，直接购买。用已有地址20，不使用优惠券，不使用积分，读取本次结算选项并准备订单卡。只准备不确认，不下单不付款。",
            "239",
            1,
        ),
        (
            "刚才那款手机改成晴空蓝256G两台，仍用地址20、不用券和积分，重新核对结算选项并准备直购订单卡。不要确认或下单付款。",
            "242",
            2,
        ),
        ("刚才的订单卡不用了，规格和数量暂时都没决定。先帮我澄清，不要准备新卡，不下单不付款。", None, None),
    ]


async def main(args):
    from backup_data import pg_connection

    state = (
        json.loads(STATE.read_text(encoding="utf-8"))
        if STATE.exists()
        else {
            "created_at": datetime.now(UTC).isoformat(),
            "baseline": unchanged(),
            "cases": {},
            "confirm_requests": 0,
        }
    )
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=45, trust_env=False) as client:
        await login(client, username="agent-lifecycle-6c72a0f5")
        if args.mode == "cleanup":
            for case in state["cases"].values():
                if case.get("deleted"):
                    continue
                cid = case["conversation_id"]
                snapshot = await api(client, MEMBER + "/conversations/" + cid)
                assert not snapshot["active_run"], "Do not delete a live model run"
                await api(
                    client,
                    MEMBER + "/conversations/" + cid,
                    "DELETE",
                    {"expected_version": snapshot["conversation"]["version"]},
                )
                assert (await client.get(MEMBER + "/conversations/" + cid)).status_code == 404
                case["deleted"] = True
                save(state)
            assert unchanged() == state["baseline"]
            print("PASS dedicated conversations deleted; original business/policies unchanged")
            return
        case = state["cases"].setdefault(args.case, {"kind": args.case, "turns": []})
        assert not case.get("deleted"), "Never resurrect a deleted acceptance conversation"
        if not case.get("conversation_id"):
            case.setdefault("create_key", str(uuid4()))
            save(state)
            conversation = await api(
                client,
                MEMBER + "/conversations",
                "POST",
                {"title": "[TEST ONLY] 会员自然选择 " + args.case, "entry_context": {"kind": "GENERAL"}},
                case["create_key"],
                201,
            )
            case["conversation_id"] = conversation["id"]
            save(state)
        cid = case["conversation_id"]
        while len(case["turns"]) <= args.turn:
            question, sku, quantity = turns(args.case)[len(case["turns"])]
            case["turns"].append(
                {"question": question, "sku": sku, "quantity": quantity, "key": str(uuid4())}
            )
        assert all(t.get("review") == "PASSED" for t in case["turns"][: args.turn])
        turn = case["turns"][args.turn]
        if turn.get("review") == "PASSED":
            print("PASS already verified", args.case, args.turn)
            return
        if not turn.get("run_id"):
            with pg_connection(ROOT, True) as conn:
                used = conn.execute(
                    "SELECT coalesce(sum(CASE WHEN state='SETTLED' THEN settled_units ELSE reserved_units END),0) n FROM ag_quota_reservation WHERE bucket_key='model-requests:DIALOG' AND state<>'RELEASED' AND period_start=date_trunc('day',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
                ).fetchone()["n"]
                assert used <= 180, (
                    "Preserve the existing 200 daily cap; no profile switching or usage clearing"
                )
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
                current = await api(client, MEMBER + "/runs/" + turn["run_id"])
                if current["state"] not in {"QUEUED", "RUNNING", "STOP_REQUESTED"}:
                    break
                await asyncio.sleep(2)
        snapshot = await api(client, MEMBER + "/conversations/" + cid)
        turn.update(run=current, snapshot=snapshot, finished_at=datetime.now(UTC).isoformat())
        with pg_connection(ROOT, True) as conn:
            turn["model_calls"] = list(
                conn.execute(
                    "SELECT capability,status FROM ag_model_call WHERE run_id=%s ORDER BY created_at,id",
                    (turn["run_id"],),
                )
            )
            turn["dispatches"] = conn.execute(
                "SELECT count(*) n FROM ag_action_dispatch d JOIN ag_action a ON a.id=d.action_id WHERE a.conversation_id=%s",
                (cid,),
            ).fetchone()["n"]
        save(state)
        assert current["state"] == "COMPLETED", "Terminal failure retained; do not silently resend"
        assert turn["dispatches"] == 0
        cards = snapshot["pending_actions"]
        if turn["sku"]:
            expected = {
                "action_type": "ADD_CART" if args.case.startswith("ADD_CART") else "CREATE_ORDER",
                "items": [{"product_id": "45", "sku_id": turn["sku"], "quantity": turn["quantity"]}],
            }
            if args.case.startswith("DIRECT"):
                expected |= {"source": "DIRECT", "address_id": "20", "coupon_id": None, "use_points": 0}
            assert len(cards) == 1 and cards[0]["parameters"] == expected, (
                "Exact member-selected card required"
            )
            turn["action"] = cards[0]
        else:
            assert not cards, "Unresolved latest choices cannot create a pending card"
        for old in case["turns"][: args.turn]:
            if old.get("action"):
                action = await api(client, MEMBER + "/actions/" + old["action"]["id"])
                assert action["state"] in {"STALE", "EXPIRED"}
                assert action["parameters"] == old["action"]["parameters"]
        assert unchanged() == state["baseline"]
        turn["review"] = "PASSED"
        save(state)
        print("PASS real-model", args.case, args.turn, "exact choices/clarification; no dispatch")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["run", "cleanup"])
    parser.add_argument(
        "--case", choices=["ADD_CART", "ADD_CART_FIXED", "DIRECT", "DIRECT_FIXED"], default="ADD_CART"
    )
    parser.add_argument("--turn", type=int, choices=range(4), default=0)
    args = parser.parse_args()
    try:
        run(main(args))
    except Exception as error:
        print("STOP", type(error).__name__, "state retained; inspect the same run before continuing")
        print(
            "LOCATION",
            [
                (Path(f.filename).name, f.name, f.lineno)
                for f in traceback.extract_tb(error.__traceback__)[-3:]
            ],
        )
        raise SystemExit(1) from None
