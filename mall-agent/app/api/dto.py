from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EntryContext(Input):
    kind: Literal["GENERAL", "PRODUCT", "ORDER"]
    product_id: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,18}$")
    order_id: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,18}$")

    @model_validator(mode="after")
    def check_ids(self):
        expected = {"GENERAL": (False, False), "PRODUCT": (True, False), "ORDER": (False, True)}
        if (self.product_id is not None, self.order_id is not None) != expected[self.kind]:
            raise ValueError("入口类型与业务ID不匹配")
        return self


class ConversationCreate(Input):
    title: str = Field(default="新会话", max_length=100)
    entry_context: EntryContext


class MessageInput(Input):
    client_message_id: UUID
    text: str = Field(max_length=10000)
    attachment_ids: list[UUID] = Field(max_length=5)

    @model_validator(mode="after")
    def nonempty(self):
        if not self.text.strip() and not self.attachment_ids:
            raise ValueError("消息不能为空")
        return self


class VersionInput(Input):
    expected_version: int = Field(ge=1)


class ConversationRename(VersionInput):
    title: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def nonblank(self):
        self.title = self.title.strip()
        if not self.title:
            raise ValueError("会话标题不能为空")
        return self


class ReadInput(Input):
    last_seq: str = Field(pattern=r"^(0|[1-9][0-9]{0,18})$")

    @model_validator(mode="after")
    def bounded(self):
        if int(self.last_seq) > 9223372036854775807:
            raise ValueError("已读位置无效")
        return self


class FeedbackInput(Input):
    vote: Literal[-1, 1]
    reason: str = Field(default="", max_length=500)


class TranscriptionInput(Input):
    attachment_id: UUID


class SpeechInput(Input):
    voice: str | None = Field(default=None, max_length=80)
    part: int = Field(default=0, ge=0, le=49)
