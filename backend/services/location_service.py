"""Resolve the cities a user types into exact US places, using only our own database.

Why this exists: intake (services/agent/nodes/extract.py) resolves origin and
destination ONCE and saves each result as a Place in TripConstraints. Hotels, fare
trends and (later) flights all read that saved place, so they always agree on which
"Greenville" the trip is about, and none of them interprets a city name again.

How it works: plain lookups in two tables loaded by scripts/ingest_locations.py. There
are no AI calls and no API calls, so a lookup is free, fast and gives the same answer
every time.
- us_places: US cities, towns and census-designated places from the Census Bureau
  (2024 Gazetteer + ACS 2020-2024 population), with city-center coordinates from the
  USGS GNIS where it has them. Public domain.
- airports: US airports with scheduled flights from OurAirports (public domain), each
  linked to its DOT city market, the ids DOT Table 6 uses.

Matching is always on the WHOLE city name, never a partial one: "Charlotte" must not
find "Charlottesville", and a typo should show the "couldn't find that city" card
instead of a different city that happens to start with the same letters.
"""

import re
import unicodedata
from math import asin, cos, radians, sin, sqrt

from sqlalchemy import inspect, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from models.location import Airport, USPlace
from services.agent.state import Place

DEFAULT_RADIUS_KM = 120   # how far a "nearby" airport can be (straight line)
MAX_CHOICES = 5           # most places shown on a "Which Greenville?" card
NO_LOCATION_DATA = ("The location tables (us_places, airports) are missing or empty. Load them "
                    "from backend/ with: python -m scripts.ingest_locations (see docs/data.md)")

# Nicknames only; keep this short. Spelling variants (St./Saint, case, spaces) are
# handled by normalize_name, and a state can always be typed after a comma.
ALIASES = {
    "nyc": ("New York", "NY"),
    "new york city": ("New York", "NY"),  # the Census name is "New York"
    "la": ("Los Angeles", "CA"),
    "sf": ("San Francisco", "CA"),
    "dc": ("Washington", "DC"),
    "philly": ("Philadelphia", "PA"),
    "vegas": ("Las Vegas", "NV"),
    "nola": ("New Orleans", "LA"),
}

# The 50 states and DC (the app is US-only; territories have no DOT Table 6 markets).
STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}

# Short forms that mean the same word in a city name: "St. Louis" = "Saint Louis".
_SPELLINGS = {"st": "saint", "ste": "sainte", "ft": "fort", "mt": "mount"}
# ", USA" and similar at the end of the text: the app is US-only anyway.
_COUNTRY = re.compile(r"[\s,]+(usa|us|u\.s\.a\.?|u\.s\.|united states( of america)?)\s*$", re.IGNORECASE)


class LocationDataMissing(RuntimeError):
    """The location tables are empty, so every city would look unknown."""


def _clean(text: str) -> str:
    """Lowercase ASCII words: accents, periods and apostrophes dropped, other punctuation a space."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[.']", "", text.casefold())
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def normalize_name(text: str) -> str:
    """The form city names are compared in: "St. Louis" and "saint  louis" -> "saint louis".

    Ingestion stores this for every place (USPlace.name_key), so the lookup is an exact
    match on an indexed column.
    """
    return " ".join(_SPELLINGS.get(word, word) for word in _clean(text).split())


_STATE_CODES = {_clean(key): code for code, name in STATES.items() for key in (code, name)}
# A state name on its own means the state, not a town named after it ("Florida" is not
# Florida, NY), except where the state's name is also a big city's.
_STATE_NAMES = {_clean(name) for name in STATES.values()} - {"new york", "washington"}


def state_code(text: str) -> str | None:
    """"SC", "s.c." or "South Carolina" -> "SC"; None if it isn't a US state or DC."""
    return _STATE_CODES.get(_clean(text))


def _readings(text: str):
    """Ways to read the text as (name_key, state or None), most likely first.

    The rules, in order: a nickname from ALIASES; "City, ST" or "City, State name"
    (city and state both exact); the whole text as a city name in any state, unless it
    is only a state's name; and last, a state typed without a comma ("Washington DC",
    "Portland Maine").
    """
    text = _COUNTRY.sub("", text.strip()).strip()
    if not text:
        return
    alias = ALIASES.get(_clean(text))
    if alias:
        yield normalize_name(alias[0]), alias[1]
        return
    if "," in text:
        city, _, state = text.rpartition(",")
        code = state_code(state)
        if code is None:  # "Greenville, Narnia": not a US state, so nothing can match
            return
        alias = ALIASES.get(_clean(city))
        if alias and alias[1] == code:  # "NYC, NY"
            yield normalize_name(alias[0]), code
        else:
            yield normalize_name(city), code
        return
    if _clean(text) in _STATE_NAMES:
        return
    yield normalize_name(text), None
    words = _clean(text).split()
    for size in (1, 2, 3):  # "dc", "new hampshire", "district of columbia"
        if len(words) > size:
            code = state_code(" ".join(words[-size:]))
            if code:
                yield normalize_name(" ".join(words[:-size])), code


