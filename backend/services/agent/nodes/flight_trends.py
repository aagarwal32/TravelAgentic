"""Offline historical fares and nearby alternatives for the resolved trip.

The node writes only flight_trends. It uses the completed location/fare services,
local airport provenance, and no live API. Route averages compare the trip's
quarters across all loaded years (annual fallback); charts retain seasonal gaps.
"""
from csv import Error as CSVError
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from db.database import SessionLocal
from scripts.ingest_locations import (DEFAULT_TABLE1A, MARKET_FIXES, RENAMED_CODES,
                                      read_table1a_markets)
from services.agent.state import (CandidateDates, FlightTrends, TripConstraints,
                                  TripState, empty_result, flight_skip_reason)
from services.fare_trends_service import (
    FARE_TYPE, QUARTER_MONTHS, SOURCE, FareCandidate, estimate_flight_cost,
    get_market_passenger_totals, get_price_trend_for_markets,
    get_quarterly_fares_for_markets, get_route_fare_for_markets,
    quarters_in_window, select_alternative, select_departure_dates,
)
from services.location_service import (LocationDataMissing, nearby_alternative_markets,
                                       place_label, search_airport_codes)


class AirportMetadataUnavailable(Exception):
    """Local evidence needed to distinguish search-eligible airports is unreadable."""


def read_verified_airport_markets(path: Path | None = None) -> dict[str, int]:
    """Reuse the loader's local Table 1a evidence, checked mappings and code aliases."""
    try:
        table1a = read_table1a_markets(DEFAULT_TABLE1A if path is None else path)
    except (OSError, ValueError, UnicodeError, CSVError) as exc:
        raise AirportMetadataUnavailable("local Table 1a airport metadata is unreadable") from exc
    if not table1a:
        raise AirportMetadataUnavailable("local Table 1a airport metadata is empty")
    mappings = {**MARKET_FIXES, **{code: value[0] for code, value in table1a.items()}}
    for local_code, dot_code in RENAMED_CODES.items():
        if dot_code in mappings:
            mappings[local_code] = mappings[dot_code]
    return mappings


def _missing_dates(c: TripConstraints) -> str | None:
    if c.trip_type is None:
        return "trip type is missing"
    if c.depart_date is not None:
        if c.trip_type == "round_trip" and c.return_date is None:
            return "round-trip return date or stay length is missing"
    elif c.flexible_start is None or c.flexible_end is None:
        return "departure date or complete departure window is missing"
    elif c.trip_type == "round_trip" and c.trip_nights is None:
        return "round-trip stay length is missing"
    return None


def _candidate_dates(c: TripConstraints, home_quarters: dict,
                     alternative_quarters: list[dict]) -> list[CandidateDates]:
    if c.depart_date is not None:
        return [{"depart_date": c.depart_date.isoformat(),
                 "return_date": c.return_date.isoformat() if c.trip_type == "round_trip" else None}]
    end = c.flexible_end
    # A window bounds departures, not returns. Only the date type's maximum can
    # shorten its feasible departure range; never subtract nights from the window.
    if c.trip_type == "round_trip":
        end = min(end, date.max - timedelta(days=c.trip_nights))
    if end < c.flexible_start:
        return []
    departures = select_departure_dates(c.flexible_start, end, home_quarters, alternative_quarters)
    return [{"depart_date": day.isoformat(),
             "return_date": (day + timedelta(days=c.trip_nights)).isoformat()
             if c.trip_type == "round_trip" else None}
            for day in departures]


