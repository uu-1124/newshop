import asyncio
from uuid import uuid4

import pytest
from test_handoffs import actor, claim, conversation, online, request
from test_runtime import settings_for

from app.bootstrap import Services
from app.infrastructure.postgres.database import execute, one


@pytest.mark.anyio
async def test_reply_to_another_conversation_does_not_wait_for_same_staff_blocked_reply(integration_config):
    svc = Services(settings_for(integration_config, handoffs_enabled=True))
    h = svc.extensions.handoffs.require()
    member, staff = actor(), actor(True)
    acquired = asyncio.Event()
    attempted = asyncio.Event()
    original = h._staff_locks
    tasks = []

    def body():
        return {
            "client_message_id": str(uuid4()),
            "text": "[TEST ONLY] independent reply",
            "attachment_ids": [],
            "cards": [],
        }

    async def staff_locks(conn, ids, shared=False):
        attempted.set()
        result = await original(conn, ids, shared=shared)
        if tasks and asyncio.current_task() is tasks[0]:
            acquired.set()
        return result

    try:
        await online(h, staff)
        a = await conversation(svc, member)
        b = await conversation(svc, member)
        await claim(h, staff, await request(svc, member, a))
        await claim(h, staff, await request(svc, member, b))
        h._staff_locks = staff_locks
        async with svc.db.transaction() as conn:
            await one(
                conn, "SELECT id FROM ag_conversation WHERE id=CAST(:id AS uuid) FOR UPDATE", id=a["id"]
            )
            tasks.append(asyncio.create_task(h.send(staff, a["id"], str(uuid4()), body())))
            await asyncio.wait_for(acquired.wait(), 15)
            second = asyncio.create_task(h.send(staff, b["id"], str(uuid4()), body()))
            tasks.append(second)
            reply = await asyncio.wait_for(second, 15)
            assert reply["conversation_id"] == b["id"] and not tasks[0].done()
        first = await asyncio.wait_for(tasks[0], 15)
        assert first["conversation_id"] == a["id"]
        # Profile edits keep the exclusive lock: replies must wait for their new display name.
        async with svc.db.transaction() as conn:
            await one(
                conn, "SELECT admin_id FROM ag_staff_profile WHERE admin_id=:id FOR UPDATE", id=staff.id
            )
            await execute(
                conn,
                "UPDATE ag_staff_profile SET display_name='[TEST ONLY] edited' WHERE admin_id=:id",
                id=staff.id,
            )
            attempted.clear()
            pending = asyncio.create_task(h.send(staff, b["id"], str(uuid4()), body()))
            tasks.append(pending)
            await asyncio.wait_for(attempted.wait(), 15)
            assert not pending.done()
        assert (await asyncio.wait_for(pending, 15))["sender_display_name"] == "[TEST ONLY] edited"
    finally:
        h._staff_locks = original
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await svc.close()
