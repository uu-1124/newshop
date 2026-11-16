"""External capability boundaries. Inactive modules do not expose pretend-success APIs."""

from collections.abc import AsyncIterator
from typing import Protocol


class DialogProvider(Protocol):
    def stream_turn(
        self, inputs: list[dict], instructions: str, tools: list[dict], record
    ) -> AsyncIterator: ...


class KnowledgeIndex(Protocol):
    async def retrieve(self, query: str, visibility: str) -> list[dict]: ...


class PrivateObjects(Protocol):
    async def read_authorized(self, object_key: str) -> bytes: ...


class Notifications(Protocol):
    async def wake(self, topic: str, resource_id: str) -> None: ...
