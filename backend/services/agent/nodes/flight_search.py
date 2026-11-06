"""Live flight search (Duffel): find real offers and shortlist about 5 in code.

Skeleton stub (planning contracts issue). The real logic is the flight search issue:
search state["flight_trends"]["search_airports"] on its candidate_dates (or, when
trends has no result, the places' own airports on the trip's dates) through the
guarded Duffel client, measure each offer (FlightMetrics), and shortlist them. Until
then the result is "empty", so hotel_search uses the full budget.
"""
from services.agent.state import FlightOffers, TripConstraints, TripState, empty_result, flight_skip_reason


def flight_search(state: TripState) -> dict:
    c = TripConstraints(**state["constraints"])
    skip = flight_skip_reason(c)
    result: FlightOffers = empty_result("skipped", skip) if skip else empty_result("empty", "not implemented yet")
    return {"flight_offers": result}
