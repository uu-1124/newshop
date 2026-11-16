"""Repeat the eight-request baseline and attribute retained capacity run timings."""
# ruff: noqa: E402

import json
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "deployment"), str(Path(__file__).parent)]
from backup_data import pg_connection
from expansion_client import MEMBER, api, login

from app.infrastructure.postgres.database import Database, one
from app.runtime import run


def summary(values):
    values = sorted(values)
    return {
        "samples": len(values),
        **{
            f"p{p}_ms": round(values[min(len(values) - 1, int(len(values) * p / 100))], 2) if values else None
            for p in (50, 95)
        },
    }


async def main():
    result = {}
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8201", trust_env=False, timeout=60) as client:
        await login(client)
        for path in (MEMBER + "/conversations?limit=20", MEMBER + "/orders?limit=5", MEMBER + "/me"):
            samples = []
            for _ in range(8):
                start = time.perf_counter()
                await api(client, path)
                samples.append((time.perf_counter() - start) * 1000)
            result[path] = summary(samples)
    config = json.loads((ROOT / "deployment/.run/agent-env.json").read_text(encoding="utf-8"))
    db = Database(config["databaseUrl"])
    acquire, query = [], []
    try:
        for _ in range(12):
            start = time.perf_counter()
            async with db.connection() as conn:
                acquire.append((time.perf_counter() - start) * 1000)
                start = time.perf_counter()
                await one(conn, "SELECT 1")
                query.append((time.perf_counter() - start) * 1000)
        result["diagnostics"] = {
            "cold_acquisition_ms": round(acquire[0], 2),
            "warm_acquisition": summary(acquire[1:]),
            "select_one_roundtrip": summary(query),
            "note": "SELECT 1 includes driver/network/server time; isolated pool acquisition is not live contention.",
        }
    finally:
        await db.engine.dispose()
    with pg_connection(ROOT, True) as conn:
        result["counters"] = dict(
            conn.execute(
                "SELECT (SELECT count(*) FROM ag_model_call) models,(SELECT count(*) FROM ag_action WHERE state IN ('EXECUTING','UNKNOWN')) pending"
            ).fetchone()
        )
        capacity = {}
        for path in sorted((ROOT / "deployment/.run").glob("capacity-*.json")):
            state = json.loads(path.read_text(encoding="utf-8"))
            if not state.get("created_at", "").startswith("2026-11-16") or not state.get("model_runs"):
                continue
            runs, stages = [], defaultdict(list)
            for item in state.get("final_database", {}).get("runs", []):
                if str(item["id"]) not in state["model_runs"]:
                    continue

                def elapsed(end, start):
                    return (
                        round(
                            (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
                            * 1000,
                            2,
                        )
                        if end and start
                        else None
                    )

                runs.append(
                    {
                        "id": item["id"],
                        "queue_ms": elapsed(item["started_at"], item["created_at"]),
                        "first_text_ms": elapsed(item.get("first_text_at"), item["created_at"]),
                        "total_ms": elapsed(item["finished_at"], item["created_at"]),
                    }
                )
            calls = conn.execute(
                """SELECT c.run_id,c.capability,c.latency_ms,p.prompt_key FROM ag_model_call c
                LEFT JOIN ag_prompt_version p ON p.id=c.prompt_version_id WHERE c.run_id=ANY(%s::uuid[]) ORDER BY c.created_at""",
                (state["model_runs"],),
            ).fetchall()
            for call in calls:
                stages[call["capability"] + ":" + (call["prompt_key"] or "adapter")].append(
                    call["latency_ms"]
                )
            capacity[path.name] = {
                "runs": runs,
                "supplier_stages": {k: summary(v) for k, v in stages.items()},
                "queue": summary([r["queue_ms"] for r in runs if r["queue_ms"] is not None]),
                "first_text": summary([r["first_text_ms"] for r in runs if r["first_text_ms"] is not None]),
                "total": summary([r["total_ms"] for r in runs if r["total_ms"] is not None]),
            }
        result["capacity_attribution"] = capacity
    output = ROOT / "deployment/.run/conservative-latency-after-20261116.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "capacity_attribution"}, ensure_ascii=False))


if __name__ == "__main__":
    run(main())
