"""Real quota-denied reply and human fallback; never increases limits or clears usage."""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from expansion_client import MEMBER, api, login
from smoke_stream_ui import baseline

from app.runtime import run

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "deployment/.run/completion-reply-recovery-ui-20261114.json"
sys.path.insert(0, str(ROOT / "deployment"))


def save(state):
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


async def main(mode):
    from backup_data import pg_connection

    state = (
        json.loads(STATE.read_text(encoding="utf-8"))
        if STATE.exists()
        else {
            "created_at": datetime.now(UTC).isoformat(),
            "keys": {},
            "baseline": baseline(),
        }
    )

    def key(name):
        if name not in state["keys"]:
            state["keys"][name] = str(uuid4())
            save(state)
        return state["keys"][name]

    async with httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=45, trust_env=False) as buyer:
        await login(buyer, username="agent-lifecycle-6c72a0f5")
        if mode == "prepare":
            if not state.get("run_id"):
                with pg_connection(ROOT, True) as conn:
                    used = conn.execute(
                        "SELECT coalesce(sum(CASE WHEN state='SETTLED' THEN settled_units ELSE reserved_units END),0) n FROM ag_quota_reservation WHERE bucket_key='model-requests:DIALOG' AND state<>'RELEASED' AND period_start=date_trunc('day',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
                    ).fetchone()["n"]
                    assert used >= 200, "This live case requires the existing exhausted daily quota"
                if not state.get("conversation_id"):
                    conv = await api(
                        buyer,
                        MEMBER + "/conversations",
                        "POST",
                        {"title": "[TEST ONLY] 回复失败恢复", "entry_context": {"kind": "GENERAL"}},
                        key("conversation"),
                        201,
                    )
                    state["conversation_id"] = conv["id"]
                    save(state)
                sent = await api(
                    buyer,
                    MEMBER + f"/conversations/{state['conversation_id']}/messages",
                    "POST",
                    {
                        "client_message_id": key("question"),
                        "text": "[TEST ONLY] 请读取我的购物车，仅查询、不执行任何操作。",
                        "attachment_ids": [],
                    },
                    key("question"),
                    202,
                )
                state["run_id"] = sent["run"]["id"]
                save(state)
            async with asyncio.timeout(90):
                while True:
                    current = await api(buyer, MEMBER + "/runs/" + state["run_id"])
                    if current["state"] not in {"QUEUED", "RUNNING", "STOP_REQUESTED"}:
                        break
                    await asyncio.sleep(1)
            assert current["state"] == "FAILED" and current["error_code"] == "MODEL_QUOTA_EXCEEDED"
            snapshot = await api(buyer, MEMBER + "/conversations/" + state["conversation_id"])
            assistant = next(m for m in snapshot["recent_messages"] if m["run_id"] == state["run_id"])
            assert assistant["reply_failure"]["code"] == "MODEL_QUOTA_EXCEEDED"
            assert assistant["reply_failure"]["retry_after"]
            history = await api(buyer, MEMBER + f"/conversations/{state['conversation_id']}/messages")
            assert (
                next(m for m in history["items"] if m["id"] == assistant["id"])["reply_failure"]
                == assistant["reply_failure"]
            )
            state.update(run=current, failed_snapshot=snapshot)
            assert baseline() == state["baseline"]
            save(state)
            print(
                "PASS real quota rejection has safe recovery guidance in snapshot/history; no supplier call"
            )
        elif mode == "capture":
            snapshot = await api(buyer, MEMBER + "/conversations/" + state["conversation_id"])
            state.setdefault("observations", []).append(snapshot)
            assert baseline() == state["baseline"]
            save(state)
            print("PASS captured actual page transition state " + snapshot["conversation"]["state"])
        elif mode == "verify-old-retry":
            snapshot = await api(buyer, MEMBER + "/conversations/" + state["conversation_id"])
            assert snapshot["conversation"]["state"] == "AI_ACTIVE" and not snapshot["active_run"]
            assert len([m for m in snapshot["recent_messages"] if m["sender_kind"] == "MEMBER"]) >= 2
            response = await buyer.post(
                MEMBER + "/runs/" + state["run_id"] + "/retry",
                headers={"Idempotency-Key": key("old-retry")},
                json={"expected_version": state["run"]["version"]},
            )
            assert response.status_code == 409 and response.json()["code"] == "RUN_NOT_RETRYABLE"
            async with httpx.AsyncClient(
                base_url="http://127.0.0.1:8201", timeout=45, trust_env=False
            ) as other:
                await login(other, username="member")
                assert (await other.get(MEMBER + "/runs/" + state["run_id"])).status_code == 404
                assert (
                    await other.get(MEMBER + "/conversations/" + state["conversation_id"])
                ).status_code == 404
            with pg_connection(ROOT, True) as conn:
                count = conn.execute(
                    "SELECT count(*) n FROM ag_run WHERE conversation_id=%s", (state["conversation_id"],)
                ).fetchone()["n"]
                assert count == 1
            state["old_retry_rejected"] = {
                "http": 409,
                "code": "RUN_NOT_RETRYABLE",
                "run_count": 1,
                "other_member_reads": 404,
            }
            state["after_fallback"] = snapshot
            assert baseline() == state["baseline"]
            save(state)
            print("PASS new context rejects old retry; same single failed run; other member denied")
        elif mode == "cleanup":
            cid = state["conversation_id"]
            response = await buyer.get(MEMBER + "/conversations/" + cid)
            if response.status_code == 200:
                snapshot = response.json()["data"]
                assert not snapshot["active_run"]
                await api(
                    buyer,
                    MEMBER + "/conversations/" + cid,
                    "DELETE",
                    {"expected_version": snapshot["conversation"]["version"]},
                )
            assert (await buyer.get(MEMBER + "/conversations/" + cid)).status_code == 404
            assert baseline() == state["baseline"]
            state["deleted"] = True
            state["cleanup_verified_at"] = datetime.now(UTC).isoformat()
            save(state)
            print(
                "PASS dedicated recovery conversation deleted; original business/policies/model total unchanged"
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["prepare", "capture", "verify-old-retry", "cleanup"])
    try:
        run(main(parser.parse_args().mode))
    except Exception as error:
        print("REPLY_RECOVERY_FAILED type=" + type(error).__name__)
        raise SystemExit(1)
