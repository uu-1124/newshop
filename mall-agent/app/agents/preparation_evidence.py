"""Bind preparation to exact objects read during this dialog, never just tool names."""

from app.domain.identity import DomainError


def missing(message):
    raise DomainError("BUSINESS_SOURCE_REQUIRED", message, 409)


def selection(parameters):
    result = {"action_type": "CREATE_ORDER", "source": parameters["source"]}
    if parameters["source"] == "CART":
        result["cart_item_ids"] = sorted(parameters["cart_item_ids"], key=int)
    else:
        result["items"] = sorted(
            parameters["items"], key=lambda item: (int(item["product_id"]), int(item["sku_id"]))
        )
    return result


def require_items(items, results):
    products = {}
    for tool in results:
        if tool["name"] not in {"search_products", "get_product", "compare_products"}:
            continue
        data = tool["result"]
        values = [data] if tool["name"] == "get_product" else data.get("items", data.get("products", []))
        for product in values:
            products[product["id"]] = product
    for item in items:
        product = products.get(item["product_id"], {})
        sku = next((s for s in product.get("sku_options", []) if s["id"] == item["sku_id"]), {})
        if not sku or not sku.get("visible") or sku.get("available_quantity", 0) < item["quantity"]:
            missing("请先读取所选商品的确切规格与可用数量，不能用其他商品或规格的查询代替")


def require_cart(ids, results):
    # A later empty page replaces its old contents rather than authorizing removed rows.
    pages = {}
    for tool in results:
        if tool["name"] == "get_cart":
            query = tool["query"]
            pages[(query.get("after"), query.get("limit", 20))] = tool["result"]
    items = {item["id"]: item for page in pages.values() for item in page.get("items", [])}
    if any(not items.get(identifier, {}).get("available") for identifier in ids):
        missing("请先读取本人购物车，并选择当前可用的购物车条目；不能猜测条目编号")


def require_preparation_evidence(name, parameters, results):
    action = parameters["action_type"]
    if action == "ADD_CART":
        require_items(parameters["items"], results)
        return
    if action == "CREATE_ORDER":
        if parameters["source"] == "DIRECT":
            require_items(parameters["items"], results)
        else:
            require_cart(parameters["cart_item_ids"], results)
        if name == "get_checkout_options":
            return
        wanted = selection(parameters)
        options = next(
            (
                tool["result"]
                for tool in reversed(results)
                if tool["name"] == "get_checkout_options" and selection(tool["query"]) == wanted
            ),
            None,
        )
        if options is None:
            missing("请先按本次相同商品、规格、数量和购买方式读取结算选项；选择变化后必须重新读取")
        if parameters["address_id"] not in {a["id"] for a in options.get("addresses", [])}:
            missing("所选地址不在本次结算选项中，请重新读取并让会员选择已有地址")
        coupon = parameters["coupon_id"]
        if coupon is not None and coupon not in {
            c["id"] for c in options.get("coupons", []) if c.get("available") is True
        }:
            missing("所选优惠券不在本次可用选项中，请重新选择")
        if parameters["use_points"] > options.get("available_points", 0):
            missing("所选积分超过本次可用数量，请重新选择")
        return
    if action == "CANCEL_ORDER":
        order = next(
            (
                tool["result"]
                for tool in reversed(results)
                if tool["name"] == "query_my_orders"
                and tool["query"].get("kind") == "ORDER"
                and tool["result"].get("id") == parameters["order_id"]
            ),
            None,
        )
        if order is None or type(order.get("status")) is not int or order["status"] != 0:
            missing("请先读取这笔本人未付款订单的详情；其他订单或列表查询不能代替")
        return
    eligibility = next(
        (
            tool["result"]
            for tool in reversed(results)
            if tool["name"] == "query_my_orders"
            and tool["query"].get("kind") == "ELIGIBILITY"
            and tool["result"].get("order_id") == parameters["order_id"]
            and tool["result"].get("order_item_id") == parameters["order_item_id"]
        ),
        None,
    )
    if (
        eligibility is None
        or eligibility.get("eligible") is not True
        or parameters["quantity"] > eligibility.get("remaining_quantity", 0)
        or parameters["reason_code"] not in {r["code"] for r in eligibility.get("reason_codes", [])}
    ):
        missing("请先核验本次所选订单明细的退货资格、剩余数量和可选原因，不能沿用其他明细")


def require_return_read(query, results):
    if query.kind != "ELIGIBILITY":
        return
    detail = next(
        (
            tool["result"]
            for tool in reversed(results)
            if tool["name"] == "query_my_orders"
            and tool["query"].get("kind") == "ORDER"
            and (
                tool["result"].get("id") == query.order_id
                if query.order_id
                else tool["result"].get("order_sn") == query.order_sn
            )
        ),
        None,
    )
    if detail is None or query.order_item_id not in {
        line["order_item_id"] for line in detail.get("lines", [])
    }:
        missing("请先读取目标订单ORDER详情，并使用该订单返回的order_item_id查询资格；不能猜明细编号")
