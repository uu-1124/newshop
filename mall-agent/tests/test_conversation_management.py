import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from jsonschema import Draft202012Validator
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from pydantic import ValidationError

from app.agents.graph import build_graph
from app.api.dto import ConversationRename, ReadInput
from app.application.conversations import Conversations
from app.application.jobs import Jobs
from app.application.runs import Runs
from app.application.runtime_check import runtime_handlers
from app.bootstrap import Services
from app.domain.identity import DomainError, Principal
from app.infrastructure.cursors import Cursors
from app.infrastructure.postgres.database import Database, encoded, execute, one, rows
from app.infrastructure.providers import FixtureDialogProvider
from app.infrastructure.runtime_auth import RuntimeAuth
from app.main import create_app
from app.settings import Settings
from app.workers.jobs import execute_job


@asynccontextmanager
async def harness(config):
    settings = Settings(
        _env_file=None,
        database_url=config["databaseUrl"],
        bridge_secret=config["bridgeSecret"],
        broker_secret=config["brokerSecret"],
        cursor_secret=config["cursorSecret"],
        provider="fixture",
    )
    db = Database(config["databaseUrl"])
    principal = Principal(910000000 + uuid4().int % 10000000, "conversation-test")
    conversations = Conversations(db, RuntimeAuth(180), Cursors(config["cursorSecret"]), settings)
    runs = Runs(db, settings)
    try:
        yield db, conversations, runs, principal, settings
    finally:
        async with db.transaction() as conn:
            await execute(
                conn,
                """UPDATE ag_generation_slot SET run_id=NULL,lease_until=NULL WHERE run_id IN
                (SELECT r.id FROM ag_run r JOIN ag_conversation c ON c.id=r.conversation_id WHERE c.member_id=:member)""",
                member=principal.id,
            )
            await execute(
                conn,
                """UPDATE ag_run SET state='STOPPED',lease_owner=NULL,lease_until=NULL WHERE conversation_id IN
                (SELECT id FROM ag_conversation WHERE member_id=:member) AND state IN ('QUEUED','RUNNING','STOP_REQUESTED')""",
                member=principal.id,
            )
        await db.engine.dispose()


async def create(conversations, member, title="会话测试"):
    return await conversations.create(member, uuid4(), {"title": title, "entry_context": {"kind": "GENERAL"}})


async def send(conversations, member, cid, text="原问题"):
    return await conversations.send(
        member,
        "test-bearer-not-persisted",
        cid,
        uuid4(),
        {"client_message_id": str(uuid4()), "text": text, "attachment_ids": []},
    )


def validate_response(name, data):
    contract = json.loads((Path(__file__).parents[2] / "docs/api/openapi.json").read_text(encoding="utf-8"))
    Draft202012Validator(
        {"$ref": f"#/components/schemas/{name}", "components": contract["components"]}
    ).validate(json.loads(encoded(data)))


def test_management_inputs_reject_blank_titles_and_invalid_bigint_positions():
    for title in ("", " \n\t ", "x" * 101):
        with pytest.raises(ValidationError):
            ConversationRename(title=title, expected_version=1)
    assert ConversationRename(title="  新标题  ", expected_version=1).title == "新标题"
    for value in ("-1", "01", "1e2", "9223372036854775808", "9" * 100):
        with pytest.raises(ValidationError):
            ReadInput(last_seq=value)


