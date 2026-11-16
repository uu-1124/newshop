import asyncio
import logging
from uuid import uuid4

import httpx
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.agents.graph import build_graph
from app.agents.live_workflow import build_live_workflow
from app.application.attachments import Attachments
from app.application.extensions import Extensions
from app.application.runs import Runs
from app.domain.identity import DomainError, Principal
from app.infrastructure.dialog_provider import ResponsesDialogProvider
from app.infrastructure.gpt_vision import GptVision
from app.infrastructure.knowledge_runtime import KnowledgeRuntime
from app.infrastructure.model_call_context import ModelCallContext, current_model_call
from app.infrastructure.model_ledger import ModelLedger
from app.infrastructure.model_quota import model_actor
from app.infrastructure.postgres.database import Database
from app.infrastructure.private_objects import PrivateObjects
from app.infrastructure.providers import FixtureDialogProvider
from app.infrastructure.worker_status import worker_status
from app.runtime import run
from app.settings import get_settings

log = logging.getLogger(__name__)


async def main():
    settings = get_settings()
    db = Database(settings.database_url.get_secret_value())
    runs, ledger = Runs(db, settings), ModelLedger(db, settings)
    live = settings.provider == "openai_responses"
    provider = None if live else FixtureDialogProvider()
    objects = PrivateObjects(settings) if settings.attachments_enabled else None
    attachments = Attachments(db, objects, settings)
    if live:
        await ledger.setup()
    owner = f"generation-{uuid4()}"
    async with AsyncPostgresSaver.from_conn_string(settings.checkpoint_url()) as saver:
        if not live:
            graph = build_graph(provider, saver)
        async with httpx.AsyncClient(base_url=settings.broker_url, timeout=15, trust_env=False) as client:

            async def process(claimed):
                actor_context = model_actor.set(("MEMBER", claimed["member_id"]))
                call_context = current_model_call.set(
                    ModelCallContext(
                        run_id=claimed["id"],
                        lease_owner=claimed["lease_owner"],
                        fencing_token=claimed["fencing_token"],
                    )
                )
                error = None
                resources = []
                stream_refs = []

                async def search(query):
                    if not await runs.heartbeat(claimed):
                        raise DomainError("LEASE_LOST", "运行任务已失效")
                    response = await client.post(
                        f"/internal/runtime-auth/{claimed['id']}/products",
                        headers={"X-Broker-Key": settings.broker_secret.get_secret_value()},
                        json={
                            "lease_owner": owner,
                            "fencing_token": claimed["fencing_token"],
                            **({"filters": query} if isinstance(query, dict) else {"query": query}),
                        },
                    )
                    if response.status_code != 200:
                        raise DomainError(response.json().get("code", "JAVA_UNAVAILABLE"), "商品查询暂不可用")
                    return response.json()["data"]

                async def detail(product_id):
                    if not await runs.heartbeat(claimed):
                        raise DomainError("LEASE_LOST", "运行任务已失效")
                    response = await client.post(
                        f"/internal/runtime-auth/{claimed['id']}/products",
                        headers={"X-Broker-Key": settings.broker_secret.get_secret_value()},
                        json={
                            "lease_owner": owner,
                            "fencing_token": claimed["fencing_token"],
                            "kind": "DETAIL",
                            "product_id": product_id,
                        },
                    )
                    if response.status_code != 200:
                        raise DomainError(response.json().get("code", "JAVA_UNAVAILABLE"), "商品详情暂不可用")
                    return response.json()["data"]

                async def emit(chunk):
                    if not await runs.append(claimed, chunk, stream_refs):
                        raise DomainError("LEASE_LOST", "运行任务已失效")

                async def bind_sources(refs):
                    await runs.bind_stream_sources(claimed, refs)
                    stream_refs[:] = refs

                async def order_query(query):
                    if not await runs.heartbeat(claimed):
                        raise DomainError("LEASE_LOST", "运行任务已失效")
                    response = await client.post(
                        f"/internal/runtime-auth/{claimed['id']}/products",
                        headers={"X-Broker-Key": settings.broker_secret.get_secret_value()},
                        json={
                            "lease_owner": owner,
                            "fencing_token": claimed["fencing_token"],
                            "kind": "ORDERS",
                            "order_query": query,
                        },
                    )
                    if response.status_code != 200:
                        raise DomainError(
                            response.json().get("code", "JAVA_UNAVAILABLE"),
                            "订单查询未通过身份或状态复核",
                            response.status_code,
                        )
                    return response.json()["data"]

                async def runtime_tool(name, arguments):
                    if not await runs.heartbeat(claimed):
                        raise DomainError("LEASE_LOST", "运行任务已失效", 409)
                    response = await client.post(
                        f"/internal/runtime-auth/{claimed['id']}/tools",
                        headers={"X-Broker-Key": settings.broker_secret.get_secret_value()},
                        json={
                            "lease_owner": owner,
                            "fencing_token": claimed["fencing_token"],
                            "tool": name,
                            "arguments": arguments,
                        },
                    )
                    if response.status_code != 200:
                        data = response.json()
                        raise DomainError(
                            data.get("code", "JAVA_UNAVAILABLE"),
                            data.get("message", "业务准备暂不可用"),
                            response.status_code,
                        )
                    return response.json()["data"]

                async def generate():
                    snapshot = claimed["config_snapshot"]
                    if snapshot.get("provider", "fixture") != settings.provider:
                        raise DomainError("MODEL_CONFIG_CHANGED", "模型配置已切换，请重新发送")
                    if live:
                        frozen = await ledger.for_snapshot(snapshot)
                        run_provider = ResponsesDialogProvider(frozen.settings)
                        resources.append(run_provider)
                        run_graph, run_context = build_live_workflow(run_provider, saver, frozen.settings)
                        run_vision = GptVision(frozen.settings, frozen) if settings.vision_enabled else None
                        if run_vision:
                            resources.append(run_vision)
                        run_extensions = Extensions()
                        if settings.knowledge_enabled:
                            run_knowledge = KnowledgeRuntime(
                                db, frozen.settings, None, None, objects, frozen, frozen=True
                            )
                            resources.append(run_knowledge)
                            run_extensions.retrieval.implementation = run_knowledge.retrieval

                        async def guard(state):
                            if not await runs.heartbeat(claimed):
                                raise DomainError("LEASE_LOST", "运行已停止或租约失效", 409)

                        async def assemble(state):
                            observations = []
                            if claimed.get("has_images"):
                                await observe_images(observations)
                            context = {}
                            order_context = {}
                            entry = claimed.get("entry_context", {})
                            if entry.get("kind") == "PRODUCT":
                                product = await detail(entry["product_id"])
                                context = {"product_id": product["id"], "category_id": product["category_id"]}
                            elif entry.get("kind") == "ORDER":
                                order_context = await order_query(
                                    {"kind": "ORDER", "order_id": entry["order_id"]}
                                )
                            return {
                                "scope_context": context,
                                "history": await runs.history(claimed),
                                "image_observations": observations,
                                "order_context": order_context,
                            }

                        async def observe_images(observations):
                            if not run_vision or not frozen.profiles.get("VISION", {}).get("enabled"):
                                raise DomainError("MODEL_CONFIG_CHANGED", "图片模型配置已变化，请重新发送")
                            images = await attachments.for_run(claimed)
                            if not images or len(images) > 2 or len(images) != claimed["image_count"]:
                                raise DomainError("ATTACHMENT_UNAVAILABLE", "本轮图片已失效，请重新上传")
                            for image in images:
                                raw = await attachments.read(
                                    Principal(claimed["member_id"], "runtime-member"), image["id"]
                                )
                                summary = await run_vision.understand_image(
                                    raw, claimed["question"], claimed["id"]
                                )
                                await runs.save_image_observation(
                                    claimed, image, summary, frozen.profiles["VISION"]
                                )
                                observations.append(summary)

                        await run_graph.ainvoke(
                            {
                                "schema_version": "agent-state-v1",
                                "run_id": str(claimed["id"]),
                                "conversation_id": str(claimed["conversation_id"]),
                                "control_epoch": claimed["control_epoch"],
                                "memory_epoch": claimed["memory_epoch"],
                                "steps_used": 0,
                                "question": claimed["question"],
                                "history": [],
                                "answer": "",
                                "tool_results": [],
                                "image_observations": [],
                            },
                            {"configurable": {"thread_id": claimed["checkpoint_thread_id"]}},
                            context=run_context(
                                guard=guard,
                                assemble=assemble,
                                search=search,
                                emit=emit,
                                record=frozen.recorder("DIALOG", claimed["id"]),
                                route_record=frozen.recorder(
                                    "DIALOG", claimed["id"], frozen.routing_prompt_id
                                ),
                                fact_record=frozen.recorder("DIALOG", claimed["id"], frozen.fact_prompt_id),
                                extensions=run_extensions,
                                detail=detail,
                                knowledge_record=frozen.recorder(
                                    "DIALOG", claimed["id"], frozen.knowledge_prompt_id
                                ),
                                persist_knowledge=lambda answer, refs: runs.append_knowledge(
                                    claimed, answer, refs
                                ),
                                bind_sources=bind_sources,
                                persist_products=lambda answer, products: runs.append_products(
                                    claimed, answer, products
                                ),
                                prompts=frozen.prompts,
                                order_query=order_query,
                                persist_orders=lambda answer, orders: runs.append_orders(
                                    claimed, answer, orders
                                ),
                                runtime_tool=runtime_tool,
                                live_stream=True,
                            ),
                        )
                    else:
                        products = await search(None)
                        result = await graph.ainvoke(
                            {"question": claimed["question"], "products": products["items"], "answer": ""},
                            {"configurable": {"thread_id": claimed["checkpoint_thread_id"]}},
                        )
                        for pos in range(0, len(result["answer"]), 24):
                            await emit(result["answer"][pos : pos + 24])
                            await asyncio.sleep(settings.fixture_chunk_delay)

                async def renew():
                    while True:
                        if not await runs.heartbeat(claimed):
                            return
                        await asyncio.sleep(2)

                job = asyncio.create_task(generate())
                heartbeat = asyncio.create_task(renew())
                try:
                    done, _ = await asyncio.wait(
                        {job, heartbeat},
                        timeout=settings.run_timeout_seconds,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if job in done:
                        await job
                    elif heartbeat in done:
                        await heartbeat
                        error = "LEASE_LOST"
                    else:
                        error = "MODEL_DEADLINE_EXCEEDED"
                except DomainError as exc:
                    error = exc.code
                except Exception as exc:
                    log.error("generation failed run=%s type=%s", claimed["id"], type(exc).__name__)
                    error = "GENERATION_UNAVAILABLE"
                finally:
                    job.cancel()
                    heartbeat.cancel()
                    await asyncio.gather(job, heartbeat, return_exceptions=True)
                    for resource in resources:
                        await resource.close()
                    await runs.finish(claimed, error)
                    model_actor.reset(actor_context)
                    current_model_call.reset(call_context)

            pending = set()
            try:
                while True:
                    pending = {task for task in pending if not task.done()}
                    await worker_status(db, owner, "generation", "RUNNING" if pending else "IDLE")
                    if len(pending) < 3:
                        claimed = await runs.claim(owner)
                        if claimed:
                            pending.add(asyncio.create_task(process(claimed)))
                            continue
                    await asyncio.sleep(settings.poll_seconds)
            finally:
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                if objects:
                    objects.close()
                await db.engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run(main())
