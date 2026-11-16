import base64
import io
import json
import wave
from uuid import uuid4

import httpx
import pytest
from test_handoffs import actor, conversation
from test_runtime import settings_for

from app.application.media import speech_parts
from app.bootstrap import Services
from app.domain.identity import DomainError
from app.infrastructure.aliyun_models import AliyunModels
from app.infrastructure.audio import normalize_supplier_wav, wav_info
from app.infrastructure.postgres.database import encoded, execute, one, rows


def audio(seconds=1):
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(b"\0\0" * (seconds * 16000))
    return output.getvalue()


class Objects:
    def __init__(self):
        self.data = {}

    def put(self, key, raw, content_type="image/jpeg"):
        self.data[key] = raw

    def read(self, key):
        return self.data[key]


def test_wav_size_duration_and_speech_parts():
    assert wav_info(audio()) == 1000
    assert wav_info(audio(60)) == 60000
    for raw in [b"fake WAV", audio(61), audio()[:-10]]:
        with pytest.raises(DomainError):
            wav_info(raw)
    parts = speech_parts("有据可查的回答。" * 100)
    assert "".join(parts) == "有据可查的回答。" * 100 and all(len(p) <= 200 for p in parts)
    import struct

    streaming = bytearray(audio())
    struct.pack_into("<I", streaming, 4, 0x7FFFFFBF)
    struct.pack_into("<I", streaming, 40, 0x7FFFFF9B)
    with pytest.raises(DomainError):
        wav_info(bytes(streaming))
    assert normalize_supplier_wav(bytes(streaming)) == audio()


@pytest.mark.anyio
async def test_media_private_transcription_cache_parts_and_call_ownership(integration_config):
    svc = Services(
        settings_for(
            integration_config,
            attachments_enabled=True,
            objects_access_key="unit",
            objects_secret_key="unit",
            ali_api_key="synthetic-key",
        )
    )
    svc.attachments.objects = Objects()
    member, other = actor(), actor()
    calls = []

    def upstream(request):
        data = json.loads(request.content)
        calls.append(data)
        if request.url.path.endswith("chat/completions"):
            return httpx.Response(
                200,
                json={
                    "id": "synthetic-asr",
                    "choices": [{"message": {"content": "我想咨询订单"}}],
                    "usage": {"prompt_tokens": 8, "completion_tokens": 6, "seconds": 1},
                },
            )
        return httpx.Response(
            200,
            json={
                "request_id": "synthetic-tts",
                "output": {"audio": {"data": base64.b64encode(audio()).decode()}},
                "usage": {"characters": len(data["input"]["text"])},
            },
        )

    svc.media.adapter_factory = lambda settings, ledger: AliyunModels(
        settings, ledger, httpx.MockTransport(upstream)
    )
    try:
        await svc.models.setup()
        key = uuid4()
        uploaded = await svc.media.upload(member, key, "sample.wav", audio())
        assert json.loads(encoded(await svc.media.upload(member, key, "sample.wav", audio()))) == json.loads(
            encoded(uploaded)
        )
        with pytest.raises(DomainError, match="NOT_FOUND"):
            await svc.media.transcribe(other, uploaded["id"], uuid4())
        asr_key = uuid4()
        result = await svc.media.transcribe(member, uploaded["id"], asr_key)
        assert result == {"text": "我想咨询订单", "duration_ms": 1000, "editable": True}
        assert await svc.media.transcribe(member, uploaded["id"], asr_key) == result and len(calls) == 1
        conv = await conversation(svc, member)
        mid = uuid4()
        async with svc.db.transaction() as conn:
            await execute(
                conn,
                """INSERT INTO ag_message(id,conversation_id,seq,sender_kind,sender_id,text_content,status,retention_until)
                VALUES(:id,:cid,1,'ASSISTANT',0,:text,'COMPLETED',now()+interval '30 days')""",
                id=mid,
                cid=conv["id"],
                text="本人可见回答。" * 50,
            )
            assert (
                await one(conn, "SELECT count(*) n FROM ag_run WHERE conversation_id=:id", id=conv["id"])
            )["n"] == 0
        speech_key = uuid4()
        first = await svc.media.speech(member, mid, speech_key, {"part": 0})
        assert not first["cache_hit"] and first["part_count"] == 2
        assert await svc.media.read(member, first["attachment"]["id"]) == audio()
        assert json.loads(
            encoded(await svc.media.speech(member, mid, speech_key, {"part": 0}))
        ) == json.loads(encoded(first))
        cached = await svc.media.speech(member, mid, uuid4(), {"part": 0})
        assert cached["cache_hit"] and len(calls) == 2
        second = await svc.media.speech(member, mid, uuid4(), {"part": 1})
        assert second["part_index"] == 1 and len(calls) == 3
        with pytest.raises(DomainError, match="NOT_FOUND"):
            await svc.media.read(other, first["attachment"]["id"])
        with pytest.raises(DomainError, match="NOT_FOUND"):
            await svc.media.speech(other, mid, uuid4(), {"part": 0})
        with pytest.raises(DomainError, match="INVALID_SPEECH_PART"):
            await svc.media.speech(member, mid, uuid4(), {"part": 2})
        async with svc.db.transaction() as conn:
            records = await rows(
                conn,
                """SELECT c.*,q.actor_kind,q.actor_id,r.member_id FROM ag_model_call c
                JOIN ag_media_request r ON r.id=c.media_request_id JOIN ag_model_meter q ON q.call_id=c.id
                WHERE r.member_id=:member""",
                member=member.id,
            )
            assert len(records) == 3
            assert all(
                r["actor_kind"] == "MEMBER"
                and r["actor_id"] == member.id
                and r["run_id"] is None
                and r["job_id"] is None
                for r in records
            )
            await execute(
                conn,
                "UPDATE ag_attachment SET retention_until=now()-interval '1 second' WHERE id=:id",
                id=second["attachment"]["id"],
            )
        with pytest.raises(DomainError, match="NOT_FOUND"):
            await svc.media.read(member, second["attachment"]["id"])
        snap = await svc.conversations.snapshot(member, conv["id"])
        await svc.conversations.delete(
            member, conv["id"], uuid4(), {"expected_version": snap["conversation"]["version"]}
        )
        with pytest.raises(DomainError, match="NOT_FOUND"):
            await svc.media.read(member, first["attachment"]["id"])
    finally:
        await svc.close()


