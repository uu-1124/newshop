"""Member-only, bounded cart selection against currently authorized row facts."""

import json
import re

from app.agents.item_choices import QUANTITY, Catalog, Choice, count, member_preparation_paused, normalized
from app.agents.member_choices import member_texts
from app.domain.identity import DomainError

CART = re.compile(r"购物车|条目|购物行|那(?:一)?行|这(?:一)?行|cart(?:item)?")
MARKER = r"(?:(?:购物车)?(?:条目|记录|行)|购物车|cart(?:item)?)(?:编号|id|号)?"
ROW_IDS = re.compile(MARKER + r"[:：#]?(\d+)(?![\da-z_.])((?:(?:、|和|及|与|以及|还有)\d+(?![\da-z_.]))*)")
UNCERTAIN = re.compile(
    r"[?？/／|]|还是|或|(?:\d)or(?=\d)|任选|随便|左右|大约|没决定|没选|没想好|不确定|未决定|如果|假如|例如|解释|什么意思|是否|可以吗|要不要"
)
NEGATIVE = re.compile(r"不要|不用|不选|不买|别|勿|取消|撤销|放弃|donot|don't|never")
REVISION = re.compile(r"改用|改为|改成|换用|换成|换为|调整为|最后选|最终选")
ADDITIVE = re.compile(r"另外|再加|还要|同时|追加|也要")


def observed_cart(results):
    pages = {}
    for tool in results:
        if tool["name"] == "get_cart" and "items" in tool["result"]:
            pages[(tool["query"].get("after"), tool["query"].get("limit", 20))] = tool["result"]
    items = {row["id"]: row for page in pages.values() for row in page["items"]}
    # Only a contiguous chain starting at the first page proves completeness.
    after, visited = None, set()
    complete = False
    while after not in visited:
        visited.add(after)
        page = next((p for (a, _), p in reversed(list(pages.items())) if a == after), None)
        if page is None:
            break
        if page.get("has_more") is False:
            complete = True
            break
        after = page.get("next_cursor")
        if after is None:
            break
    return items, complete


def cart_catalog(items):
    products = {}
    for row in items.values():
        pid, sku = row.get("product_id"), row.get("sku_id")
        if not pid or not sku:
            continue
        product = products.setdefault(pid, {"id": pid, "name": row.get("name", ""), "sku_options": []})
        try:
            raw = json.loads(row.get("specification", ""))
            attributes = [
                {"name": v["key"], "value": v["value"]}
                for v in raw
                if isinstance(v, dict) and isinstance(v.get("key"), str) and isinstance(v.get("value"), str)
            ]
        except (ValueError, TypeError):
            attributes = []
        if not any(s["id"] == sku for s in product["sku_options"]):
            product["sku_options"].append({"id": sku, "attributes": attributes})
    return Catalog(products)


def clause_rows(text, items, catalog, complete, focus):
    marked = list(ROW_IDS.finditer(text))
    if marked:
        selected = set()
        for match in marked:
            selected.add(match[1])
            selected.update(re.findall(r"\d+", match[2]))
        if not selected <= items.keys():
            return None
        quantities = {count(m[1] or m[2]) for m in QUANTITY.finditer(text)}
        if quantities and (
            len(quantities) != 1
            or None in quantities
            or any(items[key].get("quantity") != next(iter(quantities)) for key in selected)
        ):
            return None
        residual = ROW_IDS.sub("", text)
        if catalog.anchors(residual):
            extra = clause_rows(residual, items, catalog, complete, focus)
            if extra is None:
                return None
            if not re.search(r"和|以及|还有|另外|再加|同时|也要", residual) and not extra <= selected:
                return None
            selected.update(extra)
        return selected
    anchors = catalog.anchors(text)
    if (
        not anchors
        and focus in items
        and items[focus].get("product_id")
        and re.search(r"色|容量|规格|数量|\d+[gt]|那(?:一)?行|这(?:一)?行", text)
    ):
        text = "商品" + items[focus]["product_id"] + text
        anchors = catalog.anchors(text)
    if not anchors:
        return set()
    if not complete:
        return None
    pids = set().union(*(a[2] for a in anchors))
    if len(pids) != 1:
        return None
    pid = next(iter(pids))
    if pid not in catalog.products:
        return None
    choice = Choice(pid)
    # Negation applies to the resolved row, not to its specifications.
    description = NEGATIVE.sub("", text)
    catalog.update(choice, description)
    if choice.spec_invalid:
        return None
    candidates = []
    for row in items.values():
        if row.get("product_id") != pid:
            continue
        sku = next((s for s in catalog.products[pid]["sku_options"] if s["id"] == row.get("sku_id")), {})
        attrs = {a["name"]: normalized(a["value"]) for a in sku.get("attributes", [])}
        if choice.sku_id and row.get("sku_id") != choice.sku_id:
            continue
        if any(attrs.get(k) != v for k, v in choice.dimensions.items()):
            continue
        if choice.quantity is not None and choice.quantity != row.get("quantity"):
            continue
        candidates.append(row["id"])
    return set(candidates) if len(candidates) == 1 else None


