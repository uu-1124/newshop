"""Full graph topology used by the live generation worker.

Specialist nodes fail closed until their implementation is bound. Context callbacks
are process-local and never enter checkpoint state. Every specialist and emission
passes the caller's live run fence again.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from app.domain.identity import DomainError

Intent = Literal["GUIDE", "KNOWLEDGE", "SHOPPING", "ORDER", "RETURN", "HANDOFF", "CLARIFY", "GENERAL"]
Node = Callable[[dict], Awaitable[dict]]
INTENTS = {
    "GUIDE": "N04",
    "KNOWLEDGE": "N05",
    "SHOPPING": "N06",
    "ORDER": "N07",
    "RETURN": "N08",
    "HANDOFF": "N09",
    "CLARIFY": "N10",
    "GENERAL": "N10",
}


class WorkflowState(TypedDict, total=False):
    schema_version: str
    run_id: str
    conversation_id: str
    control_epoch: int
    memory_epoch: int
    question: str
    intent: Intent
    steps_used: int
    safe_context: list[dict]
    source_refs: list[dict]
    action_ids: list[str]
    products: list[dict]
    answer: str
    history: list[dict]
    image_observations: list[str]
    tool_results: list[dict]
    scope_context: dict
    orders: list[dict]
    order_context: dict
    needs_requery: bool
    review_attempts: int
    needs_user_input: bool
    retrieval_degraded: bool


@dataclass
class WorkflowContext:
    guard: Node
    nodes: dict[str, Node] = field(default_factory=dict)
    max_steps: int = 12


def build_workflow_graph(checkpointer=None):
    graph = StateGraph(WorkflowState, context_schema=WorkflowContext)

    def node_for(node_id):
        async def invoke(state: WorkflowState, runtime: Runtime[WorkflowContext]):
            context = runtime.context
            if state.get("steps_used", 0) >= context.max_steps:
                raise DomainError("WORKFLOW_BUDGET_EXCEEDED", "本轮处理已达到上限", 409)
            await context.guard(state)
            handler = context.nodes.get(node_id)
            if handler is None:
                raise DomainError("WORKFLOW_NODE_NOT_ACTIVE", "该处理流程尚未开放", 409)
            update = await handler(state)
            if not isinstance(update, dict) or "steps_used" in update:
                raise DomainError("WORKFLOW_PROTOCOL_ERROR", "处理流程返回无效结果", 503)
            for key in ("run_id", "conversation_id", "control_epoch", "memory_epoch"):
                if key in update and update[key] != state.get(key):
                    raise DomainError("WORKFLOW_PROTOCOL_ERROR", "处理流程不能修改运行身份", 503)
            if node_id == "N03" and update.get("intent") not in INTENTS:
                raise DomainError("WORKFLOW_INTENT_INVALID", "问题类型无法确定", 422)
            if node_id == "N11" and update.get("needs_requery"):
                if state.get("review_attempts", 0) >= 1:
                    raise DomainError("WORKFLOW_REQUERY_EXHAUSTED", "无法稳定核实信息，请稍后重问", 409)
                update = update | {"review_attempts": 1}
            return update | {"steps_used": state.get("steps_used", 0) + 1}

        return invoke

    for number in range(1, 13):
        node_id = f"N{number:02}"
        graph.add_node(node_id, node_for(node_id))
    graph.add_edge(START, "N01")
    graph.add_edge("N01", "N02")
    graph.add_edge("N02", "N03")
    graph.add_conditional_edges("N03", lambda state: INTENTS[state["intent"]], list(set(INTENTS.values())))
    for number in range(4, 10):
        graph.add_edge(f"N{number:02}", "N10")
    graph.add_edge("N10", "N11")
    graph.add_conditional_edges(
        "N11", lambda state: "N03" if state.get("needs_requery") else "N12", ["N03", "N12"]
    )
    graph.add_edge("N12", END)
    return graph.compile(checkpointer=checkpointer)
