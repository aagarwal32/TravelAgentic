"""Load the location tables (us_places and airports) and link airports to DOT markets.

Usage, from backend/:
    python scripts/download_datasets.py us_places_gazetteer us_places_population us_places_centers ourairports dot_table1a_airports
    python -m scripts.ingest_table6       # first: some airports are linked by Table 6 market names
    python -m scripts.ingest_locations    # loads both tables from backend/datasets/
    python -m scripts.ingest_locations --airports path/to/airports.csv   # (each file can be overridden)

Where the data comes from (all public domain, see docs/data.md):
- Places: the Census Bureau's 2024 Gazetteer places file gives every incorporated place
  and census-designated place (CDP); population comes from the ACS 2020-2024 5-year
  table B01003, joined on the Census GEOID.
- Coordinates: the Gazetteer's point is only somewhere inside the city's boundary (for
  San Francisco it is in the ocean near the Farallon Islands, 54 km from downtown), so
  where the USGS GNIS has the city's "Populated Place" point (its traditional center),
  that point is used instead: the one with the same Census place code and name, or else
  the only one with that name in the state (GNIS keeps old codes for consolidated
  cities like Louisville, and none at all for El Paso). A GNIS point farther than
  CENTER_MAX_RATIO x the place's radius from the Gazetteer point is treated as a GNIS
  error and ignored.
- Airports: OurAirports airports.csv, kept only for US airports with scheduled
  service, type large_airport or medium_airport, and an IATA code.
- Airport -> DOT market: DOT Consumer Airfare Report Table 1a lists airport pairs with
  each airport's DOT city market id, the same ids as Table 6 (DFW and DAL are both
  30194 "Dallas/Fort Worth, TX"). It only covers airports in the biggest markets, so an
  airport that isn't in it is matched by its city and state against the Table 6 market
  names, one city of the name at a time ("College Station" -> "College Station/Bryan,
  TX"), but only into a market that Table 1a doesn't already cover (Orlando Sanford,
  SFB, must not join Orlando: DOT has a separate Sanford market).

Like ingest_table6, the script REPLACES both tables' contents in one transaction, so
running it twice gives the same result and a failed load keeps the old rows.
"""

import argparse
import csv
import io
import math
import re
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import NamedTuple

from sqlalchemy import delete, insert, select, union
from sqlalchemy.orm import Session

from db.database import SessionLocal, create_tables
from models.airfare import AirfareQuarter
from models.location import Airport, USPlace
from services.location_service import STATES, distance_km, normalize_name

DATASETS = Path(__file__).resolve().parent.parent / "datasets"
DEFAULT_GAZETTEER = DATASETS / "us_places_gazetteer_2024.zip"
DEFAULT_POPULATION = DATASETS / "us_places_population_2024.dat"
DEFAULT_CENTERS = DATASETS / "us_places_centers_gnis.zip"
DEFAULT_AIRPORTS = DATASETS / "ourairports_airports.csv"
DEFAULT_TABLE1A = DATASETS / "dot_table1a_airports.csv"
BATCH_SIZE = 5000

GAZETTEER_COLUMNS = ["USPS", "GEOID", "ANSICODE", "NAME", "ALAND", "AWATER", "INTPTLAT", "INTPTLONG"]
CENTER_COLUMNS = ["feature_id", "feature_name", "feature_class", "census_code", "state_numeric",
                  "county_name", "county_sequence", "prim_lat_dec", "prim_long_dec"]
# A GNIS center can be this many times the place's radius (that of a circle with its
# land + water area) from the Gazetteer point. San Francisco needs 3.9 (its area reaches
# the Farallon Islands); the GNIS errors seen in the data are 27x or more.
CENTER_MAX_RATIO = 5
POPULATION_COLUMNS = ["GEO_ID", "B01003_E001"]
PLACE_GEO_ID = "1600000US"  # ACS GEO_ID prefix of a place (summary level 160)
AIRPORT_COLUMNS = ["iata_code", "name", "type", "iso_country", "iso_region", "municipality",
                   "scheduled_service", "latitude_deg", "longitude_deg"]
AIRPORT_TYPES = {"large_airport", "medium_airport"}
TABLE1A_COLUMNS = ["airport_1", "citymarketid_1", "city1", "airport_2", "citymarketid_2", "city2"]

