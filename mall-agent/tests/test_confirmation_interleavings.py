"""Real PG row-lock waits prove both orders of confirm/new-member-message races."""

import asyncio
from uuid import uuid4

import pytest
from test_actions import settings
from test_preparation_revisions import CASES, RevisionBridge

from app.bootstrap import Services
from app.domain.identity import DomainError, Principal
from app.infrastructure.postgres.database import one


async def lock_wait(svc, task):
    for _ in range(50):
        async with svc.db.transaction() as conn:
            blocked = await one(
                conn,
                "SELECT count(*) n FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE :pattern",
                pattern="%SELECT * FROM ag_conversation%",
            )
        if blocked["n"] and not task.done():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("The contender must actually wait on a real conversation row lock")


@pytest.mark.anyio
@pytest.mark.parametrize("parameters", CASES, ids=[x["action_type"] for x in CASES])
@pytest.mark.parametrize("first", ["message", "confirm"])
async def test_confirm_and_member_message_serialize_without_replaying_business(
    integration_config, monkeypatch, parameters, first
):
    from app.application import actions as actions_module
    from app.application import conversations as conversations_module

    svc = Services(settings(integration_config).model_copy(update={"orders_enabled": True}))
    buyer = Principal(1300000000 + uuid4().int % 100000000, "racing buyer")
    bridge = RevisionBridge(buyer)
    actions = svc.extensions.actions.require()
    actions.bridge = bridge
    locked, commit, executing, finish_business = (asyncio.Event() for _ in range(4))
    tasks = []
    cid = None
    original_action_execute = actions_module.execute
    original_conv_rows = conversations_module.rows
    original_business = bridge.execute_action

    async def pause_confirm(conn, sql, **params):
        result = await original_action_execute(conn, sql, **params)
        if sql.lstrip().startswith("INSERT INTO ag_audit_log") and params.get("operation") == "confirmAction":
            locked.set()
            await commit.wait()
        return result

    async def pause_message(conn, sql, **params):
        result = await original_conv_rows(conn, sql, **params)
        if sql.startswith("UPDATE ag_action SET state='STALE',error_code='MEMBER_CONTEXT_CHANGED'"):
            locked.set()
            await commit.wait()
        return result

    async def business(token, body):
        executing.set()
        await finish_business.wait()
        return await original_business(token, body)

    try:
        conv = await svc.conversations.create(
            buyer,
            str(uuid4()),
            {"title": "[TEST ONLY] confirmation interleaving", "entry_context": {"kind": "GENERAL"}},
        )
        cid = conv["id"]
        action = await actions.prepare(
            buyer,
            "synthetic-only",
            cid,
            str(uuid4()),
            {"expected_conversation_version": conv["version"], "parameters": parameters},
        )
        if first == "confirm":
            monkeypatch.setattr(actions_module, "execute", pause_confirm)
        else:
            monkeypatch.setattr(conversations_module, "rows", pause_message)
        bridge.execute_action = business

        async def confirm():
            return await actions.confirm(
                buyer, "synthetic-only", action["id"], str(uuid4()), {"expected_version": action["version"]}
            )

        async def message():
            return await svc.conversations.send(
                buyer,
                "synthetic-only",
                cid,
                str(uuid4()),
                {
                    "client_message_id": str(uuid4()),
                    "text": "修改这次选择，旧卡不再使用",
                    "attachment_ids": [],
                },
            )

        winner = asyncio.create_task(confirm() if first == "confirm" else message())
        tasks.append(winner)
        await asyncio.wait_for(locked.wait(), 20)
        contender = asyncio.create_task(message() if first == "confirm" else confirm())
        tasks.append(contender)
        await lock_wait(svc, contender)
        commit.set()
        if first == "message":
            await asyncio.wait_for(winner, 20)
            with pytest.raises(DomainError) as rejected:
                await asyncio.wait_for(contender, 20)
            assert rejected.value.code in {"VERSION_CONFLICT", "ACTION_STATE_CONFLICT"}
            assert not executing.is_set() and not bridge.calls
            expected = "STALE"
            dispatches = 0
        else:
            await asyncio.wait_for(executing.wait(), 20)
            await asyncio.wait_for(contender, 20)
            assert (await actions.get(buyer, action["id"]))["state"] == "EXECUTING"
            finish_business.set()
            result = await asyncio.wait_for(winner, 20)
            assert result["state"] == "SUCCEEDED" and len(bridge.calls) == 1
            assert bridge.calls[0]["parameters"] == parameters
            expected = "SUCCEEDED"
            dispatches = 1
        async with svc.db.transaction() as conn:
            current = await one(conn, "SELECT state,parameters FROM ag_action WHERE id=:id", id=action["id"])
            assert current["state"] == expected and current["parameters"] == parameters
            counts = await one(
                conn,
                "SELECT (SELECT count(*) FROM ag_action_dispatch WHERE action_id=:id) dispatches,(SELECT count(*) FROM ag_audit_log WHERE resource_id=:resource AND operation='confirmAction') audits",
                id=action["id"],
                resource=action["id"],
            )
            assert counts == {"dispatches": dispatches, "audits": dispatches}
    finally:
        commit.set()
        finish_business.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        if cid:
            snapshot = await svc.conversations.snapshot(buyer, cid)
            active = snapshot["active_run"]
            if active and active["state"] == "QUEUED":
                await svc.runs.stop(
                    buyer, active["id"], str(uuid4()), {"expected_version": active["version"]}
                )
        await svc.close()
