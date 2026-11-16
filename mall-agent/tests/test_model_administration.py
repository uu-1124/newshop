import asyncio
import json
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from test_handoffs import actor
from test_runtime import settings_for

from app.bootstrap import Services
from app.domain.identity import DomainError
from app.infrastructure.dialog_provider import ResponsesDialogProvider
from app.infrastructure.model_config import ADAPTER_VERSION, configuration_hash
from app.infrastructure.model_ledger import ModelLedger
from app.infrastructure.postgres.database import execute, one
from app.workers.jobs import execute_job

PERMISSIONS = {
    "agent:model:read",
    "agent:model:write",
    "agent:model:activate",
    "agent:prompt:read",
    "agent:prompt:write",
    "agent:prompt:publish",
    "agent:job:read",
    "agent:job:retry",
}


async def prepared(cfg):
    settings = settings_for(cfg).model_copy(
        update={
            "dialog_api_key": __import__("pydantic").SecretStr("unit-only-private-key"),
            "provider": "openai_responses",
        }
    )
    svc = Services(settings)
    await svc.models.setup()
    return svc, replace(actor(True), permissions=frozenset(PERMISSIONS))


def candidate(name="候选测试"):
    return {
        "name": name,
        "capability": "DIALOG",
        "provider": "gpt",
        "model_name": "unit-candidate-model",
        "endpoint_ref": "gpt.responses.v1",
        "secret_ref": "local-env:dialog.v1",
        "public_options": {"max_output_tokens": 512},
    }


@pytest.mark.anyio
async def test_profile_validation_permissions_pagination_and_redaction(integration_config):
    svc, staff = await prepared(integration_config)
    admin = svc.extensions.model_admin.require()
    try:
        with pytest.raises(DomainError) as forbidden:
            await admin.create(actor(True), uuid4(), candidate())
        assert forbidden.value.status == 403
        for body in (
            candidate() | {"endpoint_ref": "https://arbitrary.invalid/v1"},
            candidate() | {"secret_ref": "raw-secret"},
            candidate() | {"public_options": {"api_key": "secret"}},
            candidate() | {"public_options": {"retry_limit": 2}},
            candidate() | {"public_options": {"max_output_tokens": 50000}},
        ):
            with pytest.raises(DomainError):
                await admin.create(staff, uuid4(), body)
        key = uuid4()
        created = await admin.create(staff, key, candidate())
        assert (await admin.create(staff, key, candidate())) == created
        assert "secret_ref" not in created and created["secret_configured"]
        first = await admin.listing(staff, 1, None)
        assert first["has_more"]
        second = await admin.listing(staff, 1, first["next_cursor"])
        assert first["items"][0]["id"] != second["items"][0]["id"]
        with pytest.raises(DomainError):
            await admin.listing(replace(staff, id=staff.id + 1), 1, first["next_cursor"])
        history = await admin.listing(staff, 20, None, created["id"])
        assert history["items"][0]["compatibility_status"] == "UNVERIFIED"
        assert "unit-only-private" not in json.dumps(history, default=str)
        assert not any("secret" in k for k in history["items"][0])
        registry = await admin.registry(staff)
        assert all("://" not in entry["endpoint_ref"] for entry in registry["entries"])
    finally:
        await svc.close()


