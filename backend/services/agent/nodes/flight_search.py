"""Probe flight dates and airports within the trip budget and shortlist Duffel offers."""
from datetime import date, datetime, timedelta, timezone
from itertools import product
import re

from pydantic import ValidationError

from services.agent.state import FlightOffers, TripConstraints, TripState, empty_result, flight_skip_reason
from services.duffel_client import DuffelError, DuffelSearchBlocked
from services.flight_service import _initial_departure_dates, _search_offers, _shortlist_offers

MAX_SEARCHES_PER_TRIP = 10
NEAR_TIE_RATIO = 1.05   # a probe date within 5% of the cheapest is also worth a look


def _sides(airports: dict, kinds: dict) -> dict:
    """{"origin": {"home": [...], "alternative": [...], "near_miss": [...]}, ...}, trends order."""
    grouped = {}
    for side, codes in airports.items():
        groups = {"home": [], "alternative": [], "near_miss": []}
        for code in codes:
            groups[kinds[side].get(code, "home")].append(code)
        if not groups["home"]:
            # Trends always lists the home airports first; never search without one
            groups["home"] = codes[:1]
        grouped[side] = groups
    return grouped


def _priority_routes(airports: dict, kinds: dict) -> list[tuple[tuple[str, str], str]]:
    """Airport pairs in search order, each with its kind (home, alternative, near_miss).

    1. Home to home: the main airports first, then each place's own airport.
    2. An alternative against the other side's main airport.
    3. Alternative to alternative.
    4. A near miss against the other side's main airport only.
    """
    sides = _sides(airports, kinds)
    o, d = sides["origin"], sides["destination"]
    main_o, main_d = o["home"][0], d["home"][0]
    ordered = (
        [((a, b), "home") for a, b in product(o["home"], d["home"])]
        + [((a, main_d), "alternative") for a in o["alternative"]]
        + [((main_o, b), "alternative") for b in d["alternative"]]
        + [((a, b), "alternative") for a, b in product(o["alternative"], d["alternative"])]
        + [((a, main_d), "near_miss") for a in o["near_miss"]]
        + [((main_o, b), "near_miss") for b in d["near_miss"]]
    )
    routes = {}
    for pair, kind in ordered:
        if pair[0] != pair[1]:
            routes.setdefault(pair, kind)
    return list(routes.items())


def _extra_kind(pair: tuple[str, str], kinds: dict) -> str:
    """The kind of a pair outside the priority list: its non-home end decides."""
    found = {kinds["origin"].get(pair[0], "home"), kinds["destination"].get(pair[1], "home")}
    return "near_miss" if "near_miss" in found else "alternative" if "alternative" in found else "home"


def _extras(anchor: date, near_ties: list[date], start: date, end: date, prices: dict,
            routes: list, kinds: dict, airports: dict):
    """Searches worth spending what is left of the cap on, best first.

    a. The near-tie probe dates on the cheapest route found.
    b. The days next to the chosen date on that route (inside the departure window).
    c. Each alternative or near-miss route, cheapest first, to the other side's
       remaining home airports (the place's own airport when it isn't the main one).
    """
    if not prices:
        return
    route_kind = dict(routes)
    order = {pair: index for index, (pair, _) in enumerate(routes)}
    # Ties go to the higher-priority route, then the earlier date
    _, best = min(prices, key=lambda key: (prices[key], order.get(key[1], len(order)), key[0]))
    for day in near_ties:
        yield day, best
    for offset in (-1, 1):
        ordinal = anchor.toordinal() + offset
        if start.toordinal() <= ordinal <= end.toordinal():
            yield date.fromordinal(ordinal), best
    sides = _sides(airports, kinds)
    priced = sorted({pair for _, pair in prices if route_kind.get(pair, "home") != "home"},
                    key=lambda pair: min(price for (_, p), price in prices.items() if p == pair))
    for origin, destination in priced:
        if kinds["origin"].get(origin) and not kinds["destination"].get(destination):
            for home in sides["destination"]["home"][1:]:
                if home != origin:
                    yield anchor, (origin, home)
        elif kinds["destination"].get(destination) and not kinds["origin"].get(origin):
            for home in sides["origin"]["home"][1:]:
                if home != destination:
                    yield anchor, (home, destination)


