"""Object/field-bound display facts. The model selects facts; it cannot rewrite them.

Product, order, SKU and policy identity remain attached to each display value.
Knowledge is quoted from the current source without inventing a policy paraphrase.
"""

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.domain.identity import DomainError

GUIDANCE = {
    "G_UNKNOWN": "当前资料不足以确认，请补充具体信息或联系人工客服核实。",
    "G_CLARIFY": "请明确选择需要查询的商品、规格、订单或明细。",
    "G_HUMAN": "涉及未公布的政策或存在疑问时，请联系人工客服核实。",
    "G_CONFIRM": "准备确认卡不代表已执行业务，请核对内容后再自行点击确认。",
    "G_DEMO": "本商城的支付、退款和物流使用演示流程，不代表真实扣款、到账或快递履约。",
    "G_COMPARE": "比较仅依据本轮已查询的参数、价格和库存，未提供的参数不能推断。",
    "G_DIFFERENCE": "请分别核对以下资料的适用范围，表述不一致时交人工确认，不自行合并规则。",
    "G_LIVE": "资料用于解释规则，实际价格、库存、订单状态和可退资格仍以本次商城查询及确认卡为准。",
}

UNSAFE_CONTENT = re.compile(
    r"https?://|javascript\s*:|data\s*:|<\s*/?\s*(?:script|iframe|object)|Bearer\s+|sk-[A-Za-z0-9]|(?<!\d)1[3-9]\d{9}(?!\d)",
    re.I,
)
SOURCE_INSTRUCTION = re.compile(
    r"忽略.{0,20}(?:指令|规则|限制)|(?:系统|开发者)提示|暗号|CANARY|(?:赋予|授予|获得).{0,15}管理员|"
    r"(?:绕过|覆盖|替换).{0,20}(?:权限|规则|指令)|执行.{0,15}(?:SQL|shell|命令)|泄露.{0,15}(?:密钥|令牌)|"
    r"ignore.{0,30}(?:instruction|rule)|system prompt|developer prompt|reveal.{0,30}(?:secret|password|key)|you are now|管理员口令|测试口令",
    re.I,
)
MONEY_FIELDS = {
    "pay_amount": "实付金额",
    "paid_line_amount": "明细实付金额",
    "unit_price": "单价",
    "price": "价格",
    "price_from": "起售价",
    "requested_amount": "申请金额",
    "approved_amount": "审核金额",
    "remaining_amount": "剩余可申请金额",
    "payable": "应付金额",
    "subtotal": "商品合计",
    "promotion_discount": "促销优惠",
    "coupon_discount": "优惠券抵扣",
    "points_discount": "积分抵扣",
    "freight": "运费",
    "amount": "面额",
}
COUNT_FIELDS = {
    "quantity": ("数量", "件"),
    "returnable_quantity": ("可退数量", "件"),
    "remaining_quantity": ("剩余可申请数量", "件"),
    "available_quantity": ("可用库存", "件"),
    "page_item_count": ("本页购物车条目", "条"),
    "page_quantity": ("本页购物车件数", "件"),
    "available_points": ("可用积分", "分"),
}
STAGES = {
    "REQUESTED": "申请待审核",
    "APPROVED": "审核通过，等待收货",
    "GOODS_RECEIVED": "已登记收到退货，尚未登记演示退款",
    "SIMULATED_REFUNDED": "演示退款已登记，不代表真实资金到账",
    "REJECTED": "申请已拒绝",
    "LEGACY": "历史售后记录，需人工核对明细",
}
ORDER_STATES = {0: "待付款", 1: "待发货", 2: "已发货", 3: "已完成", 4: "已关闭", 5: "无效订单"}


@dataclass(frozen=True)
class DisplayFact:
    id: str
    text: str
    path: str
    source_index: int | None = None
    required: bool = False


class FactCatalogue:
    def __init__(self):
        self.facts = {}
        self.seen = set()
        self.characters = 0

    def add(self, text, path, source_index=None, required=False):
        text = text.strip()
        if not text or UNSAFE_CONTENT.search(text) or text in self.seen:
            return
        if len(text) > 8000:
            raise DomainError("EVIDENCE_BUDGET_EXCEEDED", "单条资料过长，请缩小查询范围", 409)
        if len(self.facts) >= 512 or self.characters + len(text) > 48000:
            raise DomainError("EVIDENCE_BUDGET_EXCEEDED", "本轮事实过多，请缩小查询范围", 409)
        identifier = f"F{len(self.facts) + 1:04}"
        self.facts[identifier] = DisplayFact(identifier, text, path, source_index, required)
        self.seen.add(text)
        self.characters += len(text)

    def wire(self):
        return [
            {
                "id": fact.id,
                "text": fact.text,
                "field": fact.path,
                "source_number": fact.source_index + 1 if fact.source_index is not None else None,
            }
            for fact in self.facts.values()
        ] + [
            {"id": identifier, "text": text, "field": "guidance", "source_number": None}
            for identifier, text in GUIDANCE.items()
        ]

    def resolve(self, identifier):
        if identifier in GUIDANCE:
            return DisplayFact(identifier, GUIDANCE[identifier], "guidance")
        if identifier not in self.facts:
            raise DomainError("FACT_RECHECK_REQUIRED", "回复引用了本轮不存在的事实", 409)
        return self.facts[identifier]


