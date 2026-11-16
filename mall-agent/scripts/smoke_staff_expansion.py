import io
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from expansion_client import ADMIN, MEMBER, api, login
from PIL import Image

from app.runtime import run

ROOT = Path(__file__).resolve().parents[2]


async def main():
    evidence = {"verified_at": datetime.now(UTC).isoformat(), "checks": []}

    def passed(check):
        evidence["checks"].append(check)
        print("PASS", check, flush=True)

    async with (
        httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=45, trust_env=False) as staff,
        httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=45, trust_env=False) as member,
        httpx.AsyncClient(base_url="http://127.0.0.1:8201", timeout=30, trust_env=False) as stranger,
    ):
        await login(staff, True)
        await login(member)
        await login(stranger, username="agent-conversation-bd63b203")
        me = await api(staff, ADMIN + "/me")
        all_staff = await api(staff, ADMIN + "/staff?limit=100")
        profile = next(s for s in all_staff["items"] if s["id"] == me["id"])
        initial = profile["presence"]
        await api(
            staff, ADMIN + "/presence", "PUT", {"expected_version": profile["version"], "presence": "ONLINE"}
        )
        conv = await api(
            member,
            MEMBER + "/conversations",
            "POST",
            {"title": "[TEST ONLY] 人工扩展验收", "entry_context": {"kind": "GENERAL"}},
            expected=201,
        )
        cid = conv["id"]
        evidence["conversation_id"] = cid
        try:
            handoff = await api(
                member,
                MEMBER + f"/conversations/{cid}/handoff",
                "POST",
                {"expected_conversation_version": conv["version"], "reason": "[TEST ONLY] images and cards"},
                expected=201,
            )
            await api(
                staff,
                ADMIN + f"/handoffs/{handoff['id']}/claim",
                "POST",
                {"expected_version": handoff["version"]},
            )
            mid = str(uuid4())
            await api(
                member,
                MEMBER + f"/conversations/{cid}/messages",
                "POST",
                {"client_message_id": mid, "text": "[TEST ONLY] 人工未读验收", "attachment_ids": []},
                expected=202,
            )
            snap = await api(staff, ADMIN + f"/conversations/{cid}")
            assert snap["conversation"]["unread_count"] == 1
            await api(staff, ADMIN + f"/conversations/{cid}/read", "POST", {"last_seq": snap["latest_seq"]})
            assert (await api(staff, ADMIN + f"/conversations/{cid}"))["conversation"]["unread_count"] == 0
            passed("staff_independent_unread_and_read_cursor")
            context = await api(staff, ADMIN + f"/conversations/{cid}/context?product_id=45")
            assert context["products"][0]["id"] == "45" and context["phone_masked"]
            assert "receiver_phone" not in json.dumps(context) and "address_summary" not in json.dumps(
                context
            )
            cards = [{"kind": "PRODUCT", "id": "45"}]
            assert context["orders"], "Expected seeded demo member orders for read-only ownership acceptance"
            oid = context["orders"][0]["id"]
            cards.append({"kind": "ORDER", "id": oid})
            mismatch = await staff.get(ADMIN + f"/conversations/{cid}/context?order_id=9223372036854775807")
            assert mismatch.status_code == 404
            passed("signed_current_member_context_and_owned_order_summary")
            image = io.BytesIO()
            Image.new("RGB", (96, 64), "blue").save(image, format="PNG")
            raw = image.getvalue()
            upload_key = str(uuid4())
            upload = await staff.post(
                ADMIN + "/attachments",
                headers={"Idempotency-Key": upload_key},
                data={"purpose": "CHAT_IMAGE"},
                files={"file": ("synthetic-blue.png", raw, "image/png")},
            )
            assert upload.status_code == 201, upload.json().get("code")
            attachment = upload.json()["data"]
            aid = attachment["id"]
            assert (await member.get(MEMBER + f"/attachments/{aid}/content")).status_code == 404
            cursor = snap["event_cursor"]
            note = "[TEST ONLY] PRIVATE_NOTE_NOT_IN_EVENT_BODY"
            await api(
                staff,
                ADMIN + f"/conversations/{cid}/notes",
                "POST",
                {"client_note_id": str(uuid4()), "body": note},
                expected=201,
            )
            key = str(uuid4())
            body = {
                "client_message_id": key,
                "text": "[TEST ONLY] 图片、商品与订单卡片",
                "attachment_ids": [aid],
                "cards": cards,
            }
            message = await api(staff, ADMIN + f"/conversations/{cid}/messages", "POST", body, key, 201)
            assert (await api(staff, ADMIN + f"/conversations/{cid}/messages", "POST", body, key, 201))[
                "id"
            ] == message["id"]
            assert [b["type"] for b in message["blocks"]] == ["attachment", "product", "staff_order"]
            image_response = await member.get(MEMBER + f"/attachments/{aid}/content")
            assert image_response.status_code == 200 and image_response.content.startswith(b"\xff\xd8")
            assert (await stranger.get(MEMBER + f"/attachments/{aid}/content")).status_code == 404
            assert (await staff.get(ADMIN + f"/attachments/{aid}/content")).status_code == 200
            passed("staff_private_image_card_send_replay_and_member_isolation")
            seen = []
            async with staff.stream(
                "GET", ADMIN + f"/events?after={cursor}&conversation_id={cid}", timeout=30
            ) as stream:
                assert stream.status_code == 200 and "text/event-stream" in stream.headers.get(
                    "content-type", ""
                )
                async for line in stream.aiter_lines():
                    if line.startswith("data: "):
                        event = json.loads(line[6:])
                        seen.append(event["type"])
                        assert note not in line
                        if (
                            event["type"] == "message.completed"
                            and event["payload"]["message"]["id"] == message["id"]
                        ):
                            break
            assert "note.created" in seen and "message.completed" in seen
            passed("staff_sse_replay_without_note_body_leak")
            audit = await api(staff, ADMIN + f"/audit-logs?resource_id={cid}")
            assert audit["items"] and all("details" not in item for item in audit["items"])
            assert note not in json.dumps(audit)
            passed("supervisor_sanitized_audit_query")
            buyer = await api(member, MEMBER + f"/conversations/{cid}")
            assert buyer["conversation"]["unread_count"] == 1 and buyer["active_run"] is None
            evidence.update(message_id=message["id"], attachment_id=aid, order_id=oid, staff_id=me["id"])
            (ROOT / "deployment/.run/staff-expansion-smoke.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        finally:
            snap = await api(member, MEMBER + f"/conversations/{cid}")
            await api(
                member,
                MEMBER + f"/conversations/{cid}",
                "DELETE",
                {"expected_version": snap["conversation"]["version"]},
            )
            profile = next(
                s for s in (await api(staff, ADMIN + "/staff?limit=100"))["items"] if s["id"] == me["id"]
            )
            await api(
                staff,
                ADMIN + "/presence",
                "PUT",
                {"expected_version": profile["version"], "presence": initial},
            )
            evidence["test_conversation_deleted"] = True
            (ROOT / "deployment/.run/staff-expansion-smoke.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
            )


if __name__ == "__main__":
    run(main())