@pytest.mark.anyio
async def test_audio_retention_releases_speech_refs_and_redacts_transcription(integration_config):
    from datetime import UTC, datetime

    from app.application.governance import PublicPolicy
    from app.application.retention import Retention

    svc = Services(settings_for(integration_config))
    try:
        member = actor()
        conv = await conversation(svc, member)
        mid, aid, rid = uuid4(), uuid4(), uuid4()
        async with svc.db.transaction() as conn:
            await execute(
                conn,
                """INSERT INTO ag_message(id,conversation_id,seq,sender_kind,sender_id,text_content,status,retention_until)
                VALUES(:id,:cid,1,'ASSISTANT',0,'answer','COMPLETED',now()+interval '30 days')""",
                id=mid,
                cid=conv["id"],
            )
            await execute(
                conn,
                """INSERT INTO ag_attachment(id,owner_kind,owner_id,purpose,object_key,original_name,mime_type,byte_size,sha256,state,created_at,retention_until)
                VALUES(:id,'MEMBER',:member,'SPEECH',:key,'speech.wav','audio/wav',10,:sha,'READY',now()-interval '8 days',now()-interval '1 day')""",
                id=aid,
                member=member.id,
                key=f"chat/test/{aid}.wav",
                sha="a" * 64,
            )
            await execute(
                conn,
                "INSERT INTO ag_attachment_ref(id,attachment_id,ref_kind,message_id) VALUES(:id,:aid,'SPEECH',:mid)",
                id=uuid4(),
                aid=aid,
                mid=mid,
            )
            await execute(
                conn,
                """INSERT INTO ag_media_request(id,member_id,kind,idempotency_key,request_hash,message_id,lease_owner,state,result,retention_until)
                VALUES(:id,:member,'TTS',:key,:hash,:mid,'test','SUCCEEDED',CAST(:result AS jsonb),now()-interval '1 second')""",
                id=rid,
                member=member.id,
                key=uuid4(),
                hash="a" * 64,
                mid=mid,
                result=json.dumps({"text": "expired private text"}),
            )
            retention = Retention(svc.db, svc.runs.settings, svc.jobs)
            await retention.sweep_attachments(conn, PublicPolicy(), datetime.now(UTC))
            assert (await one(conn, "SELECT state FROM ag_attachment WHERE id=:id", id=aid))[
                "state"
            ] == "DELETING"
            assert (await one(conn, "SELECT result FROM ag_media_request WHERE id=:id", id=rid))[
                "result"
            ] is None
    finally:
        await svc.close()