def money(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9]\d{0,15})\.\d{2}", value):
        raise DomainError("FACT_PROTOCOL_ERROR", "金额事实格式无效", 503)
    try:
        if Decimal(value) < 0:
            raise ValueError()
    except (InvalidOperation, ValueError):
        raise DomainError("FACT_PROTOCOL_ERROR", "金额事实格式无效", 503) from None
    return value


def specification(value):
    if isinstance(value, list):
        return " / ".join(
            f"{a['name']}：{a['value']}"
            for a in value
            if isinstance(a, dict) and "name" in a and "value" in a
        )
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return " / ".join(
                    f"{a['key']}：{a['value']}"
                    for a in parsed
                    if isinstance(a, dict) and "key" in a and "value" in a
                )
        except (ValueError, TypeError):
            pass
        return value
    return ""


def add_fields(catalogue, data, subject, path):
    for key, label in MONEY_FIELDS.items():
        if key in data and data[key] is not None:
            catalogue.add(f"{subject}的{label}为¥{money(data[key])}。", path + "." + key)
    for key, (label, unit) in COUNT_FIELDS.items():
        if key in data:
            value = data[key]
            if type(value) is not int or not 0 <= value <= 2147483647:
                raise DomainError("FACT_PROTOCOL_ERROR", "数量事实格式无效", 503)
            catalogue.add(f"{subject}的{label}为{value}{unit}。", path + "." + key)


def product_facts(catalogue, product, path):
    subject = "商品「" + str(product.get("name", "未提供名称")) + "」"
    add_fields(catalogue, product, subject, path)
    if product.get("brand"):
        catalogue.add(f"{subject}的品牌为{product['brand']}。", path + ".brand")
    for i, fact in enumerate(product.get("facts", [])):
        if SOURCE_INSTRUCTION.search(str(fact.get("value", ""))):
            continue
        catalogue.add(f"{subject}的参数「{fact['name']}」为「{fact['value']}」。", f"{path}.facts[{i}]")
    for i, sku in enumerate(product.get("sku_options", [])):
        label = specification(sku.get("attributes", []))
        sku_subject = subject + (f"（规格「{label}」）" if label else f"（本轮第{i + 1}个规格）")
        add_fields(catalogue, sku, sku_subject, f"{path}.sku_options[{i}]")
        if sku.get("visible") is False:
            catalogue.add(f"{sku_subject}当前不可选。", f"{path}.sku_options[{i}].visible")


def order_facts(catalogue, order, path):
    subject = "订单「" + str(order.get("order_sn", "本轮所查订单")) + "」"
    add_fields(catalogue, order, subject, path)
    if type(order.get("status")) is int and order["status"] in ORDER_STATES:
        catalogue.add(f"{subject}当前状态为「{ORDER_STATES[order['status']]}」。", path + ".status")
    if order.get("is_simulated") is True:
        catalogue.add(f"{subject}使用演示流程，不代表真实资金或快递履约。", path + ".is_simulated")
    for i, line in enumerate(order.get("lines", [])):
        item = subject + f"的第{i + 1}项明细「" + str(line.get("name", "所选明细")) + "」"
        if line.get("specification"):
            item += "（" + specification(line["specification"]) + "）"
        add_fields(catalogue, line, item, f"{path}.lines[{i}]")
    for i, item in enumerate(order.get("returns", [])):
        return_facts(catalogue, item, subject + "的售后记录", f"{path}.returns[{i}]")


def return_facts(catalogue, item, subject, path):
    if item.get("id"):
        subject += "「申请" + str(item["id"]) + "」"
    add_fields(catalogue, item, subject, path)
    if item.get("stage") in STAGES:
        catalogue.add(f"{subject}的阶段为「{STAGES[item['stage']]}」。", path + ".stage")
    if type(item.get("eligible")) is bool:
        catalogue.add(
            f"{subject}当前{'符合' if item['eligible'] else '不符合'}演示退货申请条件。", path + ".eligible"
        )
    if item.get("failure_reason"):
        catalogue.add(f"{subject}的资格核验提示为「{item['failure_reason']}」。", path + ".failure_reason")
    if item.get("deadline"):
        catalogue.add(f"{subject}的申请截止时间为{item['deadline']}。", path + ".deadline")
    for i, reason in enumerate(item.get("reason_codes", [])):
        catalogue.add(f"{subject}可选的退货原因为「{reason['label']}」。", f"{path}.reason_codes[{i}]")


