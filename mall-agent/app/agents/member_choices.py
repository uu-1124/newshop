"""Match explicit member choices to current options; model/tool prose cannot choose."""

import re

from app.domain.identity import DomainError

UNCERTAIN = re.compile(
    r"[？?]|还是|或者|或是|是否|是不是|能否|哪[个张]|多少|例如|假如|如果|解释|这句话|可以吗|能不能|要不要|该不该|不确定|没决定"
)
NEGATIVE = re.compile(r"不|别|勿|取消|撤销|放弃|禁止|未|没有|\b(?:not|never|don't|without)\b", re.IGNORECASE)
REVISION = re.compile(
    r"改用|改为|改成|换用|换成|换为|调整为|调整到|更改为|"
    r"(?:最后|最终)(?:不使用|不用|不要|使用|选择|用|选)"
)


def relevant_choice(messages, noun, labels=()):
    """Latest member turn for a field, with only an explicit revision superseding it."""
    for message in reversed(messages):
        parts = [
            part
            for part in re.split(r"[，,、。；;\n]", message)
            if re.search(noun, part) or any(len(label) >= 2 and label in part for label in labels)
        ]
        if not parts:
            continue
        text = "，".join(parts)
        if UNCERTAIN.search(text) or re.search(
            r"(?:^|[，,。；;\n])\s*(?:这样)?(?:可以|行|对|好|合适)吗[？?]?", message
        ):
            return None
        revisions = list(REVISION.finditer(text))
        if revisions:
            last = revisions[-1]
            if re.search(r"(?:不(?:要|想|能|会|打算)?|别|勿|禁止)(?:再)?\s*$", text[: last.start()]):
                return None
            # Keep final/last negation ("最后不用券") rather than discarding it.
            if last.group().startswith(("最后", "最终")):
                return text[last.start() + 2 :]
            return text[last.end() :]
        return text
    return None


def member_texts(history, question):
    values = [item.get("content", "") for item in history if item.get("role") == "user"] + [question]
    cleaned = []
    for value in values:
        if not isinstance(value, str) or value.startswith(("[更早会员需求的有界摘录", "[会员累计需求摘要")):
            continue
        # Image observations and system-added context are not member selections.
        value = re.split(r"\n\[(?:本轮图片观察|此前图片观察|服务端已核验入口)", value)[0]
        value = re.sub(
            r"```[\s\S]*?```|`[^`]*`|“[^”]*”|「[^」]*」|『[^』]*』|\"[^\"]*\"|'[^']*'|‘[^’]*’", "", value
        )
        value = re.sub(r"(?m)^\s*>.*$", "", value)
        cleaned.append(value)
    return cleaned


def explicit_option(messages, field, options, *, allow_none=False, default=False):
    noun = {"address": r"地址", "coupon": r"优惠券|券"}[field]
    text = relevant_choice(messages, noun, [o.get("label", o.get("name", "")) for o in options])
    if text is None:
        return False, None
    no_coupon = r"(?:不使用|不用|不要|不选|无)(?:优惠券|券)(?!\s*(?:编号|ID|id|号|\d))"
    matches = set()
    if allow_none and re.search(no_coupon, text):
        matches.add(None)
    # Negating a particular option is not an affirmative choice of that option.
    # Remove only the recognized no-coupon phrase before checking other negations.
    if NEGATIVE.search(re.sub(no_coupon, "", text) if allow_none else text):
        return False, None
    if default and re.search(r"(?:用|选|使用)(?:我的)?默认地址", text):
        matches.update(o["id"] for o in options if o.get("is_default"))
    for option in options:
        identifier = re.escape(option["id"])
        marked_id = re.search(
            rf"(?:{noun})\s*(?:编号|ID|id|号|选|用|为|是)?\s*[:：#]?\s*{identifier}(?![0-9A-Za-z_.])", text
        )
        label = option.get("label", option.get("name", ""))
        if (
            marked_id
            or re.fullmatch(r"\s*" + identifier + r"\s*", text)
            or (len(label) >= 2 and label in text)
        ):
            matches.add(option["id"])
    return (True, next(iter(matches))) if len(matches) == 1 else (False, None)


def explicit_points(messages):
    text = relevant_choice(messages, r"积分")
    if text is None:
        return None
    none_pattern = r"(?:不使用|不用|不要|不选)(?:(?:优惠券|券)(?:和|及))?积分(?!\s*[\d:：])"
    no_points = bool(re.search(none_pattern, text))
    if NEGATIVE.search(re.sub(none_pattern, "", text)):
        return None
    values = set(re.findall(r"(?:使用|用|选择|积分\s*[:：])\s*(\d{1,9})(?:\s*(?:个|点))?\s*积分", text))
    values.update(re.findall(r"积分\s*[:：]\s*(\d{1,9})(?![0-9A-Za-z_.])", text))
    # A revision such as "积分从100改成200" already carries the field context.
    final_number = re.fullmatch(r"\s*(\d{1,9})(?:\s*(?:个|点)?\s*积分)?\s*", text)
    if final_number:
        values.add(final_number[1])
    if no_points:
        values.add("0")
    return int(next(iter(values))) if len(values) == 1 else None


def require_checkout_choices(parameters, results, history, question):
    if parameters["action_type"] != "CREATE_ORDER":
        return
    from app.agents.preparation_evidence import selection

    options = next(
        tool["result"]
        for tool in reversed(results)
        if tool["name"] == "get_checkout_options" and selection(tool["query"]) == selection(parameters)
    )
    messages = member_texts(history, question)
    address_ok, address = explicit_option(messages, "address", options.get("addresses", []), default=True)
    coupon_ok, coupon = explicit_option(messages, "coupon", options.get("coupons", []), allow_none=True)
    # "不用优惠券和积分" makes both choices explicit.
    missing = []
    if not address_ok or address != parameters["address_id"]:
        missing.append("已有收货地址")
    if not coupon_ok or coupon != parameters["coupon_id"]:
        missing.append("优惠券（可明确选择不使用）")
    if explicit_points(messages) != parameters["use_points"]:
        missing.append("积分数量（可明确选择不使用）")
    if missing:
        raise DomainError(
            "MEMBER_CHOICE_REQUIRED",
            "请明确选择" + "、".join(missing) + "。模型不能替你决定；选择后仍须点击确认卡。",
            409,
        )