# The kind of place at the end of a Census name ("Greenville city", "Urban Honolulu
# CDP"), longest first so "city and borough" goes before "borough".
NAME_SUFFIXES = (" city and borough", " metropolitan government", " consolidated government",
                 " unified government", " metro government", " metro township", " urban county",
                 " municipality", " corporation", " borough", " village", " town", " city", " CDP")
# Consolidated city-counties and other places whose Census name isn't what people type,
# by GEOID (Census name in the comment).
NAME_FIXES = {
    "4752006": "Nashville",    # Nashville-Davidson metropolitan government (balance)
    "2148006": "Louisville",   # Louisville/Jefferson County metro government (balance)
    "2146027": "Lexington",    # Lexington-Fayette urban county
    "1303440": "Athens",       # Athens-Clarke County unified government (balance)
    "1304204": "Augusta",      # Augusta-Richmond County consolidated government (balance)
    "1349008": "Macon",        # Macon-Bibb County
    "3011397": "Butte",        # Butte-Silver Bow (balance)
    "3001675": "Anaconda",     # Anaconda-Deer Lodge County
    "1571550": "Honolulu",     # Urban Honolulu CDP
    "1608830": "Boise",        # Boise City city (Idaho; Boise City, OK keeps its name)
    "0665042": "Ventura",      # San Buenaventura (Ventura) city
    "0622300": "Paso Robles",  # El Paso de Robles (Paso Robles) city
    "4732742": "Hartsville",   # Hartsville/Trousdale County
    "4744382": "Lynchburg",    # Lynchburg, Moore County metropolitan government
}

# Airports whose IATA code changed after DOT's data was published: OurAirports code -> DOT code.
RENAMED_CODES = {"DJT": "PBI"}  # Palm Beach International, renamed in OurAirports in 2026
# Hand-checked links for airports outside Table 1a whose OurAirports city isn't in their
# DOT market's name, so the name match can't find them (checked against Table 6's names).
MARKET_FIXES = {
    "MLI": 33367,  # Quad City Intl, Moline -> "Quad Cities, IL (Metropolitan Area)"
    "TRI": 35323,  # Tri-Cities Regional, Blountville -> "Bristol/Johnson City/Kingsport, TN"
    "CMI": 31067,  # Willard, Savoy -> "Champaign/Urbana, IL"
    "MBS": 33184,  # MBS Intl, Freeland -> "Saginaw/Bay City/Midland, MI"
    "MQT": 33459,  # Sawyer, Gwinn -> "Marquette, MI"
    "IMT": 32335,  # Ford Airport, Kingsford -> "Iron Mountain/Kingsfd, MI"
    "CIU": 31013,  # Chippewa County Intl, Kincheloe -> "Sault Ste. Marie, MI"
    "HHH": 32124,  # Hilton Head Island -> "Hilton Head, SC"
    "OAJ": 33795,  # Albert J Ellis, Richlands -> "Jacksonville/Camp Lejeune, NC"
    "PIB": 34109,  # Hattiesburg Laurel Regional, Moselle -> "Hattiesburg/Laurel, MS"
    "GRK": 31982,  # Killeen Regional, Fort Cavazos -> "Killeen, TX"
    "CKB": 31027,  # North Central West Virginia, Bridgeport -> "Clarksburg/Fairmont, WV"
    "BKW": 30654,  # Raleigh County Memorial, Beaver -> "Beckley, WV"
    "HTS": 32223,  # Tri-State, Huntington -> "Ashland, WV" (DOT's name for the market)
    "SHD": 34802,  # Shenandoah Valley Regional, Weyers Cave -> "Staunton, VA"
}
# Metro airports printed after loading so their markets can be checked by hand.
CHECK_AIRPORTS = ["DFW", "DAL", "JFK", "LGA", "EWR", "ORD", "MDW", "IAD", "DCA", "BWI"]


def _float(text: str | None) -> float:
    """A finite float, or ValueError."""
    value = float((text or "").strip())
    if not math.isfinite(value):
        raise ValueError("not a finite number")
    return value


def _check_header(fieldnames, required: list[str], what: str) -> None:
    """Raise ValueError if a file lacks a column we need (a wrong file would load nothing)."""
    missing = [c for c in required if c not in (fieldnames or [])]
    if missing:
        raise ValueError(f"{what} is missing columns: {', '.join(missing)}")


