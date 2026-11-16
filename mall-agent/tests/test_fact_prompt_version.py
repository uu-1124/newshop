from uuid import uuid4

import pytest
from test_model_administration import prepared

from app.infrastructure.postgres.database import one


@pytest.mark.anyio
async def test_fact_selector_is_immutable_run_snapshot_and_call_ledger_use_actual_prompt_version(
    integration_config,
):
    svc, staff = await prepared(integration_config)
    admin = svc.extensions.model_admin.require()
    original = await svc.models.capture()
    assert original["fact_prompt_version_id"]
    try:
        frozen = await svc.models.for_snapshot(original)
        assert frozen.fact_prompt_id == original["fact_prompt_version_id"]
        initial = frozen.prompts["fact-selector"]
        draft = await admin.create_prompt(
            staff,
            uuid4(),
            {
                "prompt_key": "fact-selector",
                "content": initial + "\n隔离测试：优先保留限定条件。",
                "change_note": "[TEST ONLY] selector version freeze",
            },
        )
        await admin.publish_prompt(staff, draft["id"], uuid4(), {"expected_version": draft["version"]})
        latest = await svc.models.for_snapshot(await svc.models.capture())
        assert latest.fact_prompt_id != frozen.fact_prompt_id
        assert (await svc.models.for_snapshot(original)).prompts["fact-selector"] == initial
        record = frozen.recorder("DIALOG", prompt_id=frozen.fact_prompt_id)
        call_id = await record({"stage": "reserve", "reservation_units": {"REQUEST": 1, "TOKEN": 10}})
        await record(
            {
                "call_id": call_id,
                "status": "SUCCEEDED",
                "usage": {"input_tokens": 2, "output_tokens": 1},
                "latency_ms": 1,
                "provider_request_id": "synthetic-fact-prompt-only",
                "error_code": None,
            }
        )
        async with svc.db.transaction() as conn:
            row = await one(conn, "SELECT prompt_version_id FROM ag_model_call WHERE id=:id", id=call_id)
        assert str(row["prompt_version_id"]) == original["fact_prompt_version_id"]
    finally:
        async with svc.db.transaction() as conn:
            prior = await one(
                conn,
                "SELECT id,version FROM ag_prompt_version WHERE id=CAST(:id AS uuid)",
                id=original["fact_prompt_version_id"],
            )
        await admin.publish_prompt(staff, prior["id"], uuid4(), {"expected_version": prior["version"]})
        await svc.close()
