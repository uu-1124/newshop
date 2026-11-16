"""Bind cancellation targets to member text and current owned order facts."""

import json
import re
from decimal import Decimal, InvalidOperation

from app.agents.item_choices import Catalog, Choice, member_preparation_paused, normalized
from app.agents.member_choices import member_texts
from app.domain.identity import DomainError

ID = re.compile(
    r"(?:订单(?:内部)?(?:编号|id)?|内部编号)[:：#]?(\d+)(?![\da-z_.-])((?:(?:、|和|及|与|以及)\d+(?![\da-z_.-]))*)"
)
SN = re.compile(r"(?:完整)?订单号(?:码)?[:：#]?([a-z0-9][a-z0-9_-]{0,79})(?![a-z0-9_.-])")
UNCERTAIN = re.compile(
    r"没决定|没选|没想好|未决定|不确定|含糊|哪(?:一)?笔|任选|随便|如果|假如|例如|解释|这句话|什么意思|还是|或|尾号|末尾|[?？/／|]"
)
REVISION = re.compile(r"改选|改为|改成|换成|换为|换选|重新选择|最后选|最终选")
NEGATIVE = re.compile(r"不要|不用|不选|不取消|不想|别|勿|不处理|不办理|不再|撤回|放弃|donot|don't|never")
OTHER = re.compile(r"另一笔|另外一笔|其他那笔|另外那笔")
REVOKED = re.compile(
    r"(?:不再|先别|暂不|不要再|不想再|不用再)取消|(?:这笔|那笔|订单).*(?:不要|别|不)取消|不取消(?:这笔|那笔|订单)|撤回(?:取消|选择)|先不(?:处理|办理)|算了"
)
MONEY = re.compile(
    r"(?:金额|应付|实付)[:：]?(\d+(?:\.\d{1,2})?)(?![\da-z_.])|(?<![\da-z_.])(\d+(?:\.\d{1,2})?)元"
)


def observed_orders(results):
    orders, complete = {}, False
    for tool in results:
        if tool["name"] != "query_my_orders":
            continue
        data, query = tool["result"], tool["query"]
        if query.get("kind") == "ORDERS" and isinstance(data.get("items"), list):
            if not query.get("cursor") and not query.get("order_sn") and query.get("status") in {None, 0}:
                orders = {row["id"]: row for row in data["items"]}
                complete = data.get("has_more") is False
            else:
                orders.update((row["id"], row) for row in data["items"])
        elif query.get("kind") == "ORDER" and data.get("id"):
            orders[data["id"]] = data
    return orders, complete


def explicit_references(text, orders):
    matches, unknown = set(), False
    spans = []
    for marker, field in [(SN, "order_sn"), (ID, "id")]:
        for match in marker.finditer(text):
            spans.append(match.span())
            values = [match[1]] + (re.findall(r"\d+", match[2]) if marker is ID else [])
            for value in values:
                found = {
                    key for key, order in orders.items() if normalized(str(order.get(field, ""))) == value
                }
                if not found:
                    unknown = True
                matches.update(found)
    for key, order in orders.items():
        number = normalized(order.get("order_sn", ""))
        if number and re.search(r"(?<![a-z0-9_.-])" + re.escape(number) + r"(?![a-z0-9_.-])", text):
            matches.add(key)
    # A malformed marked numeric ID must not silently reuse a previous choice.
    residual = text
    for start, end in reversed(sorted(spans)):
        residual = residual[:start] + " " * (end - start) + residual[end:]
    if re.search(r"(?:订单号|订单编号|订单id|内部编号|订单)[:：#]?[a-z0-9]", residual):
        unknown = True
    if any(
        not any(normalized(order.get("order_sn", "")) == match[0] for order in orders.values())
        for match in re.finditer(r"(?<!\d)\d{10,}(?!\d)", text)
    ):
        unknown = True
    return matches, unknown


