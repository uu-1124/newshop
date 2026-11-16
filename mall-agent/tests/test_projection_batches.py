from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event

from app.application.expiry import filter_events
from app.application.views import messages_projection
from app.infrastructure.postgres.database import Database, encoded, execute, one, rows
from app.infrastructure.request_metrics import record_request


@pytest.mark.anyio
async def test_large_replay_page_keeps_current_sources_expiry_protection_and_action_scope(integration_config):
    db = Database(integration_config["databaseUrl"])
    counter = [0]

    def count(*args):
        counter[0] += 1

    event.listen(db.engine.sync_engine, "before_cursor_execute", count)
    try:
        async with db.transaction() as conn:
            cids, did, vid, kid = [uuid4(), uuid4()], uuid4(), uuid4(), uuid4()
            for cid in cids:
                await execute(
                    conn,
                    "INSERT INTO ag_conversation(id,member_id,title,entry_context) VALUES(:id,201,'[TEST ONLY] projection','{}')",
                    id=cid,
                )
            await execute(
                conn,
                "INSERT INTO ag_kb_document(id,title,category,visibility,created_by) VALUES(:id,'projection','TEST_ONLY','MEMBER',201)",
                id=did,
            )
            await execute(
                conn,
                "INSERT INTO ag_kb_version(id,document_id,version_no,state,source_sha256,created_by,published_at) VALUES(:id,:doc,1,'READY',:hash,201,now())",
                id=vid,
                doc=did,
                hash="a" * 64,
            )
            await execute(
                conn, "UPDATE ag_kb_document SET active_version_id=:vid WHERE id=:id", vid=vid, id=did
            )
            await execute(
                conn,
                "INSERT INTO ag_kb_chunk(id,version_id,ordinal,chunk_kind,content) VALUES(:id,:vid,1,'PARENT','fixed policy')",
                id=kid,
                vid=vid,
            )
            aids = [uuid4() for _ in range(3)]
            for i, aid in enumerate(aids):
                await execute(
                    conn,
                    """INSERT INTO ag_attachment(id,owner_kind,owner_id,purpose,object_key,original_name,mime_type,byte_size,sha256,state,retention_until)
                  VALUES(:id,'MEMBER',201,'CHAT_IMAGE',:key,'test.jpg','image/jpeg',10,:hash,'READY',now()+make_interval(days=>:days))""",
                    id=aid,
                    key="test-only/" + str(aid),
                    hash="b" * 64,
                    days=90 if i == 0 else -1,
                )
            action_ids = [uuid4(), uuid4()]
            for i, aid in enumerate(action_ids):
                await execute(
                    conn,
                    """INSERT INTO ag_action(id,conversation_id,member_id,action_type,state,conversation_version,parameters,parameters_hash,operation_no,expires_at,control_epoch,quote_snapshot)
                  VALUES(:id,:cid,201,'ADD_CART','CANCELLED',1,'{}',:hash,:id,now()+interval '5 minutes',1,'{"verified":"current"}')""",
                    id=aid,
                    cid=cids[i],
                    hash="c" * 64,
                )
            source = {
                "document_id": str(did),
                "version_id": str(vid),
                "chunk_id": str(kid),
                "title": "source",
            }
            blocks = [{"type": "source", "source": source}]
            blocks += [{"type": "attachment", "attachment": {"id": str(aid)}} for aid in aids]
            blocks += [
                {
                    "type": "action",
                    "action": {"id": str(aid), "state": "SUCCEEDED", "quote": {"untrusted": "copied"}},
                }
                for aid in action_ids
            ]
            payload = [{"id": str(uuid4()), "seq": i + 1, "blocks": blocks} for i in range(30)]
            messages = await rows(
                conn,
                """INSERT INTO ag_message(id,conversation_id,seq,sender_kind,sender_id,status,text_content,blocks,retention_until)
              SELECT r.id,:cid,r.seq,'ASSISTANT',0,'COMPLETED','visible',r.blocks,now()+interval '90 days'
              FROM jsonb_to_recordset(CAST(:items AS jsonb)) AS r(id uuid,seq bigint,blocks jsonb) RETURNING *""",
                cid=cids[0],
                items=encoded(payload),
            )
            messages = sorted(messages, key=lambda m: m["seq"])
            await execute(
                conn,
                """INSERT INTO ag_kb_citation(message_id,document_id,version_id,chunk_id,content_sha256)
              SELECT id,:doc,:vid,:kid,encode(sha256(convert_to('fixed policy','UTF8')),'hex') FROM ag_message WHERE conversation_id=:cid""",
                doc=did,
                vid=vid,
                kid=kid,
                cid=cids[0],
            )
            await execute(
                conn,
                "INSERT INTO ag_attachment_ref(id,attachment_id,ref_kind,message_id,protected) VALUES(:id,:aid,'MESSAGE',:mid,true)",
                id=uuid4(),
                aid=aids[2],
                mid=messages[0]["id"],
            )
            counter[0] = 0
            projected = await messages_projection(conn, messages)
            assert counter[0] <= 3, "Projection queries must not grow with page size or repeated blocks"
            assert [m["id"] for m in projected] == [str(m["id"]) for m in messages]
            for message in projected:
                assert message["blocks"][0]["source"]["available"]
                assert [b["attachment"]["id"] for b in message["blocks"] if b["type"] == "attachment"] == [
                    str(aids[0]),
                    str(aids[2]),
                ]
                actions = [b["action"] for b in message["blocks"] if b["type"] == "action"]
                assert (
                    len(actions) == 1
                    and actions[0]["state"] == "CANCELLED"
                    and actions[0]["quote"] == {"verified": "current"}
                )
            await execute(conn, "UPDATE ag_kb_document SET enabled=false WHERE id=:id", id=did)
            assert all(
                not m["blocks"][0]["source"]["available"] for m in await messages_projection(conn, messages)
            )
            await execute(conn, "UPDATE ag_kb_document SET enabled=true WHERE id=:id", id=did)
            await execute(conn, "UPDATE ag_kb_chunk SET content='changed policy' WHERE id=:id", id=kid)
            assert all(
                not m["blocks"][0]["source"]["available"] for m in await messages_projection(conn, messages)
            )
            await execute(conn, "DELETE FROM ag_kb_citation WHERE message_id=:id", id=messages[-1]["id"])
            await execute(conn, "UPDATE ag_kb_chunk SET content='fixed policy' WHERE id=:id", id=kid)
            values = await messages_projection(conn, messages)
            assert (
                values[0]["blocks"][0]["source"]["available"]
                and not values[-1]["blocks"][0]["source"]["available"]
            )
            await execute(
                conn, "UPDATE ag_event_clock SET last_event_id=last_event_id+31 WHERE singleton_id=1"
            )
            clock = (await one(conn, "SELECT last_event_id FROM ag_event_clock WHERE singleton_id=1"))[
                "last_event_id"
            ]
            replay = []
            for i, message in enumerate(messages):
                # The event carries an outdated copy; current message/blocks must win.
                replay.append(
                    {
                        "id": clock - 30 + i,
                        "message": {"message": {"id": str(message["id"]), "text": "outdated"}},
                    }
                )
            events = await rows(
                conn,
                """INSERT INTO ag_event_outbox(event_id,conversation_id,conversation_version,event_type,payload,visibility,control_epoch,retention_until)
              SELECT r.id,:cid,1,'message.completed',r.message,'CONVERSATION',1,now()+interval '7 days'
              FROM jsonb_to_recordset(CAST(:items AS jsonb)) AS r(id bigint,message jsonb) RETURNING *""",
                cid=cids[0],
                items=encoded(replay),
            )
            await execute(
                conn,
                "UPDATE ag_message SET retention_until=now()-interval '1 second' WHERE id=:id",
                id=messages[0]["id"],
            )
            counter[0] = 0
            visible = await filter_events(conn, events)
            assert counter[0] <= 5, "Replay must not acquire or query per event"
            assert len(visible) == 29 and all(x["payload"]["message"]["text"] == "visible" for x in visible)
            assert str(messages[0]["id"]) not in {x["payload"]["message"]["id"] for x in visible}
            wrong = dict(events[1]) | {"conversation_id": cids[1]}
            assert await filter_events(conn, [wrong]) == []
            # Keep this generated fixture confined to the test transaction.
            await conn.rollback()
    finally:
        event.remove(db.engine.sync_engine, "before_cursor_execute", count)
        await db.engine.dispose()


