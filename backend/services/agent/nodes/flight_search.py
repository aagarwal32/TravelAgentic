"""Probe flight dates and airports within the trip budget and shortlist Duffel offers."""
from datetime import date, datetime, timedelta, timezone
from itertools import chain, product
import re

from pydantic import ValidationError

from services.agent.state import FlightOffers, TripConstraints, TripState, empty_result, flight_skip_reason
from services.duffel_client import DuffelError, DuffelSearchBlocked
from services.flight_service import _initial_departure_dates, _search_offers, _shortlist_offers

MAX_SEARCHES_PER_TRIP = 6


def flight_search(state: TripState) -> dict:
    """Run at most six guarded attempts, starting with the primary home route."""
    try:
        c = TripConstraints(**state["constraints"])
    except ValidationError:
        return {"flight_offers": empty_result("empty", "trip constraints are invalid for flight search")}
    skip = flight_skip_reason(c)
    if skip:
        return {"flight_offers": empty_result("skipped", skip)}
    result: FlightOffers = {
        "status": "empty", "reason": "no usable flight offers found", "searches_made": 0,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cheapest_total": None, "shortlist": [], "searches": [],
    }
    if c.travelers is None or c.trip_type is None:
        result["reason"] = "traveler count or trip type is missing"
        return {"flight_offers": result}
    start, end = c.depart_date or c.flexible_start, c.depart_date or c.flexible_end
    if start is None or end is None:
        result["reason"] = "departure dates are missing"
        return {"flight_offers": result}
    if c.trip_type == "round_trip" and c.return_date is None and c.trip_nights is None:
        result["reason"] = "return date or stay length is missing"
        return {"flight_offers": result}

    trends = state.get("flight_trends") or {}
    if trends.get("status") != "ok":
        trends = {}
    suggested = trends.get("search_airports") or {}
    if not isinstance(suggested, dict):
        suggested = {}
    # Trends chooses which airports to search; flight search only orders the searches.
    # Its options name the alternative airports for each side.
    alternatives = {"origin": set(), "destination": set()}
    for option in trends.get("options") or []:
        side = {"departure": "origin", "arrival": "destination"}.get(option.get("side"))
        if side:
            alternatives[side].update(str(code).strip().upper() for code in option.get("airports") or [])
    airports, home_airports = {}, {}
    for side, place in (("origin", c.origin_place), ("destination", c.destination_place)):
        codes = suggested.get(side)
        if not isinstance(codes, list) or not codes:
            # Without trends: the place's own airport only, the closest one with a DOT
            # market (flight_airports is closest first). Older saved places lack it.
            codes = (place.flight_airports if place.flight_airports is not None else place.airports)[:1]
        selected = [code.strip().upper() for code in codes
                    if isinstance(code, str) and re.fullmatch(r"[A-Z]{3}", code.strip().upper())]
        airports[side] = list(dict.fromkeys(selected))
        home_airports[side] = set(airports[side]) - alternatives[side]
    if not airports["origin"] or not airports["destination"]:
        result["reason"] = "no usable origin or destination airports"
        return {"flight_offers": result}

    # Narrow candidate bounds before using trend dates as calendar-heuristic seeds.
    if c.trip_type == "round_trip" and c.return_date:
        if c.trip_nights and c.depart_date is None:
            start = end = c.return_date - timedelta(days=c.trip_nights)
        else:
            end = min(end, c.return_date)
    elif c.trip_type == "round_trip":
        end = min(end, date.max - timedelta(days=c.trip_nights))
    dates = []
    candidates = trends.get("candidate_dates") or []
    if not isinstance(candidates, list):
        candidates = []
    for candidate in candidates:
        try:
            depart = date.fromisoformat(candidate["depart_date"])
            back = None
            if c.trip_type == "round_trip":
                raw_back = candidate.get("return_date")
                back = date.fromisoformat(raw_back) if raw_back is not None else (
                    c.return_date or depart + timedelta(days=c.trip_nights))
        except (KeyError, TypeError, ValueError):
            continue
        if not start <= depart <= end:
            continue
        if back is not None and (back < depart or (c.return_date and back != c.return_date)
                                 or (c.trip_nights and (back - depart).days != c.trip_nights)):
            continue
        if (depart, back) not in dates:
            dates.append((depart, back))
        if len(dates) == 3:
            break
    departures = _initial_departure_dates(c, preferred_dates=[depart for depart, _ in dates])
    home_pairs, alternative_pairs = [], []
    for origin, destination in product(airports["origin"], airports["destination"]):
        if origin == destination:
            continue
        pairs = (home_pairs if origin in home_airports["origin"] and destination in home_airports["destination"]
                 else alternative_pairs)
        pairs.append((origin, destination))
    pairs = home_pairs + alternative_pairs
    if not pairs or not departures:
        result["reason"] = "no distinct origin/destination airport pairs" if not pairs else "no usable departure dates"
        return {"flight_offers": result}

    offers, errors, attempted = [], [], set()
    blocked = None

    def search(depart, pair):
        nonlocal blocked
        origin, destination = pair
        back = None if c.trip_type == "one_way" else c.return_date or depart + timedelta(days=c.trip_nights)
        attempted.add((depart, pair))
        result["searches_made"] += 1
        log = {"origin": origin, "destination": destination, "depart_date": depart.isoformat(),
               "return_date": back.isoformat() if back else None, "offers": 0,
               "cheapest_total": None, "error": None}
        result["searches"].append(log)
        try:
            found = _search_offers(
                origin, destination, depart, c.travelers, return_date=back,
                max_connections=c.max_connections,
                from_alternative_airport=pair in alternative_pairs,
            )
        except DuffelSearchBlocked as exc:
            blocked = log["error"] = str(exc)
        except DuffelError as exc:
            errors.append(str(exc))
            log["error"] = str(exc)
        else:
            offers.extend(found)
            log.update(offers=len(found),
                       cheapest_total=min((offer["total_amount"] for offer in found), default=None))
            return found
        return []

    primary = pairs[0]
    prices_by_date = {}
    for depart in departures:
        if blocked or result["searches_made"] >= MAX_SEARCHES_PER_TRIP:
            break
        found = search(depart, primary)
        if found:
            prices_by_date[depart] = min(offer["total_amount"] for offer in found)

    anchor = min(prices_by_date, key=prices_by_date.get) if prices_by_date else departures[0]
    alternative = next((pair for pair in alternative_pairs if pair != primary), None)
    nearby = []
    if prices_by_date and start < end:
        # Compare immediate neighbors first, leaving one slot for an alternative route.
        for offset in (-1, 1, -2, 2):
            ordinal = anchor.toordinal() + offset
            if start.toordinal() <= ordinal <= end.toordinal():
                depart = date.fromordinal(ordinal)
                if depart not in departures:
                    nearby.append(depart)
        nearby = nearby[:2 if alternative else 3]

    alternative_probe = [(anchor, alternative)] if alternative else []
    remaining = chain(
        product(nearby, [primary]), alternative_probe,
        product(departures, home_pairs), product(departures, alternative_pairs),
    )
    budget_reached = False
    for depart, pair in remaining:
        if blocked:
            break
        if (depart, pair) in attempted:
            continue
        if result["searches_made"] >= MAX_SEARCHES_PER_TRIP:
            budget_reached = True
            break
        search(depart, pair)

    fetched_at = datetime.now(timezone.utc)
    shortlist = _shortlist_offers(offers, now=fetched_at)
    result.update(fetched_at=fetched_at.isoformat(timespec="seconds"), shortlist=shortlist)
    if shortlist:
        result.update(status="ok", cheapest_total=min(offer["total_amount"] for offer in shortlist))
        result.pop("reason")
    reasons = []
    if errors:
        reasons.append(f"{len(errors)} flight search attempt(s) failed: {errors[0]}")
    if blocked:
        reasons.append(f"search stopped early: {blocked}")
    elif budget_reached:
        reasons.append("per-trip search limit reached")
    if reasons:
        result["reason"] = ". ".join(reasons)
    return {"flight_offers": result}