@pytest.mark.anyio
async def test_rename_isolated_idempotent_and_does_not_interrupt_generation(integration_config):
    async with harness(integration_config) as (db, svc, runs, member, _):
        conv = await create(svc, member)
        await send(svc, member, conv["id"])
        claimed = await runs.claim("rename-test-worker")
        assert claimed and str(claimed["conversation_id"]) == conv["id"]
        snapshot = await svc.snapshot(member, conv["id"])
        body = {"title": "新标题", "expected_version": snapshot["conversation"]["version"]}
        key = uuid4()
        first, replay = await asyncio.gather(
            svc.rename(member, conv["id"], key, body), svc.rename(member, conv["id"], key, body)
        )
        assert json.loads(encoded(first)) == json.loads(encoded(replay)) and first["title"] == "新标题"
        assert first["control_epoch"] == conv["control_epoch"]
        assert await runs.append(claimed, "改名后仍可输出")
        await runs.finish(claimed)
        validate_response("Conversation", first)
        with pytest.raises(DomainError) as other:
            await svc.rename(Principal(member.id + 1, "other"), conv["id"], uuid4(), body)
        assert other.value.status == 404
        with pytest.raises(DomainError) as reused:
            await svc.rename(member, conv["id"], key, body | {"title": "篡改"})
        assert reused.value.code == "IDEMPOTENCY_CONFLICT"
        with pytest.raises(DomainError) as stale:
            await svc.rename(member, conv["id"], uuid4(), body)
        assert stale.value.code == "VERSION_CONFLICT"
        latest = await svc.snapshot(member, conv["id"])
        candidates = await asyncio.gather(
            *[
                svc.rename(
                    member,
                    conv["id"],
                    uuid4(),
                    {"title": title, "expected_version": latest["conversation"]["version"]},
                )
                for title in ("并发甲", "并发乙")
            ],
            return_exceptions=True,
        )
        assert sum(isinstance(item, dict) for item in candidates) == 1
        assert (
            sum(isinstance(item, DomainError) and item.code == "VERSION_CONFLICT" for item in candidates) == 1
        )


@pytest.mark.anyio
async def test_read_cursor_is_monotonic_counts_incoming_and_preserves_control(integration_config):
    async with harness(integration_config) as (_, svc, runs, member, _):
        conv = await create(svc, member)
        await send(svc, member, conv["id"])
        claimed = await runs.claim("read-test-worker")
        generating = await svc.snapshot(member, conv["id"])
        assert generating["read_position"]["unread_count"] == 0
        with pytest.raises(DomainError) as unfinished:
            await svc.mark_read(member, conv["id"], uuid4(), {"last_seq": "2"})
        assert unfinished.value.code == "INVALID_READ_POSITION"
        assert await runs.append(claimed, "已经完成的回复")
        await runs.finish(claimed)
        before = await svc.snapshot(member, conv["id"])
        assert before["conversation"]["unread_count"] == 1
        key = uuid4()
        result = await svc.mark_read(member, conv["id"], key, {"last_seq": "2"})
        assert result == {"last_seq": "2", "unread_count": 0}
        assert await svc.mark_read(member, conv["id"], key, {"last_seq": "2"}) == result
        older = await svc.mark_read(member, conv["id"], uuid4(), {"last_seq": "1"})
        assert older == result
        after = await svc.snapshot(member, conv["id"])
        assert after["read_position"] == result
        assert after["conversation"]["version"] == before["conversation"]["version"]
        assert after["conversation"]["control_epoch"] == before["conversation"]["control_epoch"]
        with pytest.raises(DomainError) as forbidden:
            await svc.mark_read(Principal(member.id + 1, "other"), conv["id"], uuid4(), {"last_seq": "0"})
        assert forbidden.value.status == 404
        validate_response("ReadPosition", result)


@pytest.mark.anyio
async def test_title_search_pagination_literal_wildcards_and_scoped_cursors(integration_config):
    async with harness(integration_config) as (_, svc, _, member, _):
        for title in ("分页一", "分页二", "分页三", "字面%_匹配"):
            await create(svc, member, title)
        first = await svc.list(member, 2, None, "分页")
        second = await svc.list(member, 2, first["next_cursor"], "分页")
        assert first["has_more"] and not second["has_more"]
        assert len({item["id"] for page in (first, second) for item in page["items"]}) == 3
        assert len((await svc.list(member, 20, None, "%_"))["items"]) == 1
        assert not (await svc.list(member, 20, None, "不存在"))["items"]
        with pytest.raises(DomainError):
            await svc.list(member, 2, first["next_cursor"], "其他查询")
        with pytest.raises(DomainError):
            await svc.list(Principal(member.id + 1, "other"), 2, first["next_cursor"], "分页")


