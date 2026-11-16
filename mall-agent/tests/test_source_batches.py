import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import event

from app.application.retrieval import current_sources
from app.application.return_knowledge import (
    CATALOG_KEY,
    begin_rule_sync,
    finish_rule_sync,
    refresh_rule_sync,
    validate_rules,
)
from app.domain.identity import DomainError
from app.infrastructure.postgres.database import Database, encoded, execute, one


def rules(version=1, future=False):
    return {
        "items": [
            {
                "version_no": v,
                "rule_version": "ITEM_NET_168H_DEMO_V1" if v == 1 else f"ITEM_NET_DEMO_V{v}",
                "effective_at": "2099-01-01T00:00:00Z" if future and v == version else "2000-01-01T00:00:00Z",
            }
            for v in range(1, version + 1)
        ]
    }


@pytest.mark.anyio
async def test_sources_batch_preserves_hash_scope_order_and_live_rule_switch(integration_config):
    db = Database(integration_config["databaseUrl"])
    try:
        async with db.engine.connect() as conn:
            async with conn.begin():
                for table in ["ag_setting", "ag_kb_document", "ag_kb_version", "ag_kb_chunk"]:
                    await execute(
                        conn,
                        f"CREATE TEMP TABLE {table} (LIKE public.{table} INCLUDING DEFAULTS) ON COMMIT DROP",
                    )
                did, vid, cid = [uuid4() for _ in range(3)]
                text = "当前规则 ITEM_NET_168H_DEMO_V1，收货后168小时。"
                await execute(
                    conn,
                    "INSERT INTO ag_kb_document(id,title,category,visibility,scope,active_version_id,created_by) VALUES(:id,'rule','TEST','MEMBER',:scope,:vid,1)",
                    id=did,
                    vid=vid,
                    scope=encoded({"product_ids": ["45"], "category_ids": []}),
                )
                await execute(
                    conn,
                    "INSERT INTO ag_kb_version(id,document_id,version_no,state,parsed_text,source_sha256,published_at,created_by) VALUES(:id,:did,1,'READY',:text,:sha,now(),1)",
                    id=vid,
                    did=did,
                    text=text,
                    sha="0" * 64,
                )
                await execute(
                    conn,
                    "INSERT INTO ag_kb_chunk(id,version_id,ordinal,chunk_kind,content) VALUES(:id,:vid,1,'CHILD',:text)",
                    id=cid,
                    vid=vid,
                    text=text,
                )
                ref = {
                    "document_id": str(did),
                    "version_id": str(vid),
                    "chunk_id": str(cid),
                    "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "scope_context": {"product_id": "45", "category_id": "1"},
                }
                assert await current_sources(conn, [ref]) == [None]  # No authoritative rule projection.
                await execute(
                    conn,
                    "INSERT INTO ag_setting(key,value,updated_by) VALUES(:key,CAST(:value AS jsonb),1)",
                    key=CATALOG_KEY,
                    value=encoded({"state": "READY", "items": rules(2, True)["items"]}),
                )
                calls = []

                def count(*args):
                    calls.append(1)

                event.listen(db.engine.sync_engine, "before_cursor_execute", count)
                try:
                    found = await current_sources(
                        conn,
                        [
                            ref,
                            ref | {"content_sha256": "0" * 64},
                            ref | {"scope_context": None},
                            {"invalid": True},
                            ref,
                        ],
                    )
                    assert len(calls) == 1
                    assert found[0] and found[4] and found[1:4] == [None, None, None]
                finally:
                    event.remove(db.engine.sync_engine, "before_cursor_execute", count)
                # Mentioning the current version first must not hide a stale rule later.
                await execute(
                    conn,
                    "UPDATE ag_kb_version SET parsed_text=:text WHERE id=:id",
                    id=vid,
                    text=text + " ITEM_NET_DEMO_V2",
                )
                assert await current_sources(conn, [ref]) == [None]
                await execute(
                    conn, "UPDATE ag_kb_version SET parsed_text=:text WHERE id=:id", id=vid, text=text
                )
                await execute(
                    conn,
                    "UPDATE ag_setting SET value=CAST(:value AS jsonb) WHERE key=:key",
                    key=CATALOG_KEY,
                    value=encoded({"state": "READY", "items": rules(2)["items"]}),
                )
                assert await current_sources(conn, [ref], lock=True) == [None]
    finally:
        await db.engine.dispose()


@pytest.mark.anyio
async def test_rule_publish_fence_blocks_concurrent_sync_and_failed_sync_stays_closed(integration_config):
    db = Database(integration_config["databaseUrl"])
    try:
        async with db.transaction() as conn:
            await execute(conn, "DELETE FROM ag_setting WHERE key=:key", key=CATALOG_KEY)
        owner = await begin_rule_sync(db, 1)
        with pytest.raises(DomainError, match="RULE_SYNC_IN_PROGRESS"):
            await begin_rule_sync(db, 1, recover=True)
        bridge = SimpleNamespace(get=AsyncMock(return_value=rules(2)))
        with pytest.raises(DomainError, match="RULE_SYNC_IN_PROGRESS"):
            await refresh_rule_sync(db, bridge, "synthetic", 1)
        bridge.get.assert_not_awaited()
        await finish_rule_sync(db, "wrong-owner", rules())
        async with db.transaction() as conn:
            assert (await one(conn, "SELECT value FROM ag_setting WHERE key=:key", key=CATALOG_KEY))["value"][
                "state"
            ] == "SYNCING"
        await finish_rule_sync(db, owner)
        owner = await begin_rule_sync(db, 1)
        await finish_rule_sync(db, owner, rules(2))
        async with db.transaction() as conn:
            before = await one(conn, "SELECT value,version FROM ag_setting WHERE key=:key", key=CATALOG_KEY)
            assert before["value"]["state"] == "READY"
        await refresh_rule_sync(db, bridge, "synthetic", 1)
        async with db.transaction() as conn:
            after = await one(conn, "SELECT value,version FROM ag_setting WHERE key=:key", key=CATALOG_KEY)
            assert before == after  # Merely reading the page does not revoke live sources.
    finally:
        async with db.transaction() as conn:
            await execute(conn, "DELETE FROM ag_setting WHERE key=:key", key=CATALOG_KEY)
        await db.engine.dispose()


def test_rule_projection_rejects_empty_duplicate_and_inconsistent_versions():
    assert len(validate_rules(rules(2))) == 2
    for page in [
        {"items": []},
        {"items": rules()["items"] * 2},
        {"items": [rules()["items"][0] | {"rule_version": "wrong"}]},
    ]:
        with pytest.raises(DomainError, match="JAVA_PROTOCOL_ERROR"):
            validate_rules(page)
