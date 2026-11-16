"""Explicit real GPT image smoke through the member gateway. Synthetic images only."""

import asyncio
import io
import json
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from PIL import Image, ImageDraw, ImageFont

from app.runtime import run


async def main(persistence=False):
    root = Path(__file__).resolve().parents[2]
    evidence_path = root / "deployment/.run/image-chat-smoke.json"
    prefix = "/mall-agent/v1/member"
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8201", trust_env=False, timeout=45) as client:
        login = (
            await client.post(
                "/mall-portal/sso/login", data={"username": "member", "password": "MallDemo2026!"}
            )
        ).json()
        assert login["code"] == 200
        client.headers["Authorization"] = login["data"]["tokenHead"] + login["data"]["token"]

        async def post(path, body, key=None):
            response = await client.post(
                prefix + path, json=body, headers={"Idempotency-Key": str(key or uuid4())}
            )
            assert response.is_success, (response.status_code, response.json().get("code"))
            return response.json()["data"]

        if persistence:
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            restored = (await client.get(prefix + f"/conversations/{evidence['conversation_id']}")).json()[
                "data"
            ]
            assert any(evidence["image_code"] in message["text"] for message in restored["recent_messages"])
            image = await client.get(prefix + f"/attachments/{evidence['attachment_id']}/content")
            assert image.status_code == 200 and image.content.startswith(b"\xff\xd8\xff")
            evidence["checks"].append("image_and_reply_preserved_after_api_restart")
            evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
            print("PASS image and reply persistence after API restart")
            return

        code = "PHOTO-" + secrets.token_hex(3).upper()
        image = Image.new("RGB", (760, 440), "white")
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 42)
        draw.text((30, 25), code, fill="black", font=font)
        draw.text((30, 80), "iPhone", fill="black", font=font)
        draw.rectangle((70, 150, 230, 380), fill="#145bd5")
        draw.rectangle((300, 220, 570, 380), fill="#ec8a19")
        raw = io.BytesIO()
        image.save(raw, format="PNG")
        (root / "deployment/.run/chat-image-probe.png").write_bytes(raw.getvalue())
        evidence = {"time": datetime.now(UTC).isoformat(), "image_code": code, "checks": []}

        def passed(name):
            evidence["checks"].append(name)
            print("PASS", name, flush=True)

        key = str(uuid4())

        async def upload(content, upload_key):
            return await client.post(
                prefix + "/attachments",
                headers={"Idempotency-Key": upload_key},
                data={"purpose": "CHAT_IMAGE"},
                files={"file": ("synthetic-image.png", content, "image/png")},
            )

        first, replay = await upload(raw.getvalue(), key), await upload(raw.getvalue(), key)
        assert first.status_code == replay.status_code == 201
        attachment = first.json()["data"]
        assert replay.json()["data"]["id"] == attachment["id"]
        evidence["attachment_id"] = attachment["id"]
        passed("private_upload_idempotency")
        content = await client.get(prefix + f"/attachments/{attachment['id']}/content")
        assert content.status_code == 200 and content.content.startswith(b"\xff\xd8\xff")
        assert content.headers["cache-control"] == "private, no-store"
        anonymous = await client.get(
            prefix + f"/attachments/{attachment['id']}/content", headers={"Authorization": ""}
        )
        assert anonymous.status_code == 401
        passed("authenticated_private_content")
        invalid = await upload(b"fake-jpeg-content", str(uuid4()))
        assert invalid.status_code == 415
        passed("fake_image_rejected")
        conversation = await post(
            "/conversations", {"title": "GPT图片闭环验收", "entry_context": {"kind": "GENERAL"}}
        )
        cid = evidence["conversation_id"] = conversation["id"]

        async def wait_reply(sent):
            start = time.monotonic()
            while time.monotonic() - start < 150:
                state = (await client.get(prefix + f"/runs/{sent['run']['id']}")).json()["data"]
                if state["state"] not in {"QUEUED", "RUNNING", "STOP_REQUESTED"}:
                    assert state["state"] == "COMPLETED", (state["state"], state["error_code"])
                    snapshot = (await client.get(prefix + f"/conversations/{cid}")).json()["data"]
                    return next(
                        message["text"]
                        for message in reversed(snapshot["recent_messages"])
                        if message["run_id"] == sent["run"]["id"]
                    )
                await asyncio.sleep(1)
            raise AssertionError("Image run timeout")

        message_id = uuid4()
        sent = await post(
            f"/conversations/{cid}/messages",
            {
                "client_message_id": str(message_id),
                "text": "这是测试用的商品标签图，请读出上方PHOTO开头的标签文字，并说明下面两个图形的颜色。",
                "attachment_ids": [attachment["id"]],
            },
            message_id,
        )
        answer = await wait_reply(sent)
        assert code in answer and "蓝" in answer and ("橙" in answer or "桔" in answer), answer
        evidence["image_run_id"], evidence["image_answer"] = sent["run"]["id"], answer
        passed("gpt_image_observation_and_streamed_reply")
        followup = await post(
            f"/conversations/{cid}/messages",
            {
                "client_message_id": str(uuid4()),
                "text": "刚才那张图的PHOTO开头标签是什么？请只重复该标签。",
                "attachment_ids": [],
            },
        )
        followup_answer = await wait_reply(followup)
        assert code in followup_answer
        evidence["followup_answer"] = followup_answer
        passed("image_followup_uses_bounded_history")
        search = await post(
            f"/conversations/{cid}/messages",
            {
                "client_message_id": str(uuid4()),
                "text": "请按刚才图片里iPhone的线索调用商品查询，列出商城真实商品ID和价格，100字内。",
                "attachment_ids": [],
            },
        )
        product_answer = await wait_reply(search)
        assert "37" in product_answer and "5999" in product_answer
        evidence["product_answer"] = product_answer
        passed("visual_clue_to_real_mall_product_tool")
        evidence["result"] = "passed"
        evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    import sys

    run(main("--verify-persistence" in sys.argv))
