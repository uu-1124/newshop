from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from test_handoffs import actor, claim, conversation, online, request
from test_runtime import settings_for

from app.api.staff_events import staff_event_batch
from app.bootstrap import Services
from app.domain.capabilities import AccessContext
from app.domain.identity import DomainError
from app.infrastructure.postgres.database import one, rows
from app.main import create_app


@pytest.mark.anyio
async def test_staff_read_positions_are_independent_and_note_events_are_private(integration_config):
    svc = Services(settings_for(integration_config, handoffs_enabled=True))
    h = svc.extensions.handoffs.require()
    member, left, right = actor(), actor(True), actor(True)
    try:
        await online(h, left)
        await online(h, right)
        conv = await conversation(svc, member)
        handoff = await request(svc, member, conv)
        await claim(h, left, handoff)
        await svc.conversations.send(
            member,
            "unit",
            conv["id"],
            uuid4(),
            {"client_message_id": str(uuid4()), "text": "会员消息", "attachment_ids": []},
        )
        snap = await h.snapshot(left, conv["id"])
        assert snap["conversation"]["unread_count"] == 1
        assert (await svc.conversations.snapshot(member, conv["id"]))["conversation"]["unread_count"] == 0
        position = await h.mark_read(left, conv["id"], uuid4(), {"last_seq": "1"})
        assert position == {"last_seq": "1", "unread_count": 0}
        assert (await h.list(left, "MINE", "", 20, None))["items"][0]["unread_count"] == 0
        with pytest.raises(DomainError) as denied:
            await h.mark_read(right, conv["id"], uuid4(), {"last_seq": "1"})
        assert denied.value.status == 404
        async with svc.db.transaction() as conn:
            start = (await one(conn, "SELECT last_event_id FROM ag_event_clock WHERE singleton_id=1"))[
                "last_event_id"
            ]
        await h.send(
            left, conv["id"], uuid4(), {"client_note_id": str(uuid4()), "body": "员工内部备注"}, note=True
        )
        await h.send(
            left,
            conv["id"],
            uuid4(),
            {"client_message_id": str(uuid4()), "text": "公开人工回复", "attachment_ids": [], "cards": []},
        )
        visible, _ = await staff_event_batch(svc, start, left, conv["id"])
        assert {event["event_type"] for event in visible} == {"note.created", "message.completed"}
        no_notes = replace(left, permissions=left.permissions - {"agent:conversation:note"})
        visible, _ = await staff_event_batch(svc, start, no_notes, conv["id"])
        assert [event["event_type"] for event in visible] == ["message.completed"]
        visible, watermark = await staff_event_batch(svc, start, right)
        assert all(event["conversation_id"] != conv["id"] for event in visible)
        assert watermark >= start
        async with svc.db.transaction() as conn:
            private = await rows(
                conn,
                "SELECT * FROM ag_event_outbox WHERE conversation_id=:cid AND event_type IN ('read.updated','note.created')",
                cid=conv["id"],
            )
            assert private and all(item["visibility"] == "STAFF_ONLY" for item in private)
        assert (await svc.conversations.snapshot(member, conv["id"]))["conversation"]["unread_count"] == 1
        snap = await h.snapshot(left, conv["id"])
        await svc.conversations.delete(
            member, conv["id"], uuid4(), {"expected_version": snap["conversation"]["version"]}
        )
        with pytest.raises(DomainError) as deleted:
            await staff_event_batch(svc, start, left, conv["id"])
        assert deleted.value.status == 404
    finally:
        await svc.close()


@pytest.mark.anyio
async def test_audit_http_permission_scope_cursor_and_summary_do_not_leak_details(integration_config):
    svc = Services(settings_for(integration_config, handoffs_enabled=True))
    h = svc.extensions.handoffs.require()
    member, left, right = actor(), actor(True), actor(True)
    await online(h, left)
    await online(h, right)
    conv = await conversation(svc, member)
    handoff = await request(svc, member, conv)
    await claim(h, left, handoff)
    await h.send(
        left,
        conv["id"],
        uuid4(),
        {"client_note_id": str(uuid4()), "body": "PRIVATE_NOTE_NEVER_IN_AUDIT"},
        note=True,
    )
    await h.send(
        left,
        conv["id"],
        uuid4(),
        {"client_message_id": str(uuid4()), "text": "审计分页验收", "attachment_ids": [], "cards": []},
    )
    normal = replace(left, permissions=left.permissions | {"agent:audit:read", "agent:events:subscribe"})
    supervisor = replace(
        right, permissions=right.permissions | {"agent:audit:read", "agent:conversation:history:all"}
    )

    class Auth:
        async def authenticate(self, token):
            principal = {"Bearer left": normal, "Bearer supervisor": supervisor, "Bearer denied": right}.get(
                token
            )
            if not principal:
                raise DomainError("AUTH_REQUIRED", "请登录", 401)
            return AccessContext(principal, uuid4(), principal.permissions)

    svc.extensions.staff_identity.implementation = Auth()
    app = (
        create_app(svc.settings)
        if hasattr(svc, "settings")
        else create_app(settings_for(integration_config, handoffs_enabled=True))
    )
    app.state.services = svc
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            path = "/mall-agent/v1/admin/audit-logs"
            denied = await client.get(path, headers={"Authorization": "Bearer denied"})
            assert denied.status_code == 403
            denied_events = await client.get(
                "/mall-agent/v1/admin/events", headers={"Authorization": "Bearer denied"}
            )
            assert denied_events.status_code == 403
            result = await client.get(
                path, params={"resource_id": conv["id"], "limit": 1}, headers={"Authorization": "Bearer left"}
            )
            assert result.status_code == 200 and result.json()["data"]["has_more"]
            assert "PRIVATE_NOTE" not in result.text and "details" not in result.text
            cursor = result.json()["data"]["next_cursor"]
            crossed = await client.get(
                path,
                params={"resource_id": conv["id"], "limit": 1, "cursor": cursor},
                headers={"Authorization": "Bearer supervisor"},
            )
            assert crossed.status_code == 400
            broad = await client.get(path, headers={"Authorization": "Bearer supervisor"})
            assert broad.status_code == 200
            assert any(row["actor_kind"] == "MEMBER" for row in broad.json()["data"]["items"])
            own = await client.get(path, headers={"Authorization": "Bearer left"})
            assert all(
                row["actor_kind"] == "STAFF" and row["actor_id"] == str(left.id)
                for row in own.json()["data"]["items"]
            )
            unauthorized = await client.get(
                "/mall-agent/v1/admin/events",
                params={"after": "9999999999999999999"},
                headers={"Authorization": "Bearer left"},
            )
            assert unauthorized.status_code == 400
    finally:
        snap = await h.snapshot(left, conv["id"])
        await svc.conversations.delete(
            member, conv["id"], uuid4(), {"expected_version": snap["conversation"]["version"]}
        )
        await svc.close()
