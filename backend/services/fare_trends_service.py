"""Historical airfare trends from the DOT Consumer Airfare Report, Table 6.

What the numbers mean (read this before showing them to a user):
- They are HISTORICAL averages, not live ticket prices. Live prices come from Duffel.
- Every fare is an average EACH-WAY fare in USD. A round trip is about 2x that.
- Table 6 is QUARTERLY (Q1 = Jan-Mar ... Q4 = Oct-Dec), never monthly.
- Table 6 is directionless: one row per city pair per quarter, and either city can
  be city1. Chicago -> Denver and Denver -> Chicago are the same route.
- Small markets (and many Hawaii / Alaska routes) are not in Table 6 at all.

Every function takes city names as the user said them ("Chicago", "denver") and
returns a plain dict that a LangGraph tool can hand to the LLM as is. When there is
no data, or a city is unknown or ambiguous, the result is
    {"no_history": True, "reason": "..."}
instead of an exception, so the trip search can continue with live prices only.
All aggregation happens in SQL; dollar amounts are rounded to cents.
"""

from datetime import date
from typing import NamedTuple

from sqlalchemy import and_, func, or_, select, union
from sqlalchemy.orm import Session

from models.airfare import AirfareQuarter

SOURCE = "DOT Consumer Airfare Report, Table 6"
FARE_TYPE = "average each-way, USD"
QUARTER_MONTHS = {1: "Jan-Mar", 2: "Apr-Jun", 3: "Jul-Sep", 4: "Oct-Dec"}
MAX_LISTED_MATCHES = 5  # how many candidate markets an "ambiguous city" reason names


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
    """Every DOT city market whose name starts with `name` (case-insensitive), as {id: name}."""
    prefix = (name or "").strip()
    if not prefix:
        return {}
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
    (Metropolitan Area)". Returns None when nothing matches, and also when more
    than one market matches ("Portland" is both "Portland, ME" and "Portland, OR"):
    we never guess between cities.
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
    if missing:
        return missing
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
    if missing:
        return missing
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
    if missing:
        return missing
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
