# Run from backend/: python3 services/duffel_client/nodes/call_api.py

from pathlib import Path
import sys
from typing import TypedDict

import httpx
from langgraph.graph import END, START, StateGraph

# Allow core.config imports when running this file directly.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))



# State: information passed through the graph.
class FlightState(TypedDict):
    origin: str
    destination: str
    date: str
    api_result: list


DUFFEL_URL = "https://api.duffel.com/air/offer_requests"


def call_api(state: FlightState) -> dict:
    from core.config import settings

    body = {
        "data": {
            "slices": [{
                "origin": state["origin"],
                "destination": state["destination"],
                "departure_date": state["date"],
            }],
            "passengers": [{"type": "adult"}],
            "cabin_class": "economy",
        }
    }
    headers = {
        "Authorization": f"Bearer {settings.DUFFEL_API_KEY}",
        "Duffel-Version": "v2",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    resp = httpx.post(DUFFEL_URL, params={"return_offers": "true"},
                      json=body, headers=headers, timeout=30)
    resp.raise_for_status()
    offers = resp.json()["data"]["offers"]

    offers.sort(key=lambda o: float(o["total_amount"]))
    simplified = []
    for o in offers[:5]:
        first_slice = o["slices"][0]
        simplified.append({
            "airline": o["owner"]["name"],
            "price": f'{o["total_amount"]} {o["total_currency"]}',
            "stops": len(first_slice["segments"]) - 1,
            "duration": first_slice["duration"],
        })

    print(f"[call_api] got {len(offers)} offers, kept {len(simplified)}")
    return {"api_result": simplified}


# Edges: START -> call_api -> END
builder = StateGraph(FlightState)
builder.add_node("call_api", call_api)
builder.add_edge(START, "call_api")
builder.add_edge("call_api", END)

graph = builder.compile()


if __name__ == "__main__":
    result = graph.invoke({
        "origin": "MIA",
        "destination": "TPA",
        "date": "2026-11-20",
        "api_result": [],
    })
    for offer in result["api_result"]:
        print(offer)

    image_path = Path(__file__).with_name("flight_agent.png")
    image_path.write_bytes(graph.get_graph().draw_mermaid_png())
    print(f"Graph saved to {image_path}")
