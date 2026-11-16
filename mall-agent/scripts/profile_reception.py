"""Fixed real-gateway text reception comparison. This is not the 15-minute A08 test."""

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from expansion_client import ADMIN, MEMBER, api, login

from app.runtime import run

ROOT = Path(__file__).resolve().parents[2]


def summary(values):
    values = sorted(values)
    return {
        "samples": len(values),
        "p50_ms": round(values[len(values) // 2], 2),
        "p95_ms": round(values[min(len(values) - 1, int(len(values) * 0.95))], 2),
        "max_ms": round(values[-1], 2),
    }


async def main(stage):
    state = {
        "stage": stage,
        "started_at": datetime.now(UTC).isoformat(),
        "scope": "3 concurrent human conversations, 18 buyer/staff exchanges; no models or business commands",
        "paid_model_calls": 0,
        "business_commands": 0,
        "conversation_ids": [],
        "errors": [],
    }
    path = ROOT / "deployment/.run" / f"completion-reception-{stage}-20261113.json"
    timings = {"member_send": [], "staff_send": [], "member_history": []}
    initial = None
    stop = asyncio.Event()
    heartbeat = None

    def save():
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    async with (
        httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=40, trust_env=False) as buyer,
        httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=40, trust_env=False) as staff,
    ):

        async def profile():
            me = await api(staff, ADMIN + "/me")
            return next(
                x for x in (await api(staff, ADMIN + "/staff?limit=100"))["items"] if x["id"] == me["id"]
            )

        async def keep_online():
            while not stop.is_set():
                await api(staff, ADMIN + "/presence/heartbeat", "POST", {})
                try:
                    await asyncio.wait_for(stop.wait(), 15)
                except TimeoutError:
                    pass

        async def measured(label, client, path, method="GET", body=None, key=None, expected=200):
            started = time.perf_counter()
            result = await api(client, path, method, body, key, expected)
            timings[label].append((time.perf_counter() - started) * 1000)
            return result

        async def exchange(cid):
            for i in range(6):
                sent = await measured(
                    "member_send",
                    buyer,
                    MEMBER + f"/conversations/{cid}/messages",
                    "POST",
                    {
                        "client_message_id": str(uuid4()),
                        "text": "[TEST ONLY] reception latency sample",
                        "attachment_ids": [],
                    },
                    expected=202,
                )
                assert sent["run"] is None
                answer = await measured(
                    "staff_send",
                    staff,
                    ADMIN + f"/conversations/{cid}/messages",
                    "POST",
                    {
                        "client_message_id": str(uuid4()),
                        "text": "[TEST ONLY] measured human reply",
                        "attachment_ids": [],
                        "cards": [],
                    },
                    expected=201,
                )
                history = await measured(
                    "member_history", buyer, MEMBER + f"/conversations/{cid}/messages?limit=5"
                )
                assert answer["id"] in {m["id"] for m in history["items"]}

        try:
            await login(buyer, username="agent-lifecycle-6c72a0f5")
            await login(staff, True)
            initial = await profile()
            if initial["active_count"] or initial["capacity"] < 3:
                raise RuntimeError("Reception benchmark requires three currently unused slots")
            current = await api(
                staff,
                ADMIN + "/presence",
                "PUT",
                {"expected_version": initial["version"], "presence": "ONLINE"},
            )
            state["original_presence"] = initial["presence"]
            state["staff_id"] = current["id"]
            save()
            heartbeat = asyncio.create_task(keep_online())
            for _ in range(3):
                conv = await api(
                    buyer,
                    MEMBER + "/conversations",
                    "POST",
                    {"title": "[TEST ONLY] reception latency " + stage, "entry_context": {"kind": "GENERAL"}},
                    expected=201,
                )
                cid = conv["id"]
                state["conversation_ids"].append(cid)
                save()
                handoff = await api(
                    buyer,
                    MEMBER + f"/conversations/{cid}/handoff",
                    "POST",
                    {
                        "expected_conversation_version": conv["version"],
                        "reason": "[TEST ONLY] reception comparison",
                    },
                    expected=201,
                )
                await api(
                    staff,
                    ADMIN + "/handoffs/" + handoff["id"] + "/claim",
                    "POST",
                    {"expected_version": handoff["version"]},
                )
            await asyncio.gather(*(exchange(cid) for cid in state["conversation_ids"]))
            state["timings"] = {k: summary(v) for k, v in timings.items()}
            state["result"] = "PASSED"
        except Exception as error:
            state["result"] = "FAILED"
            state["errors"].append(type(error).__name__)
        finally:
            stop.set()
            if heartbeat:
                await asyncio.gather(heartbeat, return_exceptions=True)
            state["cleanup_errors"] = []
            for cid in state["conversation_ids"]:
                try:
                    snap = await api(buyer, MEMBER + "/conversations/" + cid)
                    await api(
                        buyer,
                        MEMBER + "/conversations/" + cid,
                        "DELETE",
                        {"expected_version": snap["conversation"]["version"]},
                    )
                except Exception as error:
                    state["cleanup_errors"].append(type(error).__name__)
            if initial:
                try:
                    current = await profile()
                    await api(
                        staff,
                        ADMIN + "/presence",
                        "PUT",
                        {"expected_version": current["version"], "presence": initial["presence"]},
                    )
                except Exception as error:
                    state["cleanup_errors"].append(type(error).__name__)
            state["finished_at"] = datetime.now(UTC).isoformat()
            save()
            print(
                "RECEPTION stage="
                + stage
                + " result="
                + state["result"]
                + " cleanup_errors="
                + str(len(state["cleanup_errors"])),
                flush=True,
            )
            if state.get("timings"):
                print(json.dumps(state["timings"], ensure_ascii=False), flush=True)
    if state["result"] != "PASSED" or state["cleanup_errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=["before", "after"], required=True)
    run(main(parser.parse_args().stage))
