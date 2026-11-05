"""Flight trends: compare the user's route with cheaper nearby airports (DOT Table 6).

Skeleton stub (planning contracts issue). The real logic is the flight trends issue:
it picks at most one cheaper alternative airport per side, and tells flight_search
where and when to search (search_airports, candidate_dates). Until then the result is
"empty", so flight_search falls back to the places' own airports and the trip's dates.
"""
from services.agent.state import FlightTrends, TripConstraints, TripState, empty_result, flight_skip_reason


def flight_trends(state: TripState) -> dict:
    c = TripConstraints(**state["constraints"])
    skip = flight_skip_reason(c)
    result: FlightTrends = empty_result("skipped", skip) if skip else empty_result("empty", "not implemented yet")
    return {"flight_trends": result}
