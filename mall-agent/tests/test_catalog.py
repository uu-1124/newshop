import json
from pathlib import Path

import httpx
import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.agents.dialog import DialogContext, build_dialog_graph
from app.application.catalog import CatalogCompare, CatalogDetail, CatalogQuery, compare_products
from app.bootstrap import Services
from app.domain.identity import DomainError, Principal
from app.infrastructure.dialog_provider import DialogEvent
from app.main import create_app
from app.settings import Settings


def product(pid, price="100.00", stock=2, facts=None):
    return {
        "id": pid,
        "name": "商品" + pid,
        "brand": "测试品牌",
        "category_id": "1",
        "price_from": price,
        "image_url": "",
        "detail_path": f"/pages/product/product?id={pid}",
        "sku_options": [
            {"id": pid + "0", "attributes": [], "price": price, "available_quantity": stock, "visible": True}
        ],
        "facts": facts or [],
        "checked_at": "2026-11-12T00:00:00Z",
    }


def settings():
    return Settings(
        _env_file=None,
        database_url="postgresql+psycopg://unused:unused@127.0.0.1:1/unused",
        bridge_secret="test-only-bridge",
        broker_secret="test-only-broker",
        cursor_secret="test-only-cursor",
        provider="fixture",
    )


def test_catalog_queries_bound_price_ids_page_size_and_fields():
    for body in (
        {"min_price": "1.001"},
        {"min_price": "1.01", "max_price": "1.00"},
        {"brand_id": "9223372036854775808"},
        {"limit": True},
        {"limit": 101},
        {"sort": "price;DROP TABLE"},
        {"member_id": "2"},
    ):
        with pytest.raises(ValidationError):
            CatalogQuery(**body)
    assert CatalogQuery(query=" 手机 ", in_stock=True).parameters()["query"] == "手机"
    assert CatalogQuery(in_stock=True).parameters()["in_stock"] == "true"
    with pytest.raises(ValidationError):
        CatalogDetail(product_id="-1")
    with pytest.raises(ValidationError):
        CatalogCompare(product_ids=["1", "1"])
    with pytest.raises(ValidationError):
        CatalogCompare(product_ids=["1", "2", "3", "4", "5"])


def test_comparison_preserves_missing_values_budget_and_stock_without_fabricated_scores():
    products = [
        product("1", facts=[{"name": "屏幕", "value": "6.1英寸"}]),
        product("2", "90.00", 0),
        product("3", "120.00", 10),
    ]
    result = compare_products(products, CatalogCompare(product_ids=["1", "2", "3"], max_price="110.00"))
    assert result["comparison"] == [{"name": "屏幕", "values": ["6.1英寸", "未提供", "未提供"]}]
    assert result["recommended_product_id"] == "1"
    assert "score" not in result and "性能" in result["missing_values"]
    none = compare_products(products, CatalogCompare(product_ids=["1", "2", "3"], max_price="89.99"))
    assert none["recommended_product_id"] is None
    availability = compare_products(
        products, CatalogCompare(product_ids=["1", "2", "3"], criterion="availability")
    )
    assert availability["recommended_product_id"] == "3"
    with pytest.raises(DomainError):
        compare_products(products[:1], CatalogCompare(product_ids=["1", "2"]))


@pytest.mark.anyio
async def test_catalog_http_uses_current_member_and_rejects_unknown_or_duplicate_filters():
    svc = Services(settings())
    app = create_app(settings())
    app.state.services = svc
    calls = []

    async def identity(token):
        if token != "Bearer unit-member":
            raise DomainError("AUTH_EXPIRED", "请登录", 401)
        return Principal(930000001, "catalog-test")

    async def search(token, query):
        calls.append((token, query.parameters()))
        return {
            "items": [product("1")],
            "has_more": False,
            "next_cursor": None,
            "filters": {"brands": [], "categories": []},
        }

    async def detail(token, pid):
        assert token == "Bearer unit-member"
        if pid == "2":
            raise DomainError("PRODUCT_UNAVAILABLE", "商品已下架", 404)
        return product(pid, facts=[{"name": "颜色", "value": "黑色"}])

    svc.bridge.identity = identity
    svc.bridge.catalog = search
    svc.bridge.product = detail
    contract = json.loads((Path(__file__).parents[2] / "docs/api/openapi.json").read_text(encoding="utf-8"))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {"Authorization": "Bearer unit-member"}
            base = "/mall-agent/v1/member/products"
            assert (await client.get(base)).status_code == 401
            result = await client.get(
                base, params={"query": "手机", "max_price": "100.00", "in_stock": "true"}, headers=headers
            )
            assert result.status_code == 200
            Draft202012Validator(
                {"$ref": "#/components/schemas/searchProductsResponse", "components": contract["components"]}
            ).validate(result.json())
            assert calls[0][1]["max_price"] == "100.00" and calls[0][1]["in_stock"] == "true"
            for suffix in (
                "?member_id=2",
                "?limit=1&limit=2",
                "?min_price=100.01&max_price=100.00",
                "?brand_id=9223372036854775808",
            ):
                assert (await client.get(base + suffix, headers=headers)).status_code == 422
            assert len(calls) == 1
            assert (await client.get(base + "/2", headers=headers)).status_code == 404
            result = await client.get(base + "/1", headers=headers)
            Draft202012Validator(
                {"$ref": "#/components/schemas/getProductResponse", "components": contract["components"]}
            ).validate(result.json())
    finally:
        await svc.close()