@pytest.mark.anyio
async def test_atomic_request_metric_persists_and_pool_connection_returns_to_transaction_mode(
    integration_config,
):
    import time

    db = Database(integration_config["databaseUrl"])
    rid = uuid4()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                services=SimpleNamespace(
                    db=db, conversations=SimpleNamespace(settings=SimpleNamespace(governance_enabled=True))
                )
            )
        ),
        scope={"route": SimpleNamespace(operation_id="testAtomicMetric")},
        state=SimpleNamespace(request_id=str(rid)),
        method="GET",
    )
    try:
        await record_request(request, 200, time.perf_counter())
        async with db.transaction() as conn:
            row = await one(conn, "SELECT * FROM ag_request_metric WHERE request_id=:id", id=rid)
            assert row and row["status_code"] == 200 and row["operation"] == "testAtomicMetric"
            assert (await one(conn, "SELECT current_setting('transaction_isolation') value"))[
                "value"
            ] == "read committed"
            await execute(conn, "DELETE FROM ag_request_metric WHERE request_id=:id", id=rid)
        with pytest.raises(RuntimeError):
            async with db.transaction() as conn:
                await execute(
                    conn,
                    "INSERT INTO ag_setting(key,value,updated_by) VALUES(:key,'{}',0)",
                    key="rollback-" + str(rid),
                )
                raise RuntimeError("rollback probe")
        async with db.transaction() as conn:
            assert not await one(
                conn, "SELECT key FROM ag_setting WHERE key=:key", key="rollback-" + str(rid)
            )
        assert db.pool_metrics()["checked_out"] == 0
    finally:
        await db.engine.dispose()
