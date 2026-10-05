# Run from backend/: python3 services/travel_agent.py

import argparse
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from openai import OpenAI

if __package__:
    from .chatgpt_auth import choose_model, get_access_token
else:
    from chatgpt_auth import choose_model, get_access_token


class State(TypedDict):
    input: str
    output: str
    model: str


def call_model(state: State) -> dict:
    """Send the input unchanged; collect the model's text as output."""
    parts = []
    completed = False
    with OpenAI(api_key=get_access_token(), max_retries=0, timeout=60) as client:
        with client.responses.create(
            model=state["model"],
            input=[{"role": "user", "content": state["input"]}],
            store=False,
            stream=True,
        ) as stream:
            for event in stream:
                if event.type == "response.output_text.delta":
                    parts.append(event.delta)
                elif event.type == "response.completed":
                    completed = True
                elif event.type in ("response.failed", "response.incomplete", "error"):
                    raise RuntimeError("Model request failed or did not complete.")
    if not completed:
        raise RuntimeError("Model stream ended without completion.")
    return {"output": "".join(parts)}


builder = StateGraph(State)
builder.add_node("call_model", call_model)
builder.add_edge(START, "call_model")
builder.add_edge("call_model", END)
graph = builder.compile()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="One input, one model call.")
    parser.add_argument("--model", help="Available model slug; otherwise choose interactively.")
    parser.add_argument("--input", help="Message; otherwise enter it interactively.")
    args = parser.parse_args()

    model = args.model or choose_model()
    message = args.input if args.input is not None else input("You: ")
    result = graph.invoke({"input": message, "output": "", "model": model})
    print(result["output"])

    # PNG rendering uses the online Mermaid service.
    image_path = Path(__file__).with_name("travel_agent.png")
    try:
        image_path.write_bytes(graph.get_graph().draw_mermaid_png())
        print(f"Graph saved to {image_path}")
    except Exception:
        print("Model call completed, but the graph PNG could not be rendered.")
