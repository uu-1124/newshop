from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from fastapi import Depends, Query, Request
from pydantic import Field, model_validator

from app.api.admin import staff
from app.api.dependencies import idempotency, member, ok, services
from app.api.dto import Input, VersionInput
from app.application.catalog import MONEY
from app.application.orders import OrderRead, java_id
from app.infrastructure.postgres.database import encoded, execute
from app.infrastructure.staff_auth import require_permission


class ReturnReview(VersionInput):
    decision: Literal["APPROVE", "REJECT"]
    approved_amount: str | None = Field(default=None, pattern=MONEY)
    company_address_id: str | None = None
    note: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def valid(self):
        if not self.note.strip():
            raise ValueError("请填写处理说明")
        if self.decision == "APPROVE":
            java_id(self.company_address_id)
            if self.approved_amount is None or Decimal(self.approved_amount) < 0:
                raise ValueError("审核金额无效")
        elif self.approved_amount is not None or self.company_address_id is not None:
            raise ValueError("拒绝不接收批准金额或地址")
        return self


class ReturnRuleInput(Input):
    expected_latest_version: int = Field(ge=1, le=2147483646, strict=True)
    return_window_hours: int = Field(ge=1, le=720, strict=True)
    allow_shipped: bool = Field(strict=True)
    effective_at: datetime
    note: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def valid(self):
        if self.effective_at.tzinfo is None or not self.note.strip():
            raise ValueError("生效时间须带时区，发布说明不能为空")
        self.effective_at = self.effective_at.astimezone(UTC)
        return self


class ReturnTransition(VersionInput):
    note: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def nonblank(self):
        if not self.note.strip():
            raise ValueError("请填写处理说明")
        return self


class LogisticsInput(VersionInput):
    carrier: str = Field(min_length=1, max_length=100)
    tracking_no: str = Field(min_length=1, max_length=100)


class NodeInput(Input):
    occurred_at: datetime
    location: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def aware(self):
        if self.occurred_at.tzinfo is None or not self.location.strip() or not self.description.strip():
            raise ValueError("物流时间须带时区，地点和描述不能为空")
        return self


class NodeCreate(NodeInput):
    expected_shipment_version: int = Field(ge=1)


class NodeUpdate(NodeInput):
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)


