import json

import pytest

from app.agents.cart_choices import member_cart_rows, observed_cart, require_member_cart
from app.domain.identity import DomainError


def items():
    def row(identifier, sku, color, capacity):
        return {
            "id": identifier,
            "product_id": "45",
            "sku_id": sku,
            "name": "OPPO Reno8 手机",
            "quantity": 1,
            "available": True,
            "specification": json.dumps(
                [{"key": "颜色", "value": color}, {"key": "容量", "value": capacity}]
            ),
        }

    return {r["id"]: r for r in [row("30", "239", "鸢尾紫", "128G"), row("31", "242", "晴空蓝", "256G")]}


@pytest.mark.parametrize(
    "question,expected",
    [
        ("购物车条目30，用地址10，不用券和积分，准备订单", {"30"}),
        ("只结算购物车条目30和31", {"30", "31"}),
        ("结算条目30、31", {"30", "31"}),
        ("结算购物车条目30，31", {"30", "31"}),
        ("选择购物车条目30和Reno8晴空蓝256G那行", {"30", "31"}),
        ("购物车条目30改成31", {"31"}),
        ("不要条目30，改用条目31", {"31"}),
        ("购物车里的Reno8鸢尾紫128GB那行，准备结算", {"30"}),
        ("选择购物车中的SKU242", {"31"}),
        ("选择购物车商品45晴空蓝256G", {"31"}),
        ("全选购物车，准备订单", {"30", "31"}),
        ("购物车全部商品都要，准备结算", {"30", "31"}),
        ("全选购物车，不要条目30", {"31"}),
        ("全选购物车，不要条目30，不要条目31，改用条目30", {"30"}),
        ("选择购物车Reno8鸢尾紫128G，Reno8晴空蓝256G", {"30", "31"}),
    ],
)
def test_explicit_ids_natural_specifications_and_bounded_same_turn_revisions(question, expected):
    assert member_cart_rows(items(), True, [], question) == expected


@pytest.mark.parametrize(
    "question",
    [
        "准备购物车订单",
        "先读取购物车",
        "购物车买哪些还没决定",
        "条目30还是31？",
        "不要条目30",
        "不要改用条目31",
        "选择购物车条目30.5",
        "选择条目30abc",
        "选择条目999",
        "购物车里的Reno8，规格没决定",
        "选择购物车Reno8",
        "选择购物车SKU999",
        "选择购物车商品999",
        "选择购物车Reno8红色512G",
        "选择购物车Reno8鸢尾紫128G两件",
        "购物车条目30不要准备",
        "解释这句话：选择购物车条目30",
        "如果全选购物车会怎样",
        "“选择购物车条目30”",
        "`选择购物车条目30`",
        "商品45规格239一件，直接购买",
        "不要全选购物车",
        "选择购物车条目30两件",
        "选择购物车条目30，数量改成2",
        "选择购物车条目30或31",
        "选择购物车条目30/31",
        "选择购物车条目30-31",
        "选择购物车条目30or31",
        "选择购物车条目30和商品999",
        "选择购物车条目30和SKU999",
        "选择购物车条目30Reno8晴空蓝256G那行",
    ],
)
def test_unselected_ambiguous_negated_unknown_and_quoted_choices_do_not_authorize(question):
    with pytest.raises(DomainError, match="MEMBER_CHOICE_REQUIRED"):
        require_member_cart(
            {"action_type": "CREATE_ORDER", "source": "CART", "cart_item_ids": ["30"]},
            items(),
            True,
            [],
            question,
        )


@pytest.mark.parametrize(
    "question,expected",
    [
        ("仍用地址11，不使用券和积分，重新准备", {"30"}),
        ("改用条目31", {"31"}),
        ("另外加上购物车条目31", {"30", "31"}),
        ("另外加上条目31，不要条目30", {"31"}),
        ("不要条目30，另外加条目31", {"31"}),
        ("刚才那行改为晴空蓝256G", {"31"}),
        ("改成晴空蓝256G那行", {"31"}),
        ("购物车条目还没决定", None),
        ("先读取购物车", {"30"}),
        ("选择条目999", None),
        ("直接购买商品45规格239一件", None),
        ("数量2", None),
        ("数量1", {"30"}),
        ("数量保持不变，改用地址11", {"30"}),
        ("原商品规格和数量不变，换优惠券20", {"30"}),
    ],
)
def test_latest_member_turn_revises_only_cart_and_cannot_reuse_unknown_selection(question, expected):
    assert (
        member_cart_rows(items(), True, [{"role": "user", "content": "选择购物车条目30"}], question)
        == expected
    )


def test_model_prose_summaries_and_removed_or_unavailable_rows_cannot_authorize():
    params = {"action_type": "CREATE_ORDER", "source": "CART", "cart_item_ids": ["30"]}
    for history in [
        [{"role": "assistant", "content": "选择购物车条目30"}],
        [{"role": "tool", "content": "选择购物车条目30"}],
        [{"role": "user", "content": "[会员累计需求摘要]选择购物车条目30"}],
    ]:
        with pytest.raises(DomainError):
            require_member_cart(params, items(), True, history, "准备订单")
    for data in [{"31": items()["31"]}, items() | {"30": items()["30"] | {"available": False}}]:
        with pytest.raises(DomainError):
            require_member_cart(params, data, True, [], "选择购物车条目30")
    with pytest.raises(DomainError):
        require_member_cart(params | {"cart_item_ids": ["30", "31"]}, items(), True, [], "只选择购物车条目30")


def test_incomplete_cart_allows_exact_rows_but_not_global_or_natural_claims():
    assert member_cart_rows(items(), False, [], "选择购物车条目30") == {"30"}
    assert member_cart_rows(items(), False, [], "全选购物车") is None
    assert member_cart_rows(items(), False, [], "选择购物车Reno8鸢尾紫128G") is None
    data = items() | {"31": items()["31"] | {"available": False}}
    assert member_cart_rows(data, True, [], "全选购物车") is None
    assert member_cart_rows(data, True, [], "全选可用购物车") == {"30"}


def test_observed_pages_require_a_contiguous_current_chain_to_prove_all():
    pages = [
        {
            "name": "get_cart",
            "query": {"after": None},
            "result": {"items": [items()["30"]], "has_more": True, "next_cursor": "30"},
        },
        {
            "name": "get_cart",
            "query": {"after": "30"},
            "result": {"items": [items()["31"]], "has_more": False, "next_cursor": None},
        },
    ]
    assert observed_cart(pages) == (items(), True)
    assert observed_cart(pages[1:])[1] is False
    assert (
        observed_cart(pages + [pages[0] | {"result": {"items": [], "has_more": True, "next_cursor": "50"}}])[
            1
        ]
        is False
    )
