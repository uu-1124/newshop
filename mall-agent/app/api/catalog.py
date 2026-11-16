from typing import Literal

from fastapi import Depends, Query, Request

from app.api.dependencies import member, ok, services
from app.application.catalog import CatalogDetail, CatalogQuery
from app.domain.identity import DomainError


def register_catalog_routes(app):
    prefix = "/mall-agent/v1/member"

    @app.get(prefix + "/products", operation_id="searchProducts")
    async def search(
        request: Request,
        principal=Depends(member),
        query: str | None = Query(None, max_length=200),
        min_price: str | None = Query(None, pattern=r"^(0|[1-9][0-9]{0,15})\.[0-9]{2}$"),
        max_price: str | None = Query(None, pattern=r"^(0|[1-9][0-9]{0,15})\.[0-9]{2}$"),
        category_id: str | None = Query(None, pattern=r"^[1-9][0-9]{0,18}$"),
        brand_id: str | None = Query(None, pattern=r"^[1-9][0-9]{0,18}$"),
        specification: str | None = Query(None, max_length=100),
        in_stock: bool = False,
        sort: Literal["newest", "price_asc", "price_desc", "sales"] = "newest",
        cursor: str | None = Query(None, max_length=2048),
        limit: int = Query(20, ge=1, le=100),
    ):
        allowed = set(CatalogQuery.model_fields)
        if set(request.query_params) - allowed or any(
            len(request.query_params.getlist(key)) != 1 for key in request.query_params
        ):
            raise DomainError("VALIDATION_ERROR", "筛选参数重复或未开放", 422)
        try:
            filters = CatalogQuery(
                query=query,
                min_price=min_price,
                max_price=max_price,
                category_id=category_id,
                brand_id=brand_id,
                specification=specification,
                in_stock=in_stock,
                sort=sort,
                cursor=cursor,
                limit=limit,
            )
        except ValueError:
            raise DomainError("VALIDATION_ERROR", "商品筛选条件不符合约定", 422) from None
        return ok(request, await services(request).bridge.catalog(request.headers["Authorization"], filters))

    @app.get(prefix + "/products/{product_id}", operation_id="getProduct")
    async def detail(product_id: str, request: Request, principal=Depends(member)):
        try:
            value = CatalogDetail(product_id=product_id)
        except ValueError:
            raise DomainError("VALIDATION_ERROR", "商品编号不正确", 422) from None
        return ok(
            request,
            await services(request).bridge.product(request.headers["Authorization"], value.product_id),
        )
