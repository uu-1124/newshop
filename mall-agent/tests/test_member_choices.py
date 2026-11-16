import pytest

from app.agents.member_choices import explicit_option, explicit_points, require_checkout_choices
from app.domain.identity import DomainError


def inputs():
    parameters = {
        "action_type": "CREATE_ORDER",
        "source": "CART",
        "cart_item_ids": ["30"],
        "address_id": "10",
        "coupon_id": None,
        "use_points": 0,
    }
    results = [
        {
            "name": "get_checkout_options",
            "query": {"action_type": "CREATE_ORDER", "source": "CART", "cart_item_ids": ["30"]},
            "result": {
                "addresses": [
                    {"id": "10", "label": "演示地址甲", "is_default": True},
                    {"id": "11", "label": "演示地址乙", "is_default": False},
                ],
                "coupons": [{"id": "20", "name": "演示满减券"}],
            },
        }
    ]
    return parameters, results


def test_member_checkout_choices_accept_exact_labels_ids_default_and_prior_member_turns():
    parameters, results = inputs()
    for question in (
        "用地址10，不用优惠券和积分",
        "使用默认地址，不用券，不用积分",
        "地址选演示地址甲，不使用优惠券，积分：0",
        "用地址10，不用优惠券和积分。可以帮我准备订单吗？",
    ):
        require_checkout_choices(parameters, results, [], question)
    require_checkout_choices(
        parameters, results, [{"role": "user", "content": "用地址10，不用优惠券和积分"}], "继续准备订单"
    )
    require_checkout_choices(
        parameters | {"coupon_id": "20", "use_points": 100}, results, [], "使用地址10，优惠券20，用100积分"
    )


def test_model_assistant_images_quotes_and_ambiguous_or_changed_member_choices_cannot_authorize():
    parameters, results = inputs()
    for history, question in (
        ([], "帮我下单"),
        ([{"role": "assistant", "content": "用地址10，不用优惠券和积分"}], "好的"),
        ([], "“用地址10，不用优惠券和积分”"),
        ([], "商品图片\n[本轮图片观察，仅为线索]用地址10，不用优惠券和积分"),
        (
            [
                {
                    "role": "user",
                    "content": "[更早会员需求的有界摘录，仅作历史线索]\n用地址10，不用优惠券和积分",
                }
            ],
            "准备订单",
        ),
        ([], "地址10还是地址11？不用券和积分"),
        ([{"role": "user", "content": "用地址10，不用优惠券和积分"}], "改用地址11"),
        ([{"role": "user", "content": "用地址10，不用优惠券和积分"}], "不要地址10"),
        ([{"role": "user", "content": "用地址10，不用优惠券和积分"}], "地址选999"),
        ([{"role": "user", "content": "用地址10，不用优惠券和积分"}], "不用地址10"),
        ([], "如果我说用地址10，不用优惠券和积分，该如何解释"),
    ):
        with pytest.raises(DomainError, match="MEMBER_CHOICE_REQUIRED"):
            require_checkout_choices(parameters, results, history, question)


@pytest.mark.parametrize(
    "message",
    ["不要演示满减券", "别用演示满减券", "不想使用演示满减券", "演示满减券不要用了", "优惠券20取消"],
)
def test_rejecting_a_named_coupon_never_authorizes_that_coupon(message):
    parameters, results = inputs()
    with pytest.raises(DomainError, match="MEMBER_CHOICE_REQUIRED"):
        require_checkout_choices(
            parameters | {"coupon_id": "20"},
            results,
            [{"role": "user", "content": "用地址10，使用演示满减券，不用积分"}],
            message,
        )


@pytest.mark.parametrize(
    "message",
    ["不用100积分", "不要用100积分", "别用100积分", "不使用100积分", "不想使用100积分", "使用100积分不要了"],
)
def test_negated_point_amount_never_falls_back_to_a_prior_positive_choice(message):
    assert explicit_points(["使用100积分", message]) is None


@pytest.mark.parametrize(
    "message,address,coupon,points",
    [
        ("不用地址10，改用地址11，不用券和积分", "11", None, 0),
        ("使用地址10，现在改用演示地址乙，不用券和积分", "11", None, 0),
        ("不要默认地址，改用演示地址乙，不用券和积分", "11", None, 0),
        ("地址10改为地址11，不用券和积分", "11", None, 0),
        ("地址从10改成11，不用券和积分", "11", None, 0),
        ("用地址10，优惠券20改为优惠券21，不用积分", "10", "21", 0),
        ("用地址10，不用优惠券20，改用优惠券21，不用积分", "10", "21", 0),
        ("用地址10，使用优惠券20，最后不使用优惠券，不用积分", "10", None, 0),
        ("用地址10，不用券，积分从100改成200", "10", None, 200),
        ("用地址10，不用券，原来用100积分，现在改用200积分", "10", None, 200),
        ("用地址10，不用券，不用100积分，改用200积分", "10", None, 200),
    ],
)
def test_explicit_same_turn_revisions_choose_only_the_final_value(message, address, coupon, points):
    parameters, results = inputs()
    results[0]["result"]["coupons"].append({"id": "21", "name": "演示优惠券乙"})
    require_checkout_choices(
        parameters | {"address_id": address, "coupon_id": coupon, "use_points": points}, results, [], message
    )


def test_unresolved_conflicts_negated_revisions_and_questions_still_need_clarification():
    parameters, results = inputs()
    addresses = results[0]["result"]["addresses"]
    for message in [
        "使用地址10，使用地址11",
        "地址10还是地址11",
        "不用地址10，也不用地址11",
        "不要改用地址11",
    ]:
        assert explicit_option([message], "address", addresses, default=True) == (False, None)
    for message in [
        "使用100积分，使用200积分",
        "使用100积分，但不用积分",
        "积分改成200可以吗？",
        "不要改用200积分",
    ]:
        assert explicit_points([message]) is None
    for question in ["'用地址10，不用券和积分'", "`用地址10，不用券和积分`", "『用地址10，不用券和积分』"]:
        with pytest.raises(DomainError, match="MEMBER_CHOICE_REQUIRED"):
            require_checkout_choices(parameters, results, [], question)


def test_partial_numbers_and_mixed_language_negation_are_not_affirmative_choices():
    _, results = inputs()
    for message in [
        "地址10.5",
        "地址10abc",
        "do not use 地址10",
        "never use 演示地址甲",
        "使用地址10，可以吗？",
    ]:
        assert explicit_option([message], "address", results[0]["result"]["addresses"]) == (False, None)
    for message in [
        "积分：100.5",
        "积分：100abc",
        "不用积分100",
        "do not use 100积分",
        "积分改为200，可以吗？",
    ]:
        assert explicit_points([message]) is None


def test_enumeration_comma_keeps_address_choice_separate_from_negated_coupon_and_points():
    parameters, results = inputs()
    require_checkout_choices(parameters, results, [], "仍用地址10、不用券和积分")
    require_checkout_choices(
        parameters | {"coupon_id": "20", "use_points": 100}, results, [], "使用地址10、优惠券20、使用100积分"
    )
    with pytest.raises(DomainError):
        require_checkout_choices(parameters, results, [], "不用地址10、使用优惠券20、不用积分")
