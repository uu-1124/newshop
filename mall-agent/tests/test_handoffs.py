import asyncio
from uuid import UUID, uuid4

import httpx
import pytest
from test_runtime import settings_for

from app.bootstrap import Services
from app.domain.identity import DomainError, Principal
from app.infrastructure.postgres.database import execute, one, rows
from app.main import create_app

PERMISSIONS = frozenset(
    {
        "agent:conversation:read",
        "agent:conversation:reply",
        "agent:conversation:note",
        "agent:handoff:claim",
        "agent:handoff:transfer",
        "agent:handoff:close",
        "agent:presence:write",
        "agent:staff:read",
        "agent:staff:manage",
    }
)


def actor(staff=False):
    return Principal(
        1000000000 + uuid4().int % 1000000000,
        "test-staff" if staff else "test-member",
        "STAFF" if staff else "MEMBER",
        PERMISSIONS if staff else frozenset(),
    )


async def online(h, staff, capacity=3):
    await h.ensure_profile(staff)
    await h.set_presence(staff, str(uuid4()), {"expected_version": 1, "presence": "ONLINE"})
    if capacity != 3:
        await h.update_profile(
            staff,
            staff.id,
            str(uuid4()),
            {"expected_version": 2, "display_name": staff.display_name, "capacity": capacity},
        )


async def conversation(svc, member):
    return await svc.conversations.create(
        member, str(uuid4()), {"title": "[TEST ONLY] handoff", "entry_context": {"kind": "GENERAL"}}
    )


async def request(svc, member, conv):
    snap = await svc.conversations.snapshot(member, conv["id"])
    return await svc.extensions.handoffs.require().request(
        member,
        conv["id"],
        str(uuid4()),
        {"expected_conversation_version": snap["conversation"]["version"], "reason": "[TEST ONLY]"},
    )


async def claim(h, staff, handoff):
    return await h.command(
        staff, handoff["id"], str(uuid4()), {"expected_version": handoff["version"]}, "claim"
    )


@pytest.mark.anyio
async def test_handoff_stops_old_generation_waiting_choice_withdrawal_and_member_isolation(
    integration_config,
):
    svc = Services(settings_for(integration_config, handoffs_enabled=True))
    member, other = actor(), actor()
    h = svc.extensions.handoffs.require()
    try:
        conv = await conversation(svc, member)
        await svc.conversations.send(
            member,
            "test-only",
            conv["id"],
            str(uuid4()),
            {"client_message_id": str(uuid4()), "text": "hello", "attachment_ids": []},
        )
        running = await svc.runs.claim("test-handoff")
        assert str(running["conversation_id"]) == conv["id"]
        assert await svc.runs.append(running, "partial")
        handoff = await request(svc, member, conv)
        assert not await svc.runs.append(running, "MUST_NOT_APPEAR")
        assert not await svc.runs.heartbeat(running)
        await svc.runs.finish(running)
        snap = await svc.conversations.snapshot(member, conv["id"])
        assert snap["active_run"] is None and snap["conversation"]["state"] == "WAITING_HUMAN"
        assert snap["conversation"]["control_epoch"] > running["control_epoch"]
        assert snap["recent_messages"][-1]["status"] == "STOPPED"
        sent = await svc.conversations.send(
            member,
            "test-only",
            conv["id"],
            str(uuid4()),
            {"client_message_id": str(uuid4()), "text": "waiting", "attachment_ids": []},
        )
        assert sent["run"] is None
        with pytest.raises(DomainError, match="NOT_FOUND"):
            await h.request(other, conv["id"], str(uuid4()), {"expected_conversation_version": 1})
        snap = await svc.conversations.snapshot(member, conv["id"])
        enabled = await h.waiting(
            member,
            conv["id"],
            str(uuid4()),
            {"expected_version": snap["conversation"]["version"], "enabled": True},
        )
        assert enabled["waiting_ai_enabled"]
        sent = await svc.conversations.send(
            member,
            "test-only",
            conv["id"],
            str(uuid4()),
            {"client_message_id": str(uuid4()), "text": "while waiting ai", "attachment_ids": []},
        )
        assert sent["run"]
        withdrawn = await h.waiting(
            member, conv["id"], str(uuid4()), {"expected_version": handoff["version"]}, withdraw=True
        )
        assert withdrawn["state"] == "WITHDRAWN"
        snap = await svc.conversations.snapshot(member, conv["id"])
        assert snap["active_run"] is None and snap["conversation"]["state"] == "AI_ACTIVE"
        assert await svc.runs.claim("test-no-queued") is None
    finally:
        await svc.close()


