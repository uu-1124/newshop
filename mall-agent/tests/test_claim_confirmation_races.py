"""Real PG serialization of staff claim and the four buyer confirmations."""

import asyncio
from uuid import uuid4

import pytest
from test_action_deadlines import fixture, no_dispatch, waiting_transaction
from test_handoffs import actor, online
from test_preparation_revisions import CASES

from app.domain.identity import DomainError


@pytest.mark.anyio
@pytest.mark.parametrize("parameters", CASES, ids=[x["action_type"] for x in CASES])
@pytest.mark.parametrize("first", ["claim", "confirm"])
async def test_staff_claim_and_confirmation_serialize_without_losing_started_business(
    integration_config, monkeypatch, parameters, first
):
    from app.application import actions as module

    svc, buyer, actions, bridge, conv, _ = await fixture(integration_config, parameters)
    h, staff = svc.extensions.handoffs.require(), actor(True)
    locked, release, executing, finish = (asyncio.Event() for _ in range(4))
    tasks = []
    original_audit, original_fence, original_business = module.audit, h._fence, bridge.execute_action

    async def paused_audit(conn, principal, operation, *args, **kwargs):
        result = await original_audit(conn, principal, operation, *args, **kwargs)
        if operation == "confirmAction":
            locked.set()
            await release.wait()
        return result

    async def paused_claim(conn, current):
        result = await original_fence(conn, current)
        if current["state"] == "HUMAN_ACTIVE":
            locked.set()
            await release.wait()
        return result

    async def business(token, body):
        executing.set()
        await finish.wait()
        return await original_business(token, body)

    try:
        await online(h, staff)
        snap = await svc.conversations.snapshot(buyer, conv["id"])
        waiting = await h.request(
            buyer,
            conv["id"],
            str(uuid4()),
            {"expected_conversation_version": snap["conversation"]["version"], "reason": "claim race"},
        )
        snap = await svc.conversations.snapshot(buyer, conv["id"])
        await h.waiting(
            buyer,
            conv["id"],
            str(uuid4()),
            {"expected_version": snap["conversation"]["version"], "enabled": True},
        )
        snap = await svc.conversations.snapshot(buyer, conv["id"])
        action = await actions.prepare(
            buyer,
            "synthetic-only",
            conv["id"],
            str(uuid4()),
            {"expected_conversation_version": snap["conversation"]["version"], "parameters": parameters},
        )
        if first == "claim":
            monkeypatch.setattr(h, "_fence", paused_claim)
        else:
            monkeypatch.setattr(module, "audit", paused_audit)
        bridge.execute_action = business

        async def confirm():
            return await actions.confirm(
                buyer, "synthetic-only", action["id"], str(uuid4()), {"expected_version": action["version"]}
            )

        async def claim():
            return await h.command(
                staff, waiting["id"], str(uuid4()), {"expected_version": waiting["version"]}, "claim"
            )

        winner = asyncio.create_task(claim() if first == "claim" else confirm())
        tasks.append(winner)
        await asyncio.wait_for(locked.wait(), 20)
        contender = asyncio.create_task(confirm() if first == "claim" else claim())
        tasks.append(contender)
        await waiting_transaction(svc, contender, "ag_conversation")
        release.set()
        if first == "claim":
            taken = await asyncio.wait_for(winner, 20)
            with pytest.raises(DomainError) as error:
                await asyncio.wait_for(contender, 20)
            assert error.value.code in {"VERSION_CONFLICT", "ACTION_STATE_CONFLICT"}
            assert not executing.is_set() and not bridge.calls
            assert (await actions.get(buyer, action["id"]))["state"] == "STALE"
            await no_dispatch(svc, action)
        else:
            await asyncio.wait_for(executing.wait(), 20)
            taken = await asyncio.wait_for(contender, 20)
            assert (await actions.get(buyer, action["id"]))["state"] == "EXECUTING"
            finish.set()
            assert (await asyncio.wait_for(winner, 20))["state"] == "SUCCEEDED"
            assert len(bridge.calls) == 1 and bridge.calls[0]["parameters"] == parameters
        assert (await svc.conversations.snapshot(buyer, conv["id"]))["conversation"][
            "state"
        ] == "HUMAN_ACTIVE"
        await h.command(staff, taken["id"], str(uuid4()), {"expected_version": taken["version"]}, "close")
    finally:
        release.set()
        finish.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await svc.close()
