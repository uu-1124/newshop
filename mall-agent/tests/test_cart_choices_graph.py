"""The real dialog graph must not let a model choose cart rows for a member."""

import json

import pytest
from test_preparation_tools import model_parameters

from app.agents.dialog import DialogContext, build_dialog_graph
from app.infrastructure.dialog_provider import DialogEvent


@pytest.mark.anyio
async def test_reading_a_cart_does_not_authorize_a_model_selected_row():
    parameters = {
        "action_type": "CREATE_ORDER",
        "source": "CART",
        "cart_item_ids": ["30"],
        "address_id": "10",
        "coupon_id": None,
        "use_points": 0,
    }
    steps = [
        ("get_cart", {"limit": 20, "after": None}),
        ("get_checkout_options", {"source": "CART", "cart_item_ids": ["30"], "items": None}),
        ("prepare_business_action", model_parameters(parameters)),
    ]
    calls = []

    class Provider:
        n = 0

        async def stream_turn(self, inputs, prompt, tools, record):
            assert self.n < len(steps), "Missing choices should clarify without another model loop"
            name, arguments = steps[self.n]
            self.n += 1
            yield DialogEvent(
                "completed",
                [
                    {
                        "type": "function_call",
                        "name": name,
                        "call_id": str(self.n),
                        "arguments": json.dumps(arguments),
                    }
                ],
            )

    async def noop(*args):
        return {}

    async def runtime(name, arguments):
        calls.append(name)
        if name == "get_cart":
            return {"items": [{"id": "30", "available": True}, {"id": "31", "available": True}]}
        if name == "get_checkout_options":
            return {"addresses": [{"id": "10"}], "coupons": [], "available_points": 0}
        return {"id": "e2c2ad43-80d8-4c30-9e9f-a9cc35c219c3", "state": "PENDING_CONFIRMATION"}

    result = await build_dialog_graph(Provider(), None, 3).ainvoke(
        {"question": "先读取购物车，具体买哪些条目还没决定。用地址10，不用优惠券和积分。", "history": []},
        context=DialogContext(noop, noop, noop, runtime_tool=runtime, preparation_actions=("CREATE_ORDER",)),
    )
    assert "prepare_business_action" not in calls
    assert result["needs_user_input"] and "购物车" in result["answer"]
