"""Non-secret endpoint registry and adapter-supported configuration only."""

import re

from app.domain.identity import DomainError
from app.infrastructure.postgres.database import digest

CAPABILITIES = ("DIALOG", "EMBEDDING", "RERANK", "VISION", "ASR", "TTS")
ENDPOINTS = {
    "DIALOG": "gpt.responses.v1",
    "VISION": "gpt.responses.v1",
    "EMBEDDING": "aliyun.compatible.v1",
    "ASR": "aliyun.compatible.v1",
    "RERANK": "aliyun.native.v1",
    "TTS": "aliyun.native.v1",
}
ADAPTER_VERSION = "model-adapters-20261112.1"


def registry(settings):
    return {
        "gpt.responses.v1": settings.dialog_base_url,
        "aliyun.compatible.v1": settings.ali_compatible_url,
        "aliyun.native.v1": settings.ali_native_url,
    }


def provider_for(capability):
    return "gpt" if capability in {"DIALOG", "VISION"} else "aliyun"


def secret_for(capability):
    return "local-env:dialog.v1" if provider_for(capability) == "gpt" else "local-env:aliyun.v1"


def configured(settings, capability):
    return bool(
        (
            settings.dialog_api_key if provider_for(capability) == "gpt" else settings.ali_api_key
        ).get_secret_value()
    )


def validate_profile(settings, data, legacy=False):
    if not data.get("name", "model").strip():
        raise DomainError("VALIDATION_ERROR", "请填写配置名称", 422)
    cap = data["capability"]
    if cap not in CAPABILITIES or data["provider"] != provider_for(cap):
        raise DomainError("MODEL_ADAPTER_UNSUPPORTED", "模型能力与供应商适配器不匹配", 422)
    endpoint = ENDPOINTS[cap]
    if data["endpoint_ref"] != endpoint and not (
        legacy and data["endpoint_ref"] == registry(settings)[endpoint]
    ):
        raise DomainError("MODEL_ENDPOINT_UNREGISTERED", "请选择已登记的模型端点", 422)
    if data.get("secret_ref", data.get("secret_version_ref")) != secret_for(cap):
        raise DomainError("MODEL_SECRET_UNREGISTERED", "请选择该能力已登记的凭据引用", 422)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}", data["model_name"]):
        raise DomainError("VALIDATION_ERROR", "模型名称格式不正确", 422)
    options = data["public_options"]
    allowed = {
        "DIALOG": {"max_output_tokens", "timeout_seconds", "daily_limit_units"},
        "VISION": {"detail", "max_output_tokens", "timeout_seconds", "daily_limit_units"},
        "EMBEDDING": {"dimension", "daily_limit_units"},
        "RERANK": {"daily_limit_units"},
        "ASR": {"daily_limit_units"},
        "TTS": {"voice", "daily_limit_units"},
    }[cap]
    if not isinstance(options, dict) or set(options) - allowed:
        raise DomainError("VALIDATION_ERROR", "配置包含适配器尚未支持的选项", 422)
    bounds = {
        "max_output_tokens": (128, 4096),
        "timeout_seconds": (10, 120),
        "dimension": (64, 4096),
        "daily_limit_units": (1, 10000),
    }
    for key, value in options.items():
        if key in bounds and (type(value) is not int or not bounds[key][0] <= value <= bounds[key][1]):
            raise DomainError("VALIDATION_ERROR", "模型参数超出适配器范围", 422)
        if key == "detail" and value != "auto" or key == "voice" and value != "Cherry":
            raise DomainError("VALIDATION_ERROR", "请选择已支持的图像精度或音色", 422)
    if cap == "EMBEDDING" and "dimension" not in options:
        raise DomainError("VALIDATION_ERROR", "向量模型必须声明维度", 422)
    return data


def configuration_hash(data):
    return digest(
        {
            key: data.get(key, data.get("secret_version_ref") if key == "secret_ref" else None)
            for key in (
                "capability",
                "provider",
                "model_name",
                "endpoint_ref",
                "secret_ref",
                "public_options",
            )
        }
    )


def resolve_settings(base, profiles):
    update = {}
    names = {
        "DIALOG": "dialog_model",
        "VISION": "vision_model",
        "EMBEDDING": "embedding_model",
        "RERANK": "rerank_model",
        "ASR": "asr_model",
        "TTS": "tts_model",
    }
    for cap, profile in profiles.items():
        validate_profile(base, profile, legacy=True)
        update[names[cap]] = profile["model_name"]
        if cap == "DIALOG":
            update["dialog_max_output_tokens"] = profile["public_options"].get(
                "max_output_tokens", base.dialog_max_output_tokens
            )
            update["model_timeout_seconds"] = profile["public_options"].get(
                "timeout_seconds", base.model_timeout_seconds
            )
        if cap == "EMBEDDING":
            update["embedding_dimension"] = profile["public_options"]["dimension"]
    return base.model_copy(update=update)
