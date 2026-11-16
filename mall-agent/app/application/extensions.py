"""Composition slots for deferred workflows; all slots fail closed until implemented.

Binding a concrete port is a code change, never a feature flag that turns an empty
handler into a successful business response. Real implementations own authorization
and the transaction rules described in their module design.
"""

from dataclasses import dataclass, field
from typing import Generic, TypeVar

from app.domain import capabilities as ports
from app.domain.identity import DomainError

T = TypeVar("T")


@dataclass
class Extension(Generic[T]):
    module_id: str
    name: str
    implementation: T | None = field(default=None, repr=False)

    def require(self) -> T:
        if self.implementation is None:
            raise DomainError("FEATURE_NOT_ACTIVE", f"{self.name}尚未开放", 409)
        return self.implementation

    @property
    def bound(self) -> bool:
        return self.implementation is not None


@dataclass
class Extensions:
    staff_identity: Extension[ports.StaffIdentity] = field(
        default_factory=lambda: Extension("M01", "员工鉴权")
    )
    actions: Extension[ports.Actions] = field(default_factory=lambda: Extension("M06", "业务确认与核对"))
    handoffs: Extension[ports.Handoffs] = field(default_factory=lambda: Extension("M07", "人工接待"))
    knowledge: Extension[ports.KnowledgeManagement] = field(
        default_factory=lambda: Extension("M08", "知识管理")
    )
    retrieval: Extension[ports.AuthorizedKnowledge] = field(
        default_factory=lambda: Extension("M09", "知识检索与引用")
    )
    retention: Extension[ports.Retention] = field(default_factory=lambda: Extension("M12", "留存与删除"))
    observability: Extension[ports.Observability] = field(
        default_factory=lambda: Extension("M13", "审计统计与反馈")
    )
    governance: Extension[ports.Governance] = field(default_factory=lambda: Extension("M16", "治理工作台"))
    model_admin: Extension[ports.ModelAdministration] = field(
        default_factory=lambda: Extension("M11", "模型配置管理")
    )
    transactions: Extension[ports.JavaTransactions] = field(
        default_factory=lambda: Extension("M18", "商城交易桥接")
    )
    after_sale: Extension[ports.JavaAfterSale] = field(
        default_factory=lambda: Extension("M19", "售后与物流桥接")
    )
    backups: Extension[ports.BackupOperations] = field(
        default_factory=lambda: Extension("M20", "隔离备份恢复")
    )

    def status(self):
        return {name: {"module": slot.module_id, "bound": slot.bound} for name, slot in vars(self).items()}
