"""Read deployed graph checkpoints, model accounting and notification state safely."""

import json
from datetime import UTC, datetime
from pathlib import Path

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from pydantic import SecretStr

from app.infrastructure.postgres.database import Database, one, rows
from app.runtime import run
from app.settings import Settings


async def main():
    root = Path(__file__).resolve().parents[2]
    cfg = json.loads((root / "deployment/.run/agent-env.json").read_text())
    settings = Settings(
        database_url=cfg["databaseUrl"],
        bridge_secret=cfg["bridgeSecret"],
        broker_secret=cfg["brokerSecret"],
        cursor_secret=cfg["cursorSecret"],
    )
    evidence = {"time": datetime.now(UTC).isoformat(), "checks": []}
    db = Database(cfg["databaseUrl"])
    ids = [json.loads((root / "deployment/.run/agent-smoke.json").read_text())["run_id"]]
    image = json.loads((root / "deployment/.run/image-chat-smoke.json").read_text())
    async with db.transaction() as conn:
        image_runs = await rows(
            conn,
            "SELECT id FROM ag_run WHERE conversation_id=CAST(:id AS uuid) AND state='COMPLETED'",
            id=image["conversation_id"],
        )
        ids += [str(item["id"]) for item in image_runs]
    assert len(ids) >= 4
    secrets = [
        value.get_secret_value()
        for value in settings.model_dump().values()
        if isinstance(value, SecretStr) and len(value.get_secret_value()) >= 16
    ]
    async with AsyncPostgresSaver.from_conn_string(settings.checkpoint_url()) as saver:
        for run_id in ids:
            async with db.transaction() as conn:
                record = await one(conn, "SELECT * FROM ag_run WHERE id=CAST(:id AS uuid)", id=run_id)
                calls = await rows(
                    conn,
                    "SELECT c.capability,p.prompt_key FROM ag_model_call c LEFT JOIN ag_prompt_version p ON p.id=c.prompt_version_id WHERE c.run_id=CAST(:id AS uuid) AND c.status='SUCCEEDED'",
                    id=run_id,
                )
            assert record["state"] == "COMPLETED"
            assert record["config_snapshot"]["graph_version"] == "runtime-0.5"
            assert any(call["prompt_key"] == "workflow-router" for call in calls)
            checkpoint = await saver.aget_tuple(
                {"configurable": {"thread_id": record["checkpoint_thread_id"]}}
            )
            assert checkpoint
            state = checkpoint.checkpoint["channel_values"]
            assert state["steps_used"] == (7 if state["intent"] == "GUIDE" else 6) and state["answer"]
            assert state["intent"] in {"GENERAL", "GUIDE", "CLARIFY"}
            assert all(
                "branch:to:" + key in checkpoint.checkpoint["channel_versions"]
                for key in ["N01", "N02", "N03", "N10", "N11", "N12"]
            )
            if state["intent"] == "GUIDE":
                assert state["tool_results"] and state["tool_results"][0]["name"] == "search_products"
            serialized = json.dumps(state, ensure_ascii=False)
            assert "data:image/" not in serialized and "Authorization" not in serialized
            assert not any(secret in serialized for secret in secrets)
    evidence["checks"].append("four_completed_real_gpt_runs_use_full_graph_and_versioned_router")
    evidence["checks"].append("guide_checkpoint_contains_real_product_tool_results")
    evidence["checks"].append("no_raw_images_or_config_secrets_in_completed_checkpoints")
    async with db.transaction() as conn:
        workers = await rows(
            conn,
            "SELECT DISTINCT process_kind FROM ag_worker_heartbeat WHERE last_seen_at>now()-interval '20 seconds'",
        )
        assert {"generation", "scheduler", "jobs", "outbox"} <= {worker["process_kind"] for worker in workers}
        version = await one(conn, "SELECT version_num FROM alembic_version")
        tables = await one(
            conn,
            "SELECT count(*) AS n FROM information_schema.tables WHERE table_schema='public' AND table_name LIKE 'ag_%'",
        )
        assert version["version_num"] == "0003" and tables["n"] == 32
        pending = await one(conn, "SELECT count(*) AS n FROM ag_event_outbox WHERE published_at IS NULL")
        assert pending["n"] == 0
    evidence.update(result="passed", migration="0003", domain_tables=32, verified_runs=len(ids))
    evidence["checks"].append("four_worker_kinds_live_and_all_committed_events_published")
    (root / "deployment/.run/runtime-state.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(evidence, ensure_ascii=False))
    await db.engine.dispose()


if __name__ == "__main__":
    run(main())
