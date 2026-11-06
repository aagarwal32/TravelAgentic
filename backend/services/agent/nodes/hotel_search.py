"""Live hotel search (LiteAPI): up to 5 hotel picks (cheapest, better value, upscale) within the hotel budget.

Skeleton stub (planning contracts issue). It already works out the search inputs for
real, so the fallback rules are in place before the search exists:
- the stay dates, from the chosen flights, else flight trends, else the trip itself
- the number of rooms
- the hotel budget: the trip budget minus the cheapest flight, if flights found one
The search and the picks (search_hotels_with_prices, then the shortlist) are the hotel issue;
until then the result is "empty" with these inputs filled in.
"""
from datetime import date, timedelta
from math import ceil
from typing import Optional

from services.agent.state import (FlightOffers, FlightTrends, HotelOptions, TripConstraints,
                                  TripState, empty_result)

PEOPLE_PER_ROOM = 2


def _flight_stay(offers: FlightOffers) -> Optional[tuple[date, Optional[date]]]:
    """Check-in = the cheapest shortlisted flight's arrival day; check-out = its return departure day."""
    shortlist = offers.get("shortlist") or []
    if offers.get("status") != "ok" or not shortlist:
        return None
    cheapest = min(shortlist, key=lambda offer: offer["total_amount"])
    checkin = date.fromisoformat(cheapest["outbound"]["arrive_at"][:10])
    inbound = cheapest.get("inbound")
    return checkin, date.fromisoformat(inbound["depart_at"][:10]) if inbound else None


def _trends_stay(trends: FlightTrends) -> Optional[tuple[date, Optional[date]]]:
    """The first of flight trends' candidate dates (the window rule in state.py)."""
    candidates = trends.get("candidate_dates") or []
    if trends.get("status") != "ok" or not candidates:
        return None
    first = candidates[0]
    back = first.get("return_date")
    return date.fromisoformat(first["depart_date"]), date.fromisoformat(back) if back else None


def stay_dates(state: TripState) -> Optional[tuple[date, date]]:
    """(check-in, check-out), or None when the trip has no stay length.

    Dates come from the most concrete source available: the flights the user would
    take, else the dates flight trends picked, else the trip's own dates (an exact
    departure, or the start of the departure window). Check-out falls back to
    check-in + trip_nights; without it (or a return date) there is no stay to price.
    """
    c = TripConstraints(**state["constraints"])
    offers, trends = state.get("flight_offers"), state.get("flight_trends")
    stay = (_flight_stay(offers) if offers else None) or (_trends_stay(trends) if trends else None)
    if stay is None:
        checkin = c.depart_date or c.flexible_start
        if checkin is None:
            return None
        stay = checkin, c.return_date
    checkin, checkout = stay
    if checkout is None and c.trip_nights:
        checkout = checkin + timedelta(days=c.trip_nights)
    if checkout is None or checkout <= checkin:
        return None
    return checkin, checkout


def room_count(travelers: int) -> int:
    """One room per two travelers, rounded up."""
    return ceil(travelers / PEOPLE_PER_ROOM)


def hotel_budget(state: TripState) -> float:
    """The trip budget minus the cheapest flight, if flights found offers; never below 0."""
    budget = TripConstraints(**state["constraints"]).budget or 0.0
    offers = state.get("flight_offers")
    cheapest = offers.get("cheapest_total") if offers and offers["status"] == "ok" else None
    if cheapest is not None:
        budget -= cheapest
    return round(max(budget, 0.0), 2)


def hotel_search(state: TripState) -> dict:
    c = TripConstraints(**state["constraints"])
    if c.destination_place is None:
        return {"hotel_options": empty_result("skipped", "destination not resolved to a place")}
    stay = stay_dates(state)
    if stay is None:
        return {"hotel_options": empty_result("skipped", "no stay length (one-way trip without nights)")}
    checkin, checkout = stay
    result: HotelOptions = {"status": "empty", "reason": "not implemented yet",
                            "checkin": checkin.isoformat(), "checkout": checkout.isoformat(),
                            "rooms": room_count(c.travelers or 1), "hotel_budget": hotel_budget(state)}
    return {"hotel_options": result}
