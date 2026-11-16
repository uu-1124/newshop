from uuid import uuid4

from app.domain.capabilities import AccessContext
from app.domain.identity import DomainError


class StaffAuthorization:
    def __init__(self, bridge):
        self.bridge = bridge

    async def authenticate(self, authorization):
        if not authorization.startswith("Bearer "):
            raise DomainError("AUTH_REQUIRED", "请先登录管理后台", 401)
        principal = await self.bridge.staff_identity(authorization)
        return AccessContext(principal, uuid4(), principal.permissions)


def require_permission(principal, permission):
    if principal.kind != "STAFF" or permission not in principal.permissions:
        raise DomainError("FORBIDDEN", "当前员工没有相关权限", 403)