def tool_facts(catalogue, name, query, data, path):
    if name in {"search_products", "compare_products"}:
        products = data.get("items", data.get("products", []))
        for i, product in enumerate(products):
            product_facts(catalogue, product, f"{path}.products[{i}]")
        if not products:
            catalogue.add("本轮商品查询没有匹配结果，请保留原筛选条件或明确选择调整。", path + ".items")
        if name == "compare_products" and data.get("recommended_product_id"):
            match = next((p for p in products if p["id"] == data["recommended_product_id"]), None)
            if match and data.get("criterion") in {"price", "availability"}:
                criterion = (
                    "符合预算且有库存的最低价规格"
                    if data["criterion"] == "price"
                    else "符合预算的可售规格库存总数"
                )
                catalogue.add(
                    f"本轮比较按{criterion}选择了「{match['name']}」，这不是性能或质量评分。",
                    path + ".recommended_product_id",
                )
    elif name == "get_product":
        product_facts(catalogue, data, path)
    elif name == "get_cart":
        add_fields(catalogue, data, "本轮购物车查询", path)
        if data.get("has_more") is True:
            catalogue.add("购物车仍有下一页，当前页数量不能当作全部购物车数量。", path + ".has_more")
        if data.get("page_item_count") == 0 and data.get("has_more") is False and not query.get("after"):
            catalogue.add("当前购物车为空。", path + ".items")
        for i, item in enumerate(data.get("items", [])):
            subject = "购物车中的「" + item["name"] + "」"
            label = specification(item.get("specification", ""))
            if label:
                subject += "（" + label + "）"
            add_fields(catalogue, item, subject, f"{path}.items[{i}]")
            catalogue.add(
                f"{subject}当前{'可用于结算准备' if item.get('available') else '不可用于结算准备'}。",
                f"{path}.items[{i}].available",
            )
    elif name == "query_my_orders":
        kind = query.get("kind")
        if data.get("requires_user_input"):
            catalogue.add(GUIDANCE["G_CLARIFY"], path + ".requires_user_input")
        elif kind == "ORDERS":
            for i, order in enumerate(data.get("items", [])):
                order_facts(catalogue, order, f"{path}.items[{i}]")
            if not data.get("items"):
                catalogue.add("本轮本人订单查询没有匹配结果。", path + ".items")
        elif kind == "ORDER":
            order_facts(catalogue, data, path)
        elif kind == "ELIGIBILITY":
            return_facts(catalogue, data, "本轮所选订单明细", path)
        elif kind == "RETURNS":
            for i, item in enumerate(data.get("items", [])):
                return_facts(catalogue, item, "本轮订单的售后记录", f"{path}.items[{i}]")
            if not data.get("items"):
                catalogue.add("本轮所查订单暂无售后记录。", path + ".items")
        elif kind == "LOGISTICS":
            if not data.get("nodes"):
                catalogue.add("当前没有维护模拟物流轨迹，不能推断真实物流进度或到货时间。", path + ".nodes")
            for i, node in enumerate(data.get("nodes", [])):
                catalogue.add(
                    f"模拟物流节点：{node['occurred_at']}，{node['location']}，{node['description']}。",
                    f"{path}.nodes[{i}]",
                )
            catalogue.add("本轮物流轨迹为演示记录，不保证真实快递履约或到货日期。", path + ".is_simulated")
    elif name == "get_checkout_options":
        add_fields(catalogue, data, "本次所选商品结算", path)
        for i, address in enumerate(data.get("addresses", [])):
            catalogue.add(
                f"本次可选的已有收货地址为「{address['label']}」，请由你明确选择。", f"{path}.addresses[{i}]"
            )
        for i, coupon in enumerate(data.get("coupons", [])):
            if coupon.get("available"):
                add_fields(catalogue, coupon, "可用优惠券「" + coupon["name"] + "」", f"{path}.coupons[{i}]")
        if data.get("points_rule_description"):
            catalogue.add(
                f"本次结算积分说明：「{data['points_rule_description']}」。",
                path + ".points_rule_description",
            )


def build_catalogue(facts):
    catalogue = FactCatalogue()
    values = facts if isinstance(facts, list) else [facts]
    for i, value in enumerate(values):
        if not isinstance(value, dict):
            continue
        path = f"evidence[{i}]"
        if "excerpt" in value:
            # Whole paragraphs retain conditions/negations. Never detach a number
            # from its rule, or promote source instructions to a policy statement.
            paragraphs = re.split(r"\n\s*\n", value["excerpt"])
            for j, paragraph in enumerate(paragraphs):
                paragraph = paragraph.strip()
                if not paragraph or paragraph.startswith("#") or SOURCE_INSTRUCTION.search(paragraph):
                    continue
                catalogue.add(
                    f"资料《{value.get('title', '本轮资料')}》原文：“{paragraph}”\n",
                    f"{path}.paragraph[{j}]",
                    i,
                )
        elif "name" in value and "result" in value:
            tool_facts(catalogue, value["name"], value.get("query", {}), value["result"], path)
        elif "pay_amount" in value:
            order_facts(catalogue, value, path)
        elif value.get("ranking_degraded") is True:
            catalogue.add(
                "重排服务暂不可用，本次使用经授权和有效性复核的向量检索资料。",
                path + ".ranking_degraded",
                required=True,
            )
    return catalogue