@pytest.mark.anyio
async def test_retry_creates_one_new_attempt_with_original_message_and_new_credentials(integration_config):
    async with harness(integration_config) as (db, svc, runs, member, _):
        conv = await create(svc, member)
        sent = await send(svc, member, conv["id"])
        old = sent["run"]
        claimed = await runs.claim("retry-test-worker")
        assert await runs.append(claimed, "保留的失败文本")
        await runs.finish(claimed, "TEST_FAILURE")
        old = await runs.get(member, old["id"])
        key = uuid4()
        body = {"expected_version": old["version"]}
        first, duplicate = await asyncio.gather(
            svc.retry_run(member, "new-test-bearer", old["id"], key, body),
            svc.retry_run(member, "new-test-bearer", old["id"], key, body),
        )
        assert json.loads(encoded(first)) == json.loads(encoded(duplicate))
        assert first["id"] != old["id"] and first["attempt"] == 2
        assert first["trigger_message_id"] == sent["message"]["id"]
        assert svc.auth.get(first["id"]) == "new-test-bearer"
        async with db.transaction() as conn:
            counts = await one(
                conn,
                "SELECT COUNT(*) AS n FROM ag_message WHERE conversation_id=:cid AND sender_kind='MEMBER'",
                cid=conv["id"],
            )
            assert counts["n"] == 1
            retry = await one(conn, "SELECT * FROM ag_run WHERE id=:id", id=first["id"])
            assert str(retry["retry_of_run_id"]) == old["id"]
            assert retry["checkpoint_thread_id"] != claimed["checkpoint_thread_id"]
            assert "test-bearer" not in encoded(dict(retry))
        assert not await runs.append(claimed, "旧任务不能继续输出")
        with pytest.raises(DomainError) as repeated:
            await svc.retry_run(member, "test", old["id"], uuid4(), body)
        assert repeated.value.code == "RUN_ALREADY_RETRIED"
        with pytest.raises(DomainError) as forbidden:
            await svc.retry_run(Principal(member.id + 1, "other"), "test", old["id"], uuid4(), body)
        assert forbidden.value.status == 404
        new_claim = await runs.claim("retry-new-worker")
        assert str(new_claim["id"]) == first["id"]
        assert await runs.append(new_claim, "新的完整回复")
        await runs.finish(new_claim)
        snapshot = await svc.snapshot(member, conv["id"])
        assert [
            item["text"] for item in snapshot["recent_messages"] if item["sender_kind"] == "ASSISTANT"
        ] == ["保留的失败文本", "新的完整回复"]
        validate_response("Run", first)


@pytest.mark.anyio
async def test_retry_concurrency_new_keys_cannot_duplicate_attempt(integration_config):
    async with harness(integration_config) as (_, svc, runs, member, _):
        conv = await create(svc, member)
        sent = await send(svc, member, conv["id"])
        old = await runs.stop(member, sent["run"]["id"], uuid4(), {"expected_version": 1})
        responses = await asyncio.gather(
            *[
                svc.retry_run(member, "test", old["id"], uuid4(), {"expected_version": old["version"]})
                for _ in range(2)
            ],
            return_exceptions=True,
        )
        assert sum(isinstance(item, dict) for item in responses) == 1
        assert (
            sum(isinstance(item, DomainError) and item.code == "RUN_ALREADY_RETRIED" for item in responses)
            == 1
        )
        with pytest.raises(DomainError) as state:
            new = next(item for item in responses if isinstance(item, dict))
            await svc.retry_run(member, "test", new["id"], uuid4(), {"expected_version": 1})
        assert state.value.code == "RUN_NOT_RETRYABLE"


