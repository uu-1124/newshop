"""Bind item preparation to member text, never to a model's proposed parameters.

The bounded parser accepts catalog names/model names, SKU IDs, attribute values,
Chinese quantities and explicit revisions. Unresolved language asks for a choice.
It has no execution authority and uses only current, authorized catalog facts.
"""

import re
import unicodedata
from dataclasses import dataclass, field

from app.agents.member_choices import member_texts
from app.domain.identity import DomainError

NUMBER = r"(?:-?\d+(?:\.\d+)?[a-z]*|[零〇一二两三四五六七八九十百千]+)"
REVISION = re.compile(r"改成|改为|改用|换成|换为|换用|调整为|调整到|还是选|最后选|最终选")
NEGATIVE = re.compile(r"不要|不用|不选|不是|不买|不想|别|勿|取消|撤销|放弃|未选|没有选择|donot|don't|never")
UNCERTAIN = re.compile(
    r"[?？]|还是(?!选)|或者|或是|不确定|没决定|没想好|没选好|未决定|待定|如果|假如|例如|解释|什么意思|多少钱|多少件|是否|可以吗|能不能|要不要|该不该"
)
QUANTITY = re.compile(
    rf"数量\s*[:：]?\s*({NUMBER})|((?<![\da-z.\-])-?\d+(?:\.\d+)?[a-z]*|[零〇一二两三四五六七八九十百千]+)(?:件|台|部|个|双|条|套|只|盒|瓶|袋|本)"
)
PRODUCT_ID = re.compile(r"(?:商品|产品)(?:编号|id|号)?[:：#]?([0-9]+)(?![0-9a-z_.])")
SKU_ID = re.compile(r"(?:sku|规格)(?:编号|id|号)?[:：#]?([0-9]+)(?![0-9a-z_.])")


def normalized(value):
    return (
        re.sub(r"\s+", "", unicodedata.normalize("NFKC", value).casefold())
        .replace("gb", "g")
        .replace("tb", "t")
    )


def count(value):
    if value.isascii():
        return int(value) if value.isdecimal() and 1 <= int(value) <= 999 else None
    digits = {
        c: n
        for n, chars in enumerate(["零〇", "一", "二两", "三", "四", "五", "六", "七", "八", "九"])
        for c in chars
    }
    if not re.fullmatch(r"[零〇一二两三四五六七八九十百]+", value):
        return None
    total, digit, last_unit = 0, 0, 1000
    for char in value:
        if char in digits:
            if digit:
                return None
            digit = digits[char]
        else:
            unit = {"十": 10, "百": 100}[char]
            if unit >= last_unit:
                return None
            total += (digit or 1) * unit
            digit, last_unit = 0, unit
    result = total + digit
    return result if 1 <= result <= 999 else None


def catalog_products(results):
    """Latest observation of each product; a newer unavailable observation wins."""
    products = {}
    for result in results:
        name, data = result["name"], result["result"]
        if name == "get_product":
            values = [data]
        elif name in {"search_products", "compare_products"}:
            values = data.get("items", data.get("products", []))
        else:
            continue
        products.update((p["id"], p) for p in values)
    return products


@dataclass
class Choice:
    product_id: str
    dimensions: dict = field(default_factory=dict)
    quantity: int | None = None
    sku_id: str | None = None
    spec_invalid: bool = False


