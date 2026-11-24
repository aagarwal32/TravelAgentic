"""Historical airfare trends from the DOT Consumer Airfare Report, Table 6.

What the numbers mean (read this before showing them to a user):
- They are HISTORICAL averages, not live ticket prices. Live prices come from Duffel.
- Every fare is an average EACH-WAY fare in USD. A round trip is about 2x that.
- Table 6 is QUARTERLY (Q1 = Jan-Mar ... Q4 = Oct-Dec), never monthly.
- Table 6 is directionless: one row per city pair per quarter, and either city can
  be city1. Chicago -> Denver and Denver -> Chicago are the same route.
- Small markets (and many Hawaii / Alaska routes) are not in Table 6 at all.

The agent's flight_trends node calls the *_for_markets functions: they
take the DOT city market ids of the places resolved during intake (Place.dot_market_id,
see services/location_service.py), so they never interpret a city name. The original functions take city names as the
user said them ("Chicago", "denver"); they are thin wrappers kept for testing by hand
and give the same results. These presentation functions return plain dicts. When
there is no data, a market id is missing, or a city is
unknown or ambiguous, the result is
    {"no_history": True, "reason": "..."}
instead of an exception, so the trip search can continue with live prices only.
The original presentation functions aggregate in SQL and round dollars to cents.
The Issue #34 comparison helpers below retain full precision until presentation.
"""

from datetime import date, timedelta
from math import isfinite
from collections.abc import Iterable, Mapping
from typing import NamedTuple

from sqlalchemy import and_, case, func, or_, select, tuple_, union, union_all
from sqlalchemy.orm import Session

from models.airfare import AirfareQuarter

SOURCE = "DOT Consumer Airfare Report, Table 6"
FARE_TYPE = "average each-way, USD"
QUARTER_MONTHS = {1: "Jan-Mar", 2: "Apr-Jun", 3: "Jul-Sep", 4: "Oct-Dec"}
MAX_LISTED_MATCHES = 5  # how many candidate markets an "ambiguous city" reason names
MARKET_SIZE_QUARTERS = 4
ALTERNATIVE_MAX_HOME_FARE_RATIO = 0.80
CLOSER_MIN_CHEAPEST_FARE_RATIO = 0.85
CLOSER_MAX_DISTANCE_RATIO = 0.80


class MarketSizeSnapshot(NamedTuple):
    """One shared coverage window and summed Table 6 passengers/day per market."""

    quarters: tuple[tuple[int, int], ...]  # newest first; fewer than four if necessary
    passenger_totals: dict[int, float]


class FareCandidate(NamedTuple):
    """Service-level selection input, not the planning graph's RouteOption contract."""

    market_id: int
    fare: float | None
    distance_km: float


def get_market_passenger_totals(db: Session) -> MarketSizeSnapshot:
    """Compare market sizes over the dataset's same newest four loaded quarters.

    Table 6 passengers are passengers/day, so these sums are a relative size index,
    not annual unique travelers. Missing market-quarter rows contribute nothing;
    an older market never gets its own older four-quarter window. Airports are
    never joined, so two airports in Chicago cannot double Chicago's passengers.

    A route is directionless. Normalize its endpoint ids and group once per year
    and quarter before counting each endpoint. Defensive duplicate representations
    contribute MAX(passengers), not a sum; genuine Table 6 has one pair-quarter row.
    Conflicting duplicate values therefore need a source-data audit, not addition.
    Database errors propagate for the flight_trends node to map to an empty status.
    """
    quarters = tuple(tuple(row) for row in db.execute(
        select(AirfareQuarter.year, AirfareQuarter.quarter).distinct()
        .order_by(AirfareQuarter.year.desc(), AirfareQuarter.quarter.desc())
        .limit(MARKET_SIZE_QUARTERS)
    ))
    if not quarters:
        return MarketSizeSnapshot((), {})
    a, b = AirfareQuarter.citymarketid_1, AirfareQuarter.citymarketid_2
    first = case((a <= b, a), else_=b)
    second = case((a <= b, b), else_=a)
    routes = (
        select(first.label("first"), second.label("second"),
               func.max(AirfareQuarter.passengers).label("passengers"))
        .where(tuple_(AirfareQuarter.year, AirfareQuarter.quarter).in_(quarters))
        .group_by(first, second, AirfareQuarter.year, AirfareQuarter.quarter)
        .cte("market_routes")
    )
    endpoints = union_all(
        select(routes.c.first.label("market_id"), routes.c.passengers),
        select(routes.c.second.label("market_id"), routes.c.passengers)
        .where(routes.c.first != routes.c.second),
    ).subquery()
    totals = db.execute(
        select(endpoints.c.market_id, func.sum(endpoints.c.passengers))
        .group_by(endpoints.c.market_id).order_by(endpoints.c.market_id)
    )
    return MarketSizeSnapshot(quarters, dict(totals.all()))