@pytest.mark.anyio
async def test_delete_fences_generation_preserves_actions_and_cleans_checkpoint(integration_config):
    async with harness(integration_config) as (db, svc, runs, member, settings):
        create_key = uuid4()
        conv = await svc.create(
            member, create_key, {"title": "需要删除的私人标题", "entry_context": {"kind": "GENERAL"}}
        )
        await send(svc, member, conv["id"], "需要删除的私人问题")
        claimed = await runs.claim("delete-test-worker")
        assert await runs.append(claimed, "需要删除的私人回答")
        thread = {"configurable": {"thread_id": claimed["checkpoint_thread_id"]}}
        async with AsyncPostgresSaver.from_conn_string(settings.checkpoint_url()) as saver:
            graph = build_graph(FixtureDialogProvider(), saver)
            await graph.ainvoke(
                {"question": "private checkpoint question", "products": [], "answer": ""}, thread
            )
            assert await saver.aget_tuple(thread)
        async with db.transaction() as conn:
            sid = 900000000 + uuid4().int % 100000000
            await execute(
                conn,
                "INSERT INTO ag_staff_profile(admin_id,display_name) VALUES (:id,'cleanup-test')",
                id=sid,
            )
            await execute(
                conn,
                "INSERT INTO ag_staff_note(id,conversation_id,admin_id,client_note_id,body,retention_until) VALUES (:id,:cid,:sid,:client,'需要删除的私人备注',now()+interval '90 days')",
                id=uuid4(),
                cid=conv["id"],
                sid=sid,
                client=uuid4(),
            )
            for state in ("PENDING_CONFIRMATION", "UNKNOWN", "SUCCEEDED"):
                await execute(
                    conn,
                    """INSERT INTO ag_action(id,conversation_id,member_id,action_type,state,conversation_version,
                    parameters,parameters_hash,operation_no,expires_at,control_epoch)
                    VALUES (:id,:cid,:member,'ADD_CART',:state,1,'{}'::jsonb,:hash,:operation,now()+interval '5 minutes',1)""",
                    id=uuid4(),
                    cid=conv["id"],
                    member=member.id,
                    state=state,
                    hash="a" * 64,
                    operation=uuid4(),
                )
        version = (await svc.snapshot(member, conv["id"]))["conversation"]["version"]
        key, body = uuid4(), {"expected_version": version}
        assert await svc.delete(member, conv["id"], key, body) == {}
        assert await svc.delete(member, conv["id"], key, body) == {}
        assert not await runs.append(claimed, "删除后泄漏")
        with pytest.raises(DomainError):
            svc.auth.get(str(claimed["id"]))
        for operation in (
            svc.snapshot(member, conv["id"]),
            svc.messages(member, conv["id"], 20, 1000),
            runs.get(member, str(claimed["id"])),
        ):
            with pytest.raises(DomainError) as inaccessible:
                await operation
            assert inaccessible.value.status == 404
        assert not (await svc.list(member, 20, None, "需要删除"))["items"]
        registry = runtime_handlers(db, settings)
        jobs = Jobs(db, settings, registry)
        async with db.transaction() as conn:
            deleted = await one(conn, "SELECT * FROM ag_conversation WHERE id=:cid", cid=conv["id"])
            assert deleted["memory_epoch"] == 2 and deleted["control_epoch"] == 2
            action_states = {
                item["state"]
                for item in await rows(
                    conn, "SELECT state FROM ag_action WHERE conversation_id=:cid", cid=conv["id"]
                )
            }
            assert action_states == {"STALE", "UNKNOWN", "SUCCEEDED"}
            pending_job = await one(
                conn, "SELECT * FROM ag_job WHERE dedupe_key=:key", key=f"conversation-delete:{conv['id']}:2"
            )
            assert pending_job
            # No worker is running this synthetic claim; advance the grace boundary
            # explicitly instead of waiting through the production provider deadline.
            await execute(conn, "UPDATE ag_job SET next_attempt_at=now() WHERE id=:id", id=pending_job["id"])
        job = await jobs.claim("cleanup-test-worker", pending_job["id"])
        finished = await execute_job(jobs, registry, job)
        assert finished["state"] == "SUCCEEDED", finished
        async with db.transaction() as conn:
            note = await one(conn, "SELECT body FROM ag_staff_note WHERE conversation_id=:id", id=conv["id"])
            assert note["body"] == "已删除"
        async with AsyncPostgresSaver.from_conn_string(settings.checkpoint_url()) as saver:
            assert await saver.aget_tuple(thread) is None
        async with db.transaction() as conn:
            messages = await rows(
                conn,
                "SELECT text_content,blocks,deleted_at FROM ag_message WHERE conversation_id=:cid",
                cid=conv["id"],
            )
            assert all(
                not item["text_content"] and not item["blocks"] and item["deleted_at"] for item in messages
            )
            assert not await rows(
                conn, "SELECT event_id FROM ag_event_outbox WHERE conversation_id=:cid", cid=conv["id"]
            )
        with pytest.raises(DomainError) as create_replay:
            await svc.create(
                member, create_key, {"title": "需要删除的私人标题", "entry_context": {"kind": "GENERAL"}}
            )
        assert create_replay.value.status == 404
        assert await svc.delete(member, conv["id"], key, body) == {}