def clean_place_name(name: str, geoid: str) -> str:
    """The city name people use, from a Census place name.

    "Greenville city" -> "Greenville", "Milford city (balance)" -> "Milford",
    "Village of Oak Creek (Big Park) CDP" -> "Village of Oak Creek", and the
    NAME_FIXES ("Urban Honolulu CDP" -> "Honolulu").
    """
    if geoid in NAME_FIXES:
        return NAME_FIXES[geoid]
    name = name.strip().removesuffix(" (balance)")
    for suffix in NAME_SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return re.sub(r"\s*\([^)]*\)$", "", name).strip()


def place_id(state: str, city: str) -> str:
    """Our place id: "us-sc-greenville", "us-mo-saint-louis"."""
    return f"us-{state.lower()}-{normalize_name(city).replace(' ', '-')}"


def read_population(path: Path) -> dict[str, int]:
    """Population of every Census place in the ACS table, as {GEOID: people}."""
    population = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter="|")
        _check_header(reader.fieldnames, POPULATION_COLUMNS, "population file")
        for row in reader:
            geo_id = row["GEO_ID"] or ""
            if not geo_id.startswith(PLACE_GEO_ID):
                continue  # the file covers every geography: nation, counties, tracts...
            try:
                people = int(row["B01003_E001"])
            except (TypeError, ValueError):
                continue
            if people >= 0:
                population[geo_id[len(PLACE_GEO_ID):]] = people
    return population


def _text_lines(path: Path) -> io.TextIOBase:
    """A downloaded text file, from its .zip (one .txt inside) or already unzipped."""
    if path.suffix.lower() != ".zip":
        return open(path, newline="", encoding="utf-8-sig")
    with zipfile.ZipFile(path) as archive:
        members = [n for n in archive.namelist() if n.lower().endswith(".txt")]
        if len(members) != 1:
            raise ValueError(f"expected one .txt file in {path.name}, found {len(members)}")
        data = archive.read(members[0])
    return io.StringIO(data.decode("utf-8-sig"), newline="")


class CityCenters(NamedTuple):
    """GNIS centers and county labels joined by the Gazetteer's exact ANSI feature ID."""
    by_code: dict[str, list[tuple[str, float, float]]]       # {GEOID: [(name_key, lat, lon)]}
    by_name: dict[tuple[str, str], list[tuple[float, float]]]  # {(state FIPS, name_key): [(lat, lon)]}
    counties: dict[str, set[str]]  # {zero-padded ANSI feature ID: county/equivalent names}


def read_city_centers(path: Path) -> CityCenters:
    """Read the GNIS Federal Codes file's "Populated Place" points (city centers)."""
    centers = CityCenters(defaultdict(list), defaultdict(list), defaultdict(set))
    with _text_lines(path) as f:
        reader = csv.DictReader(f, delimiter="|")
        _check_header(reader.fieldnames, CENTER_COLUMNS, "GNIS file")
        for row in reader:
            # Civil/Census entities and every county count here, even though only
            # Populated Place records supply centers. Never infer a county by name.
            feature_id, county = (row["feature_id"] or "").strip(), (row["county_name"] or "").strip()
            if feature_id and county:
                centers.counties[feature_id.zfill(8)].add(county)
            # One row per county a feature lies in; the first one is enough
            if row["feature_class"] != "Populated Place" or row["county_sequence"] != "1":
                continue
            try:
                latitude, longitude = _float(row["prim_lat_dec"]), _float(row["prim_long_dec"])
            except ValueError:
                continue
            if latitude == 0 and longitude == 0:  # GNIS's "unknown"
                continue
            name_key = normalize_name(row["feature_name"] or "")
            if row["census_code"]:
                centers.by_code[row["state_numeric"] + row["census_code"]].append((name_key, latitude, longitude))
            centers.by_name[(row["state_numeric"], name_key)].append((latitude, longitude))
    return centers


def _city_center(geoid: str, name_key: str, latitude: float, longitude: float, area_km2: float,
                 centers: CityCenters, *, allow_name_fallback: bool = True) -> tuple[float, float] | None:
    """The GNIS center of a place, unless it is implausibly far from the Gazetteer point.

    First the point with the same Census code and name; otherwise the only GNIS point
    with that name in the state (a name used twice in a state is too risky to guess).
    """
    radius_km = max(math.sqrt(area_km2 / math.pi), 0.5)

    def near(point: tuple[float, float]) -> bool:
        return distance_km(latitude, longitude, *point) <= CENTER_MAX_RATIO * radius_km

    for key, center_lat, center_lon in centers.by_code.get(geoid, []):
        if key == name_key and near((center_lat, center_lon)):
            return center_lat, center_lon
    same_name = centers.by_name.get((geoid[:2], name_key), [])
    if allow_name_fallback and len(same_name) == 1 and near(same_name[0]):
        return same_name[0]
    return None


