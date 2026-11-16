"""Live source attribution for one knowledge run and one compatibility job.

Normal authenticated commands only. Never changes configuration, confirms a
transaction, settles unknown usage, or guesses historical call ownership.
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
from expansion_client import ADMIN, MEMBER, api, login
from smoke_stream_ui import baseline

from app.runtime import run

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "deployment/.run/completion-model-attribution-live-20261114.json"
sys.path.insert(0, str(ROOT / "deployment"))


def save(state):
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def old_calls_fingerprint(conn, cutoff):
    records = conn.execute(
        "SELECT row_to_json(c)::text value FROM ag_model_call c WHERE created_at<%s ORDER BY id", (cutoff,)
    ).fetchall()
    return {
        "count": len(records),
        "sha256": hashlib.sha256("\n".join(row["value"] for row in records).encode()).hexdigest(),
    }


async def main(mode):
    from backup_data import pg_connection

    if STATE.exists():
        state = json.loads(STATE.read_text(encoding="utf-8"))
    else:
        state = {"created_at": datetime.now(UTC).isoformat(), "keys": {}, "baseline": baseline()}
        with pg_connection(ROOT, True) as conn:
            state["historical_calls"] = old_calls_fingerprint(conn, state["created_at"])
            state["profiles"] = list(
                conn.execute(
                    "SELECT id,version,capability,is_active,enabled FROM ag_model_profile ORDER BY id"
                )
            )
        save(state)

    def key(name):
        if name not in state["keys"]:
            state["keys"][name] = str(uuid4())
            save(state)
        return state["keys"][name]

    async with httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=45, trust_env=False) as client:
        await login(
            client, staff=mode == "job", username=None if mode == "job" else "agent-lifecycle-6c72a0f5"
        )
        if mode == "run":
            assert not state.get("deleted"), "Deleted acceptance data must stay deleted"
            if not state.get("conversation_id"):
                conversation = await api(
                    client,
                    MEMBER + "/conversations",
                    "POST",
                    {"title": "[TEST ONLY] 检索调用任务归属", "entry_context": {"kind": "GENERAL"}},
                    key("conversation"),
                    201,
                )
                state["conversation_id"] = conversation["id"]
                save(state)
            if not state.get("run_id"):
                sent = await api(
                    client,
                    MEMBER + f"/conversations/{state['conversation_id']}/messages",
                    "POST",
                    {
                        "client_message_id": key("message"),
                        "text": "[TEST ONLY] 根据商城演示规则，模拟退款是否代表实际资金到账？请读取规则并引用有效来源，仅咨询，不执行任何业务。",
                        "attachment_ids": [],
                    },
                    key("message"),
                    202,
                )
                state["run_id"] = sent["run"]["id"]
                save(state)
            async with asyncio.timeout(240):
                while True:
                    record = await api(client, MEMBER + "/runs/" + state["run_id"])
                    if record["state"] not in {"QUEUED", "RUNNING", "STOP_REQUESTED"}:
                        break
                    await asyncio.sleep(2)
            state["run"] = record
            state["snapshot"] = await api(client, MEMBER + "/conversations/" + state["conversation_id"])
            save(state)
            assert record["state"] == "COMPLETED"
            with pg_connection(ROOT, True) as conn:
                calls = list(
                    conn.execute(
                        "SELECT c.id,c.capability,c.status,c.run_id,c.job_id,m.actor_kind,m.actor_id FROM ag_model_call c JOIN ag_model_meter m ON m.call_id=c.id WHERE c.created_at>=%s ORDER BY c.created_at,c.id",
                        (state["created_at"],),
                    )
                )
                state["run_calls"] = calls
            save(state)
            assert {c["capability"] for c in calls} >= {"DIALOG", "EMBEDDING", "RERANK"}
            assert all(
                str(c["run_id"]) == state["run_id"] and c["job_id"] is None and c["actor_kind"] == "MEMBER"
                for c in calls
            )
            assert all(c["status"] == "SUCCEEDED" for c in calls)
            messages = state["snapshot"]["recent_messages"]
            assert any(b.get("type") == "source" for m in messages for b in m.get("blocks", [])), (
                "Live answer must expose a valid source"
            )
            state["run_review"] = "PASSED"
            save(state)
            print("PASS knowledge run includes attributed DIALOG/EMBEDDING/RERANK calls")
        elif mode == "job":
            assert state.get("run_review") == "PASSED"
            if not state.get("job_id"):
                profile = next(
                    p
                    for p in state["profiles"]
                    if p["capability"] == "EMBEDDING" and p["is_active"] and p["enabled"]
                )
                job = await api(
                    client,
                    ADMIN + f"/model-profiles/{profile['id']}/test",
                    "POST",
                    {"expected_version": profile["version"]},
                    key("job"),
                    202,
                )
                state["job_id"] = job["id"]
                save(state)
            async with asyncio.timeout(180):
                while True:
                    job = await api(client, ADMIN + "/jobs/" + state["job_id"])
                    if job["state"] not in {"QUEUED", "RUNNING"}:
                        break
                    await asyncio.sleep(2)
            state["job"] = job
            with pg_connection(ROOT, True) as conn:
                state["job_calls"] = list(
                    conn.execute(
                        "SELECT c.id,c.capability,c.status,c.run_id,c.job_id,m.actor_kind,m.actor_id FROM ag_model_call c JOIN ag_model_meter m ON m.call_id=c.id WHERE c.job_id=%s ORDER BY c.created_at,c.id",
                        (state["job_id"],),
                    )
                )
            save(state)
            assert job["state"] == "SUCCEEDED" and len(state["job_calls"]) == 1
            call = state["job_calls"][0]
            assert (
                call["capability"] == "EMBEDDING"
                and call["status"] == "SUCCEEDED"
                and call["run_id"] is None
                and call["actor_kind"] == "STAFF"
            )
            state["job_review"] = "PASSED"
            save(state)
            print("PASS real compatibility job has job_id and staff attribution; profile unchanged")
        else:
            cid = state["conversation_id"]
            response = await client.get(MEMBER + "/conversations/" + cid)
            if response.status_code == 200:
                snapshot = response.json()["data"]
                assert not snapshot["active_run"]
                await api(
                    client,
                    MEMBER + "/conversations/" + cid,
                    "DELETE",
                    {"expected_version": snapshot["conversation"]["version"]},
                )
            assert (await client.get(MEMBER + "/conversations/" + cid)).status_code == 404
            state["deleted"] = True
            current = baseline()
            state["baseline_unchanged"] = {
                k: current[k] == v for k, v in state["baseline"].items() if k != "model_calls"
            }
            with pg_connection(ROOT, True) as conn:
                assert old_calls_fingerprint(conn, state["created_at"]) == state["historical_calls"]
                profiles = list(
                    conn.execute(
                        "SELECT id,version,capability,is_active,enabled FROM ag_model_profile ORDER BY id"
                    )
                )
                assert json.loads(json.dumps(profiles, default=str)) == state["profiles"]
            assert all(state["baseline_unchanged"].values())
            save(state)
            print("PASS conversation deleted; historical calls/profiles/business/policies unchanged")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["run", "job", "cleanup"])
    try:
        run(main(parser.parse_args().mode))
    except Exception as error:
        print("STOP", type(error).__name__, "state retained; inspect same run/job")
        print(
            "LOCATION",
            [
                (Path(f.filename).name, f.name, f.lineno)
                for f in traceback.extract_tb(error.__traceback__)[-3:]
            ],
        )
        raise SystemExit(1) from None