@pytest.mark.anyio
async def test_model_tools_keep_budget_and_compare_actual_detail_facts():
    class Provider:
        count = 0

        async def stream_turn(self, inputs, prompt, tools, record):
            self.count += 1
            if self.count == 1:
                yield DialogEvent(
                    "completed",
                    [
                        {
                            "type": "function_call",
                            "call_id": "search",
                            "name": "search_products",
                            "arguments": json.dumps(
                                {"query": "手机", "max_price": "110.00", "in_stock": True}
                            ),
                        }
                    ],
                )
            elif self.count == 2:
                assert json.loads(inputs[-1]["output"])["items"][0]["id"] == "1"
                yield DialogEvent(
                    "completed",
                    [
                        {
                            "type": "function_call",
                            "call_id": "compare",
                            "name": "compare_products",
                            "arguments": json.dumps(
                                {"product_ids": ["1", "2"], "max_price": "110.00", "criterion": "price"}
                            ),
                        }
                    ],
                )
            else:
                comparison = json.loads(inputs[-1]["output"])
                assert comparison["recommended_product_id"] == "1"
                assert comparison["comparison"][0]["values"] == ["6.1英寸", "未提供"]
                yield DialogEvent("text", "按当前预算和可售价格推荐商品1，商品2屏幕参数未提供。")
                yield DialogEvent("completed", [])

    searched = []

    async def search(query):
        searched.append(query)
        return {"items": [product("1"), product("2", "120.00")]}

    async def detail(pid):
        return product(
            pid,
            "100.00" if pid == "1" else "120.00",
            facts=[{"name": "屏幕", "value": "6.1英寸"}] if pid == "1" else [],
        )

    async def ignore(*unused):
        pass

    result = await build_dialog_graph(Provider(), None).ainvoke(
        {"question": "预算110元，比较两件商品", "history": [], "answer": "", "tool_results": []},
        context=DialogContext(search, ignore, ignore, detail=detail),
    )
    assert searched[0]["max_price"] == "110.00" and searched[0]["in_stock"] is True
    assert [item["name"] for item in result["tool_results"]] == ["search_products", "compare_products"]


@pytest.mark.anyio
async def test_budget_query_with_no_results_does_not_fall_back_to_unfiltered_products():
    from types import SimpleNamespace

    from app.agents.live_workflow import build_live_workflow
    from app.application.extensions import Extensions

    class Provider:
        count = 0

        async def stream_turn(self, inputs, prompt, tools, record, schema=None):
            if schema:
                yield DialogEvent("text", '{"intent":"SHOPPING"}')
                return
            self.count += 1
            if self.count == 1:
                yield DialogEvent(
                    "completed",
                    [
                        {
                            "type": "function_call",
                            "name": "search_products",
                            "call_id": "search",
                            "arguments": '{"query":"手机","max_price":"1.00"}',
                        }
                    ],
                )
            else:
                yield DialogEvent("text", "没有符合预算的商品。")
                yield DialogEvent("completed", [])

    queries = []
    saved = []

    async def search(query):
        queries.append(query)
        return {"items": []}

    async def ignore(*unused):
        pass

    async def assemble(*unused):
        return {"history": []}

    async def persist(answer, products):
        saved.append(products)

    graph, factory = build_live_workflow(
        Provider(), None, SimpleNamespace(max_model_rounds=3, actions_enabled=True)
    )
    await graph.ainvoke(
        {"question": "1元以内手机加购", "steps_used": 0},
        context=factory(
            guard=ignore,
            assemble=assemble,
            search=search,
            emit=ignore,
            record=ignore,
            route_record=ignore,
            extensions=Extensions(),
            persist_products=persist,
        ),
    )
    assert len(queries) == 1 and isinstance(queries[0], dict) and queries[0]["max_price"] == "1.00"
    assert saved == [[]]
