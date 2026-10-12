"""Local textbox demo. Run from backend: python -m services.agent.graph"""
import argparse
import json
from uuid import uuid4
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import START, END, StateGraph
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command, interrupt
from services.agent.state import TripState
from services.agent.nodes.extract import extract
from services.agent.nodes.clarify import clarify


def wait_for_reply(state: TripState) -> dict:
    answer = interrupt({"message": state["messages"][-1].content,
                        "missing": state["missing"], "errors": state["errors"]})
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("Resume with a nonempty textbox string.")
    return {"messages": [HumanMessage(content=answer.strip())]}


def next_step(state: TripState) -> dict:
    # Replace this node with the planning agents when their implementation lands.
    return {"messages": [AIMessage(content="Your required trip details are ready for planning.")]}


def build_graph(checkpointer=None):
    builder = StateGraph(TripState)
    builder.add_node("extract", extract)
    builder.add_node("clarify", clarify)
    builder.add_node("wait_for_reply", wait_for_reply)
    builder.add_node("planning", next_step)
    builder.add_edge(START, "extract")
    builder.add_conditional_edges("extract", lambda s: "planning" if s["ready"] else "clarify")
    builder.add_edge("clarify", "wait_for_reply")
    builder.add_edge("wait_for_reply", "extract")
    builder.add_edge("planning", END)
    return builder.compile(checkpointer=checkpointer or InMemorySaver())


def main():
    from services.agent.nodes.chatgpt_auth import choose_model
    parser = argparse.ArgumentParser(description="Conversational trip intake with ChatGPT OAuth")
    parser.add_argument("--model", help="Available model slug; otherwise choose after sign-in")
    args = parser.parse_args()
    model = args.model or choose_model()
    graph = build_graph()
    config = {"configurable": {"thread_id": str(uuid4())}}
    try:
        message = input("You: ").strip()
        if not message:
            return
        result = graph.invoke({"messages": [HumanMessage(content=message)], "model": model,
                               "constraints": {}, "missing": [], "skipped": []}, config)
        while result.get("__interrupt__"):
            print("Assistant:", result["__interrupt__"][0].value["message"])
            answer = input("You (or /quit): ").strip()
            if answer == "/quit":
                return
            if not answer:
                print("Please enter a response.")
                continue
            result = graph.invoke(Command(resume=answer), config)
        print("Assistant:", result["messages"][-1].content)
        print(json.dumps(result["constraints"], indent=2))
    except (EOFError, KeyboardInterrupt):
        print("\nConversation ended.")
    except Exception:
        # SDK exceptions can include request/response details. Keep demo errors generic.
        print("The request failed. Check your connection, OAuth sign-in, and selected model.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
