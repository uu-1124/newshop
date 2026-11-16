"""Elapsed deadlines and invalid historical cards never authorize a dispatch.

All storage is the dedicated integration database; Java is an explicit synthetic
bridge. Row-lock waits and elapsed time are observed in PostgreSQL, not mocked.
"""

import asyncio
from uuid import uuid4

import pytest
from test_actions import settings
from test_preparation_revisions import CASES, RevisionBridge

from app.bootstrap import Services
from app.domain.identity import DomainError, Principal
from app.infrastructure.postgres.database import execute, one


async def waiting_transaction(svc, task, table):
    for _ in range(50):
        async with svc.db.transaction() as observer:
            waiting = await one(
                observer,
                "SELECT xact_start FROM pg_stat_activity WHERE datname=current_database() "
                "AND wait_event_type='Lock' AND query LIKE :pattern ORDER BY xact_start LIMIT 1",
                pattern=f"%FROM {table} WHERE id=%",
            )
        if waiting and not task.done():
            return waiting["xact_start"]
        await asyncio.sleep(0.05)
    raise AssertionError("The command did not wait on the expected real row lock")


async def elapsed(conn, deadline):
    for _ in range(100):
        row = await one(conn, "SELECT clock_timestamp() AS current")
        remaining = (deadline - row["current"]).total_seconds()
        if remaining <= 0:
            return row["current"]
        await asyncio.sleep(min(remaining + 0.01, 0.5))
    raise AssertionError("The actual database deadline did not elapse")


async def fixture(cfg, parameters):
    svc = Services(settings(cfg).model_copy(update={"orders_enabled": True}))
    buyer = Principal(1400000000 + uuid4().int % 100000000, "deadline test buyer")
    actions = svc.extensions.actions.require()
    bridge = RevisionBridge(buyer)
    actions.bridge = bridge
    conv = await svc.conversations.create(
        buyer,
        str(uuid4()),
        {"title": "[TEST ONLY] confirmation deadline", "entry_context": {"kind": "GENERAL"}},
    )
    action = await actions.prepare(
        buyer,
        "synthetic-only",
        conv["id"],
        str(uuid4()),
        {"expected_conversation_version": conv["version"], "parameters": parameters},
    )
    return svc, buyer, actions, bridge, conv, action


async def no_dispatch(svc, action):
    async with svc.db.transaction() as conn:
        counts = await one(
            conn,
            "SELECT (SELECT count(*) FROM ag_action_dispatch WHERE action_id=:id) dispatches,"
            "(SELECT count(*) FROM ag_audit_log WHERE resource_id=:resource AND operation='confirmAction') audits",
            id=action["id"],
            resource=action["id"],
        )
    assert counts == {"dispatches": 0, "audits": 0}


