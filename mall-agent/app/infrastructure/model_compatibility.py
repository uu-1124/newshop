"""Explicit, billed compatibility jobs. Health checks never invoke these probes."""

import io
import json
import wave
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.capabilities import JobType
from app.domain.identity import DomainError
from app.infrastructure.aliyun_models import AliyunModels
from app.infrastructure.dialog_provider import ResponsesDialogProvider
from app.infrastructure.gpt_vision import GptVision
from app.infrastructure.model_config import (
    ADAPTER_VERSION,
    configuration_hash,
    resolve_settings,
    validate_profile,
)
from app.infrastructure.model_ledger import ModelLedger
from app.infrastructure.model_quota import model_actor
from app.infrastructure.postgres.database import one


class CheckInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: UUID
    profile_version: int = Field(ge=1)
    configuration_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    adapter_version: str


def register_model_checks(registry, db, settings):
    def validate(data):
        try:
            value = CheckInput.model_validate(data)
            if value.adapter_version != ADAPTER_VERSION:
                raise ValueError()
            return value.model_dump(mode="json")
        except ValueError:
            raise DomainError("VALIDATION_ERROR", "兼容验收参数不正确", 422) from None

    async def check(lease):
        parameters = validate(lease.parameters)
        async with db.transaction(snapshot=True) as conn:
            job = await one(conn, "SELECT created_by FROM ag_job WHERE id=:id", id=lease.id)
            version = await one(
                conn,
                "SELECT * FROM ag_model_profile_version WHERE profile_id=:id AND version_no=:v",
                id=parameters["profile_id"],
                v=parameters["profile_version"],
            )
        if not version or configuration_hash(version) != parameters["configuration_sha256"]:
            raise DomainError("MODEL_CONFIG_CHANGED", "待验收配置版本不匹配", 409)
        validate_profile(settings, version, legacy=True)
        cap = version["capability"]
        ledger = ModelLedger(db, resolve_settings(settings, {cap: version}))
        ledger.profiles = {
            cap: {
                "id": str(version["profile_id"]),
                "version": version["version_no"],
                "model": version["model_name"],
                "enabled": True,
            }
        }
        ledger.records = {cap: version}
        actor_context = model_actor.set(("STAFF", job["created_by"]) if job else ("SYSTEM", None))
        try:
            await probe(cap, ledger)
        finally:
            model_actor.reset(actor_context)
        return parameters | {
            "passed": True,
            "kind": "adapter-protocol-v1",
            "semantic_accuracy_evaluated": False,
        }

    registry.register(JobType.MODEL_COMPATIBILITY, check, "agent:model:write", validate, timeout=180)


async def probe(cap, ledger):
    if ledger.settings.provider == "fixture":
        raise DomainError("LIVE_PROVIDER_REQUIRED", "兼容验收需要真实供应商配置", 409)
    if cap == "DIALOG":
        provider = ResponsesDialogProvider(ledger.settings)
        tool = {
            "type": "function",
            "name": "compat_echo",
            "description": "兼容测试：返回ok=true，不执行任何业务操作。",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": False,
            },
        }
        inputs = [{"role": "user", "content": "请调用 compat_echo，参数ok必须为true。"}]
        try:
            output = []
            async for event in provider.stream_turn(
                inputs, "这是隔离协议验收。必须调用compat_echo且ok=true。", [tool], ledger.recorder(cap)
            ):
                if event.kind == "completed":
                    output = event.data
            calls = [item for item in output if item.get("type") == "function_call"]
            if (
                len(calls) != 1
                or calls[0].get("name") != "compat_echo"
                or json.loads(calls[0].get("arguments", "{}")) != {"ok": True}
            ):
                raise DomainError("MODEL_TOOL_COMPATIBILITY_FAILED", "模型未通过严格工具调用验收", 422)
            inputs += output + [
                {"type": "function_call_output", "call_id": calls[0]["call_id"], "output": '{"ok":true}'}
            ]
            schema = {
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": False,
            }
            text = []
            async for event in provider.stream_turn(
                inputs, "根据工具结果返回JSON，ok=true。", [], ledger.recorder(cap), schema
            ):
                if event.kind == "text":
                    text.append(event.data)
            if json.loads("".join(text)) != {"ok": True}:
                raise DomainError("MODEL_SCHEMA_COMPATIBILITY_FAILED", "模型未通过结构化回复验收", 422)
        finally:
            await provider.close()
    elif cap == "VISION":
        from PIL import Image

        raw = io.BytesIO()
        Image.new("RGB", (64, 64), "blue").save(raw, format="JPEG")
        vision = GptVision(ledger.settings, ledger)
        try:
            await vision.understand_image(raw.getvalue(), "描述图片的颜色")
        finally:
            await vision.close()
    else:
        models = AliyunModels(ledger.settings, ledger)
        try:
            if cap == "EMBEDDING":
                await models.embed(["商城模型兼容测试"])
            elif cap == "RERANK":
                if not await models.rerank("蓝色商品", ["蓝色商品", "红色商品"], 2):
                    raise DomainError("MODEL_PROTOCOL_ERROR", "重排结果为空", 422)
            elif cap == "ASR":
                raw = io.BytesIO()
                with wave.open(raw, "wb") as audio:
                    audio.setnchannels(1)
                    audio.setsampwidth(2)
                    audio.setframerate(16000)
                    audio.writeframes(b"\0\0" * 16000)
                if not isinstance(await models.transcribe(raw.getvalue()), str):
                    raise DomainError("MODEL_PROTOCOL_ERROR", "转写协议不匹配", 422)
            elif cap == "TTS":
                if not await models.synthesize("模型兼容测试。"):
                    raise DomainError("MODEL_PROTOCOL_ERROR", "合成结果为空", 422)
        finally:
            await models.close()