def _place_qualifier(row: dict, centers: CityCenters) -> str:
    """Readable distinction for same-state names, without changing the lookup key."""
    source_name = row["_source_name"].removesuffix(" (balance)")
    kind = next((suffix.strip() for suffix in NAME_SUFFIXES if source_name.endswith(suffix)), source_name)
    counties = centers.counties.get(row["_ansi"], set())
    labels = []
    for county in sorted(counties):
        if row["state"] == "AK" or "(city)" in county:
            labels.append(county.replace("(", "").replace(")", ""))
        else:
            labels.append(f"{county} {'Parish' if row['state'] == 'LA' else 'County'}")
    area = " / ".join(labels) if labels else f"lat {row['latitude']:.6f}, lon {row['longitude']:.6f}"
    return f"{kind}; {area}"


def read_places(gazetteer: Path, population: dict[str, int],
                centers: CityCenters) -> tuple[list[dict], dict]:
    """Read the Gazetteer into us_places rows; return (rows, counts).

    Kept: the 50 states and DC (Puerto Rico is left out, like its airports). A place
    with no population figure gets 0. Coordinates are the GNIS center when `centers`
    (from read_city_centers) has a plausible one, otherwise the Gazetteer point.
    Every distinct GEOID is retained. Same-state names receive GEOID-suffixed IDs
    and qualified display names; their name_key stays the ordinary city name.
    Name-only GNIS center fallback is disabled for these ambiguous names.
    counts: {"skipped": bad rows, "ambiguous": same-state names disambiguated,
             "no_population": places loaded with population 0,
             "centers": places loaded with a GNIS center}
    """
    groups = defaultdict(list)
    seen = set()
    counts = {"skipped": 0, "ambiguous": 0, "no_population": 0, "centers": 0}
    with _text_lines(gazetteer) as f:
        reader = csv.DictReader(f, delimiter="\t")
        # The last header has trailing spaces in the Census file
        reader.fieldnames = [name.strip() for name in reader.fieldnames or []]
        _check_header(reader.fieldnames, GAZETTEER_COLUMNS, "Gazetteer file")
        for raw in reader:
            state, geoid = (raw["USPS"] or "").strip(), (raw["GEOID"] or "").strip()
            if state not in STATES:
                continue
            try:
                latitude, longitude = _float(raw["INTPTLAT"]), _float(raw["INTPTLONG"])
            except ValueError:
                counts["skipped"] += 1
                continue
            try:
                area_km2 = (_float(raw["ALAND"]) + _float(raw["AWATER"])) / 1e6
            except ValueError:
                area_km2 = 0.0  # only limits how far a GNIS center may be
            city = clean_place_name(raw["NAME"] or "", geoid)
            name_key = normalize_name(city)
            if not geoid or not name_key or not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
                counts["skipped"] += 1
                continue
            if geoid in seen:
                raise ValueError(f"Duplicate Census GEOID: {geoid}")
            seen.add(geoid)
            if geoid not in population:
                counts["no_population"] += 1
            row = {"id": place_id(state, city), "geoid": geoid, "city": city, "state": state,
                   "name_key": name_key, "latitude": latitude, "longitude": longitude,
                   "population": population.get(geoid, 0), "_area": area_km2,
                   "_source_name": (raw["NAME"] or "").strip(),
                   "_ansi": (raw["ANSICODE"] or "").strip().zfill(8)}
            groups[(state, name_key)].append(row)
    rows = []
    for group in groups.values():
        ambiguous = len(group) > 1
        if ambiguous:
            counts["ambiguous"] += len(group)
        for row in group:
            center = _city_center(row["geoid"], row["name_key"], row["latitude"], row["longitude"],
                                  row.pop("_area"), centers, allow_name_fallback=not ambiguous)
            if center:
                row["latitude"], row["longitude"] = center
                counts["centers"] += 1
            if ambiguous:
                row["id"] += "-" + row["geoid"]
                row["city"] += f" ({_place_qualifier(row, centers)})"
            row.pop("_source_name")
            row.pop("_ansi")
        # Rare identical county/type/coordinate labels must still be selectable.
        labels = Counter(row["city"] for row in group)
        for row in group:
            if labels[row["city"]] > 1:
                row["city"] = row["city"][:-1] + f"; Census {row['geoid']})"
        rows.extend(group)
    return rows, counts