def _money(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def _analyze(db: Session, c: TripConstraints, verified: dict[str, int]) -> FlightTrends:
    origin, destination = c.origin_place, c.destination_place
    origin_id, destination_id = origin.dot_market_id, destination.dot_market_id
    if c.depart_date is not None:
        end = c.return_date if c.trip_type == "round_trip" else c.depart_date
        quarters = quarters_in_window(c.depart_date, end)
    else:
        quarters = quarters_in_window(c.flexible_start, c.flexible_end)
    sizes = get_market_passenger_totals(db)
    home_fare = get_route_fare_for_markets(db, origin_id, destination_id, quarters)
    home_quarters = get_quarterly_fares_for_markets(db, origin_id, destination_id)
    home_trend = get_price_trend_for_markets(db, origin_id, destination_id)
    home_label = f"{place_label(origin)} → {place_label(destination)}"
    years = set(home_trend.get("years_covered", []))
    options = []
    series = [{"label": home_label,
               "values": [_money(home_quarters[q]) for q in QUARTER_MONTHS], "home": True}]
    seasonal_alternatives = []
    selected = {}
    for side, place in (("departure", origin), ("arrival", destination)):
        nearby = nearby_alternative_markets(db, place, sizes.passenger_totals, verified)
        candidates = [FareCandidate(
            market.market_id,
            get_route_fare_for_markets(
                db, market.market_id if side == "departure" else origin_id,
                destination_id if side == "departure" else market.market_id, quarters),
            market.distance_km,
        ) for market in nearby]
        choice = select_alternative(candidates, home_fare)
        selected[side] = choice
        if choice is None:
            continue
        a = choice.market_id if side == "departure" else origin_id
        b = destination_id if side == "departure" else choice.market_id
        trend = get_price_trend_for_markets(db, a, b)
        years.update(trend.get("years_covered", []))
        # Retain the city coordinates to order this market's airports by distance
        # from the user's city, while selecting only the alternative market.
        alternative_place = place.model_copy(update={"dot_market_id": choice.market_id, "airports": []})
        codes = search_airport_codes(db, alternative_place, verified)
        market_label = trend.get("origin" if side == "departure" else "destination",
                                 f"DOT market {choice.market_id}")
        route = (f"{market_label} → {place_label(destination)}" if side == "departure"
                 else f"{place_label(origin)} → {market_label}")
        options.append({"route": route, "side": side, "airports": codes,
                        "distance_km": round(choice.distance_km, 2),
                        "avg_fare": _money(choice.fare),
                        "savings_each_way": _money(home_fare - choice.fare) if home_fare is not None else None,
                        "suggested": True})
        seasonal = get_quarterly_fares_for_markets(db, a, b)
        seasonal_alternatives.append(seasonal)
        series.append({"label": f"{'From' if side == 'departure' else 'To'} {market_label} ({'/'.join(codes)})",
                       "values": [_money(seasonal[q]) for q in QUARTER_MONTHS], "home": False})
    candidates = _candidate_dates(c, home_quarters, seasonal_alternatives)
    if not candidates:
        return empty_result("empty", "no representable return dates within the departure window")
    cost = estimate_flight_cost(home_fare, c.travelers, c.trip_type)
    return {
        "status": "ok",
        "home": {"route": home_label, "avg_fare": _money(home_fare), "has_data": home_fare is not None},
        "options": options,
        "chart": {
            "x": [f"Q{q} ({months})" for q, months in QUARTER_MONTHS.items()],
            "series": series,
            "years": (str(min(years)) if len(years) == 1 else f"{min(years)}–{max(years)}")
            if years else "No fare history",
            "note": f"{SOURCE}; historical {FARE_TYPE}, not live prices. "
                    "Quarterly means across loaded years; null means no data. "
                    "Route comparisons and flight-only budget estimates use trip-quarter means, "
                    "with all-year fallback. Round trips estimate two each-way fares per traveler. "
                    "Distances are straight-line km; hotels and ground transport are excluded.",
        },
        "search_airports": {
            key: search_airport_codes(db, place, verified,
                                      selected[side].market_id if selected[side] is not None else None)
            for key, side, place in (("origin", "departure", origin),
                                     ("destination", "arrival", destination))
        },
        "candidate_dates": candidates,
        "budget_check": {"typical_trip_total": _money(cost),
                         "fits": None if cost is None or c.budget is None else cost <= c.budget},
    }


def flight_trends(state: TripState) -> dict:
    c = TripConstraints(**state["constraints"])
    skip = flight_skip_reason(c)
    if skip:
        return {"flight_trends": empty_result("skipped", skip)}
    missing = _missing_dates(c)
    if missing:
        return {"flight_trends": empty_result("empty", missing)}
    try:
        verified = read_verified_airport_markets()
        with SessionLocal() as db:
            result = _analyze(db, c, verified)
    except (LocationDataMissing, AirportMetadataUnavailable) as exc:
        result = empty_result("empty", str(exc))
    except SQLAlchemyError:
        # Keep database internals and connection strings out of graph state.
        result = empty_result("empty", "historical flight data is unavailable (database error)")
    return {"flight_trends": result}
