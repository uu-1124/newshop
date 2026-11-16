from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, Request
from pydantic import Field, model_validator

from app.api.dependencies import idempotency, member, ok, services
from app.api.dto import Input, VersionInput


class SelectedItem(Input):
    product_id: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    sku_id: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    quantity: int = Field(ge=1, le=999, strict=True)

    @model_validator(mode="after")
    def id_range(self):
        if max(int(self.product_id), int(self.sku_id)) > 9223372036854775807:
            raise ValueError("商品编号无效")
        return self


class AddCartParameters(Input):
    action_type: Literal["ADD_CART"]
    items: list[SelectedItem] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def unique(self):
        if len({item.sku_id for item in self.items}) != len(self.items):
            raise ValueError("同一规格请合并数量")
        return self


class CheckoutSelection(Input):
    action_type: Literal["CREATE_ORDER"]
    source: Literal["CART", "DIRECT"]
    cart_item_ids: list[str] | None = Field(default=None, min_length=1, max_length=20)
    items: list[SelectedItem] | None = Field(default=None, min_length=1, max_length=20)

    @model_validator(mode="after")
    def valid_selection(self):
        import re

        if (self.source == "CART") != (self.cart_item_ids is not None) or (self.source == "DIRECT") != (
            self.items is not None
        ):
            raise ValueError("直购与购物车选择不匹配")
        if self.items is not None and len({v.sku_id for v in self.items}) != len(self.items):
            raise ValueError("同一规格请合并数量")
        selected = self.cart_item_ids or []
        if len(set(selected)) != len(selected) or any(
            not re.fullmatch(r"[1-9][0-9]{0,18}", value) or int(value) > 9223372036854775807
            for value in selected
        ):
            raise ValueError("购物车选择无效")
        return self

    def wire(self):
        return self.model_dump(mode="json", exclude_none=True)


class CreateOrderParameters(CheckoutSelection):
    address_id: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    coupon_id: str | None = Field(pattern=r"^[1-9][0-9]{0,18}$")
    use_points: int = Field(ge=0, le=100000000, strict=True)

    @model_validator(mode="after")
    def valid_ids(self):
        if any(int(value) > 9223372036854775807 for value in (self.address_id, self.coupon_id) if value):
            raise ValueError("地址或优惠券编号无效")
        return self

    def wire(self):
        return super().wire() | {"coupon_id": self.coupon_id}


class CancelOrderParameters(Input):
    action_type: Literal["CANCEL_ORDER"]
    order_id: str = Field(pattern=r"^[1-9][0-9]{0,18}$")

    @model_validator(mode="after")
    def valid_id(self):
        if int(self.order_id) > 9223372036854775807:
            raise ValueError("订单编号无效")
        return self

    def wire(self):
        return self.model_dump(mode="json")


class CreateReturnParameters(CancelOrderParameters):
    action_type: Literal["CREATE_RETURN"]
    order_item_id: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    quantity: int = Field(ge=1, le=999, strict=True)
    reason_code: str = Field(min_length=1, max_length=80)
    description: str = Field(max_length=1000)
    attachment_ids: list[UUID] = Field(max_length=5)

    @model_validator(mode="after")
    def valid_return(self):
        if int(self.order_item_id) > 9223372036854775807 or len(set(self.attachment_ids)) != len(
            self.attachment_ids
        ):
            raise ValueError("明细或凭证无效")
        return self


class ActionPrepareInput(Input):
    expected_conversation_version: int = Field(ge=1)
    parameters: Annotated[
        AddCartParameters | CreateOrderParameters | CancelOrderParameters | CreateReturnParameters,
        Field(discriminator="action_type"),
    ]

    def wire(self):
        parameters = (
            self.parameters.wire()
            if hasattr(self.parameters, "wire")
            else self.parameters.model_dump(mode="json")
        )
        return {"expected_conversation_version": self.expected_conversation_version, "parameters": parameters}


def register_action_routes(app):
    base = "/mall-agent/v1/member"

    def service(request):
        return services(request).extensions.actions.require()

    @app.post(base + "/checkout-options", operation_id="getCheckoutOptions")
    async def checkout_options(
        body: CheckoutSelection, request: Request, principal=Depends(member), key=Depends(idempotency)
    ):
        return ok(
            request,
            await service(request).checkout_options(
                principal, request.headers["authorization"], body.wire(), key
            ),
        )

    @app.post(
        base + "/conversations/{conversation_id}/actions", status_code=201, operation_id="prepareAction"
    )
    async def prepare(
        conversation_id: UUID,
        body: ActionPrepareInput,
        request: Request,
        principal=Depends(member),
        key=Depends(idempotency),
    ):
        return ok(
            request,
            await service(request).prepare(
                principal,
                request.headers["authorization"],
                conversation_id,
                key,
                body.wire(),
            ),
        )

    @app.get(base + "/actions/{action_id}", operation_id="getAction")
    async def get(action_id: UUID, request: Request, principal=Depends(member)):
        return ok(request, await service(request).get(principal, action_id))

    @app.post(base + "/actions/{action_id}/confirm", status_code=202, operation_id="confirmAction")
    async def confirm(
        action_id: UUID,
        body: VersionInput,
        request: Request,
        principal=Depends(member),
        key=Depends(idempotency),
    ):
        return ok(
            request,
            await service(request).confirm(
                principal, request.headers["authorization"], action_id, key, body.model_dump(mode="json")
            ),
        )

    @app.post(base + "/actions/{action_id}/cancel", operation_id="cancelAction")
    async def cancel(
        action_id: UUID,
        body: VersionInput,
        request: Request,
        principal=Depends(member),
        key=Depends(idempotency),
    ):
        return ok(
            request, await service(request).cancel(principal, action_id, key, body.model_dump(mode="json"))
        )