def member_cart_rows(items, complete, history, question):
    catalog, selected, cart_context = cart_catalog(items), None, False
    if member_preparation_paused(history, question):
        return None
    for message in member_texts(history, question)[-21:]:
        text = normalized(message)
        text = re.sub(r"(?<=\d)[，,](?=\d)", "、", text)
        if re.search(r"直接购买|直购", text) and not re.search(r"改.*购物车|换.*购物车", text):
            selected, cart_context = None, False
            continue
        cart_context = cart_context or bool(CART.search(text))
        if not cart_context:
            continue
        contextual = bool(
            selected
            and (
                (
                    re.search(r"改|换|不买|不选|不要准备|不用准备", text)
                    and re.search(r"规格|颜色|容量|数量|色|\d+[gt]|不买|不选|准备", text)
                )
                or "数量" in text
                or QUANTITY.search(text)
            )
        )
        mentioned = CART.search(text) or catalog.anchors(text) or contextual
        if not mentioned:
            continue
        if re.search(r"先看|看看|查询|读取|核对|多少钱|怎么样|有没有", text) and not re.search(
            r"准备|结算|购买|选|要|改|换", text
        ):
            continue
        # Address/coupon/points questions in another clause do not turn an
        # explicit row selection into a question about those unrelated fields.
        relevant = [
            part
            for part in re.split(r"[，,。；;\n]", text)
            if CART.search(part)
            or catalog.anchors(part)
            or (
                contextual
                and not re.search(r"地址|券|积分", part)
                and (re.search(r"改|换|不买|不选|准备|数量", part) or QUANTITY.search(part))
            )
            or (CART.search(text) and ("数量" in part or QUANTITY.search(part)))
        ]
        if any(UNCERTAIN.search(part) or re.search(r"\d\s*[-~～至到]\s*\d", part) for part in relevant):
            selected = None
            continue
        invalid, changed = False, False
        focus = next(iter(selected)) if selected and len(selected) == 1 else None
        additive = bool(ADDITIVE.search(text))
        turn = set(selected or ()) if additive else set()
        for part in relevant:
            if re.fullmatch(
                r"(?:(?:原|原来|刚才)(?:的)?(?:商品|那行)?)?(?:规格|数量)(?:(?:和|及)?(?:规格|数量))*"
                r"(?:保持)?不变",
                part,
            ):
                continue
            if (
                not CART.search(part)
                and not catalog.anchors(part)
                and ("数量" in part or QUANTITY.search(part))
            ):
                quantities = {count(m[1] or m[2]) for m in QUANTITY.finditer(part)}
                targets = turn or selected
                if (
                    not targets
                    or len(quantities) != 1
                    or None in quantities
                    or any(items.get(key, {}).get("quantity") != next(iter(quantities)) for key in targets)
                ):
                    invalid = True
                continue
            revision = list(REVISION.finditer(part))
            if revision:
                last = revision[-1]
                if re.search(r"数量", part[: last.start()]):
                    # Preparation cannot mutate a cart row to a requested new
                    # quantity. Read it again after the member edits the cart.
                    invalid = True
                    continue
                if NEGATIVE.search(part[: last.start()]) and re.search(
                    r"(?:不要|别|勿|不想)$", part[: last.start()]
                ):
                    invalid = True
                    continue
                turn.clear()
                prefix, part = part[: last.start()], part[last.end() :]
                if re.fullmatch(r"\d+", part) and re.search(r"条目|记录|购物行", prefix):
                    part = "条目" + part
            if re.search(r"(?:全选|全部|所有)(?:可用)?购物车|购物车(?:全选|全部|所有)(?:条目|商品)?", part):
                if NEGATIVE.search(part):
                    invalid = True
                    continue
                if not complete or not items:
                    invalid = True
                    continue
                eligible = {key for key, row in items.items() if row.get("available") is True}
                if "可用" not in part and len(eligible) != len(items):
                    invalid = True
                    continue
                turn.update(eligible)
                changed = True
                continue
            ids = clause_rows(part, items, catalog, complete, focus)
            if ids is None:
                invalid = True
                continue
            if not ids:
                if re.search(r"改|换|选择|选定|不买|不选|不要|不用", part):
                    invalid = True
                continue
            if NEGATIVE.search(part):
                if not changed and not additive:
                    turn = set(selected or ())
                turn.difference_update(ids)
            else:
                turn.update(ids)
            changed = True
        if invalid:
            selected = None
        elif changed:
            selected = turn
    return selected


def require_member_cart(parameters, items, complete, history, question):
    if parameters.get("action_type") != "CREATE_ORDER" or parameters.get("source") != "CART":
        return
    selected = member_cart_rows(items, complete, history, question)
    if (
        not selected
        or selected != set(parameters["cart_item_ids"])
        or any(items.get(key, {}).get("available") is not True for key in selected)
    ):
        raise DomainError(
            "MEMBER_CHOICE_REQUIRED",
            "请明确选择本次结算的购物车条目。可以说商品名和规格，也可说条目编号；未决定、含糊、已变更或不可用时需重新选择。选择后仍须点击确认卡，当前尚未执行业务。",
            409,
        )