def read_airports(path: Path) -> tuple[list[dict], int]:
    """Read OurAirports into airports rows; return (rows, bad rows skipped).

    Kept, as the issue specifies: iso_country "US", scheduled_service "yes", type
    large_airport or medium_airport, and an IATA code. Other rows (heliports, small
    fields, foreign airports) are filtered silently and not counted as skipped.
    """
    rows, skipped, seen = [], 0, set()
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        _check_header(reader.fieldnames, AIRPORT_COLUMNS, "airports file")
        for raw in reader:
            iata = (raw["iata_code"] or "").strip().upper()
            if (raw["iso_country"] != "US" or raw["scheduled_service"] != "yes"
                    or raw["type"] not in AIRPORT_TYPES or not iata):
                continue
            region = (raw["iso_region"] or "").strip()
            state = region[3:] if region.startswith("US-") else ""
            try:
                latitude, longitude = _float(raw["latitude_deg"]), _float(raw["longitude_deg"])
            except ValueError:
                skipped += 1
                continue
            if (state not in STATES or len(iata) != 3 or iata in seen
                    or not -90 <= latitude <= 90 or not -180 <= longitude <= 180):
                skipped += 1
                continue
            seen.add(iata)
            rows.append({"iata": iata, "name": (raw["name"] or "").strip() or iata,
                         "city": (raw["municipality"] or "").strip() or None, "state": state,
                         "latitude": latitude, "longitude": longitude, "dot_market_id": None})
    return rows, skipped


def read_table1a_markets(path: Path) -> dict[str, tuple[int, str]]:
    """Each airport's DOT city market from Table 1a, as {"DFW": (30194, "Dallas/Fort Worth, TX")}.

    The file has one row per airport pair, so both sides are read. If DOT ever moved an
    airport to another market, the row with the newest last_year wins.
    """
    markets: dict[str, tuple[int, str, int]] = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        _check_header(reader.fieldnames, TABLE1A_COLUMNS, "Table 1a file")
        for row in reader:
            try:
                year = int(row.get("last_year") or 0)
            except ValueError:
                year = 0
            for side in ("1", "2"):
                code = (row[f"airport_{side}"] or "").strip().upper()
                try:
                    market_id = int(row[f"citymarketid_{side}"])
                except (TypeError, ValueError):
                    continue
                if code and (code not in markets or year > markets[code][2]):
                    markets[code] = (market_id, (row[f"city{side}"] or "").strip(), year)
    return {code: (market_id, name) for code, (market_id, name, _) in markets.items()}


def table6_markets(session: Session) -> dict[int, str]:
    """Every DOT city market in the loaded Table 6, as {30194: "Dallas/Fort Worth, TX"}."""
    query = union(select(AirfareQuarter.citymarketid_1, AirfareQuarter.city1),
                  select(AirfareQuarter.citymarketid_2, AirfareQuarter.city2))
    return {market_id: name for market_id, name in session.execute(query)}


def _market_name_keys(name: str) -> list[tuple[str, str]]:
    """(state, city) pairs a DOT market name stands for.

    "Scranton/Wilkes-Barre, PA" -> [("PA", "scranton"), ("PA", "wilkes barre")];
    "Miami, FL (Metropolitan Area)" -> [("FL", "miami")].
    """
    cities, _, rest = name.partition(",")
    state = rest.strip()[:2].upper()
    return [(state, key) for key in map(normalize_name, cities.split("/")) if key]


