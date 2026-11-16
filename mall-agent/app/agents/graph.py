from typing import TypedDict

from langgraph.graph import END, START, StateGraph


class GraphState(TypedDict):
    # No tokens, secrets, arbitrary URLs or executable parameters can enter graph state.
    question: str
    products: list[dict]
    answer: str


def build_graph(provider, checkpointer):
    async def compose(state: GraphState):
        return {"answer": await provider.answer(state["question"], state["products"])}

    graph = StateGraph(GraphState)
    graph.add_node("compose_readonly_answer", compose)
    graph.add_edge(START, "compose_readonly_answer")
    graph.add_edge("compose_readonly_answer", END)
    return graph.compile(checkpointer=checkpointer)