@pytest.mark.anyio
async def test_member_management_http_contracts_and_authentication(integration_config):
    cfg = integration_config
    settings = Settings(
        _env_file=None,
        database_url=cfg["databaseUrl"],
        bridge_secret=cfg["bridgeSecret"],
        broker_secret=cfg["brokerSecret"],
        cursor_secret=cfg["cursorSecret"],
    )
    services = Services(settings)
    principal = Principal(929000001, "http-management-test")

    async def identity(token):
        if token != "Bearer unit-management":
            raise DomainError("UNAUTHORIZED", "请登录", 401)
        return principal

    services.bridge.identity = identity
    app = create_app(settings)
    app.state.services = services
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:

            async def call(method, path, body, schema):
                response = await client.request(
                    method,
                    path,
                    json=body,
                    headers={"Authorization": "Bearer unit-management", "Idempotency-Key": str(uuid4())},
                )
                assert response.status_code in (200, 201, 202), response.text
                validate_response(schema, response.json())
                return response.json()["data"]

            base = "/mall-agent/v1/member"
            conv = await call(
                "POST",
                base + "/conversations",
                {"title": "HTTP管理", "entry_context": {"kind": "GENERAL"}},
                "createConversationResponse",
            )
            path = base + "/conversations/" + conv["id"]
            assert (
                await client.patch(path, json={"title": "非法", "expected_version": 1})
            ).status_code == 401
            conv = await call(
                "PATCH", path, {"title": "HTTP改名", "expected_version": 1}, "renameConversationResponse"
            )
            await call("POST", path + "/read", {"last_seq": "0"}, "updateReadCursorResponse")
            await call("DELETE", path, {"expected_version": conv["version"]}, "deleteConversationResponse")
            missing = await client.get(path, headers={"Authorization": "Bearer unit-management"})
            assert missing.status_code == 404
    finally:
        await services.close()