def match_places(db: Session, text: str, limit: int = MAX_CHOICES) -> list[USPlace]:
    """The us_places rows that fit the text exactly, biggest population first.

    Raises LocationDataMissing if nothing matches because the table is empty or was
    never created (ingestion not run yet), so that isn't shown as an unknown city.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    # A copied qualified card label may select one same-state place. Match the
    # entire qualifier after the indexed city lookup; never guess from part of it.
    qualifier = re.search(r"\s+\(([^()]*)\)", text or "")
    lookup_text = (text[:qualifier.start()] + text[qualifier.end():]) if qualifier else text
    try:
        for name_key, state in _readings(lookup_text or ""):
            query = select(USPlace).where(USPlace.name_key == name_key)
            if state:
                query = query.where(USPlace.state == state)
            query = query.order_by(USPlace.population.desc(), USPlace.id)
            if qualifier:
                # Filter before limiting so a selected smaller place remains reachable.
                wanted = " ".join(qualifier.group(1).casefold().split())
                rows = [row for row in db.scalars(query)
                        if " ".join(row.city.partition(" (")[2].removesuffix(")").casefold().split()) == wanted][:limit]
            else:
                rows = db.scalars(query.limit(limit)).all()
            if rows:
                return list(rows)
        empty = db.scalar(select(USPlace.id).limit(1)) is None
    except OperationalError:
        db.rollback()
        if inspect(db.get_bind()).has_table(USPlace.__tablename__):
            raise  # some other database problem
        raise LocationDataMissing(NO_LOCATION_DATA) from None
    if empty:
        raise LocationDataMissing(NO_LOCATION_DATA)
    return []


def find_places(db: Session, text: str, limit: int = MAX_CHOICES) -> list[Place]:
    """All places that fit the text, biggest population first, at most `limit`.

    Case, extra spaces, "Saint"/"St.", accents and a trailing ", USA" don't matter.
        "Chicago"          -> [Chicago, IL]
        "Greenville"       -> [Greenville, NC; Greenville, SC; Greenville, TX; ...]
        "Greenville, SC" / "Greenville, South Carolina" -> [Greenville, SC]
        "NYC"              -> [New York, NY]
        "Charlotte"        -> Charlotte, NC and other Charlottes, never Charlottesville
        "Narnia"           -> []   ("Atlantis" is a real town: Atlantis, FL)

    Intake saves the place when there is exactly one, and asks with a card listing
    these when there are several, however much bigger the first one is.
    """
    return [build_place(db, row) for row in match_places(db, text, limit)]


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Straight-line (great-circle) distance in km, haversine formula."""
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 6371.0088 * 2 * asin(sqrt(min(1.0, a)))


def nearby_airports(db: Session, lat: float, lon: float, radius_km: float = DEFAULT_RADIUS_KM) -> list[dict]:
    """Airports with scheduled flights within radius_km of a point, closest first.

    The airports table only holds airports with scheduled service (see
    scripts/ingest_locations.py). Missing or entirely empty airport data raises
    LocationDataMissing; a loaded table with no airports nearby still returns [].
    Each result:
        {"iata": "GSP", "name": "...", "city": "Greer", "state": "SC",
         "latitude": 34.89, "longitude": -82.21, "dot_market_id": 31871, "distance_km": 19.6}
    """
    if radius_km <= 0:
        raise ValueError("radius_km must be positive")
    # One degree of latitude is at least 110 km, so this band holds every airport in range.
    band = radius_km / 110.0
    try:
        candidates = db.scalars(select(Airport).where(
            Airport.latitude.between(lat - band, lat + band))).all()
        # Distinguish an unloaded dataset from a town with no airport in range.
        if not candidates and db.scalar(select(Airport.iata).limit(1)) is None:
            raise LocationDataMissing(NO_LOCATION_DATA)
    except OperationalError:
        db.rollback()
        if inspect(db.get_bind()).has_table(Airport.__tablename__):
            raise  # some other database problem
        raise LocationDataMissing(NO_LOCATION_DATA) from None
    found = []
    for airport in candidates:
        distance = distance_km(lat, lon, airport.latitude, airport.longitude)
        if distance <= radius_km:
            found.append((distance, airport))
    found.sort(key=lambda item: (item[0], item[1].iata))
    return [{"iata": a.iata, "name": a.name, "city": a.city, "state": a.state,
             "latitude": a.latitude, "longitude": a.longitude,
             "dot_market_id": a.dot_market_id, "distance_km": round(d, 1)} for d, a in found]


def build_place(db: Session, row: USPlace) -> Place:
    """A city row plus its nearby airports and DOT market.

    The DOT market is the one of the closest airport THAT HAS A MARKET. Issue #21 says
    "the market of the closest airport", but OurAirports marks some general-aviation
    fields as scheduled, and DOT puts them in no market: Fort Worth's closest airport is
    Meacham (FTW) and Denver's is Rocky Mountain Metropolitan (BJC). Taken literally,
    Fort Worth and Denver would get no market, against the issue's own Fort Worth
    example; skipping airports without a market gives Fort Worth "Dallas/Fort Worth,
    TX" from DFW. `airports` still lists every nearby airport, FTW first.
    A place with no airport with a market within DEFAULT_RADIUS_KM has dot_market_id
    None (fare trends then return no_history); one with no airports at all has [].
    """
    airports = nearby_airports(db, row.latitude, row.longitude)
    market = next((a["dot_market_id"] for a in airports if a["dot_market_id"] is not None), None)
    return Place(id=row.id, city=row.city, state=row.state, latitude=row.latitude,
                 longitude=row.longitude, airports=[a["iata"] for a in airports], dot_market_id=market)


def get_place(db: Session, place_id: str) -> Place | None:
    """Look up a place by the id from a choice card ("us-sc-greenville"); None if unknown."""
    if not isinstance(place_id, str):
        return None
    row = db.get(USPlace, place_id)
    return build_place(db, row) if row else None


def place_label(place: Place) -> str:
    """How a place is shown to the user: "Greenville, SC"."""
    return f"{place.city}, {place.state}"
