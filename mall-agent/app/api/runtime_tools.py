"""Internal capability broker. A worker can prepare, never confirm or execute."""

import hmac
import re
from typing import Literal
from uuid import UUID, uuid5

from fastapi import Header, Request
from pydantic import Field, ValidationError

from app.agents.cart_choices import require_member_cart
from app.agents.item_choices import require_member_items
from app.agents.member_choices import require_checkout_choices
from app.agents.order_choices import require_member_order
from app.api.actions import ActionPrepareInput, CheckoutSelection
from app.api.dependencies import ok, services
from app.api.dto import Input
from app.application.cart import CartRead, cart_for_member_choice, validated_cart_page
from app.application.expiry import deadline_sql, visible_sql
from app.application.orders import cancellation_choice_facts
from app.application.runtime_fence import RuntimeFence, assert_runtime_fence
from app.domain.identity import DomainError
from app.infrastructure.postgres.database import digest, encoded, one, rows


class RuntimeToolCall(Input):
    lease_owner: str = Field(min_length=1, max_length=100)
    fencing_token: int = Field(ge=1, strict=True)
    tool: Literal["get_cart", "get_checkout_options", "prepare_business_action", "request_human"]
    arguments: dict


def explicit_human_request(text):
    text = text.strip().lower()
    # Quoted material and examples are data, not the member's request.
    text = re.sub(r'```[\s\S]*?```|“[^”]*”|「[^」]*」|"[^"]*"|‘[^’]*’', "", text)
    for segment in re.split(r"[，。！？\n,!?；;]", text):
        segment = segment.strip().replace("能不能", "能")
        if re.search(r"不要|不用|不想|不需要|暂不|别|不转|不找|不能|do not|don't", segment):
            continue
        if re.search(
            r"解释|什么意思|是什么|假如|如果|举例|例如|引用|这句话|怎么说|what does|for example", segment
        ):
            continue
        if re.search(r"人工|真人", segment) and re.fullmatch(
            r"(?:请问|请您|请|麻烦您|麻烦|帮我|帮忙|我想要|我要|我想|我需要|需要|希望|给我|现在|直接|能否|能|可以|可否|可不可以|转接|接通|接入|切换到|找|转|联系|安排|一下|一位|一个|客服|人工|真人|专员|坐席|服务|吧|吗|好吗|谢谢|您|要|\s)+",
            segment,
        ):
            return True
        if re.fullmatch(
            r"(?:please |can you )?(?:transfer me to|connect me to|i want|i need|speak to) (?:a )?human(?: agent)?(?: please)?",
            segment,
        ):
            return True
    return False


