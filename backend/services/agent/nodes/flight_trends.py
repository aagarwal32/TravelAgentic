"""Offline historical fares and nearby alternatives for the resolved trip.

The node writes only flight_trends. It uses the completed location/fare services,
local airport provenance, and no live API. Route averages compare the trip's
quarters across all loaded years (annual fallback); charts retain seasonal gaps.
"""
from datetime import date, timedelta

from sqlalchemy import inspect, select
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from db.database import SessionLocal
from models.location import Airport
from services.agent.state import (CandidateDates, FlightTrends, TripConstraints,
                                  TripState, empty_result, flight_skip_reason)
from services.fare_trends_service import (
    FARE_TYPE, QUARTER_MONTHS, SOURCE, FareCandidate, estimate_flight_cost,
    get_market_passenger_totals, get_price_trend_for_markets,
    get_quarterly_fares_for_markets, get_route_fare_for_markets,
    quarters_in_window, select_alternative, select_departure_dates, select_near_miss,
)
from services.location_service import (NO_LOCATION_DATA, LocationDataMissing, market_airport_codes,
                                       nearby_alternative_markets, place_label, search_airport_codes)

# Market links flight search can trust: DOT's own Table 1a and the hand-checked fixes,
# not a Table 6 name match (which maps Teterboro, TEB, into a market).
VERIFIED_MARKET_SOURCES = ("table1a", "fixed")
RELOAD_LOCATIONS = "rerun python -m scripts.ingest_locations"


class AirportMetadataUnavailable(Exception):
    """Local evidence needed to distinguish search-eligible airports is unreadable."""


def read_verified_airport_markets(db: Session) -> dict[str, int]:
    """{IATA: DOT market} for airports whose market came from Table 1a or a checked fix.

    ingest_locations records how each airport was linked (Airport.market_source), so
    the node never reads the Table 1a download at runtime.
    """
    try:
        rows = db.execute(select(Airport.iata, Airport.dot_market_id).where(
            Airport.market_source.in_(VERIFIED_MARKET_SOURCES),
            Airport.dot_market_id.is_not(None))).all()
    except OperationalError:
        db.rollback()
        inspector = inspect(db.get_bind())
        if not inspector.has_table(Airport.__tablename__):
            raise LocationDataMissing(NO_LOCATION_DATA) from None
        columns = {column["name"] for column in inspector.get_columns(Airport.__tablename__)}
        if "market_source" not in columns:
            raise AirportMetadataUnavailable(
                f"the airports table predates airport market sources: {RELOAD_LOCATIONS}") from None
        raise
    if not rows:
        raise AirportMetadataUnavailable(f"no airport has a verified DOT market: {RELOAD_LOCATIONS}")
    return dict(rows)


def _missing_dates(c: TripConstraints) -> str | None:
    if c.trip_type is None:
        return "trip type is missing"
    if c.depart_date is not None:
        if c.trip_type == "round_trip" and c.return_date is None:
            return "round-trip return date or stay length is missing"
    elif c.flexible_start is None or c.flexible_end is None:
        return "departure date or complete departure window is missing"
    elif c.trip_type == "round_trip" and c.trip_nights is None and c.return_date is None:
        return "round-trip return date or stay length is missing"
    return None


def _candidate_dates(c: TripConstraints, home_quarters: dict,
                     alternative_quarters: list[dict]) -> list[CandidateDates]:
    if c.depart_date is not None:
        return [{"depart_date": c.depart_date.isoformat(),
                 "return_date": c.return_date.isoformat() if c.trip_type == "round_trip" else None}]
    start, end = c.flexible_start, c.flexible_end
    if c.trip_type == "round_trip" and c.return_date is not None:
        if c.trip_nights is not None:
            # A fixed return and nights allow only one departure (validated to be in the window)
            depart = c.return_date - timedelta(days=c.trip_nights)
            return [{"depart_date": depart.isoformat(), "return_date": c.return_date.isoformat()}]
        # A fixed return alone: leave inside the window, at least a day before the return
        # (on the return day only if the window starts then), and all come back that day
        end = max(start, min(end, c.return_date - timedelta(days=1)))
        departures = select_departure_dates(start, end, home_quarters, alternative_quarters)
        return [{"depart_date": day.isoformat(), "return_date": c.return_date.isoformat()}
                for day in departures]
    # A window bounds departures, not returns. Only the date type's maximum can
    # shorten its feasible departure range; never subtract nights from the window.
    if c.trip_type == "round_trip":
        end = min(end, date.max - timedelta(days=c.trip_nights))
    if end < start:
        return []
    departures = select_departure_dates(start, end, home_quarters, alternative_quarters)
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
    selected, near_misses = {}, {}
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
        near = select_near_miss(candidates, home_fare, c.travelers, c.trip_type,
                                choice.market_id if choice is not None else None)
        selected[side], near_misses[side] = choice, near
        # The alternative is suggested and charted. A near miss is only searched: the
        # reply mentions it when live prices beat the home airports.
        for picked, suggested in ((choice, True), (near, False)):
            if picked is None:
                continue
            a = picked.market_id if side == "departure" else origin_id
            b = destination_id if side == "departure" else picked.market_id
            trend = get_price_trend_for_markets(db, a, b)
            # Main airport first, ordered for the user's city.
            codes = market_airport_codes(db, place, verified, picked.market_id)
            market_label = trend.get("origin" if side == "departure" else "destination",
                                     f"DOT market {picked.market_id}")
            route = (f"{market_label} → {place_label(destination)}" if side == "departure"
                     else f"{place_label(origin)} → {market_label}")
            options.append({"route": route, "side": side, "airports": codes,
                            "distance_km": round(picked.distance_km, 2),
                            "avg_fare": _money(picked.fare),
                            "savings_each_way": _money(home_fare - picked.fare) if home_fare is not None else None,
                            "suggested": suggested})
            if not suggested:
                continue
            years.update(trend.get("years_covered", []))
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
            key: search_airport_codes(
                db, place, verified,
                selected[side].market_id if selected[side] is not None else None,
                near_misses[side].market_id if near_misses[side] is not None else None)
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
        with SessionLocal() as db:
            result = _analyze(db, c, read_verified_airport_markets(db))
    except (LocationDataMissing, AirportMetadataUnavailable) as exc:
        result = empty_result("empty", str(exc))
    except SQLAlchemyError:
        # Keep database internals and connection strings out of graph state.
        result = empty_result("empty", "historical flight data is unavailable (database error)")
    return {"flight_trends": result}
