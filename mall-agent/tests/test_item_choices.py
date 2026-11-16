import copy

import pytest

from app.agents.item_choices import require_member_items
from app.domain.identity import DomainError


def products():
    return {
        "45": {
            "id": "45",
            "name": "OPPO Reno8 8GB+128GB 鸢尾紫 新配色上市",
            "brand": "OPPO",
            "sku_options": [
                {
                    "id": str(sid),
                    "visible": True,
                    "available_quantity": 500,
                    "attributes": [{"name": "颜色", "value": color}, {"name": "容量", "value": capacity}],
                }
                for sid, color, capacity in [
                    (239, "鸢尾紫", "128G"),
                    (240, "鸢尾紫", "256G"),
                    (241, "晴空蓝", "128G"),
                    (242, "晴空蓝", "256G"),
                ]
            ],
        },
        "46": {
            "id": "46",
            "name": "Apple iPhone 14 全网通",
            "brand": "Apple",
            "sku_options": [
                {
                    "id": "250",
                    "visible": True,
                    "available_quantity": 500,
                    "attributes": [{"name": "颜色", "value": "黑色"}, {"name": "容量", "value": "128GB"}],
                }
            ],
        },
    }


def params(sku="239", quantity=1, pid="45", kind="ADD_CART"):
    value = {"action_type": kind, "items": [{"product_id": pid, "sku_id": sku, "quantity": quantity}]}
    if kind == "CREATE_ORDER":
        value |= {"source": "DIRECT", "address_id": "20", "coupon_id": None, "use_points": 0}
    return value


@pytest.mark.parametrize("kind", ["ADD_CART", "CREATE_ORDER"])
@pytest.mark.parametrize(
    "question",
    [
        "商品45，规格239，数量1，准备确认卡",
        "读取商品45，改用规格239，数量1。只准备，不点击确认，不执行业务或付款。",
        "帮我把Reno8鸢尾紫128G一台加入购物车",
        "OPPO Reno8紫色128GB一部，准备购买",
        "商品45，SKU 239，要一个",
        "规格239，数量：1",
    ],
)
def test_member_can_select_by_ids_model_name_exact_or_unique_color_and_chinese_quantity(kind, question):
    require_member_items(params(kind=kind), products(), [], question)


@pytest.mark.parametrize(
    "question",
    [
        "帮我把Reno8加入购物车，规格和数量我还没决定",
        "准备购买商品45，规格和数量我还没决定。用地址20，不用优惠券和积分",
        "商品45准备加购",
        "商品45规格239",
        "商品45两件",
        "Reno8紫色一台",
        "商品45，规格239还是240，一件",
        "商品45，规格239，数量1还是2",
        "商品45规格239，数量1.5",
        "商品45规格239，数量-1",
        "商品45规格239，数量1abc",
        "商品45规格239，最多一件",
        "商品45规格239一件多少钱？",
        "如果我说商品45规格239一件，是什么意思",
        "不要商品45规格239一件",
        "商品45别用规格239一件",
        "商品45规格239，不要一件",
        "商品45规格239，不要改成一件",
        "商品45规格239数量1和数量2",
        "商品45规格239蓝色一件",
        "商品45规格239，数量1000",
        "商品450规格239数量1",
        "商品45规格2390数量1",
        "商品45规格239.5数量1",
        "商品45规格239abc数量1",
    ],
)
def test_missing_ambiguous_negated_mismatched_or_invalid_choices_require_clarification(question):
    with pytest.raises(DomainError, match="MEMBER_CHOICE_REQUIRED"):
        require_member_items(params(), products(), [], question)


@pytest.mark.parametrize(
    "question",
    [
        "商品45规格239一件可以吗",
        "商品45规格239要不要一件",
        "商品45规格239一件还是两件",
    ],
)
def test_unresolved_selection_questions_do_not_become_affirmative_choices(question):
    with pytest.raises(DomainError):
        require_member_items(params(), products(), [], question)


