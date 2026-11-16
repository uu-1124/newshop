from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Scope(Input):
    product_ids: list[str] = Field(default_factory=list, max_length=100)
    category_ids: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def valid_ids(self):
        for value in self.product_ids + self.category_ids:
            if (
                not value.isascii()
                or not value.isdigit()
                or str(int(value)) != value
                or not 0 < int(value) <= 9223372036854775807
            ):
                raise ValueError("Invalid mall ID")
        return self


class KBInput(Input):
    title: str = Field(min_length=1, max_length=200)
    category: str = Field(min_length=1, max_length=80)
    visibility: Literal["MEMBER", "STAFF"]
    scope: Scope

    @field_validator("title", "category")
    @classmethod
    def nonblank(cls, value):
        value = value.strip()
        if not value or "\x00" in value:
            raise ValueError("Invalid knowledge metadata")
        return value


class KBUpdate(KBInput):
    expected_version: int = Field(ge=1)


class KBVersionInput(Input):
    source_attachment_id: UUID | None = None
    source_text: str | None = Field(None, min_length=1, max_length=500000)
    change_note: str = Field(max_length=500)

    @model_validator(mode="after")
    def one_source(self):
        if (self.source_attachment_id is None) == (self.source_text is None):
            raise ValueError("Exactly one source is required")
        if self.source_text is not None and (not self.source_text.strip() or "\x00" in self.source_text):
            raise ValueError("Source cannot be blank")
        return self


class KBPageText(Input):
    page: int = Field(ge=1, le=2000)
    text: str = Field(max_length=500000)

    @field_validator("text")
    @classmethod
    def no_nul(cls, value):
        if "\x00" in value:
            raise ValueError("Invalid page text")
        return value


class KBPreviewUpdate(Input):
    expected_version: int = Field(ge=1)
    parsed_text: str = Field(min_length=1, max_length=500000)
    pages: list[KBPageText] | None = Field(default=None, max_length=2000)

    @field_validator("parsed_text")
    @classmethod
    def text_valid(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("Invalid preview text")
        return value


class KBPublishInput(Input):
    expected_document_version: int = Field(ge=1)
    version_id: UUID


class KBEnabledInput(Input):
    expected_version: int = Field(ge=1)
    enabled: bool
