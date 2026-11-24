# Datasets

Datasets are too large for Git, so each person downloads them into
`backend/datasets/` with a script. The local database (`backend/database.db`) isn't
committed either; everyone builds their own from the datasets.

All commands run from `backend/` with the virtual environment active.

```bash
python scripts/download_datasets.py          # all datasets
python scripts/download_datasets.py --list   # see what's available
```

## DOT Table 6 (historical airfares)

Historical airfares from the DOT Consumer Airfare Report, Table 6, 2022 and newer.
The trends agent uses them. The data is quarterly, city pairs have no direction, and
fares are average each-way fares, not real ticket prices.

Source: https://data.transportation.gov/d/yj5y-b2ir

### Load it into the database

```bash
python scripts/download_datasets.py dot_table6   # downloads datasets/dot_table6.csv
python -m scripts.ingest_table6                  # loads it into dot_table6_fares
```

The second command replaces the table's contents, so running it twice never creates
duplicates. It prints the rows loaded, the rows skipped, and the newest year and
quarter. When DOT publishes a new quarter, run both commands again.

By default the script reads `datasets/dot_table6.csv`. To load a CSV from somewhere
else, pass its path with `--csv`:

```bash
python -m scripts.ingest_table6 --csv path/to/dot_table6.csv
```

## US places and airports (location lookup)

