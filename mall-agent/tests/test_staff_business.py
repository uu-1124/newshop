from dataclasses import replace
from uuid import uuid4

import pytest
from test_handoffs import actor, claim, conversation, online, request
from test_images import MemoryObjects, picture
from test_runtime import settings_for

from app.api.handoff_dto import StaffMessageInput
from app.application.attachments import Attachments
from app.bootstrap import Services
from app.domain.identity import DomainError
from app.infrastructure.postgres.database import one


def test_card_only_message_is_valid_and_duplicate_cards_and_ids_are_rejected():
    body = {
        "client_message_id": str(uuid4()),
        "text": "",
        "attachment_ids": [],
        "cards": [{"kind": "ORDER", "id": "42"}],
    }
    StaffMessageInput.model_validate(body)
    with pytest.raises(ValueError):
        StaffMessageInput.model_validate(body | {"cards": []})
    with pytest.raises(ValueError):
        StaffMessageInput.model_validate(body | {"cards": body["cards"] * 2})


@pytest.mark.anyio
async def test_staff_images_bind_once_and_follow_assignment_member_and_deletion(integration_config):
    settings = settings_for(integration_config, handoffs_enabled=True).model_copy(
        update={"attachments_enabled": True}
    )
    svc = Services(settings_for(integration_config, handoffs_enabled=True))
    svc.attachments = Attachments(svc.db, MemoryObjects(), settings)
    h = svc.extensions.handoffs.require()
    h.settings = settings
    member, staff, target = actor(), actor(True), actor(True)
    staff = replace(
        staff, permissions=staff.permissions | {"agent:attachment:upload", "agent:attachment:read"}
    )
    target = replace(target, permissions=target.permissions | {"agent:attachment:read"})
    try:
        await online(h, staff)
        await online(h, target)
        conv = await conversation(svc, member)
        handoff = await request(svc, member, conv)
        await claim(h, staff, handoff)
        aid_key = uuid4()
        image = await svc.attachments.upload(staff, aid_key, "CHAT_IMAGE", "reply.png", picture())
        assert (await svc.attachments.upload(staff, aid_key, "CHAT_IMAGE", "reply.png", picture()))[
            "id"
        ] == image["id"]
        with pytest.raises(DomainError):
            await svc.attachments.read(member, image["id"])
        body = {"client_message_id": str(uuid4()), "text": "", "attachment_ids": [image["id"]], "cards": []}
        key = uuid4()
        msg = await h.send(staff, conv["id"], key, body)
        assert msg["blocks"][0]["type"] == "attachment" and msg["run_id"] is None
        assert (await h.send(staff, conv["id"], key, body))["id"] == msg["id"]
        assert (await h.send(staff, conv["id"], uuid4(), body))["id"] == msg["id"]
        assert (await svc.attachments.read(member, image["id"])).startswith(b"\xff\xd8")
        assert await svc.attachments.read(staff, image["id"])
        with pytest.raises(DomainError):
            await svc.attachments.read(actor(), image["id"])
        with pytest.raises(DomainError):
            await svc.attachments.read(target, image["id"])
        with pytest.raises(DomainError):
            await svc.attachments.read(
                replace(staff, permissions=staff.permissions - {"agent:attachment:read"}), image["id"]
            )
        with pytest.raises(DomainError) as reused:
            await h.send(staff, conv["id"], uuid4(), body | {"attachment_ids": []})
        assert reused.value.code == "MESSAGE_ID_REUSED"
        with pytest.raises(DomainError) as bound:
            await h.send(staff, conv["id"], uuid4(), body | {"client_message_id": str(uuid4())})
        assert bound.value.code == "ATTACHMENT_ALREADY_LINKED"

        class Bridge:
            async def staff_target(self, token, sid):
                return {"id": sid}

        h.bridge = Bridge()
        snap = await h.snapshot(staff, conv["id"])
        await h.command(
            staff,
            handoff["id"],
            uuid4(),
            {
                "expected_version": snap["handoff"]["version"],
                "target_staff_id": str(target.id),
                "reason": "test",
            },
            "transfer",
            "unit",
        )
        with pytest.raises(DomainError):
            await svc.attachments.read(staff, image["id"])
        assert await svc.attachments.read(target, image["id"])
        snap = await h.snapshot(target, conv["id"])
        await svc.conversations.delete(
            member, conv["id"], uuid4(), {"expected_version": snap["conversation"]["version"]}
        )
        for principal in (member, staff, target):
            with pytest.raises(DomainError):
                await svc.attachments.read(principal, image["id"])
    finally:
        await svc.close()