def natural_matches(text, orders):
    orders = {
        key: order
        for key, order in orders.items()
        if type(order.get("status")) is int and order["status"] == 0
    }
    products, line_sources = {}, []
    for key, order in orders.items():
        for index, line in enumerate(order.get("lines", [])):
            name, sku = line.get("name"), line.get("sku_id")
            if not name:
                continue
            pid = f"{key}:{index}"
            try:
                raw = json.loads(line.get("specification", ""))
                attrs = [
                    {"name": item["key"], "value": item["value"]}
                    for item in raw
                    if isinstance(item, dict)
                    and isinstance(item.get("key"), str)
                    and isinstance(item.get("value"), str)
                ]
            except (ValueError, TypeError):
                attrs = []
            products[pid] = {"name": name, "sku_options": [{"id": sku or pid, "attributes": attrs}]}
            line_sources.append((pid, key, line.get("quantity")))
    catalog = Catalog(products)
    anchors = catalog.anchors(text)
    mentioned = {}
    for pid in products:
        for alias, dimensions in catalog.attributes(pid).items():
            if alias in text:
                for name, values in dimensions.items():
                    mentioned.setdefault(name, set()).update(values)
    if any(len(values) != 1 for values in mentioned.values()):
        return set()
    color_signal = bool(
        re.search(
            r"颜色|[红蓝紫黑白金银灰绿黄橙粉棕青]色|"
            r"[红蓝紫黑白金银灰绿黄橙粉棕青](?=\d+(?:g|t)|(?:那|这)(?:一)?笔|的?订单)",
            text,
        )
    )
    if color_signal and not any("颜色" in name for name in mentioned):
        return set()
    search_text = SN.sub("", ID.sub("", text))
    for match in re.finditer(r"[a-z]+[-]?\d+[a-z]*", search_text):
        token = match[0]
        if token in {normalized(order.get("order_sn", "")) for order in orders.values()}:
            continue
        if not any(token in normalized(product["name"]) for product in products.values()):
            return set()
    candidates = set()
    specification = bool(
        re.search(r"颜色|容量|规格|尺码|[红蓝紫黑白金银灰绿黄橙粉棕青]色|\d+(?:g|gb|t|tb)(?![a-z])", text)
    )
    has_product = bool(anchors) or specification or bool(mentioned)
    for pid, key, quantity in line_sources:
        if anchors and not any(pid in anchor[2] for anchor in anchors):
            continue
        if not has_product:
            continue
        choice = Choice(pid)
        # Here cancellation is the requested order action, not a negation of the
        # product specification. Keep actual negations for both parsers to reject.
        catalog.update(choice, text.replace("取消", ""))
        if choice.spec_invalid:
            continue
        sku = products[pid]["sku_options"][0]
        attrs = {a["name"]: normalized(a["value"]) for a in sku["attributes"]}
        if any(attrs.get(name) not in values for name, values in mentioned.items()):
            continue
        if choice.sku_id and choice.sku_id != sku["id"]:
            continue
        if any(attrs.get(k) != v for k, v in choice.dimensions.items()):
            continue
        if choice.quantity is not None and choice.quantity != quantity:
            continue
        candidates.add(key)
    amounts = [m[1] or m[2] for m in MONEY.finditer(text)]
    if amounts:
        try:
            values = {Decimal(value) for value in amounts}
            amount_orders = {
                key for key, order in orders.items() if Decimal(str(order.get("pay_amount", "-1"))) in values
            }
        except InvalidOperation:
            return None
        if len(values) != 1:
            return None
        candidates = candidates & amount_orders if has_product else amount_orders
    if has_product or amounts:
        return candidates
    return None


