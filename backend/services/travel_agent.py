# run: python3 services/travel_agent.py

from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph


# State: information passed through the graph.
class State(TypedDict):
    name: str
    message: str


# Node: reads state and returns updates.
def greet(state: State) -> dict:
    return {"message": f"Hello, {state['name']}!"}


# Edges: define START -> greet -> END.
builder = StateGraph(State)
builder.add_node("greet", greet)
builder.add_edge(START, "greet")
builder.add_edge("greet", END)

graph = builder.compile()


if __name__ == "__main__":
    result = graph.invoke({"name": "Duc", "message": ""})
    print(result["message"])

    # The default renderer uses the online Mermaid rendering service.
    image_path = Path(__file__).with_name("travel_agent.png")
    image_path.write_bytes(graph.get_graph().draw_mermaid_png())
    print(f"Graph saved to {image_path}")
