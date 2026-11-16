import copy
from datetime import UTC, datetime

import pytest

from app.agents.preparation_evidence import require_preparation_evidence, require_return_read
from app.application.cart import CartRead, validated_cart_page
from app.application.orders import OrderRead
from app.domain.identity import DomainError


def tool(name, query, result):
    return {"name": name, "query": query, "result": result}


def catalog():
    return tool(
        "get_product",
        {"product_id": "1"},
        {"id": "1", "sku_options": [{"id": "2", "visible": True, "available_quantity": 3}]},
    )


def direct():
    return {
        "action_type": "CREATE_ORDER",
        "source": "DIRECT",
        "items": [{"product_id": "1", "sku_id": "2", "quantity": 2}],
        "address_id": "10",
        "coupon_id": "20",
        "use_points": 100,
    }


def options(parameters):
    return tool(
        "get_checkout_options",
        {k: v for k, v in parameters.items() if k in {"action_type", "source", "items", "cart_item_ids"}},
        {"addresses": [{"id": "10"}], "coupons": [{"id": "20", "available": True}], "available_points": 100},
    )


def test_preparation_requires_same_product_sku_stock_and_exact_checkout_selection():
    parameters = direct()
    evidence = [catalog(), options(parameters)]
    require_preparation_evidence("prepare_business_action", parameters, evidence)
    for change in (
        {"items": [{"product_id": "9", "sku_id": "2", "quantity": 2}]},
        {"items": [{"product_id": "1", "sku_id": "9", "quantity": 2}]},
        {"items": [{"product_id": "1", "sku_id": "2", "quantity": 4}]},
        {"items": [{"product_id": "1", "sku_id": "2", "quantity": 1}]},
        {"address_id": "11"},
        {"coupon_id": "21"},
        {"use_points": 101},
    ):
        with pytest.raises(DomainError, match="BUSINESS_SOURCE_REQUIRED"):
            require_preparation_evidence("prepare_business_action", parameters | change, evidence)
    unavailable = copy.deepcopy(evidence)
    unavailable[-1]["result"]["coupons"][0]["available"] = False
    with pytest.raises(DomainError):
        require_preparation_evidence("prepare_business_action", parameters, unavailable)
    # A fresh unavailable SKU supersedes a prior available observation.
    sold = catalog()
    sold["result"]["sku_options"][0]["available_quantity"] = 0
    with pytest.raises(DomainError):
        require_preparation_evidence("get_checkout_options", parameters, [catalog(), sold])


def test_cart_entries_must_have_been_read_and_checkout_cannot_switch_source_or_rows():
    parameters = {
        "action_type": "CREATE_ORDER",
        "source": "CART",
        "cart_item_ids": ["30"],
        "address_id": "10",
        "coupon_id": None,
        "use_points": 0,
    }
    page = tool("get_cart", {"limit": 20}, {"items": [{"id": "30", "available": True}]})
    evidence = [page, options(parameters)]
    require_preparation_evidence("prepare_business_action", parameters, evidence)
    for traces in (
        [options(parameters)],
        [page, options(direct())],
        evidence + [tool("get_cart", {"limit": 20}, {"items": []})],
    ):
        with pytest.raises(DomainError):
            require_preparation_evidence("prepare_business_action", parameters, traces)
    with pytest.raises(DomainError):
        require_preparation_evidence("get_checkout_options", parameters | {"cart_item_ids": ["31"]}, evidence)


def test_cancel_and_return_need_exact_detail_or_eligibility_not_unrelated_reads():
    cancel = {"action_type": "CANCEL_ORDER", "order_id": "40"}
    detail = tool("query_my_orders", {"kind": "ORDER"}, {"id": "40", "status": 0})
    require_preparation_evidence("prepare_business_action", cancel, [detail])
    for traces in (
        [],
        [tool("query_my_orders", {"kind": "ORDERS"}, {"items": [{"id": "40", "status": 0}]})],
        [detail | {"result": {"id": "41", "status": 0}}],
        [detail | {"result": {"id": "40", "status": 1}}],
    ):
        with pytest.raises(DomainError):
            require_preparation_evidence("prepare_business_action", cancel, traces)
    parameters = {
        "action_type": "CREATE_RETURN",
        "order_id": "40",
        "order_item_id": "50",
        "quantity": 2,
        "reason_code": "60",
    }
    eligibility = tool(
        "query_my_orders",
        {"kind": "ELIGIBILITY"},
        {
            "order_id": "40",
            "order_item_id": "50",
            "eligible": True,
            "remaining_quantity": 2,
            "reason_codes": [{"code": "60"}],
        },
    )
    require_preparation_evidence("prepare_business_action", parameters, [eligibility])
    for change in ({"order_id": "41"}, {"order_item_id": "51"}, {"quantity": 3}, {"reason_code": "61"}):
        with pytest.raises(DomainError):
            require_preparation_evidence("prepare_business_action", parameters | change, [eligibility])


def test_cart_snapshot_rejects_wrong_page_duplicate_ids_and_unbounded_or_forged_input():
    item = {
        "id": "30",
        "product_id": "1",
        "sku_id": "2",
        "name": "演示商品",
        "specification": "蓝色",
        "quantity": 1,
        "available": True,
        "available_quantity": 2,
        "unit_price": "9.90",
    }
    page = {
        "items": [item],
        "next_cursor": None,
        "has_more": False,
        "checked_at": datetime.now(UTC).isoformat(),
    }
    validated = validated_cart_page(page, CartRead())
    assert validated["items"][0]["id"] == "30"
    assert validated["page_item_count"] == validated["page_quantity"] == 1
    empty = validated_cart_page(page | {"items": []}, CartRead())
    assert empty["page_item_count"] == empty["page_quantity"] == 0
    assert empty["quantity_scope"] == "CURRENT_PAGE_ONLY"
    for change in (
        {"items": [item, item]},
        {"has_more": True},
        {"checked_at": "2026-11-13T00:00:00"},
        {"member_id": "9"},
    ):
        with pytest.raises(DomainError, match="JAVA_PROTOCOL_ERROR"):
            validated_cart_page(page | change, CartRead())
    with pytest.raises(DomainError):
        validated_cart_page(page, CartRead(after="30"))
    for arguments in ({"limit": 21}, {"limit": True}, {"after": "9223372036854775808"}, {"member_id": "9"}):
        with pytest.raises((ValueError, DomainError)):
            CartRead.model_validate(arguments)


def test_return_eligibility_cannot_guess_a_line_from_another_order_or_without_detail():
    query = OrderRead(kind="ELIGIBILITY", order_id="40", order_item_id="50")
    detail = tool(
        "query_my_orders",
        {"kind": "ORDER"},
        {"id": "40", "order_sn": "DEMO40", "lines": [{"order_item_id": "50"}]},
    )
    require_return_read(query, [detail])
    require_return_read(OrderRead(kind="ELIGIBILITY", order_sn="DEMO40", order_item_id="50"), [detail])
    for choice, traces in (
        (query, []),
        (query.model_copy(update={"order_id": "41"}), [detail]),
        (query.model_copy(update={"order_item_id": "51"}), [detail]),
    ):
        with pytest.raises(DomainError):
            require_return_read(choice, traces)
