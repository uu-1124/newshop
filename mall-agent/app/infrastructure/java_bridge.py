import base64
import hashlib
import hmac
import time
from datetime import UTC, datetime
from urllib.parse import quote
from uuid import uuid4

import httpx

from app.domain.identity import DomainError, Principal


def sign(method: str, target: str, body: bytes, timestamp: str, nonce: str, secret: str) -> str:
    canonical = "\n".join([method.upper(), target, hashlib.sha256(body).hexdigest(), timestamp, nonce])
    return "v1:" + base64.urlsafe_b64encode(
        hmac.digest(secret.encode(), canonical.encode(), "sha256")
    ).decode().rstrip("=")


class JavaBridge:
    def __init__(self, settings):
        self.settings = settings
        self.client = httpx.AsyncClient(base_url=settings.gateway_url, timeout=10, trust_env=False)

    async def get(self, path: str, token: str, params=None, staff=False):
        query = "&".join(
            f"{quote(str(k), safe='~')}={quote(str(v), safe='~')}"
            for k, v in sorted((params or {}).items())
            if v is not None
        )
        target = path + ("?" + query if query else "")
        timestamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        nonce = str(uuid4())
        headers = {
            "Authorization": token,
            "X-Agent-Timestamp": timestamp,
            "X-Agent-Nonce": nonce,
            "X-Request-ID": str(uuid4()),
            "X-Agent-Signature": sign(
                "GET",
                target,
                b"",
                timestamp,
                nonce,
                (
                    self.settings.staff_bridge_secret if staff else self.settings.bridge_secret
                ).get_secret_value(),
            ),
        }
        try:
            response = await self.client.get(target, headers=headers)
            data = response.json()
        except (httpx.HTTPError, ValueError):
            raise DomainError("JAVA_UNAVAILABLE", "商城服务暂不可用", 503) from None
        if response.status_code == 401 or data.get("code") == 401:
            raise DomainError("AUTH_EXPIRED", "请重新登录商城", 401)
        if response.status_code == 403 or data.get("code") == 403:
            raise DomainError("FORBIDDEN", "当前身份没有相关权限", 403)
        if response.status_code in {400, 404, 422}:
            raise DomainError(
                data.get("code", "JAVA_BRIDGE_REJECTED"),
                "商品不存在、已下架或筛选条件无效",
                response.status_code,
            )
        if response.status_code != 200 or data.get("code") != "OK":
            raise DomainError("JAVA_BRIDGE_REJECTED", "商城桥接请求未通过校验", 503)
        return data["data"]

    async def identity(self, token: str):
        data = await self.get("/mall-portal/agent-bridge/v1/identity", token)
        if data["kind"] != "MEMBER":
            raise DomainError("AUTH_REQUIRED", "需要会员身份", 401)
        return Principal(int(data["id"]), data["display_name"])

    async def products(self, token: str, query: str | None = None):
        return await self.get("/mall-portal/agent-bridge/v1/products", token, {"limit": 5, "query": query})

    async def catalog(self, token, query):
        return await self.get("/mall-portal/agent-bridge/v1/products", token, query.parameters())

    async def product(self, token, product_id):
        return await self.get(f"/mall-portal/agent-bridge/v1/products/{product_id}", token)

    async def cart(self, token, query):
        return await self.get("/mall-portal/agent-bridge/v1/cart", token, query.model_dump(exclude_none=True))

    async def staff_identity(self, token: str):
        if not self.settings.staff_bridge_secret.get_secret_value():
            raise DomainError("FEATURE_NOT_ACTIVE", "员工鉴权未配置", 409)
        data = await self.get("/mall-admin/agent-bridge/v1/identity", token, staff=True)
        if data.get("kind") != "STAFF":
            raise DomainError("AUTH_REQUIRED", "需要员工身份", 401)
        return Principal(int(data["id"]), data["display_name"], "STAFF", frozenset(data["permissions"]))

    async def staff_target(self, token: str, target: str):
        return await self.get(
            "/mall-admin/agent-bridge/v1/staff/" + target + "/eligibility", token, staff=True
        )

    async def staff_context(self, token, principal, conv, cards):
        from app.infrastructure.postgres.database import encoded

        claims = {
            "kind": "STAFF_BUSINESS",
            "staff_id": str(principal.id),
            "member_id": str(conv["member_id"]),
            "conversation_id": str(conv["id"]),
            "control_epoch": conv["control_epoch"],
            "expires_at": int(time.time()) + 45,
            "cards": cards,
        }
        secret = self.settings.staff_bridge_secret.get_secret_value()
        payload = base64.urlsafe_b64encode(encoded(claims).encode()).decode().rstrip("=")
        mac = (
            base64.urlsafe_b64encode(
                hmac.digest(secret.encode(), ("staff-business.v1." + payload).encode(), "sha256")
            )
            .decode()
            .rstrip("=")
        )
        raw = encoded({"conversation_id": str(conv["id"]), "access_grant": payload + "." + mac}).encode()
        path = "/mall-admin/agent-bridge/v1/business-context"
        timestamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        nonce = str(uuid4())
        try:
            response = await self.client.post(
                path,
                content=raw,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": token,
                    "X-Agent-Timestamp": timestamp,
                    "X-Agent-Nonce": nonce,
                    "X-Request-ID": str(uuid4()),
                    "X-Agent-Signature": sign("POST", path, raw, timestamp, nonce, secret),
                },
            )
            data = response.json()
        except (httpx.HTTPError, ValueError):
            raise DomainError("JAVA_UNAVAILABLE", "商城业务摘要暂不可用", 503) from None
        if response.status_code != 200 or data.get("code") != "OK":
            status = response.status_code if response.status_code in {400, 401, 403, 404, 409, 422} else 503
            raise DomainError("BUSINESS_CONTEXT_REJECTED", "业务资料不存在、归属不符或权限已变化", status)
        return data["data"]

    async def post_action(self, path, token, body, key=None, grant=None):
        from app.infrastructure.postgres.database import encoded

        raw = encoded(body).encode()
        timestamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        nonce = str(uuid4())
        headers = {
            "Content-Type": "application/json",
            "X-Agent-Timestamp": timestamp,
            "X-Agent-Nonce": nonce,
            "X-Request-ID": str(uuid4()),
            "X-Agent-Signature": sign(
                "POST", path, raw, timestamp, nonce, self.settings.bridge_secret.get_secret_value()
            ),
        }
        if token:
            headers["Authorization"] = token
        if key:
            headers["Idempotency-Key"] = str(key)
        if grant:
            headers["X-Operation-Receipt"] = grant
        try:
            response = await self.client.post(path, content=raw, headers=headers)
            data = response.json()
        except (httpx.HTTPError, ValueError):
            raise DomainError("JAVA_UNAVAILABLE", "商城操作结果暂时无法确定，正在核对", 503) from None
        if response.status_code != 200 or data.get("code") != "OK":
            code = data.get("code") if isinstance(data.get("code"), str) else "JAVA_UNAVAILABLE"
            status = response.status_code if response.status_code in {401, 403, 409, 422, 503} else 503
            raise DomainError(code, "商品或操作状态已变化，请刷新核对", status)
        return data["data"]

    async def checkout_options(self, token, selection, key):
        return await self.post_action("/mall-portal/agent-bridge/v1/checkout-options", token, selection, key)

    async def staff_command(self, token, path, method, body, key):
        from app.infrastructure.postgres.database import encoded

        raw = encoded(body).encode()
        target = "/mall-admin/agent-bridge/v1" + path
        timestamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        nonce = str(uuid4())
        try:
            response = await self.client.request(
                method,
                target,
                content=raw,
                headers={
                    "Authorization": token,
                    "Content-Type": "application/json",
                    "Idempotency-Key": str(key),
                    "X-Request-ID": str(key),
                    "X-Agent-Timestamp": timestamp,
                    "X-Agent-Nonce": nonce,
                    "X-Agent-Signature": sign(
                        method,
                        target,
                        raw,
                        timestamp,
                        nonce,
                        self.settings.staff_bridge_secret.get_secret_value(),
                    ),
                },
            )
            data = response.json()
        except (httpx.HTTPError, ValueError):
            raise DomainError("JAVA_UNAVAILABLE", "处理结果尚未确定，请使用原请求重试核对", 503) from None
        if response.status_code != 200 or data.get("code") != "OK":
            status = response.status_code if response.status_code in {400, 401, 403, 404, 409, 422} else 503
            raise DomainError(
                data.get("code", "JAVA_UNAVAILABLE"),
                "物流时间须在订单或发货时间之后、当前时间之前，并保持节点先后顺序"
                if data.get("code") == "LOGISTICS_TIME_INVALID"
                else "业务状态或权限已变化，请刷新核对",
                status,
            )
        return data["data"]

    async def quote(self, token, parameters, key):
        return await self.post_action("/mall-portal/agent-bridge/v1/quotes", token, parameters, key)

    async def execute_action(self, token, body):
        return await self.post_action(
            "/mall-portal/agent-bridge/v1/operations", token, body, body["operation_no"]
        )

    async def reconcile_action(self, operation, parameters_hash, grant):
        return await self.post_action(
            f"/mall-portal/agent-bridge/v1/operations/{operation}/reconcile",
            None,
            {"mode": "FINALIZE_IF_ABSENT", "parameters_hash": parameters_hash},
            str(uuid4()),
            grant,
        )
