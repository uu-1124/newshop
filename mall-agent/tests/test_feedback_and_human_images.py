from uuid import uuid4

import httpx
import pytest
from test_handoffs import actor, claim, conversation, online, request
from test_images import MemoryObjects, picture
from test_runtime import settings_for

from app.bootstrap import Services
from app.domain.identity import DomainError, Principal
from app.infrastructure.postgres.database import execute, one
from app.main import create_app


@pytest.mark.anyio
async def test_feedback_api_ownership_replace_replay_and_expiry(integration_config):
    svc = Services(settings_for(integration_config))
    member = actor()
    app = create_app(svc.runs.settings)
    app.state.services = svc

    class Bridge:
        async def identity(self, token):
            return member if token == "Bearer owner" else Principal(member.id + 1, "other")

    original_bridge = svc.bridge
    svc.bridge = Bridge()
    try:
        conv = await conversation(svc, member)
        mid, user_mid = uuid4(), uuid4()
        async with svc.db.transaction() as conn:
            for id, sender, seq in [(mid, "ASSISTANT", 1), (user_mid, "MEMBER", 2)]:
                await execute(
                    conn,
                    """INSERT INTO ag_message(id,conversation_id,seq,sender_kind,sender_id,text_content,status,retention_until)
                    VALUES(:id,:cid,:seq,:sender,:sender_id,'test answer','COMPLETED',now()+interval '1 day')""",
                    id=id,
                    cid=conv["id"],
                    seq=seq,
                    sender=sender,
                    sender_id=member.id if sender == "MEMBER" else 0,
                )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            path = f"/mall-agent/v1/member/messages/{mid}/feedback"
            headers = {"Authorization": "Bearer owner", "Idempotency-Key": str(uuid4())}
            assert (await client.get(path, headers=headers)).json()["data"] is None
            response = await client.post(path, headers=headers, json={"vote": -1, "reason": "  缺少依据  "})
            assert response.status_code == 200
            first = response.json()["data"]
            assert first["reason"] == "缺少依据"
            assert (
                await client.post(path, headers=headers, json={"vote": -1, "reason": "  缺少依据  "})
            ).json()["data"] == first
            assert (await client.post(path, headers=headers, json={"vote": 1})).status_code == 409
            denied = dict(headers, Authorization="Bearer other")
            assert (await client.get(path, headers=denied)).status_code == 404
            assert (await client.post(path, headers=denied, json={"vote": 1})).status_code == 404
            assert (
                await client.post(
                    path, headers=dict(headers, **{"Idempotency-Key": str(uuid4())}), json={"vote": 0}
                )
            ).status_code == 422
            assert (
                await client.post(path.replace(str(mid), str(user_mid)), headers=headers, json={"vote": 1})
            ).status_code == 409
            updated = await svc.feedback.submit(member, mid, uuid4(), {"vote": 1, "reason": ""})
            assert updated["id"] == first["id"] and updated["vote"] == 1 and updated["reason"] is None
            assert await svc.feedback.get(member, mid) == updated
            async with svc.db.transaction() as conn:
                count = await one(conn, "SELECT count(*) n FROM ag_feedback WHERE message_id=:id", id=mid)
                assert count["n"] == 1
                await execute(
                    conn,
                    "UPDATE ag_message SET retention_until=now()-interval '1 second' WHERE id=:id",
                    id=mid,
                )
            assert (
                await client.post(path, headers=headers, json={"vote": -1, "reason": "  缺少依据  "})
            ).status_code == 404
    finally:
        svc.bridge = original_bridge
        await svc.close()


@pytest.mark.anyio
async def test_waiting_and_assigned_images_without_vision_never_start_ai(integration_config):
    settings = settings_for(
        integration_config,
        handoffs_enabled=True,
        attachments_enabled=True,
        objects_access_key="test",
        objects_secret_key="test",
        vision_enabled=False,
    )
    svc = Services(settings)
    svc.attachments.objects = MemoryObjects()
    member, staff, outsider = actor(), actor(True), actor(True)
    staff = Principal(staff.id, staff.display_name, "STAFF", staff.permissions | {"agent:attachment:read"})
    outsider = Principal(
        outsider.id,
        outsider.display_name,
        "STAFF",
        (outsider.permissions - {"agent:handoff:claim"}) | {"agent:attachment:read"},
    )
    try:
        conv = await conversation(svc, member)
        handoff = await request(svc, member, conv)

        async def image_send():
            attachment = await svc.attachments.upload(member, uuid4(), "CHAT_IMAGE", "photo.png", picture())
            body = {"client_message_id": str(uuid4()), "text": "", "attachment_ids": [attachment["id"]]}
            key = uuid4()
            result = await svc.conversations.send(member, "unused", conv["id"], key, body)
            assert result["run"] is None
            replay = await svc.conversations.send(member, "unused", conv["id"], key, body)
            assert result["message"]["id"] == replay["message"]["id"]
            return attachment, result

        waiting, _ = await image_send()
        assert await svc.attachments.read(member, waiting["id"])
        with pytest.raises(DomainError, match="NOT_FOUND"):
            await svc.attachments.read(outsider, waiting["id"])
        h = svc.extensions.handoffs.require()
        await online(h, staff)
        await claim(h, staff, handoff)
        assigned, result = await image_send()
        assert await svc.attachments.read(staff, assigned["id"])
        snap = await h.snapshot(staff, conv["id"])
        assert result["message"]["id"] in [m["id"] for m in snap["recent_messages"]]
        async with svc.db.transaction() as conn:
            assert (
                await one(conn, "SELECT count(*) n FROM ag_run WHERE conversation_id=:id", id=conv["id"])
            )["n"] == 0
        current = await svc.conversations.snapshot(member, conv["id"])
        await svc.conversations.delete(
            member, conv["id"], uuid4(), {"expected_version": current["conversation"]["version"]}
        )
        for principal in [member, staff]:
            with pytest.raises(DomainError, match="NOT_FOUND"):
                await svc.attachments.read(principal, assigned["id"])
    finally:
        await svc.close()
