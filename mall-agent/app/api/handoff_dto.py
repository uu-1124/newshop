from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.api.dto import Input, VersionInput


class HandoffInput(Input):
    expected_conversation_version: int = Field(ge=1)
    reason: str = Field(default="", max_length=500)


class WaitingAIInput(VersionInput):
    enabled: bool


class CloseInput(VersionInput):
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def nonempty(self):
        if not self.reason.strip():
            raise ValueError("请填写原因")
        return self


class TransferInput(CloseInput):
    target_staff_id: str = Field(pattern=r"^[1-9][0-9]{0,18}$")


class CardInput(Input):
    kind: Literal["PRODUCT", "ORDER"]
    id: str = Field(pattern=r"^[1-9][0-9]{0,18}$")


class StaffMessageInput(Input):
    client_message_id: UUID
    text: str = Field(max_length=10000)
    attachment_ids: list[UUID] = Field(max_length=2)
    cards: list[CardInput] = Field(max_length=5)

    @model_validator(mode="after")
    def nonempty(self):
        if not self.text.strip() and not self.attachment_ids and not self.cards:
            raise ValueError("回复不能为空")
        if len(set(self.attachment_ids)) != len(self.attachment_ids) or len(
            {(c.kind, c.id) for c in self.cards}
        ) != len(self.cards):
            raise ValueError("请勿重复附加图片或卡片")
        return self


class NoteInput(Input):
    client_note_id: UUID
    body: str = Field(min_length=1, max_length=5000)

    @model_validator(mode="after")
    def nonempty(self):
        if not self.body.strip():
            raise ValueError("备注不能为空")
        return self


class PresenceInput(VersionInput):
    presence: Literal["ONLINE", "BUSY", "OFFLINE"]


class StaffProfileInput(VersionInput):
    display_name: str = Field(min_length=1, max_length=80)
    capacity: int = Field(ge=1, le=20)


class Empty(Input):
    pass