@pytest.mark.anyio
async def test_concurrent_claim_and_capacity_are_atomic(integration_config):
    svc = Services(settings_for(integration_config, handoffs_enabled=True))
    h = svc.extensions.handoffs.require()
    member, left, right = actor(), actor(True), actor(True)
    try:
        await online(h, left, capacity=1)
        await online(h, right, capacity=1)
        conv = await conversation(svc, member)
        handoff = await request(svc, member, conv)
        results = await asyncio.gather(
            claim(h, left, handoff), claim(h, right, handoff), return_exceptions=True
        )
        assert sum(isinstance(x, dict) for x in results) == 1
        assert sum(isinstance(x, DomainError) for x in results) == 1
        winner = left if isinstance(results[0], dict) else right
        loser = right if winner == left else left
        with pytest.raises(DomainError, match="NOT_FOUND"):
            await h.snapshot(loser, conv["id"])
        more = [await conversation(svc, member), await conversation(svc, member)]
        queued = [await request(svc, member, c) for c in more]
        results = await asyncio.gather(*(claim(h, loser, q) for q in queued), return_exceptions=True)
        assert sum(isinstance(x, dict) for x in results) == 1
        assert any(isinstance(x, DomainError) and x.code == "STAFF_CAPACITY_REACHED" for x in results)
        async with svc.db.transaction() as conn:
            count = await one(
                conn,
                "SELECT count(*) AS n FROM ag_conversation WHERE state='HUMAN_ACTIVE' AND assigned_admin_id=:id",
                id=loser.id,
            )
            assert count["n"] == 1
        with pytest.raises(DomainError, match="FORBIDDEN"):
            await claim(h, Principal(left.id, "revoked", "STAFF"), handoff)
        # Queue pages are stable and FIFO, and signed cursors cannot cross actors.
        page = await h.list(left, "WAITING", "", 1, None, queue=True)
        if page["next_cursor"]:
            with pytest.raises(DomainError):
                await h.list(right, "WAITING", "", 1, page["next_cursor"], queue=True)
    finally:
        await svc.close()


@pytest.mark.anyio
async def test_staff_reply_note_isolation_transfer_offline_and_close(integration_config):
    svc = Services(settings_for(integration_config, handoffs_enabled=True))
    h = svc.extensions.handoffs.require()
    member, left, right = actor(), actor(True), actor(True)
    try:
        await online(h, left)
        await online(h, right)
        conv = await conversation(svc, member)
        handoff = await claim(h, left, await request(svc, member, conv))
        body = {
            "client_message_id": str(uuid4()),
            "text": "public staff reply",
            "attachment_ids": [],
            "cards": [],
        }
        key = str(uuid4())
        first = await h.send(left, conv["id"], key, body)
        replay = await h.send(left, conv["id"], key, body)
        another_key = await h.send(left, conv["id"], str(uuid4()), body)
        assert first["id"] == replay["id"] == another_key["id"]
        assert first["sender_display_name"] == left.display_name
        with pytest.raises(DomainError, match="IDEMPOTENCY_CONFLICT"):
            await h.send(left, conv["id"], key, body | {"text": "different"})
        note = await h.send(
            left,
            conv["id"],
            str(uuid4()),
            {"client_note_id": str(uuid4()), "body": "SECRET_INTERNAL_NOTE"},
            note=True,
        )
        assert (await h.history(left, conv["id"], 20, None, notes=True))["items"][0]["id"] == note["id"]
        snap = await svc.conversations.snapshot(member, conv["id"])
        assert "SECRET_INTERNAL_NOTE" not in str(snap)
        async with svc.db.transaction() as conn:
            events = await rows(
                conn, "SELECT payload FROM ag_event_outbox WHERE conversation_id=:id", id=conv["id"]
            )
            assert "SECRET_INTERNAL_NOTE" not in str(events)
            await execute(
                conn,
                "UPDATE ag_staff_profile SET presence_expires_at=now()-interval '1 second' WHERE admin_id=:id",
                id=left.id,
            )
        snap = await svc.conversations.snapshot(member, conv["id"])
        assert snap["conversation"]["state"] == "HUMAN_ACTIVE"
        assert snap["conversation"]["assigned_staff"]["presence"] == "OFFLINE"
        sent = await svc.conversations.send(
            member,
            "test-only",
            conv["id"],
            str(uuid4()),
            {"client_message_id": str(uuid4()), "text": "human-only", "attachment_ids": []},
        )
        assert sent["run"] is None

        async def eligible(token, target):
            assert target == str(right.id)
            return {"eligible": True}

        svc.bridge.staff_target = eligible
        handoff = await h.command(
            left,
            handoff["id"],
            str(uuid4()),
            {
                "expected_version": handoff["version"],
                "target_staff_id": str(right.id),
                "reason": "test transfer",
            },
            "transfer",
        )
        with pytest.raises(DomainError, match="NOT_FOUND"):
            await h.send(left, conv["id"], str(uuid4()), body | {"client_message_id": str(uuid4())})
        assert (await h.snapshot(right, conv["id"]))["conversation"]["assigned_staff"]["id"] == str(right.id)
        handoff = await h.command(
            right,
            handoff["id"],
            str(uuid4()),
            {"expected_version": handoff["version"], "reason": "test complete"},
            "close",
        )
        assert handoff["state"] == "CLOSED"
        with pytest.raises(DomainError, match="CONVERSATION_NOT_ACTIVE"):
            await svc.conversations.send(
                member,
                "test-only",
                conv["id"],
                str(uuid4()),
                {"client_message_id": str(uuid4()), "text": "cannot reopen", "attachment_ids": []},
            )
        assert (await h.snapshot(right, conv["id"]))["conversation"]["state"] == "CLOSED"
        assert not (await h.list(left, "HISTORY", "", 100, None))["items"]
    finally:
        await svc.close()