class Catalog:
    def __init__(self, products):
        self.products = products
        self.skus = {}
        self.aliases = {}
        for pid, product in products.items():
            name = normalized(product.get("name", ""))
            aliases = {name} if len(name) >= 2 else set()
            raw = unicodedata.normalize("NFKC", product.get("name", "")).casefold()
            aliases.update(
                normalized(v)
                for v in re.findall(r"[a-z]+\s*[-]?\d+[a-z]*(?:\s+(?:pro|max|plus|ultra))?", raw)
            )
            for alias in aliases:
                self.aliases.setdefault(alias, set()).add(pid)
            for sku in product.get("sku_options", []):
                self.skus.setdefault(sku["id"], set()).add(pid)

    def anchors(self, text):
        found = [(m.start(), m.end(), {m[1]}, "product") for m in PRODUCT_ID.finditer(text)]
        for m in SKU_ID.finditer(text):
            # A SKU may identify a product, but cannot overrule a different explicit product.
            found.append((m.start(), m.end(), self.skus.get(m[1], set()), "sku"))
        for alias, pids in self.aliases.items():
            for m in re.finditer(re.escape(alias), text):
                if (
                    alias[0].isascii()
                    and m.start()
                    and text[m.start() - 1].isascii()
                    and text[m.start() - 1].isalnum()
                    and not any(
                        normalized(self.products[pid].get("brand", ""))
                        and text[: m.start()].endswith(normalized(self.products[pid].get("brand", "")))
                        for pid in pids
                    )
                ):
                    continue
                if (
                    alias[-1:].isascii()
                    and text[m.end() : m.end() + 1].isalnum()
                    and text[m.end() : m.end() + 1].isascii()
                ):
                    continue
                found.append((m.start(), m.end(), pids, "name"))
        return sorted(found)

    def attributes(self, pid):
        values = {}
        for sku in self.products[pid].get("sku_options", []):
            for attribute in sku.get("attributes", []):
                name, value = attribute["name"], normalized(attribute["value"])
                aliases = {value}
                if "颜色" in name and value:
                    aliases.add(value.rstrip("色")[-1] + "色")
                for alias in aliases:
                    if alias:
                        values.setdefault(alias, {}).setdefault(name, set()).add(value)
        return values

    def resolve(self, choice):
        candidates = []
        for sku in self.products[choice.product_id].get("sku_options", []):
            attrs = {a["name"]: normalized(a["value"]) for a in sku.get("attributes", [])}
            if (choice.sku_id is None or choice.sku_id == sku["id"]) and choice.dimensions == attrs:
                candidates.append(sku["id"])
        return candidates[0] if len(candidates) == 1 and not choice.spec_invalid else None

    def update(self, choice, text):
        if re.fullmatch(
            r"(?:(?:刚才|之前|原来)(?:的)?(?:那款|这款|商品)?|原(?:有|先)?(?:的)?|那款|这款)?(?:商品)?"
            r"(?:颜色|容量|规格|尺码|数量)(?:(?:和|及|与)?(?:颜色|容量|规格|尺码|数量))*"
            r"(?:(?:保持|沿用)(?:原来|原有)?)?(?:不变|原样|原有选择)",
            text,
        ) or re.fullmatch(r"(?:颜色|容量|规格|尺码)(?:保持|沿用)(?:原来|原有)?(?:不变|选择)?", text):
            return
        revision = list(REVISION.finditer(text))
        if len(revision) > 16:
            choice.sku_id, choice.dimensions, choice.quantity = None, {}, None
            choice.spec_invalid = True
            return
        if revision and not re.search(r"(?:不要|别|不想|勿)(?:再)?$", text[: revision[-1].start()]):
            prefix = text[: revision[-1].start()]
            self.update(choice, prefix)
            tail = text[revision[-1].end() :]
            noun = "数量" if "数量" in prefix else "规格" if "规格" in prefix else ""
            text = (noun if re.fullmatch(NUMBER, tail) else "") + tail
        quantities = list(QUANTITY.finditer(text))
        sku_ids = list(SKU_ID.finditer(text))
        attributes = self.attributes(choice.product_id)
        mentioned = {}
        spans = []
        for alias, names in attributes.items():
            for match in re.finditer(re.escape(alias), text):
                # Capacity 128G cannot be matched inside 1128G or 128Gplus.
                if alias[0].isdigit() and match.start() and text[match.start() - 1].isdigit():
                    continue
                if (
                    alias[-1:].isascii()
                    and text[match.end() : match.end() + 1].isascii()
                    and text[match.end() : match.end() + 1].isalnum()
                ):
                    continue
                spans.append(match.span())
                for name, values in names.items():
                    mentioned.setdefault(name, set()).update(values)
        unknown_spec = bool(
            any(
                not any(a <= m.start() and m.end() <= b for a, b in spans)
                for m in re.finditer(r"\d+(?:\.\d+)?[gt](?![a-z])|[红蓝紫黑白金银灰绿黄橙粉棕青]色", text)
            )
            or (
                re.search(r"规格|颜色|容量|尺码", text)
                and not sku_ids
                and not mentioned
                and not re.search(
                    r"(?:查询|先查|读取|核对|核验|重读|查看|真实).*?(?:规格|颜色|容量|尺码)", text
                )
            )
        )
        item_text = bool(
            quantities or sku_ids or mentioned or unknown_spec or re.search(r"规格|数量|那款|这款|不买", text)
        )
        if not item_text:
            return
        negative = NEGATIVE.search(text)
        if negative and negative.start():
            self.update(choice, text[: negative.start()])
            self.update(choice, text[negative.start() :])
            return
        if negative or UNCERTAIN.search(text) or unknown_spec:
            if quantities or "数量" in text:
                choice.quantity = None
            if sku_ids or mentioned or unknown_spec or "规格" in text:
                choice.sku_id, choice.dimensions = None, {}
                choice.spec_invalid = True
            if negative and not (
                quantities or sku_ids or mentioned or unknown_spec or "规格" in text or "数量" in text
            ):
                choice.sku_id, choice.dimensions, choice.quantity = None, {}, None
                choice.spec_invalid = True
            return
        if sku_ids:
            identifiers = {m[1] for m in sku_ids}
            available = {s["id"]: s for s in self.products[choice.product_id].get("sku_options", [])}
            if len(identifiers) != 1 or not identifiers <= available.keys():
                choice.sku_id, choice.dimensions = None, {}
                choice.spec_invalid = True
                return
            choice.sku_id = next(iter(identifiers))
            choice.spec_invalid = False
            choice.dimensions = {
                a["name"]: normalized(a["value"]) for a in available[choice.sku_id].get("attributes", [])
            }
        if mentioned:
            if any(len(v) != 1 for v in mentioned.values()):
                choice.sku_id, choice.dimensions = None, {}
                choice.spec_invalid = True
                return
            updates = {k: next(iter(v)) for k, v in mentioned.items()}
            if sku_ids and any(choice.dimensions.get(k) != v for k, v in updates.items()):
                choice.sku_id, choice.dimensions = None, {}
                choice.spec_invalid = True
                return
            if not sku_ids:
                choice.sku_id = None
            choice.dimensions.update(updates)
            choice.spec_invalid = False
        if quantities:
            values = {count(m[1] or m[2]) for m in quantities}
            if (
                len(values) != 1
                or None in values
                or re.search(r"最多|至少|大约|左右|不超过|少于|多于|至多|每", text)
            ):
                choice.quantity = None
            else:
                choice.quantity = next(iter(values))
        elif "数量" in text:
            choice.quantity = None


