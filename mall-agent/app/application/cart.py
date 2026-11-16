"""Current-member cart snapshots, never a source of checkout totals."""

from datetime import datetime

from pydantic import Field, model_validator

from app.api.dto import Input
from app.application.catalog import JAVA_ID, MONEY
from app.application.orders import java_id
from app.domain.identity import DomainError


class CartRead(Input):
    limit: int = Field(default=20, ge=1, le=20, strict=True)
    after: str | None = Field(default=None, pattern=JAVA_ID)

    @model_validator(mode="after")
    def valid_cursor(self):
        if self.after:
            java_id(self.after)
        return self


class CartItem(Input):
    id: str = Field(pattern=JAVA_ID)
    product_id: str | None = Field(pattern=JAVA_ID)
    sku_id: str | None = Field(pattern=JAVA_ID)
    name: str = Field(max_length=500)
    specification: str = Field(max_length=300)
    quantity: int = Field(ge=0, le=2147483647, strict=True)
    available: bool = Field(strict=True)
    available_quantity: int = Field(ge=0, le=2147483647, strict=True)
    unit_price: str | None = Field(pattern=MONEY)

    @model_validator(mode="after")
    def valid_ids(self):
        for value in (self.id, self.product_id, self.sku_id):
            if value is not None:
                java_id(value)
        if self.available and (
            self.product_id is None
            or self.sku_id is None
            or self.quantity < 1
            or self.unit_price is None
            or self.available_quantity < self.quantity
        ):
            raise ValueError("Available cart item lacks current stock or price")
        return self


class CartPage(Input):
    items: list[CartItem] = Field(max_length=20)
    next_cursor: str | None = Field(pattern=JAVA_ID)
    has_more: bool = Field(strict=True)
    checked_at: datetime

    @model_validator(mode="after")
    def ordered(self):
        ids = [int(item.id) for item in self.items]
        if ids != sorted(set(ids)) or self.checked_at.tzinfo is None:
            raise ValueError("Invalid cart snapshot order or time")
        expected = self.items[-1].id if self.items and self.has_more else None
        if self.has_more and not self.items or self.next_cursor != expected:
            raise ValueError("Invalid cart snapshot continuation")
        return self


def validated_cart_page(value, query):
    try:
        page = CartPage.model_validate(value)
        if len(page.items) > query.limit or any(
            int(item.id) <= int(query.after or "0") for item in page.items
        ):
            raise ValueError("Cart snapshot does not match requested page")
        return page.model_dump(mode="json") | {
            "page_item_count": len(page.items),
            "page_quantity": sum(item.quantity for item in page.items),
            "quantity_scope": "CURRENT_PAGE_ONLY",
        }
    except (ValueError, TypeError, DomainError):
        raise DomainError("JAVA_PROTOCOL_ERROR", "购物车快照不完整，请重新读取", 503) from None


async def cart_for_member_choice(bridge, token, selected_ids):
    """Bounded current facts; large carts still allow explicit IDs past page 5."""
    items, after, complete = {}, None, False
    for _ in range(5):
        query = CartRead(limit=20, after=after)
        page = validated_cart_page(await bridge.cart(token, query), query)
        items.update((row["id"], row) for row in page["items"])
        if not page["has_more"]:
            complete = True
            break
        after = page["next_cursor"]
    if not complete:
        for identifier in selected_ids:
            if identifier in items:
                continue
            query = CartRead(limit=1, after=str(int(identifier) - 1) if int(identifier) > 1 else None)
            page = validated_cart_page(await bridge.cart(token, query), query)
            items.update((row["id"], row) for row in page["items"] if row["id"] == identifier)
    return items, complete
