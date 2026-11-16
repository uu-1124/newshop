import time
from dataclasses import dataclass, field
from uuid import uuid4

from app.domain.identity import DomainError


@dataclass(repr=False)
class Credential:
    token: str = field(repr=False)
    expires: float


class RuntimeAuth:
    """API-process memory only. Workers borrow an operation, never a bearer token."""

    def __init__(self, ttl: int):
        self.ttl, self.items = ttl, {}
        self.context_id = str(uuid4())

    def put(self, run_id: str, token: str):
        self.prune()
        if len(self.items) >= 1000 and run_id not in self.items:
            raise DomainError("AUTH_CAPACITY", "服务繁忙，请稍后发送", 503)
        self.items[run_id] = Credential(token, time.monotonic() + self.ttl)

    def get(self, run_id: str) -> str:
        self.prune()
        value = self.items.get(run_id)
        if value is None:
            raise DomainError("AUTH_CONTEXT_LOST", "身份上下文已失效，请重新发送", 409)
        return value.token

    def prune(self):
        now = time.monotonic()
        self.items = {key: value for key, value in self.items.items() if value.expires > now}

    def discard(self, run_id: str):
        self.items.pop(run_id, None)