def selected_order(orders, complete, history, question, entry_order_id=None):
    selected = entry_order_id if entry_order_id in orders else None
    for message in member_texts(history, question)[-21:]:
        text = normalized(message)
        text = re.sub(r"(?<=\d)[，,](?=\d)", "、", text)
        if not text:
            continue
        if member_preparation_paused([], message):
            selected = None
            continue
        # Execution disclaimers do not revoke a request to prepare a card.
        parts = re.split(r"[，,。；;\n]", text)
        turn, changed, invalid = set(), False, False
        for part in parts:
            if not part:
                continue
            references, unknown = explicit_references(part, orders)
            natural = natural_matches(part, orders)
            contextual = bool(
                re.search(r"订单|这笔|那笔|另一笔|取消哪|撤回选择|算了|先不办理", part)
                or REVOKED.search(part)
                or re.fullmatch(r"(?:先)?(?:不要|不用|别)取消(?:了)?", part)
            )
            if not (references or unknown or natural is not None or contextual):
                continue
            if (
                REVOKED.search(part)
                or UNCERTAIN.search(part)
                or re.search(r"\d\s*[-~～至到]\s*\d", part)
                or re.fullmatch(r"(?:先)?(?:不要|不用|别)取消(?:了)?", part)
            ):
                invalid = True
                continue
            revisions = list(REVISION.finditer(part))
            if revisions:
                last = revisions[-1]
                if re.search(r"(?:不要|别|勿|不想)$", part[: last.start()]):
                    invalid = True
                    continue
                prefix, part = part[: last.start()], part[last.end() :]
                if re.fullmatch(r"\d+", part) and re.search(r"订单|编号", prefix):
                    part = "订单" + part
                references, unknown = explicit_references(part, orders)
                natural = natural_matches(part, orders)
                turn.clear()
                # A clear later revision can replace an earlier rejected target.
                invalid = False
            if unknown:
                invalid = True
                continue
            if OTHER.search(part):
                candidates = {
                    key
                    for key, value in orders.items()
                    if type(value.get("status")) is int and value["status"] == 0
                }
                chosen = turn if len(turn) == 1 else {selected} if selected else set()
                references = candidates - chosen if complete and len(chosen) == 1 else set()
                if len(references) != 1:
                    invalid = True
                    continue
            elif (
                not references
                and natural is None
                and re.search(r"(?:刚才|之前)?(?:这|那)(?:一)?笔|本订单", part)
            ):
                references = turn.copy() if len(turn) == 1 else {selected} if selected else set()
                if not references:
                    invalid = True
                    continue
            if natural is not None:
                if not complete and not references:
                    invalid = True
                    continue
                references = references & natural if references else natural
                if len(references) != 1:
                    invalid = True
                    continue
            if references:
                changed = True
                if NEGATIVE.search(part):
                    invalid = True
                else:
                    turn.update(references)
            elif re.search(r"改|换|订单号|订单编号|不选|不取消|没决定", part):
                invalid = True
        if invalid or len(turn) > 1:
            selected = None
        elif changed:
            selected = next(iter(turn)) if len(turn) == 1 else None
    return selected


def require_member_order(parameters, orders, complete, history, question, entry_order_id=None):
    if parameters.get("action_type") != "CANCEL_ORDER":
        return
    chosen = selected_order(orders, complete, history, question, entry_order_id)
    if chosen != parameters["order_id"]:
        raise DomainError(
            "MEMBER_CHOICE_REQUIRED",
            "请明确选择要准备取消的本人订单。可提供完整订单号、订单编号或能唯一定位的商品规格；改选、未决定或有多笔匹配时需先确认目标。这里只准备确认卡，当前尚未取消订单。",
            409,
        )


def requests_cancel_preparation(question):
    text = normalized("".join(member_texts([], question)))
    if member_preparation_paused([], question) or re.search(
        r"解释|什么意思|例如|假如|示例|这句话|怎么写|教我", text
    ):
        return False
    return bool(
        re.search(r"取消", text)
        and re.search(r"(?:准备|生成|出)(?:新的|新|一张|一个|本次的)?(?:取消|订单|确认|卡)", text)
    )
