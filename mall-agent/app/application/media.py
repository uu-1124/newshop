import asyncio
import hashlib
from uuid import NAMESPACE_URL, uuid4, uuid5

from app.application.attachments import attachment_view
from app.application.conversations import dedupe, owned, save_dedupe
from app.application.expiry import deadline_sql, visible_sql
from app.application.governance import read_policy
from app.domain.identity import DomainError, Principal
from app.infrastructure.aliyun_models import AliyunModels
from app.infrastructure.audio import normalize_supplier_wav, wav_info
from app.infrastructure.model_call_context import ModelCallContext, model_call_scope, source_actor
from app.infrastructure.postgres.database import digest, encoded, execute, one


def speech_parts(text):
    # Each chunk fits the current adapter; play successive chunks to read the full answer.
    text = text.strip()
    if not text or len(text) > 10000:
        raise DomainError("SPEECH_TEXT_UNAVAILABLE", "该回答没有可播报的正文", 409)
    return [text[start : start + 200] for start in range(0, len(text), 200)]


class Media:
    def __init__(self, db, attachments, models, settings):
        self.db, self.attachments, self.models, self.settings = db, attachments, models, settings
        self.adapter_factory = AliyunModels

    def require_enabled(self):
        self.attachments.require_enabled()
        if not self.settings.ali_api_key.get_secret_value():
            raise DomainError("FEATURE_NOT_ACTIVE", "语音服务尚未配置，可继续使用文字", 409)

    async def recording(self, conn, principal, aid, lock=False):
        item = await one(
            conn,
            f"""SELECT * FROM ag_attachment a WHERE id=:id AND owner_kind='MEMBER'
            AND owner_id=:member AND purpose='RECORDING' AND state='READY'
            AND {deadline_sql("a", "audio_retention_days", 7)}>clock_timestamp()"""
            + (" FOR UPDATE" if lock else ""),
            id=aid,
            member=principal.id,
        )
        if not item:
            raise DomainError("NOT_FOUND", "录音不存在或已失效", 404)
        return item

    async def message(self, conn, principal, mid, lock=False):
        message = await one(conn, "SELECT conversation_id FROM ag_message WHERE id=:id", id=mid)
        if not message:
            raise DomainError("NOT_FOUND", "回答不存在或已失效", 404)
        await owned(conn, message["conversation_id"], principal.id, lock=lock)
        message = await one(conn, f"SELECT * FROM ag_message m WHERE id=:id AND {visible_sql()}", id=mid)
        if not message:
            raise DomainError("NOT_FOUND", "回答不存在或已失效", 404)
        if message["sender_kind"] != "ASSISTANT" or message["status"] != "COMPLETED":
            raise DomainError("SPEECH_NOT_ALLOWED", "只能播报已完成的智能助手回答", 409)
        return message

    async def upload(self, principal, key, name, raw):
        self.require_enabled()
        duration = wav_info(raw)
        sha = hashlib.sha256(raw).hexdigest()
        body = {"purpose": "RECORDING", "sha": sha, "name": name[:255]}
        aid = uuid5(NAMESPACE_URL, f"mall-agent/recording/{principal.id}/{key}")
        object_key = f"chat/{principal.id}/{aid}.wav"
        async with self.db.transaction() as conn:
            cached = await dedupe(conn, "uploadMemberAttachment", principal.id, key, body)
            if cached:
                await self.recording(conn, principal, cached["id"])
                return cached
            await execute(
                conn,
                "SELECT pg_advisory_xact_lock(hashtextextended(:key,0))",
                key=f"media-upload:{principal.id}",
            )
            existing = await one(conn, "SELECT * FROM ag_attachment WHERE id=:id FOR UPDATE", id=aid)
            if existing and (existing["sha256"] != sha or existing["state"] not in ("UPLOADING", "FAILED")):
                raise DomainError("ATTACHMENT_UNAVAILABLE", "原录音已处理或清理，请重新录制", 409)
            if not existing:
                count = await one(
                    conn,
                    "SELECT count(*) n FROM ag_attachment WHERE owner_kind='MEMBER' AND owner_id=:id AND purpose='RECORDING' AND created_at>now()-interval '1 day'",
                    id=principal.id,
                )
                if count["n"] >= 20:
                    raise DomainError("ATTACHMENT_QUOTA_EXCEEDED", "已达到今日录音次数上限", 429)
                days = (await read_policy(conn))["value"]["audio_retention_days"]
                await execute(
                    conn,
                    """INSERT INTO ag_attachment(id,owner_kind,owner_id,purpose,object_key,original_name,mime_type,byte_size,sha256,duration_ms,retention_until)
                    VALUES(:id,'MEMBER',:member,'RECORDING',:key,'语音录音.wav','audio/wav',:size,:sha,:duration,now()+make_interval(days=>:days))""",
                    id=aid,
                    member=principal.id,
                    key=object_key,
                    size=len(raw),
                    sha=sha,
                    duration=duration,
                    days=days,
                )
        await asyncio.to_thread(self.attachments.objects.put, object_key, raw, "audio/wav")
        async with self.db.transaction() as conn:
            cached = await dedupe(conn, "uploadMemberAttachment", principal.id, key, body)
            if cached:
                await self.recording(conn, principal, cached["id"])
                return cached
            item = await one(
                conn,
                "UPDATE ag_attachment SET state='READY',updated_at=now(),version=version+1 WHERE id=:id AND state IN ('UPLOADING','FAILED') RETURNING *",
                id=aid,
            )
            if not item:
                raise DomainError("ATTACHMENT_UNAVAILABLE", "录音已进入清理，请重新录制", 410)
            result = attachment_view(item)
            await save_dedupe(conn, "uploadMemberAttachment", principal.id, key, body, result)
            return result

    async def start(self, principal, kind, key, body, aid=None, mid=None):
        async with self.db.transaction() as conn:
            await execute(
                conn,
                "SELECT pg_advisory_xact_lock(hashtextextended(:key,0))",
                key=f"media:{principal.id}:{kind}:{key}",
            )
            target = (
                await self.recording(conn, principal, aid)
                if aid
                else await self.message(conn, principal, mid)
            )
            old = await one(
                conn,
                "SELECT * FROM ag_media_request WHERE member_id=:member AND kind=:kind AND idempotency_key=:key",
                member=principal.id,
                kind=kind,
                key=key,
            )
            if old:
                if old["request_hash"] != digest(body):
                    raise DomainError("IDEMPOTENCY_CONFLICT", "语音请求编号已用于不同内容", 409)
                if not await one(
                    conn,
                    "SELECT id FROM ag_media_request WHERE id=:id AND retention_until>clock_timestamp()",
                    id=old["id"],
                ):
                    raise DomainError("MEDIA_REQUEST_EXPIRED", "原语音请求已到期，请重新发起", 410)
                if old["state"] == "SUCCEEDED":
                    return old, target, old["result"]
                raise DomainError(
                    old["error_code"] or "MEDIA_REQUEST_IN_PROGRESS",
                    "该语音请求正在处理或已中断，请稍后重试或发起新请求",
                    409,
                )
            rid = uuid4()
            row = await one(
                conn,
                """INSERT INTO ag_media_request(id,member_id,kind,idempotency_key,request_hash,attachment_id,message_id,lease_owner,retention_until)
                VALUES(:id,:member,:kind,:key,:hash,:aid,:mid,:owner,LEAST(:until,now()+interval '7 days')) RETURNING *""",
                id=rid,
                member=principal.id,
                kind=kind,
                key=key,
                hash=digest(body),
                aid=aid,
                mid=mid,
                owner=f"media:{rid}",
                until=target["retention_until"],
            )
            return row, target, None

    async def finish(self, request, result=None, error=None):
        async with self.db.transaction() as conn:
            if not error:
                principal = Principal(request["member_id"], "media-owner")
                if request["message_id"]:
                    await self.message(conn, principal, request["message_id"], lock=True)
                else:
                    await self.recording(conn, principal, request["attachment_id"], lock=True)
                await source_actor(
                    conn,
                    ModelCallContext(
                        media_request_id=request["id"], lease_owner=request["lease_owner"], fencing_token=1
                    ),
                    None,
                    None,
                )
            await execute(
                conn,
                """UPDATE ag_media_request SET state=:state,result=CAST(:result AS jsonb),error_code=:error
                WHERE id=:id AND state='RUNNING' AND lease_owner=:owner""",
                id=request["id"],
                owner=request["lease_owner"],
                state="FAILED" if error else "SUCCEEDED",
                result=encoded(result) if result else None,
                error=error,
            )

    async def transcribe(self, principal, aid, key):
        self.require_enabled()
        request, recording, cached = await self.start(
            principal, "ASR", key, {"attachment_id": str(aid)}, aid=aid
        )
        if cached:
            return cached
        adapter = None
        try:
            frozen = await self.models.for_snapshot(await self.models.capture())
            adapter = self.adapter_factory(frozen.settings, frozen)
            raw = await asyncio.to_thread(self.attachments.objects.read, recording["object_key"])
            if hashlib.sha256(raw).hexdigest() != recording["sha256"]:
                raise DomainError("ATTACHMENT_INTEGRITY_ERROR", "录音校验失败", 503)
            with model_call_scope(
                ModelCallContext(
                    media_request_id=request["id"], lease_owner=request["lease_owner"], fencing_token=1
                )
            ):
                text = await adapter.transcribe(raw, "audio/wav")
            if not isinstance(text, str) or not text.strip() or len(text) > 10000:
                raise DomainError("TRANSCRIPTION_EMPTY", "未识别到有效文字，请重录或直接输入", 422)
            async with self.db.transaction() as conn:
                await self.recording(conn, principal, aid)
            result = {"text": text.strip(), "duration_ms": recording["duration_ms"], "editable": True}
            await self.finish(request, result)
            return result
        except BaseException as exc:
            await self.finish(
                request, error=exc.code if isinstance(exc, DomainError) else "MEDIA_REQUEST_INTERRUPTED"
            )
            raise
        finally:
            if adapter:
                await adapter.close()

    async def speech(self, principal, mid, key, body):
        self.require_enabled()
        async with self.db.transaction() as conn:
            message = await self.message(conn, principal, mid)
        parts = speech_parts(message["text_content"])
        index = body.get("part", 0)
        if index >= len(parts):
            raise DomainError("INVALID_SPEECH_PART", "播报段落不存在", 422)
        frozen = await self.models.for_snapshot(await self.models.capture())
        voice = frozen.records["TTS"]["public_options"].get("voice", "Cherry")
        if body.get("voice", voice) != voice:
            raise DomainError("INVALID_VOICE", "请选择已配置的播报音色", 422)
        cache_key = digest(
            {
                "message": str(mid),
                "text": message["text_content"],
                "part": index,
                "voice": voice,
                "profile": frozen.profiles["TTS"],
            }
        )
        aid = uuid5(NAMESPACE_URL, "mall-agent/speech/" + cache_key)
        request, _, replay = await self.start(principal, "TTS", key, body | {"message_id": str(mid)}, mid=mid)
        if replay:
            async with self.db.transaction() as conn:
                await self.audio_item(conn, principal, replay["attachment"]["id"])
            return replay
        adapter = None
        staged_id = None
        try:
            async with self.db.transaction() as conn:
                # Serialize the cache without holding a database lock across provider IO.
                await execute(
                    conn,
                    "SELECT pg_advisory_xact_lock(hashtextextended(:key,0))",
                    key="speech-cache:" + cache_key,
                )
                await execute(
                    conn,
                    "UPDATE ag_media_request SET cache_key=:cache WHERE id=:id",
                    cache=cache_key,
                    id=request["id"],
                )
                await self.message(conn, principal, mid, lock=True)
                item = await one(
                    conn,
                    f"""SELECT a.* FROM ag_media_request r JOIN ag_attachment a ON a.id=CAST(r.result->'attachment'->>'id' AS uuid)
                        WHERE r.cache_key=:cache AND r.state='SUCCEEDED' AND a.state='READY'
                        AND {deadline_sql("a", "tts_retention_days", 7)}>clock_timestamp() ORDER BY r.created_at DESC LIMIT 1""",
                    cache=cache_key,
                )
                if not item:
                    if await one(
                        conn,
                        "SELECT id FROM ag_media_request WHERE cache_key=:cache AND id<>:id AND state='RUNNING' AND lease_until>clock_timestamp() LIMIT 1",
                        cache=cache_key,
                        id=request["id"],
                    ):
                        raise DomainError(
                            "MEDIA_REQUEST_IN_PROGRESS", "同一回答播报正在生成，请稍后重试", 409
                        )
                    existing = await one(
                        conn,
                        "SELECT *,updated_at>clock_timestamp()-interval '5 minutes' AS recent FROM ag_attachment WHERE id=:id",
                        id=aid,
                    )
                    if existing:
                        if existing["state"] == "UPLOADING" and existing["recent"]:
                            raise DomainError(
                                "MEDIA_REQUEST_IN_PROGRESS", "同一回答播报正在生成，请稍后重试", 409
                            )
                        # Expired entries are immutable; a fresh request gets a fresh object identity.
                        aid = uuid5(NAMESPACE_URL, f"mall-agent/speech/{cache_key}/{request['id']}")
                    days = (await read_policy(conn))["value"]["tts_retention_days"]
                    object_key = f"chat/{principal.id}/{aid}.wav"
                    await execute(
                        conn,
                        """INSERT INTO ag_attachment(id,owner_kind,owner_id,purpose,object_key,original_name,mime_type,byte_size,sha256,retention_until)
                        VALUES(:id,'MEMBER',:member,'SPEECH',:key,'回答播报.wav','audio/wav',1,:sha,LEAST(:until,now()+make_interval(days=>:days)))""",
                        id=aid,
                        member=principal.id,
                        key=object_key,
                        sha="0" * 64,
                        until=message["retention_until"],
                        days=days,
                    )
                    staged_id = aid
            cache_hit = bool(item)
            if not item:
                adapter = self.adapter_factory(frozen.settings, frozen)
                with model_call_scope(
                    ModelCallContext(
                        media_request_id=request["id"], lease_owner=request["lease_owner"], fencing_token=1
                    )
                ):
                    raw = normalize_supplier_wav(await adapter.synthesize(parts[index]))
                duration = wav_info(raw)
                object_key = f"chat/{principal.id}/{aid}.wav"
                await asyncio.to_thread(self.attachments.objects.put, object_key, raw, "audio/wav")
                async with self.db.transaction() as conn:
                    await self.message(conn, principal, mid, lock=True)
                    item = await one(
                        conn,
                        "UPDATE ag_attachment SET state='READY',updated_at=now(),byte_size=:size,sha256=:sha,duration_ms=:duration WHERE id=:id AND state='UPLOADING' RETURNING *",
                        id=aid,
                        size=len(raw),
                        sha=hashlib.sha256(raw).hexdigest(),
                        duration=duration,
                    )
                    if not item:
                        raise DomainError("ATTACHMENT_UNAVAILABLE", "播报已进入清理", 410)
                    await execute(
                        conn,
                        "INSERT INTO ag_attachment_ref(id,attachment_id,ref_kind,message_id) VALUES(:id,:aid,'SPEECH',:mid)",
                        id=uuid4(),
                        aid=aid,
                        mid=mid,
                    )
            result = {
                "attachment": attachment_view(item),
                "cache_hit": cache_hit,
                "part_index": index,
                "part_count": len(parts),
            }
            await self.finish(request, result)
            return result
        except BaseException as exc:
            if staged_id:
                async with self.db.transaction() as conn:
                    await execute(
                        conn,
                        "UPDATE ag_attachment SET state='FAILED',updated_at=now() WHERE id=:id AND state='UPLOADING'",
                        id=staged_id,
                    )
            await self.finish(
                request, error=exc.code if isinstance(exc, DomainError) else "MEDIA_REQUEST_INTERRUPTED"
            )
            raise
        finally:
            if adapter:
                await adapter.close()

    async def audio_item(self, conn, principal, aid):
        item = await one(
            conn,
            """SELECT * FROM ag_attachment a WHERE id=:id AND owner_kind='MEMBER'
            AND owner_id=:member AND purpose IN ('RECORDING','SPEECH') AND state='READY'
            AND LEAST(retention_until,created_at+make_interval(days=>CASE purpose WHEN 'RECORDING' THEN
                COALESCE((SELECT (value->>'audio_retention_days')::int FROM ag_setting WHERE key='agent-runtime-v1'),7)
                ELSE COALESCE((SELECT (value->>'tts_retention_days')::int FROM ag_setting WHERE key='agent-runtime-v1'),7) END))>clock_timestamp()""",
            id=aid,
            member=principal.id,
        )
        if not item:
            raise DomainError("NOT_FOUND", "音频不存在或已失效", 404)
        if item["purpose"] == "SPEECH":
            ref = await one(
                conn,
                "SELECT message_id FROM ag_attachment_ref WHERE attachment_id=:id AND ref_kind='SPEECH' AND released_at IS NULL",
                id=aid,
            )
            if not ref:
                raise DomainError("NOT_FOUND", "播报来源已失效", 404)
            await self.message(conn, principal, ref["message_id"])
        return item

    async def read(self, principal, aid):
        self.attachments.require_enabled()
        async with self.db.transaction() as conn:
            item = await self.audio_item(conn, principal, aid)
        raw = await asyncio.to_thread(self.attachments.objects.read, item["object_key"])
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise DomainError("ATTACHMENT_INTEGRITY_ERROR", "音频校验失败", 503)
        async with self.db.transaction() as conn:
            await self.audio_item(conn, principal, aid)
        return raw
