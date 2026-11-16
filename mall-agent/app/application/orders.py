"""Typed current-member order reads and the shared after-sale facade."""

import re
from typing import Literal

from pydantic import Field, field_validator, model_validator

from app.api.dto import Input
from app.application.catalog import JAVA_ID, MONEY
from app.domain.identity import DomainError


def java_id(value):
    if not isinstance(value, str) or not re.fullmatch(JAVA_ID, value) or int(value) > 9223372036854775807:
        raise DomainError("VALIDATION_ERROR", "业务编号无效", 422)
    return value


class OrderRead(Input):
    kind: Literal["ORDERS", "ORDER", "LOGISTICS", "RETURNS", "ELIGIBILITY"]
    order_id: str | None = None
    order_item_id: str | None = None
    order_sn: str | None = Field(default=None, max_length=80)
    status: int | None = Field(default=None, ge=0, le=5, strict=True)
    limit: int = Field(default=20, ge=1, le=100, strict=True)
    cursor: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def valid(self):
        if self.kind == "ORDERS":
            if self.order_id or self.order_item_id:
                raise ValueError("列表参数无效")
        else:
            if self.order_id:
                java_id(self.order_id)
                if self.order_sn:
                    raise ValueError("请选择订单内部编号或订单号其中一种")
            elif not self.order_sn or not self.order_sn.strip():
                raise ValueError("请提供查询得到的订单内部编号或完整订单号")
            if self.kind == "ELIGIBILITY":
                java_id(self.order_item_id)
            elif self.order_item_id:
                raise ValueError("明细参数无效")
            if self.status is not None or self.cursor:
                raise ValueError("详情参数无效")
        return self


class CancellationSummary(Input):
    id: str
    order_sn: str = Field(min_length=1, max_length=80)
    pay_amount: str = Field(pattern=MONEY)
    status: Literal[0]

    @field_validator("status", mode="before")
    @classmethod
    def unpaid_status(cls, value):
        if type(value) is not int or value != 0:
            raise ValueError("订单不是待付款状态")
        return value

    @model_validator(mode="after")
    def valid(self):
        java_id(self.id)
        if not self.order_sn.strip():
            raise ValueError("取消订单摘要无效")
        return self


def validate_cancellation_quote(quote, parameters):
    if parameters.get("action_type") != "CANCEL_ORDER":
        return
    try:
        summary = CancellationSummary.model_validate(quote.get("cancellation"))
        if (
            summary.id != parameters["order_id"]
            or summary.pay_amount != quote["pricing"]["payable"]
            or quote.get("rule_version") != "CANCEL_PENDING_V2"
        ):
            raise ValueError()
    except (ValueError, KeyError, TypeError, DomainError):
        raise DomainError("JAVA_PROTOCOL_ERROR", "取消确认缺少可信订单号或金额，请重新准备", 503) from None


async def query_orders(bridge, token, query):
    if query.kind == "ORDERS":
        return await bridge.get(
            "/mall-portal/agent-bridge/v1/orders",
            token,
            query.model_dump(exclude={"kind", "order_id", "order_item_id"}, exclude_none=True),
        )
    order_id = query.order_id
    if not order_id:
        page = await bridge.get(
            "/mall-portal/agent-bridge/v1/orders", token, {"order_sn": query.order_sn, "limit": 2}
        )
        if not page.get("items"):
            raise DomainError("NOT_FOUND", "未找到本人的该订单", 404)
        if len(page["items"]) != 1:
            raise DomainError("ORDER_AMBIGUOUS", "订单号对应多笔记录，请在本人订单列表选择", 409)
        order_id = java_id(page["items"][0]["id"])
    path = "/mall-portal/agent-bridge/v1/orders/" + order_id
    path += {
        "ORDER": "",
        "LOGISTICS": "/logistics",
        "RETURNS": "/returns",
        "ELIGIBILITY": "/items/" + (query.order_item_id or "") + "/return-eligibility",
    }[query.kind]
    return await bridge.get(path, token)


async def cancellation_choice_facts(bridge, token, order_id):
    target = await query_orders(bridge, token, OrderRead(kind="ORDER", order_id=order_id))
    page = await query_orders(bridge, token, OrderRead(kind="ORDERS", status=0, limit=20))
    try:
        if not isinstance(target, dict) or target.get("id") != order_id:
            raise ValueError()
        if (
            not isinstance(page, dict)
            or not isinstance(page.get("items"), list)
            or type(page.get("has_more")) is not bool
            or len(page["items"]) > 20
        ):
            raise ValueError()
        orders = {}
        for item in [*page["items"], target]:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("order_sn"), str)
                or not item["order_sn"].strip()
                or len(item["order_sn"]) > 80
                or type(item.get("status")) is not int
                or not 0 <= item["status"] <= 5
                or not isinstance(item.get("pay_amount"), str)
                or not re.fullmatch(MONEY, item["pay_amount"])
                or not isinstance(item.get("lines", []), list)
            ):
                raise ValueError()
            for line in item.get("lines", []):
                if (
                    not isinstance(line, dict)
                    or not isinstance(line.get("name"), str)
                    or not isinstance(line.get("specification", ""), str)
                    or type(line.get("quantity")) is not int
                    or line["quantity"] <= 0
                ):
                    raise ValueError()
                java_id(line.get("product_id"))
                java_id(line.get("sku_id"))
            orders[java_id(item["id"])] = item
        if len({item["id"] for item in page["items"]}) != len(page["items"]):
            raise ValueError()
        return orders, not page["has_more"]
    except (KeyError, ValueError, TypeError, DomainError):
        raise DomainError("JAVA_PROTOCOL_ERROR", "商城订单选择依据不完整，请重新读取", 503) from None


class OrderBusiness:
    def __init__(self, bridge):
        self.bridge = bridge

    async def query(self, token, query):
        if not self.bridge.settings.orders_enabled:
            raise DomainError("FEATURE_NOT_ACTIVE", "订单与售后查询尚未开放", 409)
        return await query_orders(self.bridge, token, query)

    async def staff_transition(self, token, path, method, parameters, key):
        return await self.bridge.staff_command(token, path, method, parameters, key)
