"""Live policy answers with saved citations and first-delta timing; bounded paid calls."""

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from expansion_client import MEMBER, api, login
from policy_evaluation_fixtures import EvaluationFixtures

from app.infrastructure.postgres.database import Database, one
from app.runtime import run

ROOT = Path(__file__).resolve().parents[2]


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-name", default="completion-demo-policy-eval-20261113.json")
    parser.add_argument("--case", action="append")
    arguments = parser.parse_args()
    if Path(arguments.output_name).name != arguments.output_name or not arguments.output_name.endswith(
        ".json"
    ):
        raise ValueError("Evaluation output must be a JSON filename under deployment/.run")
    dataset = json.loads((ROOT / "mall-agent/evals/demo-policy-cases.json").read_text(encoding="utf-8"))
    published = json.loads(
        (ROOT / "deployment/.run/completion-demo-policy-publication-20261113.json").read_text(
            encoding="utf-8"
        )
    )
    output = ROOT / "deployment/.run" / arguments.output_name
    state = (
        json.loads(output.read_text(encoding="utf-8"))
        if output.exists()
        else {"started_at": datetime.now(UTC).isoformat(), "cases": [], "result": "IN_PROGRESS"}
    )

    def save():
        output.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    async def capture(record, snap):
        messages = [m for m in snap["recent_messages"] if m["sender_kind"] == "ASSISTANT"]
        message = messages[-1] if messages else {}
        record["message_status"] = message.get("status")
        record["answer"] = message.get("text", "")
        record["all_blocks"] = message.get("blocks", [])
        record["semantic_review"] = "PENDING"
        cfg = json.loads((ROOT / "deployment/.run/agent-env.json").read_text())
        db = Database(cfg["databaseUrl"])
        try:
            async with db.transaction(snapshot=True) as conn:
                timing = await one(
                    conn,
                    """SELECT min(e.created_at) first_delta,max(mc.created_at) final_call_recorded,
                    (SELECT finished_at FROM ag_run WHERE id=CAST(:run AS uuid)) run_finished
                    FROM ag_event_outbox e CROSS JOIN ag_model_call mc
                    WHERE e.conversation_id=CAST(:cid AS uuid) AND e.event_type='message.delta'
                    AND e.payload->>'run_id'=CAST(:run AS text) AND mc.run_id=CAST(:run AS uuid)""",
                    cid=record["conversation_id"],
                    run=record["run_id"],
                )
                record["server_timing"] = {k: v.isoformat() if v else None for k, v in timing.items()}
                record["delta_before_final_model_completion"] = bool(
                    timing["first_delta"]
                    and timing["final_call_recorded"]
                    and timing["first_delta"] < timing["final_call_recorded"]
                )
        finally:
            await db.engine.dispose()

    async with (
        httpx.AsyncClient(base_url="http://127.0.0.1:8201", trust_env=False, timeout=60) as staff,
        httpx.AsyncClient(base_url="http://127.0.0.1:8201", trust_env=False, timeout=60) as buyer,
    ):
        await login(staff, True)
        await login(buyer, username="agent-lifecycle-6c72a0f5")
        fixtures = EvaluationFixtures(staff, state, published, save)
        for case in dataset["cases"]:
            if arguments.case and case["id"] not in arguments.case:
                continue
            previous = next((r for r in reversed(state["cases"]) if r["case_id"] == case["id"]), None)
            if previous and not previous.get("retry_required"):
                if not previous.get("conversation_deleted"):
                    snap = await api(buyer, MEMBER + "/conversations/" + previous["conversation_id"])
                    if snap["active_run"]:
                        raise RuntimeError(
                            "Recorded evaluation run is still active; wait for it instead of issuing another"
                        )
                    await capture(previous, snap)
                    await api(
                        buyer,
                        MEMBER + "/conversations/" + previous["conversation_id"],
                        "DELETE",
                        {"expected_version": snap["conversation"]["version"]},
                    )
                    previous["conversation_deleted"] = True
                    save()
                await fixtures.cleanup(case)
                continue
            await fixtures.prepare(case)
            conv = await api(
                buyer,
                MEMBER + "/conversations",
                "POST",
                {"title": "[TEST ONLY] policy eval " + case["id"], "entry_context": {"kind": "GENERAL"}},
                expected=201,
            )
            cid = conv["id"]
            record = {
                "case_id": case["id"],
                "question": case["question"],
                "conversation_id": cid,
                "allowed_document_ids": [fixtures.id_for(n) for n in case["allowed_documents"]],
                "started_at": datetime.now(UTC).isoformat(),
            }
            state["cases"].append(record)
            save()
            try:
                started = time.monotonic()
                sent = await api(
                    buyer,
                    MEMBER + f"/conversations/{cid}/messages",
                    "POST",
                    {"client_message_id": str(uuid4()), "text": case["question"], "attachment_ids": []},
                    expected=202,
                )
                record["run_id"] = sent["run"]["id"]
                save()
                async with buyer.stream(
                    "GET",
                    MEMBER + f"/conversations/{cid}/events",
                    params={"after": sent["event_cursor"]},
                    timeout=210,
                ) as events:
                    assert events.status_code == 200
                    event_type = None
                    async for line in events.aiter_lines():
                        if line.startswith("event:"):
                            event_type = line[6:].strip()
                        if line.startswith("data:"):
                            event = json.loads(line[5:].strip())
                            if event_type == "message.delta":
                                record.setdefault(
                                    "first_delta_ms", round((time.monotonic() - started) * 1000)
                                )
                                record.setdefault("delta_events", 0)
                                record["delta_events"] += 1
                            if (
                                event_type in {"run.failed", "run.stopped"}
                                and event["payload"].get("run_id") == record["run_id"]
                            ) or (
                                event_type == "message.completed"
                                and event["payload"].get("message", {}).get("run_id") == record["run_id"]
                            ):
                                break
                            if time.monotonic() - started > 195:
                                raise AssertionError("Evaluation run deadline exceeded")
                record["terminal_ms"] = round((time.monotonic() - started) * 1000)
                snap = await api(buyer, MEMBER + "/conversations/" + cid)
                await capture(record, snap)
                print(
                    "RESULT",
                    case["id"],
                    record["message_status"],
                    "first_delta_ms",
                    record.get("first_delta_ms"),
                    flush=True,
                )
            finally:
                save()
                snap = await api(buyer, MEMBER + "/conversations/" + cid)
                await api(
                    buyer,
                    MEMBER + "/conversations/" + cid,
                    "DELETE",
                    {"expected_version": snap["conversation"]["version"]},
                )
                record["conversation_deleted"] = True
                await fixtures.cleanup(case)
                save()
    latest = {r["case_id"]: r for r in state["cases"]}
    state["result"] = (
        "PASSED_REVIEWED_LATEST_ATTEMPTS"
        if all(latest.get(c["id"], {}).get("semantic_review") == "PASSED" for c in dataset["cases"])
        else "NEEDS_SEMANTIC_REVIEW"
    )
    save()


if __name__ == "__main__":
    run(main())