@pytest.mark.anyio
async def test_cards_use_trusted_member_reject_unknown_order_and_fence_transfer_during_read(
    integration_config,
):
    svc = Services(settings_for(integration_config, handoffs_enabled=True))
    h = svc.extensions.handoffs.require()
    member, staff, other = actor(), actor(True), actor(True)
    staff = replace(staff, permissions=staff.permissions | {"agent:business:read"})
    calls = []
    mode = "ok"

    class Bridge:
        async def staff_context(self, token, principal, conv, cards):
            assert token == "unit" and principal.id == staff.id and conv["member_id"] == member.id
            calls.append(cards)
            if mode == "denied":
                raise DomainError("BUSINESS_CONTEXT_REJECTED", "not owned", 404)
            if mode == "transfer":
                snap = await h.snapshot(staff, conv["id"])
                await h.command(
                    staff,
                    snap["handoff"]["id"],
                    uuid4(),
                    {
                        "expected_version": snap["handoff"]["version"],
                        "target_staff_id": str(other.id),
                        "reason": "test",
                    },
                    "transfer",
                    "unit",
                )
            return {
                "member_display_name": "会员",
                "phone_masked": "138****0000",
                "products": [{"id": "45", "name": "真实商品"}],
                "orders": [{"id": "42", "order_sn": "OWNED"}],
            }

        async def staff_target(self, token, sid):
            return {"id": sid}

    h.bridge = Bridge()
    try:
        await online(h, staff)
        await online(h, other)
        conv = await conversation(svc, member)
        handoff = await request(svc, member, conv)
        await claim(h, staff, handoff)
        cards = [{"kind": "PRODUCT", "id": "45"}, {"kind": "ORDER", "id": "42"}]
        body = {"client_message_id": str(uuid4()), "text": "", "attachment_ids": [], "cards": cards}
        key = uuid4()
        msg = await h.send(staff, conv["id"], key, body, token="unit")
        assert [b["type"] for b in msg["blocks"]] == ["product", "staff_order"]
        assert len(calls) == 1
        assert (await h.send(staff, conv["id"], key, body, token="unit"))["id"] == msg["id"]
        assert (await h.send(staff, conv["id"], uuid4(), body, token="unit"))["id"] == msg["id"]
        assert len(calls) == 1
        with pytest.raises(DomainError) as denied:
            await h.context(other, conv["id"], "unit")
        assert denied.value.status == 403
        mode = "denied"
        with pytest.raises(DomainError):
            await h.send(
                staff,
                conv["id"],
                uuid4(),
                body | {"client_message_id": str(uuid4()), "cards": [{"kind": "ORDER", "id": "999"}]},
                token="unit",
            )
        mode = "transfer"
        with pytest.raises(DomainError):
            await h.send(staff, conv["id"], uuid4(), body | {"client_message_id": str(uuid4())}, token="unit")
        async with svc.db.transaction() as conn:
            count = await one(
                conn, "SELECT count(*) AS n FROM ag_message WHERE conversation_id=:id", id=conv["id"]
            )
            assert count["n"] == 1
        snap = await h.snapshot(other, conv["id"])
        await svc.conversations.delete(
            member, conv["id"], uuid4(), {"expected_version": snap["conversation"]["version"]}
        )
    finally:
        await svc.close()