def link_markets(airports: list[dict], table1a: dict[str, tuple[int, str]],
                 markets: dict[int, str]) -> dict:
    """Set dot_market_id on each airport row; return how many were linked each way.

    1. By IATA code from Table 1a (DOT's own assignment).
    2. The hand-checked MARKET_FIXES.
    3. Otherwise by the airport's city and state against the Table 6 market names
       (`markets`, from table6_markets): each "/"-separated city of a market name counts,
       compared like place names ("Saint Cloud" = "St. Cloud"). Only markets Table 1a
       doesn't cover can be linked this way, and only when exactly one market fits.
       Without Table 6 loaded, nothing is linked this way.
    """
    covered = {market_id for market_id, _ in table1a.values()}
    by_name = defaultdict(set)
    for market_id, name in markets.items():
        if market_id not in covered:
            for key in _market_name_keys(name):
                by_name[key].add(market_id)
    counts = {"table1a": 0, "fixed": 0, "name": 0, "none": 0}
    for airport in airports:
        code = RENAMED_CODES.get(airport["iata"], airport["iata"])
        if code in table1a:
            airport["dot_market_id"] = table1a[code][0]
            counts["table1a"] += 1
            continue
        if code in MARKET_FIXES:
            airport["dot_market_id"] = MARKET_FIXES[code]
            counts["fixed"] += 1
            continue
        found = set()
        for city in (airport["city"] or "").split("/"):  # "Chicago/Rockford" -> both
            found |= by_name.get((airport["state"], normalize_name(city)), set())
        airport["dot_market_id"] = found.pop() if len(found) == 1 else None
        counts["name" if airport["dot_market_id"] is not None else "none"] += 1
    return counts


def load_rows(session: Session, places: list[dict], airports: list[dict]) -> None:
    """Replace everything in us_places and airports, in ONE transaction.

    If anything fails the transaction is rolled back, so both tables keep their old rows
    (and never end up from two different loads).
    """
    try:
        session.execute(delete(Airport))
        session.execute(delete(USPlace))
        for table, rows in ((USPlace, places), (Airport, airports)):
            for start in range(0, len(rows), BATCH_SIZE):
                session.execute(insert(table), rows[start:start + BATCH_SIZE])
        session.commit()
    except Exception:
        session.rollback()
        raise


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point. Returns the process exit code."""
    parser = argparse.ArgumentParser(description="Load US places and airports into the database.")
    parser.add_argument("--gazetteer", type=Path, default=DEFAULT_GAZETTEER, help="Census Gazetteer places file (.zip or .txt)")
    parser.add_argument("--population", type=Path, default=DEFAULT_POPULATION, help="ACS B01003 table (.dat)")
    parser.add_argument("--centers", type=Path, default=DEFAULT_CENTERS, help="GNIS Federal Codes file (.zip or .txt)")
    parser.add_argument("--airports", type=Path, default=DEFAULT_AIRPORTS, help="OurAirports airports.csv")
    parser.add_argument("--table1a", type=Path, default=DEFAULT_TABLE1A, help="DOT Table 1a airport markets CSV")
    args = parser.parse_args(argv)

    files = {"us_places_gazetteer": args.gazetteer, "us_places_population": args.population,
             "us_places_centers": args.centers, "ourairports": args.airports,
             "dot_table1a_airports": args.table1a}
    absent = [name for name, path in files.items() if not path.exists()]
    if absent:
        print(f"Missing: {', '.join(str(files[n]) for n in absent)}\n"
              f"Run: python scripts/download_datasets.py {' '.join(absent)}")
        return 1

    places, place_counts = read_places(args.gazetteer, read_population(args.population),
                                       read_city_centers(args.centers))
    airports, airports_skipped = read_airports(args.airports)
    table1a = read_table1a_markets(args.table1a)
    if not places or not airports or not table1a:
        # Never wipe the tables because of an empty or wrong file.
        print("A file had no usable rows; the tables were NOT changed.")
        return 1

    create_tables()
    with SessionLocal() as session:
        links = link_markets(airports, table1a, table6_markets(session))
        load_rows(session, places, airports)

    print(f"Places loaded:    {len(places)}  (skipped {place_counts['skipped']}, "
          f"same-state names disambiguated {place_counts['ambiguous']}, "
          f"without population {place_counts['no_population']})")
    print(f"City centers:     {place_counts['centers']} from GNIS, "
          f"{len(places) - place_counts['centers']} from the Gazetteer point")
    print(f"Airports loaded:  {len(airports)}  (skipped {airports_skipped})")
    print(f"DOT markets:      {links['table1a']} from Table 1a, {links['fixed']} hand-checked, "
          f"{links['name']} by Table 6 name, {links['none']} without a market")
    if links["name"] == 0:
        print("  No airport was linked by name: load Table 6 first (python -m scripts.ingest_table6).")
    by_code = {a["iata"]: a["dot_market_id"] for a in airports}
    names = {market_id: name for market_id, name in table1a.values()}
    print("Check by hand:")
    for code in CHECK_AIRPORTS:
        market = by_code.get(code)
        print(f"  {code}  {market}  {names.get(market, '(not loaded)' if code not in by_code else '-')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
