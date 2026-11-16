"""Read-only final acceptance audit. Never calls paid models or prints credentials."""

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.infrastructure.postgres.database import Database, one, rows
from app.runtime import run

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "deployment/.run"


def read(name):
    return json.loads((EVIDENCE / name).read_text(encoding="utf-8"))


async def main():
    evidence = {"verified_at": datetime.now(UTC).isoformat(), "checks": []}

    def passed(name):
        evidence["checks"].append(name)
        print("PASS", name, flush=True)

    conversation = read("conversation-management-smoke.json")
    catalog = read("catalog-runtime-smoke.json")
    knowledge = read("knowledge-expansion-smoke.json")
    staff = read("staff-expansion-smoke.json")
    fixture = read("staff-browser-fixture.json")
    model = read("model-expansion-smoke.json")
    for item, minimum in ((conversation, 5), (catalog, 5), (knowledge, 6), (staff, 5), (model, 4)):
        assert len(item["checks"]) >= minimum
    assert catalog["browser_conversation_deleted"] and fixture["deleted"]
    assert staff["test_conversation_deleted"] and model["test_conversation_deleted"]
    full_log = (EVIDENCE / "expansion-full-final.log").read_text(encoding="utf-8-sig")
    assert re.search(r"88 passed in", full_log), "Latest full Python regression is missing"
    for name, marker in (
        ("expansion-admin-build-final.log", "built in"),
        ("expansion-buyer-build-final.log", "Build complete"),
        ("expansion-catalog-tests.log", "Tests run: 7, Failures: 0, Errors: 0, Skipped: 0"),
        ("expansion-staff-java-database.log", "Tests run: 10, Failures: 0, Errors: 0, Skipped: 0"),
    ):
        assert marker in (EVIDENCE / name).read_text(encoding="utf-8-sig"), name
    passed("six_groups_have_runtime_evidence_and_latest_regression_builds_passed")

    async with httpx.AsyncClient(trust_env=False, timeout=20) as client:
        response = await client.get("http://127.0.0.1:8099/readyz")
        assert response.status_code == 200
        ready = response.json()
        assert ready["migration"] == "0007"
        assert ready["capabilities"]["knowledge"] == "TRUSTED_PRODUCT_CATEGORY_LIVE"
        assert ready["capabilities"]["handoffs"] == "IMAGES_CARDS_SSE_AUDIT_LIVE"
        assert ready["capabilities"]["model_administration"] == "VERSIONED_LIVE"
        response = await client.get("http://127.0.0.1:8099/internal/framework")
        assert response.status_code == 200
        framework = response.json()
        assert (
            framework["module_count"],
            framework["contract_operation_count"],
            framework["active_python_operation_count"],
        ) == (20, 118, 73)
        evidence["runtime"] = ready
        evidence["framework"] = {
            key: framework[key]
            for key in (
                "module_count",
                "contract_operation_count",
                "active_python_operation_count",
                "full_business_runtime",
                "extensions",
                "job_handlers",
            )
        }
    passed("live_migration_0007_and_73_active_operations")

    cfg = read("agent-env.json")
    db = Database(cfg["databaseUrl"])
    try:
        ids = set(conversation["conversations"])
        ids.update(knowledge["test_conversations_deleted"])
        ids.update((catalog["browser_conversation_id"], staff["conversation_id"], fixture["conversation_id"]))
        # The first staff acceptance found the note-redaction constraint; its retried job must be done too.
        ids.add("a3e8b679-c0a1-4bc2-96f4-07c52f8e4170")
        mobile_path = EVIDENCE / "responsive-browser-smoke.json"
        if mobile_path.exists():
            mobile = read(mobile_path.name)
            assert mobile["test_conversation_deleted"]
            ids.add(mobile["conversation_id"])
            evidence["responsive_browser"] = mobile
        async with db.transaction() as conn:
            original_runs = await rows(
                conn,
                "SELECT DISTINCT conversation_id FROM ag_run WHERE id=ANY(CAST(:ids AS uuid[]))",
                ids=[model["before_run_id"], model["after_run_id"]],
            )
            ids.update(str(row["conversation_id"]) for row in original_runs)
            for cid in sorted(ids):
                conv = await one(
                    conn, "SELECT deleted_at FROM ag_conversation WHERE id=CAST(:id AS uuid)", id=cid
                )
                assert conv and conv["deleted_at"], f"Test conversation is still accessible: {cid}"
                job = await one(
                    conn,
                    """SELECT state FROM ag_job WHERE job_type='RETENTION_EXECUTE'
                    AND parameters->>'conversation_id'=:id ORDER BY created_at DESC LIMIT 1""",
                    id=cid,
                )
                assert job and job["state"] == "SUCCEEDED", f"Cleanup not finished: {cid}"
                remaining = await one(
                    conn,
                    """SELECT count(*) n FROM ag_message WHERE conversation_id=CAST(:id AS uuid)
                    AND (text_content<>'' OR blocks<>'[]'::jsonb OR deleted_at IS NULL)""",
                    id=cid,
                )
                assert remaining["n"] == 0, f"Message copy remains: {cid}"
            evidence["cleaned_test_conversations"] = len(ids)
            for name, sql in (
                (
                    "active_runs",
                    "SELECT count(*) n FROM ag_run WHERE state IN ('QUEUED','RUNNING','STOP_REQUESTED')",
                ),
                (
                    "active_labeled_test_knowledge",
                    "SELECT count(*) n FROM ag_kb_document WHERE enabled AND title LIKE '[TEST ONLY]%'",
                ),
                (
                    "recent_failed_conversation_cleanup",
                    "SELECT count(*) n FROM ag_job WHERE job_type='RETENTION_EXECUTE' AND state='FAILED' AND created_at>now()-interval '1 day'",
                ),
                (
                    "remaining_test_notes",
                    "SELECT count(*) n FROM ag_staff_note WHERE conversation_id=ANY(CAST(:ids AS uuid[])) AND body<>'已删除'",
                ),
                (
                    "remaining_test_events",
                    "SELECT count(*) n FROM ag_event_outbox WHERE conversation_id=ANY(CAST(:ids AS uuid[]))",
                ),
                (
                    "remaining_unprotected_test_images",
                    "SELECT count(*) n FROM ag_attachment a JOIN ag_attachment_ref r ON r.attachment_id=a.id JOIN ag_message m ON m.id=r.message_id WHERE m.conversation_id=ANY(CAST(:ids AS uuid[])) AND NOT r.protected AND a.state<>'DELETED'",
                ),
            ):
                result = await one(conn, sql, ids=sorted(ids))
                evidence[name] = result["n"]
                assert result["n"] == 0, name
            workers = await rows(
                conn,
                "SELECT DISTINCT process_kind FROM ag_worker_heartbeat WHERE last_seen_at>now()-interval '45 seconds'",
            )
            evidence["workers"] = sorted(row["process_kind"] for row in workers)
            assert {"generation", "scheduler", "jobs", "outbox"} <= set(evidence["workers"])
            original = await one(
                conn,
                "SELECT version,enabled,is_active FROM ag_model_profile WHERE id=CAST(:id AS uuid)",
                id=model["original_profile_id"],
            )
            candidate = await one(
                conn,
                "SELECT enabled,is_active FROM ag_model_profile WHERE id=CAST(:id AS uuid)",
                id=model["candidate_profile_id"],
            )
            prompt = await one(
                conn,
                "SELECT state FROM ag_prompt_version WHERE id=CAST(:id AS uuid)",
                id=model["original_prompt_id"],
            )
            assert original["enabled"] and original["is_active"] and original["version"] >= 3
            assert not candidate["enabled"] and not candidate["is_active"]
            assert prompt["state"] == "PUBLISHED"
            staff_state = await one(conn, "SELECT presence FROM ag_staff_profile WHERE admin_id=3")
            assert staff_state["presence"] == fixture["initial_presence"]
            evidence["staff_presence"] = staff_state["presence"]
            evidence["original_model_and_prompt_restored"] = True
            evidence["candidate_disabled"] = True
    finally:
        await db.engine.dispose()
    passed("test_conversations_messages_notes_events_and_unprotected_images_cleaned")
    passed("workers_healthy_original_model_prompt_restored_and_test_knowledge_disabled")

    evidence["jars"] = {}
    for service, expected in (
        ("admin", "7e142a6c7b9ae396806d08eb525302fd2b557d87afea375a19e12c92fa4bc506"),
        ("portal", "7e3ccb5cfa5b35f038d9a5ee8a4c71794a7668220c0d24262f546560185387b6"),
    ):
        path = EVIDENCE / f"agent-jars/mall-{service}.jar"
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        assert actual == expected, f"Runtime jar hash changed: {service}"
        evidence["jars"][service] = actual
    passed("active_admin_portal_jars_match_verified_builds")
    evidence["result"] = "passed"
    evidence["paid_model_calls"] = False
    (EVIDENCE / "expansion-final-state.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("PASS read-only final acceptance audit", flush=True)


if __name__ == "__main__":
    run(main())
