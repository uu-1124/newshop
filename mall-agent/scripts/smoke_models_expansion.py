"""Explicit paid model-switch acceptance in the local demo; restores active model and prompt."""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from expansion_client import ADMIN, MEMBER, api, login, wait_job, wait_run

from app.infrastructure.postgres.database import Database, one
from app.runtime import run

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "deployment/.run/model-expansion-smoke.json"


async def main():
    evidence = {
        "verified_at": datetime.now(UTC).isoformat(),
        "checks": [],
        "paid_usage": "EXPLICIT_COMPATIBILITY_AND_TWO_REAL_DIALOGS",
    }

    def save():
        EVIDENCE.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")

    def passed(check):
        evidence["checks"].append(check)
        save()
        print("PASS", check, flush=True)

    cfg = json.loads((ROOT / "deployment/.run/agent-env.json").read_text())
    db = Database(cfg["databaseUrl"])
    async with (
        httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=45, trust_env=False) as staff,
        httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=45, trust_env=False) as member,
    ):
        await login(staff, True)
        await login(member, username="agent-conversation-bd63b203")

        async def profiles():
            items = []
            cursor = ""
            while True:
                page = await api(
                    staff, ADMIN + "/model-profiles?limit=100" + ("&cursor=" + cursor if cursor else "")
                )
                items += page["items"]
                cursor = page["next_cursor"]
                if not cursor:
                    return items

        initial = next(p for p in await profiles() if p["capability"] == "DIALOG" and p["is_active"])
        original_prompt = next(
            p
            for p in (await api(staff, ADMIN + "/prompt-versions?prompt_key=readonly-dialog&limit=100"))[
                "items"
            ]
            if p["state"] == "PUBLISHED"
        )
        candidate = None
        conv = None
        activated = False
        published = False
        evidence.update(original_profile_id=initial["id"], original_prompt_id=original_prompt["id"])
        save()

        async def test(profile):
            job = await api(
                staff,
                ADMIN + f"/model-profiles/{profile['id']}/test",
                "POST",
                {"expected_version": profile["version"]},
                expected=202,
            )
            await wait_job(staff, job["id"])
            return job

        try:
            registry = await api(staff, ADMIN + "/model-registry")
            entry = next(e for e in registry["entries"] if e["capability"] == "DIALOG")
            candidate = await api(
                staff,
                ADMIN + "/model-profiles",
                "POST",
                {
                    "name": "[TEST ONLY] 模型切换验收候选",
                    "capability": "DIALOG",
                    "provider": entry["provider"],
                    "model_name": initial["model_name"],
                    "endpoint_ref": entry["endpoint_ref"],
                    "secret_ref": entry["secret_ref"],
                    "public_options": {"max_output_tokens": 512},
                },
                expected=201,
            )
            evidence["candidate_profile_id"] = candidate["id"]
            save()
            job = await test(candidate)
            evidence["candidate_compatibility_job_id"] = job["id"]
            passed("real_strict_tool_and_json_compatibility_job")
            conv = await api(
                member,
                MEMBER + "/conversations",
                "POST",
                {"title": "[TEST ONLY] 模型提示词切换验收", "entry_context": {"kind": "GENERAL"}},
                expected=201,
            )
            mid = str(uuid4())
            before = await api(
                member,
                MEMBER + f"/conversations/{conv['id']}/messages",
                "POST",
                {"client_message_id": mid, "text": "你好，请只回答：切换前验收通过。", "attachment_ids": []},
                expected=202,
            )
            candidate = await api(
                staff,
                ADMIN + f"/model-profiles/{candidate['id']}/activate",
                "POST",
                {"expected_version": candidate["version"], "compatibility_job_id": job["id"]},
            )
            activated = True
            prompt = await api(
                staff,
                ADMIN + "/prompt-versions",
                "POST",
                {
                    "prompt_key": "readonly-dialog",
                    "content": original_prompt["content"] + "\n本版验收附加要求：普通问候使用简短中文。",
                    "change_note": "[TEST ONLY] 发布与回滚验收，不更改工具权限",
                },
                expected=201,
            )
            await api(
                staff,
                ADMIN + f"/prompt-versions/{prompt['id']}/publish",
                "POST",
                {"expected_version": prompt["version"]},
            )
            published = True
            evidence.update(test_prompt_id=prompt["id"], before_run_id=before["run"]["id"])
            save()
            await wait_run(member, conv["id"])
            passed("queued_original_snapshot_finishes_after_active_switch")
            mid = str(uuid4())
            after = await api(
                member,
                MEMBER + f"/conversations/{conv['id']}/messages",
                "POST",
                {"client_message_id": mid, "text": "你好，请只回答：切换后验收通过。", "attachment_ids": []},
                expected=202,
            )
            await wait_run(member, conv["id"])
            async with db.transaction(snapshot=True) as conn:
                old = await one(
                    conn, "SELECT config_snapshot FROM ag_run WHERE id=:id", id=before["run"]["id"]
                )
                new = await one(
                    conn, "SELECT config_snapshot FROM ag_run WHERE id=:id", id=after["run"]["id"]
                )
                calls = await one(
                    conn,
                    "SELECT count(*) AS n FROM ag_model_call WHERE run_id=:id AND profile_id=:profile AND profile_version=:version AND status='SUCCEEDED'",
                    id=after["run"]["id"],
                    profile=candidate["id"],
                    version=candidate["version"],
                )
            assert old["config_snapshot"]["model_profiles"]["DIALOG"]["id"] == initial["id"]
            assert (
                new["config_snapshot"]["model_profiles"]["DIALOG"]["id"] == candidate["id"]
                and new["config_snapshot"]["prompt_version_id"] == prompt["id"]
            )
            assert old["config_snapshot"]["prompt_version_id"] == original_prompt["id"] and calls["n"] >= 2
            evidence["after_run_id"] = after["run"]["id"]
            passed("new_run_uses_new_model_and_prompt_versions_and_call_ledger")
        finally:
            if published:
                history = await api(staff, ADMIN + "/prompt-versions?prompt_key=readonly-dialog&limit=100")
                row = next(p for p in history["items"] if p["id"] == original_prompt["id"])
                await api(
                    staff,
                    ADMIN + f"/prompt-versions/{row['id']}/publish",
                    "POST",
                    {"expected_version": row["version"]},
                )
                evidence["original_prompt_restored"] = True
            if activated:
                row = next(p for p in await profiles() if p["id"] == initial["id"])
                job = await test(row)
                restored = await api(
                    staff,
                    ADMIN + f"/model-profiles/{row['id']}/activate",
                    "POST",
                    {"expected_version": row["version"], "compatibility_job_id": job["id"]},
                )
                evidence.update(
                    original_model_restored=True,
                    restored_profile_version=restored["version"],
                    restore_compatibility_job_id=job["id"],
                )
            if conv:
                snap = await api(member, MEMBER + f"/conversations/{conv['id']}")
                await api(
                    member,
                    MEMBER + f"/conversations/{conv['id']}",
                    "DELETE",
                    {"expected_version": snap["conversation"]["version"]},
                )
                evidence["test_conversation_deleted"] = True
            save()
            await db.engine.dispose()
        passed("original_model_and_prompt_restored_with_version_history")


if __name__ == "__main__":
    run(main())