@pytest.mark.anyio
async def test_cleanup_preserves_protected_attachment_and_deletes_unreferenced_object(
    integration_config, monkeypatch
):
    async with harness(integration_config) as (db, svc, runs, member, settings):
        conv = await create(svc, member)
        sent = await send(svc, member, conv["id"])
        await runs.stop(member, sent["run"]["id"], uuid4(), {"expected_version": 1})
        protected_id, disposable_id = uuid4(), uuid4()
        async with db.transaction() as conn:
            for aid, protected in ((protected_id, True), (disposable_id, False)):
                await execute(
                    conn,
                    """INSERT INTO ag_attachment(id,owner_kind,owner_id,purpose,object_key,original_name,mime_type,
                    byte_size,sha256,state,retention_until) VALUES (:id,'MEMBER',:member,'CHAT_IMAGE',:key,'test.jpg',
                    'image/jpeg',10,:sha,'READY',now()+interval '1 day')""",
                    id=aid,
                    member=member.id,
                    key=f"isolated-cleanup-test/{aid}",
                    sha="b" * 64,
                )
                await execute(
                    conn,
                    """INSERT INTO ag_attachment_ref(id,attachment_id,ref_kind,message_id,protected)
                    VALUES (:id,:attachment,'MESSAGE',:message,:protected)""",
                    id=uuid4(),
                    attachment=aid,
                    message=sent["message"]["id"],
                    protected=protected,
                )
        snapshot = await svc.snapshot(member, conv["id"])
        await svc.delete(
            member, conv["id"], uuid4(), {"expected_version": snapshot["conversation"]["version"]}
        )
        removed_keys = []

        class TestObjects:
            def __init__(self, _settings):
                pass

            def delete(self, key):
                removed_keys.append(key)

            def close(self):
                pass

        monkeypatch.setattr("app.application.conversation_cleanup.PrivateObjects", TestObjects)
        registry = runtime_handlers(db, settings.model_copy(update={"attachments_enabled": True}))
        jobs = Jobs(db, settings, registry)
        async with db.transaction() as conn:
            parent = await one(
                conn, "SELECT id FROM ag_job WHERE dedupe_key=:key", key=f"conversation-delete:{conv['id']}:2"
            )
        result = await execute_job(jobs, registry, await jobs.claim("protected-cleaner", parent["id"]))
        assert result["state"] == "SUCCEEDED", result
        async with db.transaction() as conn:
            protected = await one(conn, "SELECT state FROM ag_attachment WHERE id=:id", id=protected_id)
            disposable = await one(conn, "SELECT state FROM ag_attachment WHERE id=:id", id=disposable_id)
            assert protected["state"] == "READY" and disposable["state"] == "DELETING"
            child = await one(
                conn, "SELECT id FROM ag_job WHERE dedupe_key=:key", key=f"attachment-delete:{disposable_id}"
            )
            assert child
        result = await execute_job(jobs, registry, await jobs.claim("object-cleaner", child["id"]))
        assert result["state"] == "SUCCEEDED", result
        assert removed_keys == [f"isolated-cleanup-test/{disposable_id}"]
        async with db.transaction() as conn:
            assert (await one(conn, "SELECT state FROM ag_attachment WHERE id=:id", id=disposable_id))[
                "state"
            ] == "DELETED"
            assert (await one(conn, "SELECT state FROM ag_attachment WHERE id=:id", id=protected_id))[
                "state"
            ] == "READY"


@pytest.mark.anyio
async def test_cleanup_rejects_forged_scope_and_requires_tombstone(integration_config):
    from app.domain.capabilities import JobType

    async with harness(integration_config) as (db, svc, _, member, settings):
        conv = await create(svc, member, "不得删除的会话")
        registry = runtime_handlers(db, settings)
        jobs = Jobs(db, settings, registry)
        with pytest.raises(DomainError) as invalid:
            await jobs.enqueue(JobType.RETENTION_EXECUTE, str(uuid4()), {"kind": "ALL_CONVERSATIONS"})
        assert invalid.value.code == "VALIDATION_ERROR"
        forged = await jobs.enqueue(
            JobType.RETENTION_EXECUTE,
            str(uuid4()),
            {"kind": "CONVERSATION_DELETE", "conversation_id": conv["id"], "memory_epoch": 2},
        )
        result = await execute_job(jobs, registry, await jobs.claim("forged-cleaner", forged["id"]))
        assert result["state"] == "FAILED" and result["error_code"] == "ERASURE_NOT_AUTHORIZED"
        assert (await svc.snapshot(member, conv["id"]))["conversation"]["title"] == "不得删除的会话"