@pytest.mark.anyio
async def test_exact_evidence_activation_immutable_snapshot_restart_and_prompt_rollback(
    integration_config, monkeypatch
):
    svc, staff = await prepared(integration_config)
    admin = svc.extensions.model_admin.require()
    original = await svc.models.capture()
    old = await svc.models.for_snapshot(original)

    async def isolated_probe(cap, ledger):
        assert cap == "DIALOG" and ledger.settings.dialog_model == "unit-candidate-model"

    monkeypatch.setattr("app.infrastructure.model_compatibility.probe", isolated_probe)
    try:
        created = await admin.create(staff, uuid4(), candidate())
        with pytest.raises(DomainError) as missing:
            await admin.activate(
                staff, created["id"], uuid4(), {"expected_version": 1, "compatibility_job_id": str(uuid4())}
            )
        assert missing.value.code == "MODEL_COMPATIBILITY_REQUIRED"
        task = await admin.test(staff, created["id"], uuid4(), {"expected_version": 1})
        result = await execute_job(svc.jobs, svc.job_handlers, await svc.jobs.claim("model-test", task["id"]))
        assert result["state"] == "SUCCEEDED"
        active = await admin.activate(
            staff, created["id"], uuid4(), {"expected_version": 1, "compatibility_job_id": task["id"]}
        )
        assert active["is_active"] and active["version"] == 2
        snapshot = await svc.models.capture()
        new = await svc.models.for_snapshot(snapshot)
        assert (
            new.settings.dialog_model == "unit-candidate-model"
            and old.settings.dialog_model != new.settings.dialog_model
        )
        # Already queued snapshots resolve old immutable rows after the switch.
        retained = await svc.models.for_snapshot(original)
        assert retained.settings.dialog_model == old.settings.dialog_model
        restarted = ModelLedger(svc.db, svc.models.base_settings)
        await restarted.setup()
        assert restarted.profiles["DIALOG"]["id"] == created["id"]
        with pytest.raises(DomainError) as edit:
            await admin.update(
                staff,
                created["id"],
                uuid4(),
                {
                    "expected_version": 2,
                    **{
                        k: candidate()[k]
                        for k in ("name", "model_name", "endpoint_ref", "secret_ref", "public_options")
                    },
                    "enabled": True,
                },
            )
        assert edit.value.code == "MODEL_PROFILE_ACTIVE"
        # A provider call uses the captured model and captured output limit.
        seen = []

        def handler(req):
            body = json.loads(req.content)
            seen.append(body)
            return httpx.Response(
                200,
                content=b'data: {"type":"response.completed","response":{"status":"completed","output":[]}}\n\n',
            )

        async def record(data):
            return str(uuid4())

        provider = ResponsesDialogProvider(new.settings, httpx.MockTransport(handler))
        try:
            _ = [event async for event in provider.stream_turn([], "prompt", [], record)]
        finally:
            await provider.close()
        assert seen[0]["model"] == "unit-candidate-model" and seen[0]["max_output_tokens"] == 512
        prompt = await admin.create_prompt(
            staff,
            uuid4(),
            {"prompt_key": "readonly-dialog", "content": "测试提示词中文内容", "change_note": "隔离草稿"},
        )
        assert (await svc.models.capture())["prompt_version_id"] == original["prompt_version_id"]
        published = await admin.publish_prompt(staff, prompt["id"], uuid4(), {"expected_version": 1})
        assert published["state"] == "PUBLISHED"
        current = await svc.models.for_snapshot(await svc.models.capture())
        assert current.prompts["readonly-dialog"] == "测试提示词中文内容"
        assert (await svc.models.for_snapshot(original)).prompts["readonly-dialog"] == old.prompts[
            "readonly-dialog"
        ]
        async with svc.db.transaction() as conn:
            previous = await one(
                conn, "SELECT * FROM ag_prompt_version WHERE id=:id", id=original["prompt_version_id"]
            )
        await admin.publish_prompt(staff, previous["id"], uuid4(), {"expected_version": previous["version"]})
        assert (await svc.models.capture())["prompt_version_id"] == original["prompt_version_id"]
    finally:
        # Return the shared isolated DB to its original active model, preserving immutable versions.
        async with svc.db.transaction() as conn:
            await execute(conn, "UPDATE ag_model_profile SET is_active=false WHERE capability='DIALOG'")
            await execute(
                conn,
                "UPDATE ag_model_profile SET is_active=true,enabled=true WHERE id=:id",
                id=original["model_profiles"]["DIALOG"]["id"],
            )
        await svc.close()


@pytest.mark.anyio
async def test_old_evidence_cannot_activate_edited_configuration(integration_config, monkeypatch):
    svc, staff = await prepared(integration_config)
    admin = svc.extensions.model_admin.require()

    async def probe(cap, ledger):
        pass

    monkeypatch.setattr("app.infrastructure.model_compatibility.probe", probe)
    try:
        created = await admin.create(staff, uuid4(), candidate())
        task = await admin.test(staff, created["id"], uuid4(), {"expected_version": 1})
        await execute_job(svc.jobs, svc.job_handlers, await svc.jobs.claim("model-test", task["id"]))
        await admin.update(
            staff,
            created["id"],
            uuid4(),
            {
                "expected_version": 1,
                **{k: candidate()[k] for k in ("name", "model_name", "endpoint_ref", "secret_ref")},
                "public_options": {"max_output_tokens": 1024},
                "enabled": True,
            },
        )
        with pytest.raises(DomainError) as stale:
            await admin.activate(
                staff, created["id"], uuid4(), {"expected_version": 2, "compatibility_job_id": task["id"]}
            )
        assert stale.value.code == "MODEL_COMPATIBILITY_REQUIRED"
        async with svc.db.transaction() as conn:
            record = await one(conn, "SELECT * FROM ag_model_profile WHERE id=:id", id=created["id"])
            job = await one(conn, "SELECT parameters FROM ag_job WHERE id=:id", id=task["id"])
        assert configuration_hash(record) != job["parameters"]["configuration_sha256"]
        assert ADAPTER_VERSION
    finally:
        await svc.close()


@pytest.mark.anyio
async def test_parallel_run_capture_reuses_command_connection(integration_config):
    svc, _ = await prepared(integration_config)
    member = actor()
    runs = []
    try:
        conversations = [
            await svc.conversations.create(
                member,
                uuid4(),
                {"entry_context": {"kind": "GENERAL"}, "title": "[TEST ONLY] concurrent config capture"},
            )
            for _ in range(8)
        ]
        accepted = await asyncio.wait_for(
            asyncio.gather(
                *[
                    svc.conversations.send(
                        member,
                        "unit",
                        conv["id"],
                        uuid4(),
                        {
                            "client_message_id": str(uuid4()),
                            "text": "concurrent snapshot",
                            "attachment_ids": [],
                        },
                    )
                    for conv in conversations
                ]
            ),
            timeout=120,
        )
        runs = [item["run"] for item in accepted]
        assert len(runs) == 8
        for item in runs:
            async with svc.db.transaction() as conn:
                row = await one(conn, "SELECT config_snapshot FROM ag_run WHERE id=:id", id=item["id"])
                assert row["config_snapshot"]["model_profiles"]["DIALOG"]["id"]
    finally:
        for item in runs:
            await svc.runs.stop(member, item["id"], uuid4(), {"expected_version": item["version"]})
        await svc.close()