Two tables let intake turn a typed city ("Greenville, SC", "NYC") into one exact
place with coordinates, nearby airports and a DOT market (see
[agent.md](agent.md#places-origin-and-destination)). They are loaded together from
five public files:

| Dataset (`download_datasets.py` name) | Source | Used for |
|---|---|---|
| `us_places_gazetteer` | [Census Bureau 2024 Gazetteer, places](https://www.census.gov/geographies/reference-files/time-series/geo/gazetteer-files.html) | Every US city, town and census-designated place, with its state and Census id (GEOID) |
| `us_places_population` | [Census Bureau ACS 2020-2024 5-year, table B01003](https://www.census.gov/programs-surveys/acs/data/summary-file.html) | Population, which orders the "Which Greenville?" choices |
| `us_places_centers` | [USGS GNIS Federal Codes](https://www.usgs.gov/us-board-on-geographic-names/download-gnis-data) | City-center coordinates and county labels for same-state name collisions |
| `ourairports` | [OurAirports `airports.csv`](https://ourairports.com/data/) | US airports with scheduled flights |
| `dot_table1a_airports` | [DOT Consumer Airfare Report, Table 1a](https://data.transportation.gov/d/tfrh-tu9e) | Which DOT city market (the ids Table 6 uses) each airport belongs to |

### Load them into the database

Load DOT Table 6 first: airports that Table 1a doesn't list are linked to a market by
matching their city against the Table 6 market names.

```bash
python scripts/download_datasets.py us_places_gazetteer us_places_population us_places_centers ourairports dot_table1a_airports
python -m scripts.ingest_table6        # if not loaded yet
python -m scripts.ingest_locations     # loads us_places and airports
```

`ingest_locations` replaces both tables' contents in one transaction (running it twice
never creates duplicates, and a failed load keeps the old rows). It prints how many
places and airports it loaded, how airports were linked to DOT markets, and the
markets of the big metro airports so they can be checked by hand:

```
Places loaded:    32041  (skipped 0, same-state names disambiguated 416, without population 3)
City centers:     30269 from GNIS, 1772 from the Gazetteer point
Airports loaded:  502  (skipped 0)
DOT markets:      157 from Table 1a, 15 hand-checked, 202 by Table 6 name, 128 without a market
Check by hand:
  DFW  30194  Dallas/Fort Worth, TX
  DAL  30194  Dallas/Fort Worth, TX
  JFK  31703  New York City, NY (Metropolitan Area)
  LGA  31703  New York City, NY (Metropolitan Area)
  EWR  31703  New York City, NY (Metropolitan Area)
  ORD  30977  Chicago, IL
  MDW  30977  Chicago, IL
  IAD  30852  Washington, DC (Metropolitan Area)
  DCA  30852  Washington, DC (Metropolitan Area)
  BWI  30852  Washington, DC (Metropolitan Area)
```

Each file can be loaded from another path with `--gazetteer`, `--population`,
`--centers`, `--airports` and `--table1a`. Re-run both commands when a source is
updated (OurAirports changes nightly, DOT every quarter).

These counts were verified from the existing local dataset snapshot in an in-memory
database; future source releases can differ. After upgrading from the version that
discarded same-state names, re-ingest the location tables and start fresh intake
sessions. The old shared IDs for ambiguous names are intentionally not redirected
to either place. No table-column migration is needed.

### What's in the tables

- **`us_places`**: the 50 states and DC (Puerto Rico is left out, like its airports).
  A state's name alone ("Florida") is never read as a town of that name, except New
  York and Washington.
  City names are cleaned up to what people type ("Greenville city" -> "Greenville",
  "Urban Honolulu CDP" -> "Honolulu", "Nashville-Davidson metropolitan government
  (balance)" -> "Nashville"). Each place has our id (`us-sc-greenville`), the Census
  GEOID, coordinates and population. Every distinct GEOID is retained. The local
  snapshot has 416 places in 203 same-state name groups, all with distinct GEOIDs and
  ANSI feature IDs; the previous name-based deduplication incorrectly dropped 213.
  Examples include Waukesha city and village, WI, and Greenville in Greene and
  Westchester counties, NY (about 158 km apart at their Gazetteer points).
  Distinct Census entities are not necessarily non-overlapping communities: the
  Louisville group includes both the city record and the consolidated-government
  balance. The loader preserves source identities rather than inferring equivalence
  from names or population.
  For these groups, IDs append the GEOID (`us-ny-greenville-3630609`), independent of
  population rank or file order. Ordinary names keep their existing IDs. IDs may
  change if a later source release changes a name or its collision group.
  The existing `city` column carries a readable qualifier such as
  `Greenville (CDP; Greene County)`; `name_key` remains `greenville` for exact lookup.
  County names come from joining Gazetteer ANSICODE to GNIS feature_id, never from
  guessing by city name. All counties of the feature are included. Of the 416
  colliding places, 415 have this county metadata in the local snapshot. The other
  uses coordinates in its label. A final GEOID qualifier distinguishes any remaining
  identical labels. No additional database or Place fields are introduced.
- **Coordinates** are the GNIS "Populated Place" point (the traditional center: Civic
  Center for San Francisco) when GNIS has one with the same Census code and name, or
  else the only one with that name in the state (GNIS keeps old codes for Louisville or
  Nashville, and none for El Paso), and it is plausibly close to the Gazetteer point.
  Name-only fallback is disabled when multiple Census places share the name in the
  state, so they cannot inadvertently share a center through that fallback.
  Otherwise it is the Gazetteer's point, which is only somewhere inside the boundary
  (for San Francisco it is 54 km out at sea).
- **`airports`**: rows from OurAirports with `iso_country` US, `scheduled_service` yes,
  type `large_airport` or `medium_airport`, and an IATA code. OurAirports marks a few
  general-aviation fields as scheduled (BJC near Denver, FTW, TEB), so they appear too.
- **`dot_market_id`** on each airport comes, in order, from DOT Table 1a (DOT's own
  assignment, by IATA code); a short hand-checked list in `scripts/ingest_locations.py`
  for airports named after a suburb (Moline's MLI is in "Quad Cities, IL"); and matching
  the airport's city and state against each city in a Table 6 market name ("College
  Station" -> "College Station/Bryan, TX"), but never into a market Table 1a already
  covers (Orlando Sanford, SFB, isn't put in Orlando's market). The BTS Master Coordinate
  table would be the direct source, but it can only be downloaded through an
  interactive form, not a link.

### Licenses and attribution

All five sources are public domain, so no attribution is required. Credit them anyway
where the data is shown: U.S. Census Bureau (Gazetteer and American Community
Survey), U.S. Geological Survey (GNIS), OurAirports (ourairports.com), and U.S.
Department of Transportation (Consumer Airfare Report). SimpleMaps' US Cities
database (CC BY 4.0) was considered, but its download page blocks scripted downloads,
so `download_datasets.py` can't fetch it.


## Issue #34 comparison data

`get_market_passenger_totals` uses the dataset-wide newest four **distinct loaded**
year/quarter pairs, newest first. Every market uses that same window; a market
with missing recent rows cannot substitute its own older quarters. Fewer than
four loaded pairs yield a smaller shared window. Table 6's `passengers` measures
passengers/day, so the summed values form a relative size index, not annual unique
travelers. An absent market has zero recorded passengers for this comparison.

Each unordered route is normalized by endpoint market IDs and grouped once per
year/quarter before its passenger value is attributed once to each endpoint. There
is no airport join: ORD and MDW do not multiply Chicago's size. Defensive reversed
or repeated duplicates use the maximum passenger value once; conflicting source
values require an audit rather than addition. A self-pair is counted once. Home
markets and smaller markets are excluded, equal sizes remain eligible, and a place
without a home market has no size filter.

Both comparison sides use their own resolved city coordinates and the nearest
eligible airport per DOT market within an inclusive 400 km straight-line radius.
Fare selection uses the mean of positive finite route fares in relevant quarters
across all loaded years. Rows have equal weight, without passenger weighting or
averaging quarter means together. The all-year route mean is used only when no
usable requested-quarter fare exists. Comparisons retain full precision. A fare
must be at most 80% of the original home fare; when the home fare is missing, any
valid candidate fare qualifies. Replacing the original cheapest requires both
`cheapest_fare >= 0.85 * closer_fare` and
`closer_distance <= 0.80 * cheapest_distance`. See the
[selection rules and five decisions](agent.md#issue-34-flight-trends-and-alternative-airports).

Chart values are Q1–Q4 means across loaded years, matching the existing
`by_quarter` aggregation. Missing quarters stay null. Do not interpolate,
zero-fill, or use all-year fallback to fill chart gaps; retain an entirely empty
home series too. These are historical average each-way fares, not live quotes.

### Airport evidence and limitations

The persisted schema does not retain mapping provenance, the original
OurAirports type/service fields, or an independent current commercial-search
flag. Presence in `Place.airports` or a non-null DOT market alone is insufficient.
The real snapshot maps TEB/Teterboro to market 35167 by city-name fallback and
contains recent Table 6 history for that market. A code blacklist would not
repair the missing metadata.

Candidate and search-airport helpers therefore require an explicit mapping from
the existing local Table 1a file plus the loader's `MARKET_FIXES`, applying
`RENAMED_CODES`. Table 1a takes precedence. The local airport row must exist and
its market must agree with the mapping. CMI is supported by the checked mappings.
TEB, FTW and BJC lack this evidence and are excluded. Intake's wider
`Place.airports` lists remain unchanged. The node reads the existing metadata; it
does not download files, ingest rows or add schema fields.

This conservative rule can omit legitimate name-matched or unmapped commercial
airports. Without a home market, only verified codes already associated with the
place form the home search group, followed by the selected alternative's codes.
Without evidence that group is empty. Home precedes alternative; each group is
sorted by unrounded city-to-airport distance and IATA, with duplicates removed.
Historical eligibility is not proof of currently bookable service. More complete
coverage requires reliable source evidence or retained provenance.

### Read-only analysis

The node opens a database session for queries and does not ingest or update
location or fare data. Required-data access errors, including missing, unreadable
or malformed local Table 1a metadata, become an `empty` result with a reason. An
existing but empty fares table is valid absence of history: the analysis remains
`ok`, with unavailable fares and chart quarters represented as null. Empty or
missing required airport metadata is an availability error.

Flight-cost estimates use the home selection fare times travelers and directions;
round-trip doubling is an estimate. Comparing the result with the whole budget
does not include hotels. Missing inputs remain unavailable. Driving hours are
only the service heuristic `straight_line_km * 1.3 / 80`; the node's unchanged
`RouteOption` contract exposes straight-line distance, not driving time or price.

For local acceptance, the Urbana → New York example reads the already available
real database with SQLite `mode=ro` and `query_only`, records explicit constraints,
runs the actual `flight_trends` node and validates its result using
`assert_contract`. The evidence includes source coverage and database hashes.
The [recorded output](issue-34-real-data.json) is produced by
`python -m scripts.verify_flight_trends_node --database /path/to/database.db --table1a /path/to/dot_table1a_airports.csv`
from `backend/`. Fixture tests are separate; they cannot replace this real-data
run. The example was repeated through this checkout's node on November 6, 2026;
contract validation passed and both database and Table 1a hashes stayed unchanged.
During final submission preparation, the node, services and diagnostic scripts
still matched the verified source hashes, the saved report matched its local
verification copy, and both input-data hashes still matched the report. The
existing result was reused without rerunning the node. The later trip-loop test
isolation fix does not change the node or its real-data inputs.
No source data or database belongs in the source-change deliverable.
