import asyncio
from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError
from test_runtime import settings_for

from app.bootstrap import Services
from app.domain.identity import Principal
from app.infrastructure.postgres.database import append_events, one, rows


@pytest.mark.anyio
async def test_batched_event_clock_has_contiguous_order_empty_batch_and_rollback(integration_config):
    svc = Services(settings_for(integration_config))
    member = Principal(2200000000 + uuid4().int % 100000000, "event batch buyer")
    held = asyncio.Event()
    release = asyncio.Event()
    try:
        left = await svc.conversations.create(
            member, str(uuid4()), {"title": "[TEST ONLY] events A", "entry_context": {"kind": "GENERAL"}}
        )
        right = await svc.conversations.create(
            member, str(uuid4()), {"title": "[TEST ONLY] events B", "entry_context": {"kind": "GENERAL"}}
        )
        async with svc.db.transaction() as conn:
            before = (await one(conn, "SELECT last_event_id FROM ag_event_clock WHERE singleton_id=1"))[
                "last_event_id"
            ]
            assert await append_events(conn, left, []) == str(before)
            cursor = await append_events(conn, left, [("batch.test", {"position": i}) for i in range(25)])
            assert int(cursor) == before + 25
            events = await rows(
                conn,
                "SELECT event_id,payload FROM ag_event_outbox WHERE event_id>:start ORDER BY event_id",
                start=before,
            )
            assert [e["event_id"] for e in events] == list(range(before + 1, before + 26))
            assert [e["payload"]["position"] for e in events] == list(range(25))
            with pytest.raises(DBAPIError):
                async with conn.begin_nested():
                    await append_events(conn, left, [("valid.test", {}), ("x" * 1000, {})])
            assert (await one(conn, "SELECT last_event_id FROM ag_event_clock WHERE singleton_id=1"))[
                "last_event_id"
            ] == before + 25

        async def first():
            async with svc.db.transaction() as conn:
                cursor = await append_events(conn, left, [("batch.first", {"position": i}) for i in range(8)])
                held.set()
                await release.wait()
                return int(cursor)

        async def second():
            await held.wait()
            async with svc.db.transaction() as conn:
                return int(await append_events(conn, right, [("batch.second", {})]))

        a = asyncio.create_task(first())
        b = asyncio.create_task(second())
        try:
            await asyncio.wait_for(held.wait(), 15)
            await asyncio.sleep(0.3)
            assert not b.done()
            release.set()
            first_id, second_id = await asyncio.wait_for(asyncio.gather(a, b), 30)
            assert second_id == first_id + 1
        finally:
            release.set()
            await asyncio.gather(a, b, return_exceptions=True)
    finally:
        await svc.close()