def _usable_fares(origin_market_id: int | None, dest_market_id: int | None):
    """The loader accepts finite positive fares; reject unusable hand-loaded rows too."""
    return and_(
        _route_filter(origin_market_id, dest_market_id),
        AirfareQuarter.fare > 0,
        AirfareQuarter.fare < float("inf"),
    )


def get_route_fare_for_markets(db: Session, origin_market_id: int | None,
                              dest_market_id: int | None,
                              quarters: Iterable[int] | None = None) -> float | None:
    """Unrounded mean each-way fare over usable rows in the requested quarters.

    Like get_price_trend_for_markets, all available years have equal row weight;
    this is not passenger-weighted and does not average quarter means together.
    Fall back to the all-year route mean ONLY if no requested-quarter fare exists.
    None (or an empty iterable) requests the all-year average. The caller supplies
    trip quarters using the existing date/window semantics; no dates are invented.
    """
    if origin_market_id is None or dest_market_id is None or origin_market_id == dest_market_id:
        return None
    selected = tuple(quarters) if quarters is not None else ()
    if any(q not in QUARTER_MONTHS for q in selected):
        raise ValueError("quarters must be between 1 and 4")
    match = _usable_fares(origin_market_id, dest_market_id)
    query = select(func.avg(AirfareQuarter.fare)).where(match)
    if selected:
        fare = db.scalar(query.where(AirfareQuarter.quarter.in_(selected)))
        if fare is not None:
            return fare
    return db.scalar(query)


def get_quarterly_fares_for_markets(db: Session, origin_market_id: int | None,
                                   dest_market_id: int | None) -> dict[int, float | None]:
    """Raw Q1-Q4 means over all years, matching by_quarter with explicit None gaps.

    Unlike selection fares this never fills a missing quarter with the annual mean.
    Round values only when adapting them to the chart contract.
    """
    fares = dict.fromkeys(QUARTER_MONTHS)
    if origin_market_id is None or dest_market_id is None or origin_market_id == dest_market_id:
        return fares
    fares.update(db.execute(
        select(AirfareQuarter.quarter, func.avg(AirfareQuarter.fare))
        .where(_usable_fares(origin_market_id, dest_market_id))
        .group_by(AirfareQuarter.quarter)
    ).all())
    return fares


