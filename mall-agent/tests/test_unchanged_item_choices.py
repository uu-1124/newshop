import pytest
from test_item_choices import params, products

from app.agents.item_choices import require_member_items
from app.domain.identity import DomainError


@pytest.mark.parametrize(
    "question",
    [
        "刚才那款规格和数量不变",
        "原来商品规格数量不变",
        "规格和数量保持不变",
        "颜色容量数量保持不变",
        "原商品规格和数量不变",
        "仍用地址21，最后不使用优惠券，不使用积分，原商品规格和数量不变。重新读取选项并准备订单卡，不确认不下单。",
    ],
)
def test_preserving_member_selected_fields_keeps_only_previously_explicit_values(question):
    history = [{"role": "user", "content": "商品45规格239一件"}]
    require_member_items(params(), products(), history, question)
    for before in [
        [],
        [{"role": "assistant", "content": "商品45规格239一件"}],
        [{"role": "user", "content": "商品45规格和数量没决定"}],
    ]:
        with pytest.raises(DomainError, match="MEMBER_CHOICE_REQUIRED"):
            require_member_items(params(), products(), before, question)


@pytest.mark.parametrize(
    "question", ["不要保持规格和数量不变", "规格和数量不变可以吗？", "规格和数量未决定，保持不变"]
)
def test_negated_or_questioned_unchanged_instruction_cannot_authorize_another_selection(question):
    with pytest.raises(DomainError, match="MEMBER_CHOICE_REQUIRED"):
        require_member_items(
            params(), products(), [{"role": "user", "content": "商品45规格239一件"}], question
        )
