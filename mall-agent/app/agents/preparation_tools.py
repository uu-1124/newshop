"""Strict model schemas for preparation only; execute/confirm are never registered."""

import json

from app.api.actions import ActionPrepareInput, CheckoutSelection
from app.domain.identity import DomainError

JID = {"type": "string", "pattern": r"^[1-9][0-9]{0,18}$"}
ITEM = {
    "type": "object",
    "properties": {
        "product_id": JID,
        "sku_id": JID,
        "quantity": {"type": "integer", "minimum": 1, "maximum": 999},
    },
    "required": ["product_id", "sku_id", "quantity"],
    "additionalProperties": False,
}


def nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


SELECTION = {
    "source": {"type": "string", "enum": ["DIRECT", "CART"]},
    "items": nullable({"type": "array", "items": ITEM, "minItems": 1, "maxItems": 20}),
    "cart_item_ids": nullable({"type": "array", "items": JID, "minItems": 1, "maxItems": 20}),
}
PARAMETERS = {
    "action_type": {"type": "string", "enum": ["ADD_CART", "CREATE_ORDER", "CANCEL_ORDER", "CREATE_RETURN"]},
    **{key: (nullable(value) if key == "source" else value) for key, value in SELECTION.items()},
    "address_id": nullable(JID),
    "coupon_id": nullable(JID),
    "use_points": nullable({"type": "integer", "minimum": 0, "maximum": 100000000}),
    "order_id": nullable(JID),
    "order_item_id": nullable(JID),
    "quantity": nullable({"type": "integer", "minimum": 1, "maximum": 999}),
    "reason_code": nullable({"type": "string", "maxLength": 80}),
    "description": nullable({"type": "string", "maxLength": 1000}),
    "attachment_ids": nullable(
        {"type": "array", "items": {"type": "string", "format": "uuid"}, "maxItems": 5}
    ),
}


def tool(name, description, fields):
    return {
        "type": "function",
        "name": name,
        "description": description,
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": fields,
            "required": list(fields),
            "additionalProperties": False,
        },
    }


CHECKOUT_TOOL = tool(
    "get_checkout_options",
    "读取已选DIRECT或CART商品的已有地址、优惠券和积分选项；不用的选择字段填null，不能猜ID。",
    SELECTION,
)
CART_TOOL = tool(
    "get_cart",
    "分页只读当前登录会员的购物车。仅使用本轮返回的可用条目ID；下一页使用next_cursor。page_item_count与page_quantity仅指本页，has_more为true时不能说是全购物车总数。价格是快照，结算须另读选项并报价。",
    {"limit": {"type": "integer", "minimum": 1, "maximum": 20}, "after": nullable(JID)},
)
PREPARE_TOOL = tool(
    "prepare_business_action",
    "仅准备不可变确认卡，不执行任何商城业务。先查询真实商品/订单/资格和选项、收集会员明确选择；CART只用会员选定的购物车条目，取消只用会员选定的本人订单；读过对象不代表会员已选择，多笔或未决定先澄清。用户必须另行点击确认。不适用字段全部null，不猜地址、券、退货原因或凭证ID。",
    PARAMETERS,
)
HUMAN_TOOL = tool(
    "request_human",
    "仅当本轮用户明确要求人工客服时发起排队，不承诺接待时间；模型自行判断无法解答时只建议人工按钮。",
    {},
)


def parse_preparation(name, arguments, allowed_actions):
    try:
        raw = json.loads(arguments)
        if not isinstance(raw, dict):
            raise ValueError()
        fields = SELECTION if name == "get_checkout_options" else PARAMETERS
        if set(raw) != set(fields):
            raise ValueError()
        value = {key: item for key, item in raw.items() if item is not None}
        action = value.get("action_type") if name != "get_checkout_options" else "CREATE_ORDER"
        if action not in allowed_actions:
            raise ValueError()
        required = {
            "ADD_CART": ["items"],
            "CANCEL_ORDER": ["order_id"],
            "CREATE_RETURN": [
                "order_id",
                "order_item_id",
                "quantity",
                "reason_code",
                "description",
                "attachment_ids",
            ],
            "CREATE_ORDER": ["source"] + (["cart_item_ids"] if value.get("source") == "CART" else ["items"]),
        }[action]
        if name == "prepare_business_action" and action == "CREATE_ORDER":
            required += ["address_id", "use_points"]
        labels = {
            "items": "商品规格和数量",
            "order_id": "本人订单",
            "order_item_id": "退货明细",
            "quantity": "退货数量",
            "reason_code": "退货原因",
            "description": "退货说明（没有可填无）",
            "attachment_ids": "凭证选择（没有可选不提供）",
            "source": "购物车或直接购买方式",
            "cart_item_ids": "购物车商品",
            "address_id": "收货地址",
            "use_points": "积分使用数量（不使用可选0）",
        }
        missing = [labels[key] for key in required if key not in value]
        if missing:
            raise DomainError(
                "PREPARATION_NEEDS_INPUT",
                "还需要你选择或补充："
                + "、".join(missing)
                + "。补充后我再准备确认卡，当前尚未执行任何操作。",
                422,
            )
        if name == "get_checkout_options":
            if "CREATE_ORDER" not in allowed_actions:
                raise ValueError()
            return CheckoutSelection.model_validate({"action_type": "CREATE_ORDER", **value}).wire()
        if value.get("action_type") not in allowed_actions:
            raise ValueError()
        if value["action_type"] == "CREATE_ORDER":
            value["coupon_id"] = raw["coupon_id"]
        return ActionPrepareInput.model_validate(
            {"expected_conversation_version": 1, "parameters": value}
        ).wire()["parameters"]
    except (ValueError, TypeError, KeyError):
        raise DomainError(
            "TOOL_ARGUMENTS_INVALID", "业务参数不完整，请选择规格、地址或订单明细后再准备", 422
        ) from None