def select_alternative(candidates: Iterable[FareCandidate],
                       home_fare: float | None) -> FareCandidate | None:
    """Pick one candidate using the original home fare and original cheapest route.

    Savings must reach 20%, inclusive. When the home fare is unavailable, any usable
    candidate fare qualifies. A closer candidate may replace the original cheapest
    only with a 20% distance reduction and cheapest >= 85% of its fare: $100 may
    replace $85, but not $84. This is deliberately not a 15% markup comparison.
    Both tests always use the original cheapest, never a successive replacement.
    Equal cheapest fares are resolved by stable market id before testing distance;
    eligible replacements are ordered by distance, fare, then stable market id.
    """
    home_available = home_fare is not None and isfinite(home_fare) and home_fare > 0
    qualifying = []
    for candidate in candidates:
        if not isfinite(candidate.distance_km) or candidate.distance_km < 0:
            raise ValueError("candidate distance must be finite and nonnegative")
        if candidate.fare is None or not isfinite(candidate.fare) or candidate.fare <= 0:
            continue
        if not home_available or candidate.fare <= ALTERNATIVE_MAX_HOME_FARE_RATIO * home_fare:
            qualifying.append(candidate)
    if not qualifying:
        return None
    cheapest = min(qualifying, key=lambda c: (c.fare, c.market_id))
    closer = [c for c in qualifying
              if cheapest.fare >= CLOSER_MIN_CHEAPEST_FARE_RATIO * c.fare
              and c.distance_km <= CLOSER_MAX_DISTANCE_RATIO * cheapest.distance_km]
    return min(closer, key=lambda c: (c.distance_km, c.fare, c.market_id)) if closer else cheapest


def estimate_flight_cost(each_way_fare: float | None, travelers: int | None,
                         trip_type: str | None) -> float | None:
    """Historical flight-only estimate; a round trip is estimated as two each-way fares.

    The flight_trends node uses the home route and compares this amount with the
    whole trip budget. Such a comparison says
    nothing about whether hotels or other trip costs also fit. Missing/invalid
    required inputs yield None, never guessed travelers or a guessed trip type.
    """
    directions = {"one_way": 1, "round_trip": 2}.get(trip_type)
    if (each_way_fare is None or not isfinite(each_way_fare) or each_way_fare <= 0
            or type(travelers) is not int or not 1 <= travelers <= 9
            or directions is None):
        return None
    estimate = each_way_fare * travelers * directions
    return estimate if isfinite(estimate) else None


