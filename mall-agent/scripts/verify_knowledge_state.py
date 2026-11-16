"""Read source, model, checkpoint and task evidence without exporting private configuration."""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from pydantic import SecretStr

from app.infrastructure.postgres.database import Database, one, rows
from app.runtime import run
from app.settings import Settings


async def main():
    root = Path(__file__).resolve().parents[2]
    cfg = json.loads((root / "deployment/.run/agent-env.json").read_text())
    evidence = json.loads((root / "deployment/.run/knowledge-smoke.json").read_text(encoding="utf-8"))
    settings = Settings(
        database_url=cfg["databaseUrl"],
        bridge_secret=cfg["bridgeSecret"],
        broker_secret=cfg["brokerSecret"],
        cursor_secret=cfg["cursorSecret"],
    )
    db = Database(cfg["databaseUrl"])
    try:
        async with db.transaction(snapshot=True) as c:
            versions = await rows(
                c,
                """SELECT v.id,v.state,v.quality_report,v.published_at,j.state AS job_state,
                (SELECT count(*) FROM ag_kb_chunk k WHERE k.version_id=v.id) AS chunks,
                (SELECT count(*) FROM ag_job_wakeup w WHERE w.job_id IN (v.parse_job_id,v.index_job_id) AND w.published_at IS NOT NULL) AS published_wakeups
                FROM ag_kb_version v JOIN ag_job j ON j.id=v.index_job_id WHERE v.document_id=:doc ORDER BY v.version_no""",
                doc=UUID(evidence["document_id"]),
            )
            runs = await rows(
                c,
                "SELECT r.* FROM ag_run r WHERE r.conversation_id=:cid ORDER BY r.created_at",
                cid=UUID(evidence["conversation_id"]),
            )
            sources = await one(
                c,
                """SELECT count(*) AS n FROM ag_kb_citation k JOIN ag_message m ON m.id=k.message_id WHERE m.conversation_id=:cid""",
                cid=UUID(evidence["conversation_id"]),
            )
            doc = await one(
                c, "SELECT enabled FROM ag_kb_document WHERE id=:id", id=UUID(evidence["document_id"])
            )
            calls = await rows(
                c,
                """SELECT c.capability,c.status,p.prompt_key FROM ag_model_call c
                LEFT JOIN ag_prompt_version p ON p.id=c.prompt_version_id WHERE c.run_id IN
                (SELECT id FROM ag_run WHERE conversation_id=:id)""",
                id=UUID(evidence["conversation_id"]),
            )
            counts = await one(
                c,
                """SELECT (SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name LIKE 'ag_%') AS domain_tables,
                (SELECT count(*) FROM information_schema.tables WHERE table_schema='langgraph_checkpoint') AS checkpoint_tables""",
            )
        assert len(versions) == 2 and [v["state"] for v in versions] == ["RETIRED", "READY"]
        assert all(
            v["job_state"] == "SUCCEEDED"
            and v["chunks"] > 0
            and v["published_wakeups"] == 2
            and v["quality_report"]["passed"] is True
            for v in versions
        )
        assert not doc["enabled"] and sources["n"] >= 2
        assert len(runs) == 3 and all(r["state"] == "COMPLETED" for r in runs)
        assert sum(c["prompt_key"] == "knowledge-answer" and c["status"] == "SUCCEEDED" for c in calls) == 2
        secret_values = [
            v.get_secret_value()
            for v in settings.model_dump().values()
            if isinstance(v, SecretStr) and len(v.get_secret_value()) >= 16
        ]
        async with AsyncPostgresSaver.from_conn_string(settings.checkpoint_url()) as saver:
            for r in runs:
                result = await saver.aget_tuple({"configurable": {"thread_id": r["checkpoint_thread_id"]}})
                state = result.checkpoint["channel_values"]
                assert state["intent"] == "KNOWLEDGE" and state["steps_used"] == 7 and state["answer"]
                assert r["config_snapshot"]["graph_version"] == "knowledge-0.6"
                assert all(
                    secret not in json.dumps(result.checkpoint, default=str) for secret in secret_values
                )
                assert len(state.get("source_refs", [])) <= 5
        result = {
            "verified_at": datetime.now(UTC).isoformat(),
            "build": "knowledge-0.6",
            "migration": "0004",
            **dict(counts),
            "checks": [
                "two_real_versions_built_published_and_old_version_retired",
                "four_task_wakeups_published_through_outbox",
                "three_completed_runs_use_knowledge_node_and_current_graph",
                "two_gpt_knowledge_calls_record_prompt_version",
                "no_config_secrets_in_completed_checkpoints",
                "test_document_disabled_and_citation_records_preserved",
            ],
        }
        (root / "deployment/.run/knowledge-state.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("PASS knowledge sources, task wakeups, GPT ledger and full graph checkpoints verified")
    finally:
        await db.engine.dispose()


if __name__ == "__main__":
    run(main())