def member_preparation_paused(history, question):
    texts = member_texts(history, question)
    latest = next((text for text in reversed(texts) if text.strip()), "")
    return bool(re.search(r"(?:不要|不用|别|暂不|不想|不再|不需要)准备", latest))


def require_member_items(parameters, products, history, question):
    if parameters["action_type"] != "ADD_CART" and not (
        parameters["action_type"] == "CREATE_ORDER" and parameters.get("source") == "DIRECT"
    ):
        return
    texts = member_texts(history, question)[-21:]
    if member_preparation_paused(history, question):
        raise DomainError(
            "MEMBER_CHOICE_REQUIRED", "本轮尚未准备确认卡。请明确需要准备时再继续，当前尚未执行业务。", 409
        )
    catalog, choices, focus = Catalog(products), {}, None
    ambiguous = False
    for message in texts:
        seen = set()
        turn_target = False
        for text in re.split(r"[，,。；;\n]", normalized(message)):
            anchors = catalog.anchors(text)
            # Split a clause only when it explicitly starts a different product.
            starts, previous = [0], set()
            for start, _, pids, kind in anchors:
                if kind in {"product", "name"}:
                    if previous and not pids & previous:
                        starts.append(start)
                    previous = pids
            segments = [text[a:b] for a, b in zip(starts, starts[1:] + [len(text)])]
            for segment in segments:
                anchors = catalog.anchors(segment)
                pids = set().union(*(a[2] for a in anchors)) if anchors else set()
                explicit = bool(anchors)
                if len(pids) == 1 and next(iter(pids)) in products:
                    pid = next(iter(pids))
                elif explicit:
                    if focus in choices:
                        choices[focus][-1] = Choice(focus)
                    focus = None
                    for pid in pids & choices.keys():
                        choices[pid] = [Choice(pid)]
                    continue
                else:
                    pid = focus
                if pid is None:
                    continue
                if (
                    not explicit
                    and not turn_target
                    and len(choices) > 1
                    and re.search(r"数量|规格|改|换|件|台|部", segment)
                ):
                    ambiguous = True
                    continue
                if not explicit and re.search(r"换个商品|其他商品|另一个商品|[a-z]+[-]?\d+[a-z]*", segment):
                    choices[pid] = [Choice(pid)]
                    focus = None
                    continue
                revision = REVISION.search(segment)
                if (
                    explicit
                    and not turn_target
                    and focus is not None
                    and focus != pid
                    and not re.search(r"另外|再加|还要|同时|都要", message)
                ):
                    choices.clear()
                if explicit:
                    turn_target = True
                if pid not in choices:
                    choices[pid] = [Choice(pid)]
                elif (
                    explicit
                    and pid in seen
                    and not revision
                    and choices[pid][-1].quantity is not None
                    and catalog.resolve(choices[pid][-1])
                ):
                    choices[pid].append(Choice(pid))
                choice = choices[pid][-1]
                if explicit:
                    focus = pid
                    seen.add(pid)
                catalog.update(choice, segment)
    expected = {}
    for item in parameters["items"]:
        expected.setdefault(item["product_id"], []).append((item["sku_id"], item["quantity"]))
    for pid in expected.keys() | choices.keys():
        wanted = expected.get(pid, [])
        actual = [(catalog.resolve(c), c.quantity) for c in choices.get(pid, [])]
        if ambiguous or sorted(actual, key=str) != sorted(wanted, key=str):
            raise DomainError(
                "MEMBER_CHOICE_REQUIRED",
                "请明确选择商品、规格和数量；未决定、含糊或冲突的选择需要先补充。可以用商品名和规格文字，也可用编号。选择后仍须点击确认卡，当前尚未执行业务。",
                409,
            )
