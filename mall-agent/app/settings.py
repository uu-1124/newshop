from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", env_file=".env", extra="ignore")
    database_url: SecretStr
    bridge_secret: SecretStr
    broker_secret: SecretStr
    cursor_secret: SecretStr
    gateway_url: str = "http://127.0.0.1:8201"
    broker_url: str = "http://127.0.0.1:8099"
    provider: Literal["fixture", "openai_responses"] = "fixture"
    build: str = "order-0.9"
    actions_enabled: bool = False
    orders_enabled: bool = False
    governance_enabled: bool = False
    retention_enabled: bool = False
    backups_enabled: bool = False
    observability_enabled: bool = False
    confirmation_secret: SecretStr = SecretStr("")
    reconciliation_secret: SecretStr = SecretStr("")
    action_encryption_key: SecretStr = SecretStr("")
    handoffs_enabled: bool = False
    knowledge_enabled: bool = False
    milvus_uri: str = "http://127.0.0.1:19530"
    milvus_database: str = "mall_agent"
    milvus_reader_user: str = ""
    milvus_reader_password: SecretStr = SecretStr("")
    milvus_builder_user: str = ""
    milvus_builder_password: SecretStr = SecretStr("")
    staff_bridge_secret: SecretStr = SecretStr("")
    notifications_enabled: bool = False
    redis_url: SecretStr = SecretStr("")
    rabbit_url: SecretStr = SecretStr("")
    notification_namespace: str = Field("mall-agent", pattern=r"^mall-agent(?:-[a-z0-9]+)?$")
    job_lease_seconds: int = Field(30, ge=5, le=300)
    job_timeout_seconds: int = Field(60, ge=1, le=600)
    job_retry_base_seconds: float = Field(2, ge=0.1, le=60)
    dialog_base_url: str = "https://fushengyunsuan.cn/v1"
    dialog_api_key: SecretStr = SecretStr("")
    dialog_model: str = "gpt-6.1-sol"
    ali_compatible_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    ali_native_url: str = "https://dashscope.aliyuncs.com/api/v1"
    ali_api_key: SecretStr = SecretStr("")
    embedding_model: str = "text-embedding-v4"
    embedding_dimension: int = Field(1024, ge=64, le=4096)
    rerank_model: str = "qwen3-rerank"
    vision_model: str = "gpt-6.1-sol"
    asr_model: str = "qwen3-asr-flash"
    tts_model: str = "qwen3-tts-flash"
    dialog_max_output_tokens: int = Field(1536, ge=128, le=4096)
    model_timeout_seconds: int = Field(60, ge=10, le=120)
    run_timeout_seconds: int = Field(120, ge=30, le=240)
    max_model_rounds: int = Field(6, ge=1, le=8)
    vision_enabled: bool = False
    attachments_enabled: bool = False
    objects_endpoint: str = "127.0.0.1:19000"
    objects_bucket: str = "mall-agent-private"
    objects_access_key: SecretStr = SecretStr("")
    objects_secret_key: SecretStr = SecretStr("")
    model_daily_requests: int = Field(200, ge=1, le=10000)
    run_lease_seconds: int = 30
    auth_ttl_seconds: int = 180
    poll_seconds: float = 0.25
    fixture_chunk_delay: float = 0.08

    @field_validator("dialog_base_url", "ali_compatible_url", "ali_native_url")
    @classmethod
    def trusted_endpoint(cls, value):
        url = urlsplit(value)
        if url.scheme != "https" or url.username or url.password or url.query or url.fragment:
            raise ValueError("Model endpoints require HTTPS and cannot contain credentials or query strings")
        if not url.hostname or url.port not in {None, 443}:
            raise ValueError("Invalid model endpoint")
        return value.rstrip("/")

    @model_validator(mode="after")
    def configured_provider(self):
        if self.orders_enabled and not self.actions_enabled:
            raise ValueError("Order checkout requires actions to be enabled")
        if self.actions_enabled and not (
            len(self.confirmation_secret.get_secret_value()) >= 32
            and len(self.reconciliation_secret.get_secret_value()) >= 32
            and self.action_encryption_key.get_secret_value()
        ):
            raise ValueError("Actions require separate confirmation, reconciliation and envelope keys")
        if self.knowledge_enabled and not self.ali_api_key.get_secret_value():
            raise ValueError("Knowledge requires Aliyun embedding and rerank credentials")
        if self.notifications_enabled and not (
            self.redis_url.get_secret_value() and self.rabbit_url.get_secret_value()
        ):
            raise ValueError("Notifications require scoped Redis and RabbitMQ credentials")
        if self.provider == "openai_responses" and not self.dialog_api_key.get_secret_value():
            raise ValueError("The selected dialog provider requires its own API key")
        if self.attachments_enabled and not (
            self.objects_access_key.get_secret_value() and self.objects_secret_key.get_secret_value()
        ):
            raise ValueError("Private attachments require scoped object credentials")
        return self

    def checkpoint_url(self) -> str:
        # psycopg accepts a URI query with an explicit isolated search_path.
        url = self.database_url.get_secret_value().replace("postgresql+psycopg://", "postgresql://")
        return url + ("&" if "?" in url else "?") + "options=-csearch_path%3Dlanggraph_checkpoint"


@lru_cache
def get_settings() -> Settings:
    return Settings()