def register_order_routes(app):
    base = "/mall-agent/v1/member/orders"

    async def read(request, query):
        return ok(
            request,
            await services(request)
            .extensions.after_sale.require()
            .query(request.headers["authorization"], query),
        )

    @app.get(base, operation_id="listMemberOrders")
    async def listing(
        request: Request,
        principal=Depends(member),
        limit: int = Query(20, ge=1, le=100),
        cursor: str | None = Query(None, max_length=2048),
        order_sn: str | None = Query(None, max_length=80),
        status: int | None = Query(None, ge=0, le=5),
    ):
        return await read(
            request, OrderRead(kind="ORDERS", limit=limit, cursor=cursor, order_sn=order_sn, status=status)
        )

    @app.get(base + "/{order_id}", operation_id="getMemberOrder")
    async def detail(order_id: str, request: Request, principal=Depends(member)):
        return await read(request, OrderRead(kind="ORDER", order_id=java_id(order_id)))

    @app.get(base + "/{order_id}/logistics", operation_id="getMemberLogistics")
    async def logistics(order_id: str, request: Request, principal=Depends(member)):
        return await read(request, OrderRead(kind="LOGISTICS", order_id=java_id(order_id)))

    @app.get(base + "/{order_id}/returns", operation_id="listOrderReturns")
    async def returns(order_id: str, request: Request, principal=Depends(member)):
        return await read(request, OrderRead(kind="RETURNS", order_id=java_id(order_id)))

    @app.get(
        base + "/{order_id}/items/{order_item_id}/return-eligibility", operation_id="checkReturnEligibility"
    )
    async def eligibility(order_id: str, order_item_id: str, request: Request, principal=Depends(member)):
        return await read(
            request,
            OrderRead(kind="ELIGIBILITY", order_id=java_id(order_id), order_item_id=java_id(order_item_id)),
        )

    admin = "/mall-agent/v1/admin"

    async def staff_read(request, principal, path, permission, params=None):
        require_permission(principal, permission)
        return ok(
            request,
            await services(request).bridge.get(
                "/mall-admin/agent-bridge/v1" + path, request.headers["authorization"], params, staff=True
            ),
        )

    async def command(request, principal, path, permission, method, body, key):
        require_permission(principal, permission)
        svc = services(request)
        result = await svc.extensions.after_sale.require().staff_transition(
            request.headers["authorization"], path, method, body, key
        )
        # Java owns the durable command dedupe. Repeat HTTP commands remain safely replayable even if this PG audit write fails.
        async with svc.db.transaction() as conn:
            await execute(
                conn,
                "INSERT INTO ag_audit_log(id,actor_kind,actor_id,operation,resource_type,resource_id,request_id,outcome,details) VALUES(:id,'STAFF',:actor,:op,'BUSINESS',:resource,:key,'SUCCEEDED',CAST(:details AS jsonb))",
                id=uuid4(),
                actor=principal.id,
                op=permission,
                resource=path,
                key=key,
                details=encoded({"simulated": True}),
            )
        return ok(request, result)

    @app.get(admin + "/returns", operation_id="adminListReturns")
    async def staff_returns(
        request: Request,
        principal=Depends(staff),
        stage: str | None = Query(None, max_length=32),
        before: str = "0",
        limit: int = Query(20, ge=1, le=100),
    ):
        if before != "0":
            java_id(before)
        return await staff_read(
            request,
            principal,
            "/returns",
            "agent:return:read",
            {"stage": stage, "before": before, "limit": limit},
        )

    @app.get(admin + "/return-rules", operation_id="adminListReturnRules")
    async def return_rules(request: Request, principal=Depends(staff)):
        from app.application.return_knowledge import refresh_rule_sync

        require_permission(principal, "agent:return:read")
        svc = services(request)
        return ok(
            request,
            await refresh_rule_sync(svc.db, svc.bridge, request.headers["authorization"], principal.id),
        )

    @app.post(admin + "/return-rules", operation_id="adminPublishReturnRule")
    async def publish_return_rule(
        body: ReturnRuleInput, request: Request, principal=Depends(staff), key=Depends(idempotency)
    ):
        from app.application.return_knowledge import begin_rule_sync, finish_rule_sync

        require_permission(principal, "agent:return:review")
        svc = services(request)
        sync = await begin_rule_sync(svc.db, principal.id)
        try:
            result = await command(
                request,
                principal,
                "/return-rules",
                "agent:return:review",
                "POST",
                body.model_dump(mode="json"),
                key,
            )
            page = await svc.bridge.get(
                "/mall-admin/agent-bridge/v1/return-rules", request.headers["authorization"], staff=True
            )
            await finish_rule_sync(svc.db, sync, page)
            return result
        except BaseException:
            await finish_rule_sync(svc.db, sync)
            raise

    @app.get(admin + "/return-addresses", operation_id="adminReturnAddresses")
    async def addresses(request: Request, principal=Depends(staff)):
        return await staff_read(request, principal, "/return-addresses", "agent:return:review")

    @app.get(admin + "/returns/{return_id}", operation_id="adminReadReturn")
    async def staff_return(return_id: str, request: Request, principal=Depends(staff)):
        return await staff_read(request, principal, "/returns/" + java_id(return_id), "agent:return:read")

    @app.post(admin + "/returns/{return_id}/review", operation_id="adminReviewReturn")
    async def review(
        return_id: str,
        body: ReturnReview,
        request: Request,
        principal=Depends(staff),
        key=Depends(idempotency),
    ):
        return await command(
            request,
            principal,
            "/returns/" + java_id(return_id) + "/review",
            "agent:return:review",
            "POST",
            body.model_dump(mode="json", exclude_none=True),
            key,
        )

    @app.post(admin + "/returns/{return_id}/receive", operation_id="adminReceiveReturn")
    async def receive(
        return_id: str,
        body: ReturnTransition,
        request: Request,
        principal=Depends(staff),
        key=Depends(idempotency),
    ):
        return await command(
            request,
            principal,
            "/returns/" + java_id(return_id) + "/receive",
            "agent:return:receive",
            "POST",
            body.model_dump(mode="json"),
            key,
        )

    @app.post(admin + "/returns/{return_id}/simulate-refund", operation_id="adminSimulateRefund")
    async def refund(
        return_id: str,
        body: ReturnTransition,
        request: Request,
        principal=Depends(staff),
        key=Depends(idempotency),
    ):
        return await command(
            request,
            principal,
            "/returns/" + java_id(return_id) + "/simulate-refund",
            "agent:return:refund",
            "POST",
            body.model_dump(mode="json"),
            key,
        )

    @app.get(admin + "/orders/{order_id}/logistics", operation_id="adminReadLogistics")
    async def staff_logistics(order_id: str, request: Request, principal=Depends(staff)):
        return await staff_read(
            request, principal, "/orders/" + java_id(order_id) + "/logistics", "agent:logistics:read"
        )

    @app.put(admin + "/orders/{order_id}/logistics", operation_id="adminUpdateLogistics")
    async def update_logistics(
        order_id: str,
        body: LogisticsInput,
        request: Request,
        principal=Depends(staff),
        key=Depends(idempotency),
    ):
        return await command(
            request,
            principal,
            "/orders/" + java_id(order_id) + "/logistics",
            "agent:logistics:write",
            "PUT",
            body.model_dump(mode="json"),
            key,
        )

    @app.post(
        admin + "/shipments/{shipment_id}/events", operation_id="adminCreateLogisticsNode", status_code=201
    )
    async def create_node(
        shipment_id: str,
        body: NodeCreate,
        request: Request,
        principal=Depends(staff),
        key=Depends(idempotency),
    ):
        return await command(
            request,
            principal,
            "/shipments/" + java_id(shipment_id) + "/events",
            "agent:logistics:write",
            "POST",
            body.model_dump(mode="json"),
            key,
        )

    @app.patch(admin + "/shipments/{shipment_id}/events/{event_id}", operation_id="adminEditLogisticsNode")
    async def edit_node(
        shipment_id: str,
        event_id: str,
        body: NodeUpdate,
        request: Request,
        principal=Depends(staff),
        key=Depends(idempotency),
    ):
        return await command(
            request,
            principal,
            "/shipments/" + java_id(shipment_id) + "/events/" + java_id(event_id),
            "agent:logistics:write",
            "PATCH",
            body.model_dump(mode="json"),
            key,
        )