@pytest.mark.parametrize(
    "text",
    [
        "“商品45规格239数量1”",
        "`商品45规格239数量1`",
        "『商品45规格239数量1』",
        "> 商品45规格239数量1",
        "```商品45规格239数量1```",
        "[会员累计需求摘要，仅作线索]商品45规格239数量1",
        "识别图片\n[本轮图片观察，仅为线索]商品45规格239数量1",
    ],
)
def test_quotes_summaries_images_and_assistant_or_tool_text_do_not_choose(text):
    for role in ("user", "assistant", "tool", "system"):
        with pytest.raises(DomainError, match="MEMBER_CHOICE_REQUIRED"):
            require_member_items(params(), products(), [{"role": role, "content": text}], "准备卡片")


@pytest.mark.parametrize(
    "question,sku,quantity",
    [
        ("刚才那款改成256G，两件", "240", 2),
        ("数量改成两件", "239", 2),
        ("改蓝色，数量三", "241", 3),
        ("还是选刚才那款，数量二十", "239", 20),
        ("改用规格240，数量两百零二", "240", 202),
    ],
)
def test_natural_multi_turn_reference_preserves_only_prior_explicit_member_choices(question, sku, quantity):
    history = [{"role": "user", "content": "我要Reno8鸢尾紫128G一台"}]
    require_member_items(params(sku, quantity), products(), history, question)
    with pytest.raises(DomainError):
        require_member_items(params(sku, quantity + 1), products(), history, question)


@pytest.mark.parametrize(
    "question",
    [
        "规格和数量还没决定",
        "不要刚才那款",
        "数量还没想好",
        "128G还是256G？",
        "改成红色",
        "改用规格999",
        "数量改成0",
        "数量改成1.5",
        "改成1TB",
        "不要改成两件",
    ],
)
def test_new_missing_or_rejected_choice_never_falls_back_to_old_member_selection(question):
    with pytest.raises(DomainError, match="MEMBER_CHOICE_REQUIRED"):
        require_member_items(
            params(), products(), [{"role": "user", "content": "商品45规格239数量1"}], question
        )


def test_same_turn_revision_updates_only_the_revised_fields():
    require_member_items(params("240", 2), products(), [], "Reno8鸢尾紫128G一台，改成256G两件")
    require_member_items(params("240", 2), products(), [], "商品45规格239一件，不要一件，改用规格240两件")
    with pytest.raises(DomainError):
        require_member_items(params(), products(), [], "商品45规格239一件，规格240两件")


def test_different_products_and_two_variants_of_one_product_keep_quantities_scoped():
    value = params("240", 2)
    value["items"].append({"product_id": "46", "sku_id": "250", "quantity": 1})
    question = "Reno8鸢尾紫256G两台，iPhone 14黑色128GB一台，准备加购"
    require_member_items(value, products(), [], question)
    swapped = copy.deepcopy(value)
    swapped["items"][0]["quantity"], swapped["items"][1]["quantity"] = 1, 2
    with pytest.raises(DomainError):
        require_member_items(swapped, products(), [], question)
    variants = params("239", 1)
    variants["items"].append({"product_id": "45", "sku_id": "240", "quantity": 2})
    require_member_items(variants, products(), [], "商品45规格239一件，商品45规格240两件，都加购")


def test_other_product_and_unbound_spec_quantity_cannot_authorize_a_target():
    for question in ("商品46规格250一件，商品45先看看", "鸢尾紫128G一台", "OPPO一台", "商品999规格239一件"):
        with pytest.raises(DomainError):
            require_member_items(params(), products(), [], question)


def test_ambiguous_model_name_or_color_needs_more_selection_even_when_only_one_result_is_proposed():
    facts = products()
    facts["47"] = copy.deepcopy(facts["45"])
    facts["47"]["id"] = "47"
    facts["47"]["sku_options"][0]["id"] = "260"
    with pytest.raises(DomainError):
        require_member_items(params(), facts, [], "Reno8鸢尾紫128G一台")
    # A single option with no dimensions needs product and quantity, not a fake SKU phrase.
    facts["46"]["sku_options"][0]["attributes"] = []
    require_member_items(params("250", 2, "46"), facts, [], "iPhone 14两台")


