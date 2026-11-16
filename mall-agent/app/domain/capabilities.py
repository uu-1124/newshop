"""Typed extension points for modules whose business workflows are not active yet.

Contexts are created by trusted application code after authentication. None of these
ports is an HTTP DTO or an alternative authentication mechanism.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from app.domain.identity import Principal


@dataclass(frozen=True)
class AccessContext:
    principal: Principal
    request_id: UUID
    permissions: frozenset[str]
    conversation_id: UUID | None = None
    control_epoch: int | None = None
    memory_epoch: int | None = None


@dataclass(frozen=True)
class VersionedRef:
    id: UUID
    expected_version: int


@dataclass(frozen=True)
class CitationRef:
    document_id: UUID
    version_id: UUID
    chunk_id: UUID


@dataclass(frozen=True)
class ActionRef:
    id: UUID
    operation_no: str
    canonical_sha256: str
    expires_at: datetime


class StaffIdentity(Protocol):
    async def authenticate(self, authorization: str) -> AccessContext: ...


class Actions(Protocol):
    async def prepare(self, principal: Principal, token: str, cid: UUID, key: UUID, body: dict) -> dict: ...

    async def confirm(self, principal: Principal, token: str, aid: UUID, key: UUID, body: dict) -> dict: ...

    async def reconcile_due(self) -> int: ...


class Handoffs(Protocol):
    async def request(self, principal: Principal, cid: UUID, key: UUID, body: dict) -> dict: ...

    async def command(
        self, principal: Principal, hid: UUID, key: UUID, body: dict, command: str, token: str = ""
    ) -> dict: ...

    async def send(
        self, principal: Principal, cid: UUID, key: UUID, body: dict, note: bool = False
    ) -> dict: ...


class KnowledgeManagement(Protocol):
    async def create_version(self, principal: Principal, document: UUID, key: UUID, body: dict) -> dict: ...

    async def enqueue(
        self, principal: Principal, version: UUID, key: UUID, expected: int, kind: str
    ) -> dict: ...

    async def publish(self, principal: Principal, document: UUID, key: UUID, body: dict) -> dict: ...

    async def enable(self, principal: Principal, document: UUID, key: UUID, body: dict) -> dict: ...


class AuthorizedKnowledge(Protocol):
    async def retrieve(self, query: str) -> list[dict]: ...

    async def recheck(self, refs: list[dict]) -> None: ...

    async def read_citation(self, principal: Principal, version: UUID, chunk: UUID) -> dict: ...


class Retention(Protocol):
    async def preview(self, context: AccessContext, policy_version: int, cutoff: datetime) -> UUID: ...

    async def execute(self, context: AccessContext, preview: VersionedRef) -> UUID: ...

    async def erase_conversation(self, context: AccessContext, target: VersionedRef) -> None: ...


class Observability(Protocol):
    async def statistics(self, context: AccessContext, since: datetime) -> dict: ...

    async def feedback(self, context: AccessContext, message_id: UUID, value: str) -> dict: ...


class Governance(Protocol):
    async def settings(self, context: AccessContext) -> dict: ...

    async def audit(self, context: AccessContext, cursor: str | None) -> dict: ...


class ModelAdministration(Protocol):
    async def versions(self, context: AccessContext, profile_id: UUID) -> list[dict]: ...

    async def activate(self, context: AccessContext, profile: VersionedRef, evidence_job: UUID) -> dict: ...


class JavaTransactions(Protocol):
    """Only the confirmation application may obtain the execute implementation."""

    async def quote(self, token: str, parameters: dict, key: UUID) -> dict: ...

    async def execute_action(self, token: str, body: dict) -> dict: ...

    async def reconcile_action(self, operation: str, parameters_hash: str, grant: str) -> dict: ...


class JavaAfterSale(Protocol):
    async def query(self, token: str, query) -> dict: ...

    async def staff_transition(
        self, token: str, path: str, method: str, parameters: dict, key: UUID
    ) -> dict: ...


class JobType(StrEnum):
    RUNTIME_CHECK = "RUNTIME_CHECK"
    KB_PARSE = "KB_PARSE"
    KB_INDEX = "KB_INDEX"
    MODEL_COMPATIBILITY = "MODEL_COMPATIBILITY"
    RETENTION_PREVIEW = "RETENTION_PREVIEW"
    RETENTION_EXECUTE = "RETENTION_EXECUTE"
    BACKUP_VERIFY = "BACKUP_VERIFY"
    RESTORE_VERIFY = "RESTORE_VERIFY"
    ATTACHMENT_DELETE = "ATTACHMENT_DELETE"
    ACTION_RECONCILE = "ACTION_RECONCILE"


@dataclass(frozen=True)
class JobLease:
    id: UUID
    kind: JobType
    owner: str
    fencing_token: int
    parameters: dict


class JobHandler(Protocol):
    async def __call__(self, lease: JobLease) -> dict: ...


class Wakeups(Protocol):
    async def publish(self, job_id: UUID, kind: JobType) -> None: ...


class BackupOperations(Protocol):
    async def listing(self, principal, before=None, limit=20) -> dict: ...

    async def request(
        self, principal, key, mode, record_id, note, expected_policy=None, maintenance_confirmed=False
    ) -> dict: ...

    async def update_policy(self, principal, key, value, expected) -> dict: ...