def flight_search(state: TripState) -> dict:
    """Probe dates on the main route, widen to the other routes, then spend the rest.

    At most MAX_SEARCHES_PER_TRIP guarded attempts; see _priority_routes and _extras.
    """
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
    # Suggested options are alternatives; the others are near misses.
    kinds = {"origin": {}, "destination": {}}
    for option in trends.get("options") or []:
        side = {"departure": "origin", "arrival": "destination"}.get(option.get("side"))
        if side:
            kind = "near_miss" if option.get("suggested") is False else "alternative"
            for code in option.get("airports") or []:
                kinds[side].setdefault(str(code).strip().upper(), kind)
    airports = {}
    for side, place in (("origin", c.origin_place), ("destination", c.destination_place)):
        codes = suggested.get(side)
        if not isinstance(codes, list) or not codes:
            # Without trends: the place's own airport only, the closest one with a DOT
            # market (flight_airports is closest first). Older saved places lack it.
            codes = (place.flight_airports if place.flight_airports is not None else place.airports)[:1]
        selected = [code.strip().upper() for code in codes
                    if isinstance(code, str) and re.fullmatch(r"[A-Z]{3}", code.strip().upper())]
        airports[side] = list(dict.fromkeys(selected))
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
    routes = _priority_routes(airports, kinds)
    if not routes or not departures:
        result["reason"] = "no distinct origin/destination airport pairs" if not routes else "no usable departure dates"
        return {"flight_offers": result}
    route_kind = dict(routes)

    offers_by_kind = {"home": [], "alternative": [], "near_miss": []}
    errors, attempted, prices = [], set(), {}
    blocked = None

    def search(depart, pair):
        nonlocal blocked
        origin, destination = pair
        kind = route_kind.get(pair) or _extra_kind(pair, kinds)
        back = None if c.trip_type == "one_way" else c.return_date or depart + timedelta(days=c.trip_nights)
        attempted.add((depart, pair))
        result["searches_made"] += 1
        log = {"origin": origin, "destination": destination, "depart_date": depart.isoformat(),
               "return_date": back.isoformat() if back else None, "kind": kind, "offers": 0,
               "cheapest_total": None, "error": None}
        result["searches"].append(log)
        try:
            found = _search_offers(
                origin, destination, depart, c.travelers, return_date=back,
                max_connections=c.max_connections,
                from_alternative_airport=kind != "home",
            )
        except DuffelSearchBlocked as exc:
            blocked = log["error"] = str(exc)
        except DuffelError as exc:
            errors.append(str(exc))
            log["error"] = str(exc)
        else:
            offers_by_kind[kind].extend(found)
            if found:
                prices[(depart, pair)] = min(offer["total_amount"] for offer in found)
            log.update(offers=len(found), cheapest_total=prices.get((depart, pair)))

    def can_search() -> bool:
        return not blocked and result["searches_made"] < MAX_SEARCHES_PER_TRIP

    # 1. Probe: the main home route on each initial date picks the date
    probe = routes[0][0]
    for depart in departures:
        if not can_search():
            break
        search(depart, probe)
    probed = {depart: prices[(depart, probe)] for depart in departures if (depart, probe) in prices}
    anchor = min(probed, key=lambda day: (probed[day], day)) if probed else departures[0]
    near_ties = [day for day in departures if day != anchor and day in probed
                 and probed[day] <= probed[anchor] * NEAR_TIE_RATIO]

    # 2. Widen: every other route on the best date, in priority order
    budget_reached = False
    for pair, _ in routes[1:]:
        if (anchor, pair) in attempted:
            continue
        if not can_search():
            budget_reached = not blocked
            break
        search(anchor, pair)

    # 3. Extras while searches remain, chosen from what the searches found
    if not budget_reached and not blocked:
        for depart, pair in _extras(anchor, near_ties, start, end, prices, routes, kinds, airports):
            if (depart, pair) in attempted:
                continue
            if not can_search():
                break
            search(depart, pair)

    # A near miss is shown only when its live price beats the home airports.
    home_best = min((offer["total_amount"] for offer in offers_by_kind["home"]), default=None)
    near_best = min((offer["total_amount"] for offer in offers_by_kind["near_miss"]), default=None)
    near_shown = near_best is not None and (home_best is None or near_best < home_best)
    offers = offers_by_kind["home"] + offers_by_kind["alternative"] + (
        offers_by_kind["near_miss"] if near_shown else [])

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