def register_runtime_tools(app, settings):
    @app.post("/internal/runtime-auth/{rid}/tools", include_in_schema=False)
    async def call(
        rid: UUID, body: RuntimeToolCall, request: Request, x_broker_key: str = Header(default="")
    ):
        if not hmac.compare_digest(x_broker_key, settings.broker_secret.get_secret_value()):
            raise DomainError("NOT_FOUND", "接口不存在", 404)
        if len(encoded(body.arguments)) > 6000:
            raise DomainError("VALIDATION_ERROR", "准备参数过长", 422)
        svc = services(request)
        fence = RuntimeFence(rid, body.lease_owner, body.fencing_token)
        async with svc.db.transaction() as conn:
            run = await one(
                conn,
                """SELECT r.*,c.member_id,c.version AS conversation_version,c.control_epoch AS current_epoch,
                c.memory_epoch AS current_memory,c.deleted_at,m.text_content AS question
                FROM ag_run r JOIN ag_conversation c ON c.id=r.conversation_id
                JOIN ag_message m ON m.id=r.trigger_message_id WHERE r.id=:id""",
                id=rid,
            )
        if not run:
            raise DomainError("LEASE_LOST", "运行任务不存在", 409)
        token = svc.auth.get(str(rid))
        principal = await svc.bridge.identity(token)
        if principal.id != run["member_id"]:
            raise DomainError("AUTH_CONTEXT_LOST", "身份上下文不匹配", 409)
        async with svc.db.transaction() as conn:
            from app.application.conversations import owned

            conversation = await owned(conn, run["conversation_id"], principal.id, lock=True)
            await assert_runtime_fence(conn, conversation, fence)
        key = str(uuid5(rid, body.tool + ":" + digest(body.arguments)))
        try:
            cart_query = CartRead.model_validate(body.arguments) if body.tool == "get_cart" else None
            selection = (
                CheckoutSelection.model_validate(body.arguments)
                if body.tool == "get_checkout_options"
                else None
            )
            model = (
                ActionPrepareInput.model_validate(
                    {"expected_conversation_version": conversation["version"], "parameters": body.arguments}
                )
                if body.tool == "prepare_business_action"
                else None
            )
        except (ValidationError, ValueError):
            raise DomainError("VALIDATION_ERROR", "业务准备参数不完整或不符合规则，请补充选择", 422) from None
        if body.tool == "get_cart":
            if not svc.conversations.settings.actions_enabled:
                raise DomainError("FEATURE_NOT_ACTIVE", "购物车读取尚未开放", 409)
            result = validated_cart_page(await svc.bridge.cart(token, cart_query), cart_query)
        elif body.tool == "get_checkout_options":
            result = await svc.extensions.actions.require().checkout_options(
                principal, token, selection.wire(), key
            )
        elif body.tool == "prepare_business_action":
            async with svc.db.transaction() as conn:
                cached = await one(
                    conn,
                    """SELECT response_ref FROM ag_request_dedupe WHERE scope=:scope AND actor_kind='MEMBER'
                    AND actor_id=:actor AND idempotency_key=CAST(:key AS uuid)""",
                    scope=f"prepareAction:{conversation['id']}",
                    actor=principal.id,
                    key=key,
                )
            if cached:
                result = await svc.extensions.actions.require().get(
                    principal, UUID(cached["response_ref"]["id"])
                )
            else:
                parameters = model.wire()["parameters"]
                validate_member_choice = None
                async with svc.db.transaction() as conn:
                    member_messages = await rows(
                        conn,
                        f"""SELECT text_content FROM ag_message m WHERE conversation_id=:cid
                        AND {visible_sql()} AND sender_kind='MEMBER' AND status='COMPLETED'
                        AND seq<=(SELECT seq FROM ag_message WHERE id=:trigger)
                        ORDER BY seq DESC LIMIT 20""",
                        cid=conversation["id"],
                        trigger=run["trigger_message_id"],
                    )
                history = [
                    {"role": "user", "content": item["text_content"]} for item in reversed(member_messages)
                ]
                if parameters["action_type"] == "CANCEL_ORDER":
                    # A current owned detail proves the business object; the
                    # member's original text separately proves the target choice.
                    orders, complete = await cancellation_choice_facts(
                        svc.bridge, token, parameters["order_id"]
                    )
                    async with svc.db.transaction() as conn:
                        current = await owned(conn, conversation["id"], principal.id, lock=True)
                        await assert_runtime_fence(conn, current, fence)
                    entry = conversation.get("entry_context", {})
                    require_member_order(
                        parameters,
                        orders,
                        complete,
                        history,
                        "",
                        entry.get("order_id") if entry.get("kind") == "ORDER" else None,
                    )

                    async def validate_member_choice(conn, current):
                        if not await one(
                            conn,
                            f"SELECT id FROM ag_message m WHERE id=:id AND m.deleted_at IS NULL AND {deadline_sql()}>clock_timestamp()",
                            id=run["trigger_message_id"],
                        ):
                            raise DomainError("MESSAGE_EXPIRED", "消息已到期或删除，请重新发送", 410)
                        current_messages = await rows(
                            conn,
                            f"""SELECT text_content FROM ag_message m WHERE conversation_id=:cid
                            AND m.deleted_at IS NULL AND {deadline_sql()}>clock_timestamp()
                            AND sender_kind='MEMBER' AND status='COMPLETED'
                            AND seq<=(SELECT seq FROM ag_message WHERE id=:trigger)
                            ORDER BY seq DESC LIMIT 20""",
                            cid=current["id"],
                            trigger=run["trigger_message_id"],
                        )
                        entry = current.get("entry_context", {})
                        require_member_order(
                            parameters,
                            orders,
                            complete,
                            [
                                {"role": "user", "content": item["text_content"]}
                                for item in reversed(current_messages)
                            ],
                            "",
                            entry.get("order_id") if entry.get("kind") == "ORDER" else None,
                        )

                if parameters["action_type"] == "ADD_CART" or (
                    parameters["action_type"] == "CREATE_ORDER" and parameters["source"] == "DIRECT"
                ):
                    # Current Java facts, never model-supplied names/specifications.
                    # External IO stays outside SQL locks; prepare rechecks the run fence.
                    products = {}
                    for pid in dict.fromkeys(item["product_id"] for item in parameters["items"]):
                        product = await svc.bridge.product(token, pid)
                        if not isinstance(product, dict) or product.get("id") != pid:
                            raise DomainError("JAVA_PROTOCOL_ERROR", "商城商品结果不匹配，请重新读取", 503)
                        products[pid] = product
                    async with svc.db.transaction() as conn:
                        current = await owned(conn, conversation["id"], principal.id, lock=True)
                        await assert_runtime_fence(conn, current, fence)
                    require_member_items(parameters, products, history, "")
                if parameters["action_type"] == "CREATE_ORDER":
                    if parameters["source"] == "CART":
                        cart_items, complete = await cart_for_member_choice(
                            svc.bridge, token, parameters["cart_item_ids"]
                        )
                        async with svc.db.transaction() as conn:
                            current = await owned(conn, conversation["id"], principal.id, lock=True)
                            await assert_runtime_fence(conn, current, fence)
                        require_member_cart(parameters, cart_items, complete, history, "")
                    selected = CheckoutSelection.model_validate(
                        {
                            k: v
                            for k, v in parameters.items()
                            if k in {"action_type", "source", "items", "cart_item_ids"}
                        }
                    ).wire()
                    current_options = await svc.extensions.actions.require().checkout_options(
                        principal, token, selected, key
                    )
                    require_checkout_choices(
                        parameters,
                        [{"name": "get_checkout_options", "query": selected, "result": current_options}],
                        history,
                        "",
                    )
                result = await svc.extensions.actions.require().prepare(
                    principal,
                    token,
                    conversation["id"],
                    key,
                    model.wire(),
                    runtime_fence=fence,
                    validate_member_choice=validate_member_choice,
                )
        else:
            if body.arguments != {} or not explicit_human_request(run["question"]):
                raise DomainError("HUMAN_REQUEST_NOT_EXPLICIT", "请明确表示需要人工客服，或点击转人工", 409)
            result = await svc.extensions.handoffs.require().request(
                principal,
                conversation["id"],
                key,
                {"expected_conversation_version": conversation["version"], "reason": "会员明确请求人工接待"},
                runtime_fence=fence,
            )
            # The handoff command intentionally fences this run in its own transaction.
            return ok(
                request,
                {"handoff": result, "message": "已申请人工接待，请查看会话等待状态；不承诺接待时间。"},
            )
        async with svc.db.transaction() as conn:
            conversation = await owned(conn, run["conversation_id"], principal.id, lock=True)
            await assert_runtime_fence(conn, conversation, fence)
        return ok(request, result)