@pytest.mark.anyio
@pytest.mark.parametrize("parameters", CASES, ids=[x["action_type"] for x in CASES])
@pytest.mark.parametrize("gate", ["conversation", "action"])
async def test_card_expired_while_waiting_for_real_lock_cannot_dispatch(integration_config, parameters, gate):
    svc, buyer, actions, bridge, conv, action = await fixture(integration_config, parameters)
    task = None
    try:
        # Install a near future deadline before the confirmation transaction.
        async with svc.db.transaction() as conn:
            deadline = (
                await one(
                    conn,
                    "UPDATE ag_action SET expires_at=clock_timestamp()+interval '4 seconds' WHERE id=:id RETURNING expires_at",
                    id=action["id"],
                )
            )["expires_at"]
        async with svc.db.transaction() as blocker:
            table = "ag_conversation" if gate == "conversation" else "ag_action"
            await one(
                blocker,
                f"SELECT id FROM {table} WHERE id=:id FOR UPDATE",
                id=conv["id"] if gate == "conversation" else action["id"],
            )
            task = asyncio.create_task(
                actions.confirm(
                    buyer,
                    "synthetic-only",
                    action["id"],
                    str(uuid4()),
                    {"expected_version": action["version"]},
                )
            )
            began = await waiting_transaction(svc, task, table)
            released = await elapsed(blocker, deadline)
            assert began < deadline < released
        with pytest.raises(DomainError, match="ACTION_STATE_CONFLICT"):
            await asyncio.wait_for(task, 20)
        assert not bridge.calls
        await no_dispatch(svc, action)
        current = await actions.get(buyer, action["id"])
        assert current["state"] == "EXPIRED"
        async with svc.db.transaction() as conn:
            row = await one(conn, "SELECT state,confirmed_at FROM ag_action WHERE id=:id", id=action["id"])
            assert row == {"state": "EXPIRED", "confirmed_at": None}
    finally:
        if task and not task.done():
            task.cancel()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await svc.close()


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["legacy", "missing", "order", "amount", "number", "paid"])
async def test_historical_pending_cancel_requires_trusted_summary_before_dispatch(integration_config, change):
    svc, buyer, actions, bridge, _, action = await fixture(integration_config, CASES[2])
    try:
        # Historical projection fixture only. New prepare already rejects these.
        quote = action["quote"]
        if change == "legacy":
            quote["rule_version"] = "CANCEL_PENDING_V1"
        elif change == "missing":
            del quote["cancellation"]
        elif change == "order":
            quote["cancellation"]["id"] = "41"
        elif change == "amount":
            quote["cancellation"]["pay_amount"] = "0.01"
        elif change == "number":
            quote["cancellation"]["order_sn"] = " "
        else:
            quote["cancellation"]["status"] = 1
        from app.infrastructure.postgres.database import encoded

        async with svc.db.transaction() as conn:
            await execute(
                conn,
                "UPDATE ag_action SET quote_snapshot=CAST(:quote AS jsonb) WHERE id=:id",
                id=action["id"],
                quote=encoded(quote),
            )
        with pytest.raises(DomainError, match="ACTION_STATE_CONFLICT"):
            await actions.confirm(
                buyer, "synthetic-only", action["id"], str(uuid4()), {"expected_version": action["version"]}
            )
        assert not bridge.calls
        await no_dispatch(svc, action)
    finally:
        await svc.close()


@pytest.mark.anyio
@pytest.mark.parametrize("parameters", CASES, ids=[x["action_type"] for x in CASES])
async def test_deadline_is_checked_again_at_execution_transition(integration_config, monkeypatch, parameters):
    from app.application import actions as module

    svc, buyer, actions, bridge, _, action = await fixture(integration_config, parameters)
    reached, resume = asyncio.Event(), asyncio.Event()
    original = module.rows
    task = None

    async def delayed_read(conn, sql, **params):
        result = await original(conn, sql, **params)
        if sql.startswith("SELECT a.*,r.id AS ref_id"):
            reached.set()
            await resume.wait()
        return result

    try:
        async with svc.db.transaction() as conn:
            deadline = (
                await one(
                    conn,
                    "UPDATE ag_action SET expires_at=clock_timestamp()+interval '4 seconds' WHERE id=:id RETURNING expires_at",
                    id=action["id"],
                )
            )["expires_at"]
        monkeypatch.setattr(module, "rows", delayed_read)
        task = asyncio.create_task(
            actions.confirm(
                buyer, "synthetic-only", action["id"], str(uuid4()), {"expected_version": action["version"]}
            )
        )
        await asyncio.wait_for(reached.wait(), 20)
        async with svc.db.transaction() as conn:
            now = (await one(conn, "SELECT clock_timestamp() AS current"))["current"]
            assert now < deadline
            await elapsed(conn, deadline)
        resume.set()
        with pytest.raises(DomainError, match="ACTION_STATE_CONFLICT"):
            await asyncio.wait_for(task, 20)
        assert not bridge.calls
        await no_dispatch(svc, action)
    finally:
        resume.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await svc.close()