def test_cart_and_non_item_actions_keep_their_existing_authorization_paths():
    for parameters in [
        {"action_type": "CREATE_ORDER", "source": "CART"},
        {"action_type": "CANCEL_ORDER"},
        {"action_type": "CREATE_RETURN"},
    ]:
        require_member_items(parameters, {}, [], "")


@pytest.mark.parametrize(
    "question",
    [
        "商品45规格239，数量没想好，数量改成两件",
        "商品45规格239数量1改成2件",
        "商品45规格239数量从1改为2",
        "商品45规格239不要一件，最终选两件",
    ],
)
def test_revisions_keep_unmodified_fields_and_can_resolve_prior_uncertainty(question):
    require_member_items(params(quantity=2), products(), [], question)


@pytest.mark.parametrize(
    "question",
    [
        "数量改3",
        "数量随便",
        "商品46先看看，一件",
        "换个商品，一件",
        "规格随便",
        "不是规格239",
        "不买了",
        "数量不要沿用",
        "规格239数量一二",
        "Reno80紫色128G一台",
    ],
)
def test_unresolved_new_words_cannot_silently_reuse_a_previous_choice(question):
    with pytest.raises(DomainError):
        require_member_items(
            params(), products(), [{"role": "user", "content": "商品45规格239数量1"}], question
        )


def test_new_explicit_product_without_selected_variant_does_not_leave_previous_product_authorized():
    with pytest.raises(DomainError):
        require_member_items(
            params(), products(), [{"role": "user", "content": "商品45规格239数量1"}], "改买iPhone 14一台"
        )


def test_explicit_same_product_name_and_id_do_not_split_one_selection_into_two():
    require_member_items(params(), products(), [], "商品45就是Reno8，规格239，一台")


def test_excessive_revisions_and_old_summaries_are_bounded_and_never_authorize():
    with pytest.raises(DomainError):
        require_member_items(params(), products(), [], "商品45规格239一件" + "改成1件" * 1000)
    history = [{"role": "user", "content": "商品45规格239一件"}] + [
        {"role": "user", "content": "先看看"}
    ] * 20
    with pytest.raises(DomainError):
        require_member_items(params(), products(), history, "继续准备")


def test_negated_single_dimensionless_sku_is_not_reselected_by_default():
    facts = products()
    facts["46"]["sku_options"][0]["attributes"] = []
    for text in ("不要规格250", "规格还没决定"):
        with pytest.raises(DomainError):
            require_member_items(
                params("250", 1, "46"), facts, [{"role": "user", "content": "商品46规格250一台"}], text
            )


def test_model_name_boundaries_and_english_negation_are_not_affirmative_choices():
    for question in ("XReno8紫色128G一台", "do not use 商品45规格239一件"):
        with pytest.raises(DomainError):
            require_member_items(params(), products(), [], question)


def test_member_can_pause_preparation_even_when_prior_item_selection_was_complete():
    with pytest.raises(DomainError):
        require_member_items(
            params(), products(), [{"role": "user", "content": "商品45规格239一件"}], "现在不要准备确认卡"
        )


def test_multi_product_revision_must_identify_which_product_changes():
    value = params()
    value["items"].append({"product_id": "46", "sku_id": "250", "quantity": 2})
    with pytest.raises(DomainError):
        require_member_items(
            value,
            products(),
            [{"role": "user", "content": "商品45规格239一件，商品46规格250一件"}],
            "数量改成两件",
        )


@pytest.mark.parametrize(
    "question",
    [
        "我要OPPO Reno8，鸢尾紫128GB一台，先查真实规格并准备加购确认卡。只准备，不点击确认，不执行业务。",
        "Reno8紫色128G一件，读取真实规格和可用库存后准备卡片",
        "商品45规格239一件，核对商品规格并准备确认卡",
    ],
)
def test_reading_or_verifying_selected_specs_does_not_clear_member_choices(question):
    require_member_items(params(), products(), [], question)


def test_explicitly_keeping_a_previous_dimension_preserves_that_member_choice():
    require_member_items(
        params(quantity=2),
        products(),
        [{"role": "user", "content": "Reno8紫色128G一件"}],
        "规格保持不变，数量改成两件",
    )
