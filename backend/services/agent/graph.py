"""Local card demo. Run from backend: python -m services.agent.graph"""
import argparse
import json
from datetime import date
from uuid import uuid4
from pathlib import Path
from langchain_core.messages import AIMessage
from langgraph.graph import START, END, StateGraph
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command, interrupt
from services.agent.state import OPTIONAL, TripConstraints, TripState
from services.agent.nodes.extract import extract
from services.agent.nodes.clarify import build_card, clarify, to_pending


def ask_optional(state: TripState) -> dict:
    """Pause once with every optional card still open; unanswered cards count as skipped."""
    c = TripConstraints(**state["constraints"])
    skipped = list(state.get("skipped", []))
    fields = [f for f in OPTIONAL if getattr(c, f) is None and f not in skipped]
    if c.mode == "drive":
        fields = [f for f in fields if f != "max_connections"]
    if not fields:
        return {"optional_asked": True}
    cards = [build_card(f, c) for f in fields]
    reply = interrupt({"type": "optional_cards", "cards": cards})
    answers = reply.get("answers", {}) if isinstance(reply, dict) else {}
    pending = [to_pending(card, answers[card["field"]]) for card in cards if card["field"] in answers]
    skipped += [f for f in fields if f not in answers]
    return {"pending_answers": pending, "skipped": skipped, "optional_asked": True}


def route_after_extract(state: TripState) -> str:
    if state["missing"]:
        return "clarify"
    if not state.get("optional_asked"):
        return "ask_optional"
    return "planning"


def next_step(state: TripState) -> dict:
    # Replace this node with the trends agent when its implementation lands.
    return {"messages": [AIMessage(content="Your trip details are ready for planning.")]}


def build_graph(checkpointer=None):
    builder = StateGraph(TripState)
    builder.add_node("extract", extract)
    builder.add_node("clarify", clarify)
    builder.add_node("ask_optional", ask_optional)
    builder.add_node("planning", next_step)
    builder.add_edge(START, "extract")
    builder.add_conditional_edges("extract", route_after_extract,
                                  ["clarify", "ask_optional", "planning"])
    builder.add_edge("clarify", "extract")
    builder.add_edge("ask_optional", "extract")
    builder.add_edge("planning", END)
    return builder.compile(checkpointer=checkpointer or InMemorySaver())


def start_state(request: str, model: str) -> dict:
    return {"pending_answers": [{"field": None, "question": None, "text": request}],
            "model": model, "constraints": {}, "missing": [], "skipped": []}


def parse_line(card: dict, line: str) -> dict:
    """Send a line as a fixed value if it fits the card's picker/buttons, else as text."""
    kind = card["input"]
    if kind == "number" and line.isdigit():
        return {"value": int(line)}
    if kind == "choice":
        for option in card["options"]:
            if line.lower() in (str(option["value"]).lower(), option["label"].lower()):
                return {"value": option["value"]}
    if kind in ("date", "date_or_range"):
        parts = [p.strip() for p in line.split(" to ")]
        try:
            dates = [date.fromisoformat(p).isoformat() for p in parts]
        except ValueError:
            dates = []
        if len(dates) == 1:
            return {"value": dates[0]}
        if len(dates) == 2 and kind == "date_or_range":
            return {"value": {"start": dates[0], "end": dates[1]}}
    return {"text": line}


def describe(card: dict) -> str:
    kind, hint = card["input"], None
    if kind == "number":
        hint = f"{card['min']}-{card['max']}" if "max" in card else f"at least {card['min']}"
    elif kind == "choice":
        hint = " / ".join(o["label"] for o in card["options"])
    elif kind == "date":
        hint = "YYYY-MM-DD" + (f", from {card['min']}" if "min" in card else "")
    elif kind == "date_or_range":
        hint = "YYYY-MM-DD, or YYYY-MM-DD to YYYY-MM-DD"
    if card.get("allow_text"):
        hint += ", or type it"
    return f"{card['text']}" + (f" [{hint}]" if hint else "")


def main():
    from services.agent.nodes.chatgpt_auth import choose_model
    parser = argparse.ArgumentParser(description="Card-based trip intake with ChatGPT OAuth")
    parser.add_argument("--model", help="Available model slug; otherwise choose after sign-in")
    parser.add_argument("--graph-only", action="store_true", help="Render the graph without signing in")
    args = parser.parse_args()
    graph = build_graph()
    image_path = Path(__file__).with_name("trip_intake_graph.png")
    try:
        # Mermaid renders topology only; no conversation state or credentials are sent.
        image_path.write_bytes(graph.get_graph().draw_mermaid_png(max_retries=0))
        print(f"Graph saved to {image_path}")
    except Exception:
        print("Could not render graph PNG. Check your connection to the Mermaid rendering service.")
        if args.graph_only:
            raise SystemExit(1) from None
    if args.graph_only:
        return
    model = args.model or choose_model()
    config = {"configurable": {"thread_id": str(uuid4())}}
    try:
        request = input("Your trip: ").strip()
        if not request:
            return
        result = graph.invoke(start_state(request, model), config)
        while result.get("__interrupt__"):
            pause = result["__interrupt__"][0].value
            print("\nSo far:", json.dumps({k: v for k, v in result["constraints"].items() if v is not None}))
            if pause["type"] == "required_question":
                if pause["error"]:
                    print("!", pause["error"])
                line = ""
                while not line:
                    line = input(describe(pause["card"]) + "\n> ").strip()
                resume = parse_line(pause["card"], line)
            else:
                print("Optional (press Enter to skip):")
                answers = {}
                for card in pause["cards"]:
                    line = input(describe(card) + "\n> ").strip()
                    if line:
                        answers[card["field"]] = parse_line(card, line)
                resume = {"answers": answers}
            result = graph.invoke(Command(resume=resume), config)
        print("\n" + result["messages"][-1].content)
        print(json.dumps(result["constraints"], indent=2))
    except (EOFError, KeyboardInterrupt):
        print("\nIntake ended.")
    except Exception:
        # SDK exceptions can include request/response details. Keep demo errors generic.
        print("The request failed. Check your connection, OAuth sign-in, and selected model.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
