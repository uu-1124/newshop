"""Validated catalog queries and comparisons made only from current Java facts."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.identity import DomainError

MONEY = r"^(0|[1-9][0-9]{0,15})\.[0-9]{2}$"
JAVA_ID = r"^[1-9][0-9]{0,18}$"


class CatalogQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str | None = Field(default=None, max_length=200)
    min_price: str | None = Field(default=None, pattern=MONEY)
    max_price: str | None = Field(default=None, pattern=MONEY)
    brand_id: str | None = Field(default=None, pattern=JAVA_ID)
    category_id: str | None = Field(default=None, pattern=JAVA_ID)
    specification: str | None = Field(default=None, max_length=100)
    in_stock: bool = False
    sort: Literal["newest", "price_asc", "price_desc", "sales"] = "newest"
    cursor: str | None = Field(default=None, max_length=2048)
    limit: int = Field(default=20, ge=1, le=100, strict=True)

    @model_validator(mode="after")
    def ranges(self):
        if self.min_price and self.max_price and Decimal(self.min_price) > Decimal(self.max_price):
            raise ValueError("最低价不能超过最高价")
        for value in (self.brand_id, self.category_id):
            if value and int(value) > 9223372036854775807:
                raise ValueError("商品筛选编号无效")
        self.query = self.query.strip() if self.query else None
        self.specification = self.specification.strip() if self.specification else None
        return self

    def parameters(self):
        return {
            name: (str(value).lower() if isinstance(value, bool) else value)
            for name, value in self.model_dump(exclude_none=True).items()
        }


class CatalogDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: str = Field(pattern=JAVA_ID)

    @model_validator(mode="after")
    def bounded_id(self):
        if int(self.product_id) > 9223372036854775807:
            raise ValueError("商品编号无效")
        return self


class CatalogCompare(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_ids: list[str] = Field(min_length=2, max_length=4)
    max_price: str | None = Field(default=None, pattern=MONEY)
    criterion: Literal["price", "availability"] = "price"

    @model_validator(mode="after")
    def unique_ids(self):
        if len(set(self.product_ids)) != len(self.product_ids):
            raise ValueError("请选择不同商品进行比较")
        for value in self.product_ids:
            CatalogDetail(product_id=value)
        return self


def compare_products(products, choice: CatalogCompare):
    by_id = {product["id"]: product for product in products}
    if set(by_id) != set(choice.product_ids):
        raise DomainError("PRODUCT_UNAVAILABLE", "部分比较商品已下架，请重新选择", 404)
    ordered = [by_id[value] for value in choice.product_ids]
    names = sorted({fact["name"] for product in ordered for fact in product["facts"]})
    comparison = []
    for name in names:
        values = []
        for product in ordered:
            facts = [fact["value"] for fact in product["facts"] if fact["name"] == name]
            values.append(" / ".join(facts) if facts else "未提供")
        comparison.append({"name": name, "values": values})
    eligible = []
    for product in ordered:
        available = [
            sku for sku in product["sku_options"] if sku["visible"] and sku["available_quantity"] > 0
        ]
        if choice.max_price:
            available = [sku for sku in available if Decimal(sku["price"]) <= Decimal(choice.max_price)]
        if available:
            eligible.append(
                (
                    product["id"],
                    min(Decimal(sku["price"]) for sku in available),
                    sum(sku["available_quantity"] for sku in available),
                )
            )
    eligible.sort(
        key=(lambda item: (item[1], item[0]))
        if choice.criterion == "price"
        else (lambda item: (-item[2], item[1], item[0]))
    )
    return {
        "products": ordered,
        "comparison": comparison,
        "criterion": choice.criterion,
        "max_price": choice.max_price,
        "recommended_product_id": eligible[0][0] if eligible else None,
        "recommendation_basis": (
            "仅在本次比较中，选择有库存且符合预算的最低价规格所属商品。"
            if choice.criterion == "price"
            else "仅在本次比较中，按符合预算的可售规格库存总数排序，不代表性能或质量。"
        )
        if eligible
        else "本次比较没有同时符合预算且有库存的商品，不能推荐购买。",
        "missing_values": "未提供的参数不能推断；不同商品或规格的属性不代表同一性能标准。",
    }