@pytest.mark.anyio
@pytest.mark.parametrize("parameters", CASES, ids=[x["action_type"] for x in CASES])
async def test_preparation_expiring_behind_member_lock_preserves_previous_card(
    integration_config, parameters
):
    svc, buyer, actions, bridge, conv, previous = await fixture(integration_config, parameters)
    task = None
    original = bridge.quote
    deadline = None

    async def short_quote(*args):
        nonlocal deadline
        result = await original(*args)
        async with svc.db.transaction() as conn:
            deadline = (await one(conn, "SELECT clock_timestamp()+interval '4 seconds' AS deadline"))[
                "deadline"
            ]
        return result | {"expires_at": deadline.isoformat()}

    bridge.quote = short_quote
    try:
        snap = await svc.conversations.snapshot(buyer, conv["id"])
        async with svc.db.transaction() as blocker:
            await execute(blocker, "SELECT pg_advisory_xact_lock(:member)", member=buyer.id)
            task = asyncio.create_task(
                actions.prepare(
                    buyer,
                    "synthetic-only",
                    conv["id"],
                    str(uuid4()),
                    {
                        "expected_conversation_version": snap["conversation"]["version"],
                        "parameters": parameters,
                    },
                )
            )
            for _ in range(50):
                async with svc.db.transaction() as observer:
                    row = await one(
                        observer,
                        "SELECT xact_start FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE '%pg_advisory_xact_lock%' ORDER BY xact_start LIMIT 1",
                    )
                if row and deadline is not None and not task.done():
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("The replacement did not wait for the member lock")
            assert row["xact_start"] < deadline
            await elapsed(blocker, deadline)
        with pytest.raises(DomainError, match="QUOTE_EXPIRED"):
            await asyncio.wait_for(task, 20)
        after = await svc.conversations.snapshot(buyer, conv["id"])
        assert after["conversation"]["version"] == snap["conversation"]["version"]
        assert len(after["recent_messages"]) == len(snap["recent_messages"])
        assert (await actions.get(buyer, previous["id"]))["state"] == "PENDING_CONFIRMATION"
        assert not bridge.calls
        async with svc.db.transaction() as conn:
            assert (
                await one(conn, "SELECT count(*) n FROM ag_action WHERE conversation_id=:id", id=conv["id"])
            )["n"] == 1
    finally:
        if task and not task.done():
            task.cancel()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await svc.close()


@pytest.mark.anyio
@pytest.mark.parametrize("parameters", CASES, ids=[x["action_type"] for x in CASES])
@pytest.mark.parametrize("transition", ["cancel", "handoff"])
async def test_cancelled_or_taken_over_cards_cannot_be_revived(integration_config, parameters, transition):
    from test_handoffs import actor, online

    svc, buyer, actions, bridge, conv, action = await fixture(integration_config, parameters)
    try:
        if transition == "cancel":
            key, body = str(uuid4()), {"expected_version": action["version"]}
            current = await actions.cancel(buyer, action["id"], key, body)
            replay = await actions.cancel(buyer, action["id"], key, body)
            assert replay["id"] == current["id"] and replay["state"] == "CANCELLED"
        else:
            h = svc.extensions.handoffs.require()
            staff = actor(True)
            await online(h, staff)
            snap = await svc.conversations.snapshot(buyer, conv["id"])
            waiting = await h.request(
                buyer,
                conv["id"],
                str(uuid4()),
                {
                    "expected_conversation_version": snap["conversation"]["version"],
                    "reason": "deadline matrix",
                },
            )
            taken = await h.command(
                staff, waiting["id"], str(uuid4()), {"expected_version": waiting["version"]}, "claim"
            )
            current = await actions.get(buyer, action["id"])
            assert current["state"] == "STALE"
            snap = await svc.conversations.snapshot(buyer, conv["id"])
            assert snap["conversation"]["state"] == "HUMAN_ACTIVE"
            await h.command(staff, taken["id"], str(uuid4()), {"expected_version": taken["version"]}, "close")
        for version in [action["version"], current["version"]]:
            with pytest.raises(DomainError) as error:
                await actions.confirm(
                    buyer, "synthetic-only", action["id"], str(uuid4()), {"expected_version": version}
                )
            assert error.value.code in {"VERSION_CONFLICT", "ACTION_STATE_CONFLICT"}
        assert not bridge.calls
        await no_dispatch(svc, action)
    finally:
        await svc.close()
