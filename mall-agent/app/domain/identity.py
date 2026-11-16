from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    id: int
    display_name: str
    kind: str = "MEMBER"
    permissions: frozenset[str] = frozenset()


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        self.code, self.message, self.status = code, message, status
        super().__init__(code)
