import io
import json
from uuid import uuid4

import httpx
import pytest
from PIL import Image

from app.application.attachments import Attachments, decode_image
from app.application.conversations import Conversations
from app.application.runs import Runs
from app.domain.identity import DomainError, Principal
from app.infrastructure.cursors import Cursors
from app.infrastructure.gpt_vision import GptVision
from app.infrastructure.model_ledger import ModelLedger
from app.infrastructure.postgres.database import Database, execute, one
from app.infrastructure.runtime_auth import RuntimeAuth
from app.main import create_app
from app.settings import Settings


def picture(format="PNG"):
    image = Image.new("RGB", (80, 60), "blue")
    exif = Image.Exif()
    exif[270] = "private metadata"
    data = io.BytesIO()
    image.save(data, format=format, exif=exif)
    return data.getvalue()


class MemoryObjects:
    def __init__(self):
        self.data = {}

    def put(self, key, content):
        self.data[key] = content

    def read(self, key):
        return self.data[key]


def test_decoder_checks_real_image_and_removes_metadata():
    result = decode_image(picture())
    with Image.open(io.BytesIO(result)) as image:
        assert image.format == "JPEG"
        assert not image.getexif()
        assert image.size == (80, 60)
    for raw, status in [
        (b"<svg></svg>", 415),
        (b"\x89PNG\r\n\x1a\ninvalid", 422),
        (b"x" * (5 * 1024 * 1024 + 1), 413),
    ]:
        with pytest.raises(DomainError) as caught:
            decode_image(raw)
        assert caught.value.status == status


@pytest.mark.anyio
async def test_gpt_vision_uses_gpt_route_and_keeps_bytes_out_of_observation():
    settings = Settings(
        database_url="postgresql+psycopg://test:test@localhost/test",
        bridge_secret="test",
        broker_secret="test",
        cursor_secret="test",
        dialog_api_key="private-unit-key",
    )
    calls = []

    class Ledger:
        def recorder(self, capability, run_id):
            assert capability == "VISION"

            async def record(data):
                calls.append(data)
                return "image-test"

            return record

    def handler(request):
        body = json.loads(request.content)
        assert request.url.host == "fushengyunsuan.cn"
        assert body["model"] == "gpt-6.1-sol"
        assert body["input"][0]["content"][1]["image_url"].startswith("data:image/jpeg;base64,")
        assert body["tools"] == []
        events = [
            {"type": "response.output_item.added", "item": {"id": "text", "type": "message"}},
            {"type": "response.output_text.delta", "item_id": "text", "delta": "图片可见蓝色矩形。"},
            {
                "type": "response.completed",
                "response": {
                    "id": "vision-unit",
                    "status": "completed",
                    "output": [],
                    "usage": {"input_tokens": 30, "output_tokens": 8},
                },
            },
        ]
        return httpx.Response(
            200, content="".join("data: " + json.dumps(e) + "\n\n" for e in events).encode()
        )

    vision = GptVision(settings, Ledger(), httpx.MockTransport(handler))
    try:
        summary = await vision.understand_image(decode_image(picture()), "图片是什么？")
        assert summary == "图片可见蓝色矩形。"
        assert "base64" not in summary
        assert calls[-1]["status"] == "SUCCEEDED"
    finally:
        await vision.close()