def select_departure_dates(start: date, end: date,
                           home_quarter_fares: Mapping[int, float | None],
                           alternative_quarter_fares: Iterable[Mapping[int, float | None]] = ()) -> list[date]:
    """Sample the cheapest calendar-quarter segment of feasible departure dates.

    The caller must derive feasible inclusive bounds using the actual constraints
    contract. Flexible bounds describe departures, so returns may be later than
    the window. The node supplies return dates using the validated stay length;
    this helper only selects departures.
    Home seasonality wins whenever any home quarter in the window has a fare.
    Otherwise use each segment's lowest seasonal fare among selected alternatives.
    Missing fares never rank as zero or receive all-year fallback. With no fares,
    choose the earliest segment; price ties also choose the earliest segment.
    Its first, lower-middle, and last dates are deduplicated and chronological.
    """
    if end < start:
        raise ValueError("end date is before start date")
    segments = []
    cursor = start
    while True:
        quarter = _quarter_of(cursor)
        if cursor.year == date.max.year and quarter == 4:
            segment_end = end
        else:
            next_quarter = (date(cursor.year + 1, 1, 1) if quarter == 4
                            else date(cursor.year, quarter * 3 + 1, 1))
            segment_end = min(end, next_quarter - timedelta(days=1))
        segments.append((cursor, segment_end, quarter))
        if segment_end == end:
            break
        cursor = next_quarter

    def usable(fare):
        return fare is not None and isfinite(fare) and fare > 0

    use_home = any(usable(home_quarter_fares.get(q)) for _, _, q in segments)
    alternatives = list(alternative_quarter_fares)
    ranked = []
    for first, last, quarter in segments:
        fares = ([home_quarter_fares.get(quarter)] if use_home
                 else [fares.get(quarter) for fares in alternatives])
        available = [fare for fare in fares if usable(fare)]
        if available:
            ranked.append((min(available), first, last))
    if ranked:
        _, first, last = min(ranked)
    else:
        first, last, _ = segments[0]
    middle = first + timedelta(days=(last - first).days // 2)
    return sorted({first, middle, last})


class _Route(NamedTuple):
    origin_id: int
    origin: str        # DOT market name, e.g. "Chicago, IL"
    dest_id: int
    destination: str


def _no_history(reason: str) -> dict:
    return {"no_history": True, "reason": reason}


def _money(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def _share(value: float | None) -> float | None:
    """Market share as a 0-1 fraction, rounded to 0.01 percentage points."""
    return None if value is None else round(value, 4)


def _match_markets(db: Session, name: str) -> dict[int, str]:
    """DOT city markets whose name starts with `name` (case-insensitive), as {id: name}.

    When several match, a market whose city part (before the comma) is exactly
    `name` wins: "Charlotte" means "Charlotte, NC", not "Charlottesville, VA", and
    "Jacksonville" means "Jacksonville, FL", not "Jacksonville/Camp Lejeune, NC".
    Two exact matches ("Portland" -> ME and OR) stay ambiguous.
    """
    prefix = (name or "").strip()
    if not prefix:
        return {}
    matches = _prefix_matches(db, prefix)
    if len(matches) > 1:
        exact = {market_id: market for market_id, market in matches.items()
                 if market.split(",")[0].strip().casefold() == prefix.casefold()}
        if len(exact) == 1:
            return exact
    return matches


def _prefix_matches(db: Session, prefix: str) -> dict[int, str]:
    """Every DOT city market whose name starts with `prefix` (case-insensitive)."""
    # A market can show up in either column, so search both sides of the table.
    # autoescape makes "%" and "_" in the user's text literal, not wildcards.
    query = union(
        select(AirfareQuarter.citymarketid_1, AirfareQuarter.city1)
        .where(AirfareQuarter.city1.istartswith(prefix, autoescape=True)),
        select(AirfareQuarter.citymarketid_2, AirfareQuarter.city2)
        .where(AirfareQuarter.city2.istartswith(prefix, autoescape=True)),
    )
    return {market_id: market for market_id, market in db.execute(query)}


def find_city_market(db: Session, name: str) -> int | None:
    """Return the DOT city market id for a city name, or None.

    Matching is a case-insensitive "starts with" on the DOT market name, so
    "chicago" finds "Chicago, IL" and "new york" finds "New York City, NY
    (Metropolitan Area)". An exact city name beats longer names that start with
    it ("Charlotte" -> "Charlotte, NC", not "Charlottesville, VA"). Returns None
    when nothing matches, and also when more than one market still matches
    ("Portland" is both "Portland, ME" and "Portland, OR"): we never guess between cities.
    """
    matches = _match_markets(db, name)
    return next(iter(matches)) if len(matches) == 1 else None


def _resolve_route(db: Session, origin_city: str, dest_city: str) -> tuple[_Route | None, dict | None]:
    """Resolve both cities. Returns (route, None), or (None, a no_history result)."""
    resolved = []
    for name in (origin_city, dest_city):
        matches = _match_markets(db, name)
        if not matches:
            return None, _no_history(f"No DOT city market matches {name!r}.")
        if len(matches) > 1:
            names = sorted(matches.values())
            listed = "; ".join(names[:MAX_LISTED_MATCHES]) + ("; ..." if len(names) > MAX_LISTED_MATCHES else "")
            return None, _no_history(
                f"{name!r} matches several DOT city markets ({listed}); a more specific city name is needed."
            )
        resolved.extend(next(iter(matches.items())))
    return _Route(*resolved), None


def _market_names(db: Session, market_ids: set[int]) -> dict[int, str]:
    """DOT market names for the given ids, as {id: name}; ids not in Table 6 are left out."""
    query = union(
        select(AirfareQuarter.citymarketid_1, AirfareQuarter.city1)
        .where(AirfareQuarter.citymarketid_1.in_(market_ids)),
        select(AirfareQuarter.citymarketid_2, AirfareQuarter.city2)
        .where(AirfareQuarter.citymarketid_2.in_(market_ids)),
    )
    return {market_id: market for market_id, market in db.execute(query)}


def _market_route(db: Session, origin_market_id: int | None,
                  dest_market_id: int | None) -> tuple[_Route | None, dict | None]:
    """The route between two DOT market ids. Returns (route, None), or (None, a no_history result)."""
    for side, market_id in (("origin", origin_market_id), ("destination", dest_market_id)):
        if market_id is None:  # no airport with a DOT market near the place (e.g. Hawaii)
            return None, _no_history(f"The {side} has no DOT city market (no airport with DOT data nearby).")
    names = _market_names(db, {origin_market_id, dest_market_id})
    for market_id in (origin_market_id, dest_market_id):
        if market_id not in names:
            return None, _no_history(f"DOT city market {market_id} is not in Table 6.")
    if origin_market_id == dest_market_id:  # e.g. Fort Worth -> Dallas
        return None, _no_history(
            f"Both places are in the same DOT city market ({names[origin_market_id]}), "
            "so there is no airfare history between them.")
    return _Route(origin_market_id, names[origin_market_id], dest_market_id, names[dest_market_id]), None


def _route_filter(a: int, b: int):
    """SQL condition for the city pair (a, b) stored in either orientation."""
    return or_(
        and_(AirfareQuarter.citymarketid_1 == a, AirfareQuarter.citymarketid_2 == b),
        and_(AirfareQuarter.citymarketid_1 == b, AirfareQuarter.citymarketid_2 == a),
    )


def _header(route: _Route) -> dict:
    """Fields every successful result starts with."""
    return {
        "origin": route.origin,
        "destination": route.destination,
        "source": SOURCE,
        "fare_type": FARE_TYPE,
    }


def _no_route_history(route: _Route, detail: str = "") -> dict:
    return _no_history(f"No DOT Table 6 history for {route.origin} - {route.destination}{detail}.")


def _newest_quarter(db: Session, match):
    """(year, quarter) of the newest row matching `match`, or None if there are no rows."""
    return db.execute(
        select(AirfareQuarter.year, AirfareQuarter.quarter)
        .where(match)
        .order_by(AirfareQuarter.year.desc(), AirfareQuarter.quarter.desc())
        .limit(1)
    ).first()


def get_price_trend(db: Session, origin_city: str, dest_city: str) -> dict:
    """Historical each-way fares for a route, by quarter of the year.

    Each Table 6 row is the average fare paid on the route in one quarter of one
    year, so a quarter's figures summarize that quarter across every loaded year
    (example: real Chicago - Denver data, 2022 Q1 to 2026 Q1):
        {
            "origin": "Chicago, IL", "destination": "Denver, CO",
            "source": "DOT Consumer Airfare Report, Table 6",
            "fare_type": "average each-way, USD",
            "avg_fare": 193.07,               # typical fare: all quarters, all years
            "years_covered": [2022, 2023, 2024, 2025, 2026],
            "newest_quarter": {"year": 2026, "quarter": 1},
            "by_quarter": [
                {"quarter": 1, "months": "Jan-Mar",
                 "avg_fare": 186.38,          # mean of the yearly averages for Q1
                 "min_fare": 160.88, "max_fare": 210.12,  # cheapest / priciest year's average, not single tickets
                 "avg_lowest_carrier_fare": 162.38,       # mean fare of the lowest-fare airline (fare_low)
                 "data_points": 5},           # rows (years) behind these numbers
                ...
            ],
        }
    Quarters with no data are left out of by_quarter. Unknown or ambiguous cities
    and routes not in Table 6 return {"no_history": True, "reason": ...}.
    """
    route, missing = _resolve_route(db, origin_city, dest_city)
    return missing or _price_trend(db, route)


def get_price_trend_for_markets(db: Session, origin_market_id: int | None,
                                dest_market_id: int | None) -> dict:
    """get_price_trend for two DOT city market ids (origin_place/destination_place.dot_market_id).

    A missing id (None), an id not in Table 6, or two places in the same market return
    {"no_history": True, "reason": ...}.
    """
    route, missing = _market_route(db, origin_market_id, dest_market_id)
    return missing or _price_trend(db, route)


def _price_trend(db: Session, route: _Route) -> dict:
    match = _route_filter(route.origin_id, route.dest_id)

    quarters = db.execute(
        select(
            AirfareQuarter.quarter,
            func.avg(AirfareQuarter.fare).label("avg_fare"),
            func.min(AirfareQuarter.fare).label("min_fare"),
            func.max(AirfareQuarter.fare).label("max_fare"),
            func.avg(AirfareQuarter.fare_low).label("avg_fare_low"),  # AVG skips missing values
            func.count().label("data_points"),
        )
        .where(match)
        .group_by(AirfareQuarter.quarter)
        .order_by(AirfareQuarter.quarter)
    ).all()
    if not quarters:
        return _no_route_history(route)

    years = db.scalars(
        select(AirfareQuarter.year).where(match).distinct().order_by(AirfareQuarter.year)
    ).all()
    newest = _newest_quarter(db, match)
    return {
        **_header(route),
        "avg_fare": _money(db.scalar(select(func.avg(AirfareQuarter.fare)).where(match))),
        "years_covered": list(years),
        "newest_quarter": {"year": newest.year, "quarter": newest.quarter},
        "by_quarter": [
            {
                "quarter": row.quarter,
                "months": QUARTER_MONTHS[row.quarter],
                "avg_fare": _money(row.avg_fare),
                "min_fare": _money(row.min_fare),
                "max_fare": _money(row.max_fare),
                "avg_lowest_carrier_fare": _money(row.avg_fare_low),
                "data_points": row.data_points,
            }
            for row in quarters
        ],
    }


def get_cheapest_quarters(db: Session, origin_city: str, dest_city: str,
                          quarters: list[int] | None = None) -> dict:
    """Quarters of the year ranked from cheapest to most expensive for a route.

    The ranking uses the historical average each-way fare of each quarter across
    every loaded year. `quarters` (e.g. [1, 2], see quarters_in_window) limits the
    ranking to those quarters; None ranks all four.
        {
            "origin": ..., "destination": ..., "source": ..., "fare_type": ...,
            "ranking": [   # cheapest first; real Chicago - Denver values
                {"quarter": 1, "months": "Jan-Mar", "avg_fare": 186.38, "data_points": 5},
                {"quarter": 3, "months": "Jul-Sep", "avg_fare": 193.1, "data_points": 4},
                ...
            ],
        }
    Returns {"no_history": True, "reason": ...} when the cities cannot be resolved
    or there is no data for the route (in the requested quarters).
    """
    route, missing = _resolve_route(db, origin_city, dest_city)
    return missing or _cheapest_quarters(db, route, quarters)


def get_cheapest_quarters_for_markets(db: Session, origin_market_id: int | None, dest_market_id: int | None,
                                      quarters: list[int] | None = None) -> dict:
    """get_cheapest_quarters for two DOT city market ids (see get_price_trend_for_markets)."""
    route, missing = _market_route(db, origin_market_id, dest_market_id)
    return missing or _cheapest_quarters(db, route, quarters)


def _cheapest_quarters(db: Session, route: _Route, quarters: list[int] | None) -> dict:
    match = _route_filter(route.origin_id, route.dest_id)
    if quarters is not None:
        quarters = list(quarters)
        match = and_(match, AirfareQuarter.quarter.in_(quarters))

    avg_fare = func.avg(AirfareQuarter.fare).label("avg_fare")
    rows = db.execute(
        select(AirfareQuarter.quarter, avg_fare, func.count().label("data_points"))
        .where(match)
        .group_by(AirfareQuarter.quarter)
        .order_by(avg_fare, AirfareQuarter.quarter)  # quarter breaks ties so the order is stable
    ).all()
    if not rows:
        detail = "" if quarters is None else f" in quarter(s) {sorted(quarters)}"
        return _no_route_history(route, detail)

    return {
        **_header(route),
        "ranking": [
            {
                "quarter": row.quarter,
                "months": QUARTER_MONTHS[row.quarter],
                "avg_fare": _money(row.avg_fare),
                "data_points": row.data_points,
            }
            for row in rows
        ],
    }


def get_carrier_baseline(db: Session, origin_city: str, dest_city: str) -> dict:
    """The largest airline and the lowest-fare airline on a route, newest quarter only.

    Airlines change between quarters, so this reads only the newest year and
    quarter loaded for the route rather than averaging old ones (example: real
    Chicago - Denver data):
        {
            "origin": ..., "destination": ..., "source": ..., "fare_type": ...,
            "year": 2026, "quarter": 1, "months": "Jan-Mar",
            "largest_carrier":     {"airline": "UA", "market_share": 0.5175, "avg_fare": 236.79},
            "lowest_fare_carrier": {"airline": "WN", "market_share": 0.2613, "avg_fare": 187.59},
        }
    airline is the DOT two-letter carrier code; market_share is a 0-1 fraction of
    the route's passengers; avg_fare is that airline's average each-way fare.
    DOT sometimes leaves a carrier blank, so any of these values may be None.
    Returns {"no_history": True, "reason": ...} when there is no data.
    """
    route, missing = _resolve_route(db, origin_city, dest_city)
    return missing or _carrier_baseline(db, route)


def get_carrier_baseline_for_markets(db: Session, origin_market_id: int | None,
                                     dest_market_id: int | None) -> dict:
    """get_carrier_baseline for two DOT city market ids (see get_price_trend_for_markets)."""
    route, missing = _market_route(db, origin_market_id, dest_market_id)
    return missing or _carrier_baseline(db, route)


def _carrier_baseline(db: Session, route: _Route) -> dict:
    match = _route_filter(route.origin_id, route.dest_id)

    newest = _newest_quarter(db, match)
    if newest is None:
        return _no_route_history(route)
    in_newest = and_(match, AirfareQuarter.year == newest.year, AirfareQuarter.quarter == newest.quarter)

    # Table 6 has one row per route and quarter; ordering keeps the pick right even if there were more.
    largest = db.execute(
        select(AirfareQuarter.carrier_lg, AirfareQuarter.large_ms, AirfareQuarter.fare_lg)
        .where(in_newest)
        .order_by(AirfareQuarter.large_ms.desc().nulls_last())
        .limit(1)
    ).one()
    lowest = db.execute(
        select(AirfareQuarter.carrier_low, AirfareQuarter.lf_ms, AirfareQuarter.fare_low)
        .where(in_newest)
        .order_by(AirfareQuarter.fare_low.asc().nulls_last())
        .limit(1)
    ).one()

    return {
        **_header(route),
        "year": newest.year,
        "quarter": newest.quarter,
        "months": QUARTER_MONTHS[newest.quarter],
        "largest_carrier": {
            "airline": largest.carrier_lg,
            "market_share": _share(largest.large_ms),
            "avg_fare": _money(largest.fare_lg),
        },
        "lowest_fare_carrier": {
            "airline": lowest.carrier_low,
            "market_share": _share(lowest.lf_ms),
            "avg_fare": _money(lowest.fare_low),
        },
    }


def _quarter_of(day: date) -> int:
    return (day.month - 1) // 3 + 1


def quarters_in_window(start: date, end: date) -> list[int]:
    """Calendar quarters (1-4) touched by the dates start..end, inclusive.

    In travel order, without repeats, so a window that crosses New Year keeps its
    order: Feb 10 - May 5 -> [1, 2]; Nov 20 - Jan 5 -> [4, 1]. A window of a year
    or more touches all four quarters. Raises ValueError if end is before start.
    """
    if end < start:
        raise ValueError(f"end date {end} is before start date {start}")
    year, quarter = start.year, _quarter_of(start)
    last = (end.year, _quarter_of(end))
    touched = []
    # Step one quarter at a time; four steps already cover every quarter.
    while (year, quarter) <= last and len(touched) < 4:
        touched.append(quarter)
        year, quarter = (year + 1, 1) if quarter == 4 else (year, quarter + 1)
    return touched