@pytest.mark.anyio
async def test_handoff_routes_contracts_and_authentication(integration_config):
    settings = settings_for(integration_config, handoffs_enabled=True)
    app = create_app(settings)
    member, staff = actor(), actor(True)
    async with app.router.lifespan_context(app):
        svc = app.state.services

        async def identity(token):
            return member

        async def staff_identity(token):
            return staff

        svc.bridge.identity = identity
        svc.bridge.staff_identity = staff_identity
        from app.infrastructure.staff_auth import StaffAuthorization

        svc.extensions.staff_identity.implementation = StaffAuthorization(svc.bridge)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {"Authorization": "Bearer test-only"}
            assert (await client.get("/mall-agent/v1/admin/handoffs")).status_code == 401
            assert (await client.get("/mall-agent/v1/admin/me", headers=headers)).status_code == 200
            conv = await conversation(svc, member)
            response = await client.post(
                f"/mall-agent/v1/member/conversations/{conv['id']}/handoff",
                headers=headers | {"Idempotency-Key": str(uuid4())},
                json={"expected_conversation_version": 1},
            )
            assert response.status_code == 201
            handoff = response.json()["data"]
            import json
            from pathlib import Path

            from jsonschema import Draft202012Validator

            contract = json.loads(
                (Path(__file__).resolve().parents[2] / "docs/api/openapi.json").read_text(encoding="utf-8")
            )
            Draft202012Validator(
                {"$ref": "#/components/schemas/requestHandoffResponse", "components": contract["components"]}
            ).validate(response.json())
            assert (
                await client.post(
                    f"/mall-agent/v1/admin/handoffs/{handoff['id']}/claim",
                    headers=headers | {"Idempotency-Key": str(uuid4())},
                    json={"expected_version": 1},
                )
            ).json()["code"] == "STAFF_NOT_ONLINE"
            await svc.extensions.handoffs.require().set_presence(
                staff, str(uuid4()), {"expected_version": 1, "presence": "ONLINE"}
            )
            assert (
                await client.post(
                    f"/mall-agent/v1/admin/handoffs/{handoff['id']}/claim",
                    headers=headers | {"Idempotency-Key": str(uuid4())},
                    json={"expected_version": 1},
                )
            ).status_code == 200
            snap = (
                await client.get(f"/mall-agent/v1/admin/conversations/{conv['id']}", headers=headers)
            ).json()
            Draft202012Validator(
                {
                    "$ref": "#/components/schemas/adminConversationSnapshotResponse",
                    "components": contract["components"],
                }
            ).validate(snap)
            assert UUID(snap["data"]["conversation"]["id"])