@pytest.mark.anyio
async def test_private_image_owner_binding_idempotency_and_history(integration_config):
    cfg = integration_config
    settings = Settings(
        database_url=cfg["databaseUrl"],
        bridge_secret=cfg["bridgeSecret"],
        broker_secret=cfg["brokerSecret"],
        cursor_secret=cfg["cursorSecret"],
        provider="openai_responses",
        dialog_api_key="unit-only",
        vision_enabled=True,
        attachments_enabled=True,
        objects_access_key="unit",
        objects_secret_key="unit",
    )
    db = Database(cfg["databaseUrl"])
    objects = MemoryObjects()
    attachments = Attachments(db, objects, settings)
    member = Principal(900_000_000 + uuid4().int % 100_000_000, "image-owner")
    other = Principal(900000042, "other")
    models = ModelLedger(db, settings)
    svc = Conversations(db, RuntimeAuth(180), Cursors("test-images"), settings, models)
    runs = Runs(db, settings)
    try:
        await models.setup()
        key = uuid4()
        first = await attachments.upload(member, key, "CHAT_IMAGE", "test.png", picture())
        replay = await attachments.upload(member, key, "CHAT_IMAGE", "test.png", picture())
        assert first["id"] == replay["id"]
        assert (await attachments.read(member, first["id"])).startswith(b"\xff\xd8\xff")
        with pytest.raises(DomainError) as denied:
            await attachments.read(other, first["id"])
        assert denied.value.status == 404
        second = await attachments.upload(member, uuid4(), "CHAT_IMAGE", "second.png", picture())
        # Reverse UUID order so the test catches sorting by storage ID instead of message order.
        image_ids = sorted([first["id"], second["id"]], reverse=True)
        conv = await svc.create(
            member, uuid4(), {"title": "image-test", "entry_context": {"kind": "GENERAL"}}
        )
        payload = {"client_message_id": str(uuid4()), "text": "", "attachment_ids": image_ids}
        sent = await svc.send(member, "synthetic-token", conv["id"], uuid4(), payload)
        assert [block["attachment"]["id"] for block in sent["message"]["blocks"]] == image_ids
        replay = await svc.send(member, "synthetic-token", conv["id"], uuid4(), payload)
        assert replay["message"]["id"] == sent["message"]["id"]
        with pytest.raises(DomainError) as reused:
            await svc.send(member, "synthetic-token", conv["id"], uuid4(), payload | {"attachment_ids": []})
        assert reused.value.code == "MESSAGE_ID_REUSED"
        claimed = await runs.claim("image-unit-worker")
        assert str(claimed["id"]) == sent["run"]["id"]
        images = await attachments.for_run(claimed)
        assert [str(image["id"]) for image in images] == image_ids
        await runs.save_image_observation(claimed, images[0], "蓝色矩形。", models.profiles["VISION"])
        await runs.save_image_observation(claimed, images[1], "橙色矩形。", models.profiles["VISION"])
        await runs.append(claimed, "图片看起来是蓝色矩形。")
        await runs.finish(claimed)
        followup = await svc.send(
            member,
            "synthetic-token",
            conv["id"],
            uuid4(),
            {"client_message_id": str(uuid4()), "text": "刚才是什么颜色？", "attachment_ids": []},
        )
        claimed2 = await runs.claim("image-followup-worker")
        assert str(claimed2["id"]) == followup["run"]["id"]
        history = await runs.history(claimed2)
        assert any("此前图片观察" in item["content"] and "蓝色" in item["content"] for item in history)
        observation = next(item["content"] for item in history if "此前图片观察" in item["content"])
        assert observation.index("第1张图片：蓝色") < observation.index("第2张图片：橙色")
        assert "base64" not in json.dumps(history)
        await runs.stop(member, claimed2["id"], uuid4(), {"expected_version": claimed2["version"]})
        with pytest.raises(DomainError) as stopped:
            await runs.save_image_observation(
                claimed2, images[0], "迟到图片观察。", models.profiles["VISION"]
            )
        assert stopped.value.code == "LEASE_LOST"
        async with db.transaction() as conn:
            slot = await one(
                conn, "SELECT slot_no FROM ag_generation_slot WHERE run_id=:id", id=claimed2["id"]
            )
        await runs.finish(claimed2)
        # No paid upstream request exists in this test. End only this synthetic slot's cooldown.
        async with db.transaction() as conn:
            await execute(
                conn,
                "UPDATE ag_generation_slot SET lease_until=NULL WHERE slot_no=:slot AND run_id IS NULL",
                slot=slot["slot_no"],
            )
        async with db.transaction() as conn:
            record = await one(
                conn, "SELECT count(*) AS n FROM ag_attachment_ref WHERE attachment_id=:id", id=first["id"]
            )
            assert record["n"] == 1
        other_conv = await svc.create(
            other, uuid4(), {"title": "other-image", "entry_context": {"kind": "GENERAL"}}
        )
        with pytest.raises(DomainError) as cross:
            await svc.send(
                other,
                "synthetic-token",
                other_conv["id"],
                uuid4(),
                payload | {"client_message_id": str(uuid4())},
            )
        assert cross.value.status == 404
    finally:
        await db.engine.dispose()


@pytest.mark.anyio
async def test_upload_route_checks_multipart_bytes_and_auth(integration_config):
    cfg = integration_config
    settings = Settings(
        database_url=cfg["databaseUrl"],
        bridge_secret=cfg["bridgeSecret"],
        broker_secret=cfg["brokerSecret"],
        cursor_secret=cfg["cursorSecret"],
        attachments_enabled=True,
        objects_access_key="unit",
        objects_secret_key="unit",
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        svc = app.state.services
        svc.attachments.objects = MemoryObjects()
        owner_id = 900_000_000 + uuid4().int % 100_000_000

        async def identity(token):
            return Principal(owner_id if token == "Bearer image-owner" else 900000044, "image-api-test")

        svc.bridge.identity = identity
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            prefix = "/mall-agent/v1/member/attachments"
            assert (await client.post(prefix)).status_code == 401
            headers = {"Authorization": "Bearer image-owner", "Idempotency-Key": str(uuid4())}
            response = await client.post(
                prefix,
                headers=headers,
                data={"purpose": "CHAT_IMAGE"},
                files={"file": ("image.png", picture(), "application/octet-stream")},
            )
            assert response.status_code == 201
            aid = response.json()["data"]["id"]
            read = await client.get(prefix + f"/{aid}/content", headers=headers)
            assert read.status_code == 200 and read.headers["cache-control"] == "private, no-store"
            assert (
                await client.get(prefix + f"/{aid}/content", headers={"Authorization": "Bearer another"})
            ).status_code == 404
            bad = await client.post(
                prefix,
                headers={**headers, "Idempotency-Key": str(uuid4())},
                data={"purpose": "CHAT_IMAGE"},
                files={"file": ("fake.jpg", b"<svg/>", "image/jpeg")},
            )
            assert bad.status_code == 415
            too_large = await client.post(
                prefix,
                headers={
                    **headers,
                    "Content-Type": "multipart/form-data; boundary=test",
                    "Content-Length": str(6 * 1024 * 1024),
                },
                content=b"bad",
            )
            assert too_large.status_code == 413
            malformed = await client.post(
                prefix,
                headers={**headers, "Content-Type": "multipart/form-data; boundary=test"},
                content=b"invalid multipart body",
            )
            assert malformed.status_code == 422

            async def oversized_stream():
                yield (
                    b'--test\r\nContent-Disposition: form-data; name="file"; '
                    b'filename="image.png"\r\nContent-Type: image/png\r\n\r\n'
                )
                for _ in range(6):
                    yield b"x" * (1024 * 1024)
                yield b"\r\n--test--\r\n"

            chunked = await client.post(
                prefix,
                headers={**headers, "Content-Type": "multipart/form-data; boundary=test"},
                content=oversized_stream(),
            )
            assert chunked.status_code == 413
