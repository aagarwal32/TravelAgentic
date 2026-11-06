# Trip-intake agent

The LangGraph agent in `backend/services/agent/` turns a user's trip request into
validated trip details (`TripConstraints` in `state.py`) before planning starts.

## How intake works

The user describes the trip in their own words. The agent fills in what's missing
with **cards**: one card at a time for required details, then one stack of optional
cards the user can skip.

```
first request ─► extract ─┬─ required detail missing → clarify      (pause: 1 card) ─┐
                    ▲     ├─ optionals not shown yet → ask_optional (pause: stack) ──┤
                    └─────┼───────────────────────────────────────────────────────────┘
                          └─ done → planning nodes (see "Planning nodes" below)
```

- **`extract`** (`nodes/extract.py`) is the only step that saves answers, and the
  only one that can call the AI.
  - Picker and button answers (`{"value": ...}`) are saved directly, with no AI.
  - Typed answers (`{"text": ...}`) go to the AI in one call with `EXTRACT_PROMPT`.
  - Everything is checked by `TripConstraints`; invalid values are dropped, and the
    card is shown again with an error.
  - Each card only accepts its own kind of answer (`to_pending` in `clarify.py`):
    picker cards take `{"value"}`, text cards take `{"text"}`, and the departure card
    takes both. A wrong kind shows the card again with its error; it never reaches the AI.
- **`clarify`** (`nodes/clarify.py`) shows the card for the first missing required
  detail. The cards are defined in `QUESTIONS`; there is no AI here.
- **`ask_optional`** (`graph.py`) shows the optional cards once (layovers, activities,
  hotel). Cards the user leaves blank count as skipped.

| Card | Input |
|---|---|
| Fly or drive, one-way or round trip, layovers | buttons |
| Travelers, budget | number |
| Return date | date picker |
| Departure date | date or range picker, **or** typed text ("sometime in March") |
| Which place ("Which Greenville?") | buttons, **or** typed text (see [Places](#places-origin-and-destination)) |
| Origin, destination, activities, hotel | typed text |

## AI model

The agent uses Claude through the Anthropic API (`services/agent/llm.py`). Add these
to your `backend/.env` (never commit it):

```env
ANTHROPIC_API_KEY=<your Claude API key>
LLM_MODEL=claude-haiku-5-5
```

`LLM_MODEL` can be any Claude model id, e.g. `claude-sonnet-5-5` or
`claude-opus-5-5`; larger models cost more per call. Requests run at low effort
(`EFFORT` in `llm.py`), since extraction is short and well specified. The key is
optional when the app starts; AI calls raise a clear error if it is missing.

## Trip dates and length

A departure is either an **exact date** (`depart_date`) or a **departure window**
(`flexible_start` to `flexible_end`). "Sometime in December" means the user can
leave on *any* day from December 1 to December 31. The window only describes the
departure; it says nothing about when the trip ends.

A round trip also needs its length. Which card asks for it depends on the departure:

| Trip | What intake needs | Card asked if missing |
|---|---|---|
| One-way | departure date or window | none |
| Round trip, exact departure date | `return_date` or `trip_nights` (code fills in the other) | "When do you want to come back?" (date picker, from the departure date) |
| Round trip, departure window | `trip_nights` | "How many nights do you want to stay?" (number, 1 to 30) |

A fixed return date does not fit a departure window: "leave any day in December,
back on the 15th" rules out most of the window, and the old "latest date you could
be back by" card only accepted dates on or after the window's end. So a flexible
round trip stores the length of the stay instead, and later steps search "depart
any day in the window, return `trip_nights` later". The nights card is a number
box, so answering it never calls the AI; out-of-range answers show the card again
with "Enter a number of nights from 1 to 30."

Date arithmetic is done in code, not by the AI: `normalize_dates` in `state.py`
runs every time trip details are validated, so it also cleans up details saved
before this rule existed.

- exact date + `trip_nights` → `return_date` = departure + nights
- exact date + `return_date` → `trip_nights` = nights in between when that is 1 to
  30; a same-day return or a longer stay is still accepted, with no `trip_nights`
- both given and they disagree → `return_date` wins, `trip_nights` is recalculated
- a new value for one of them ("actually 6 nights") replaces the value that was
  derived from the old one
- departure window + `return_date` → `return_date` is dropped and any saved
  `trip_nights` is kept

`EXTRACT_PROMPT` only extracts what the user said ("stay 2 nights" → `trip_nights: 2`,
"for a week" → 7, "a weekend" → 2, "2–3 nights" → 3). It never turns nights into
a return date or a return date into nights.

## Terminal demo

From `backend/`, with the virtual environment active:

```bash
python -m services.agent.graph                         # uses LLM_MODEL
python -m services.agent.graph --model claude-sonnet-5-5
```

Type a trip request, then answer each card. For buttons, type the option ("fly",
"round trip"); for dates, `YYYY-MM-DD` or `YYYY-MM-DD to YYYY-MM-DD`; for optional
cards, press Enter to skip. Like the real frontend, picker cards only take a value
that fits them (`round` and `$500` work; `dec 15` on the return-date card is asked
again), and only text cards and the departure card send typed text to the AI. When every required detail is known, the demo prints the
saved trip details, then one line per planning node with its status. Flight trends
reads local historical data; the flight and hotel search nodes still return stub
results, so the demo does not search live offers or book anything. Each typed
answer costs one AI call; picker answers cost none.

## Graph image

The demo saves `services/agent/trip_intake_graph.png` at startup. To render it
without calling the model:

```bash
python -m services.agent.graph --graph-only
```

Rendering uses the online Mermaid service, so it needs network access. Only the
graph's structure is sent. Git ignores the generated image.

## Using the graph from a route

The graph uses in-memory checkpoints: state lasts while the process runs and is lost
on restart. A chat route starts a chat with
`graph.invoke(start_state(text, model), config)` and resumes after each card with
`graph.invoke(Command(resume=answer), config)`, using the same thread id each time
(`chat-<chat id>`). The pause value (`result["__interrupt__"][0].value`) is the card
or the optional stack to show.

## Places (origin and destination)

Intake resolves the origin and destination **once** into exact US places, so hotels,
fare trends and flights can use the same city instead of each interpreting the name
(one "Greenville" could otherwise mean South Carolina for hotels and nothing for fares).
`extract` looks each new city up in our own tables (`services/location_service.py`,
loaded as described in [data.md](data.md#us-places-and-airports-location-lookup)), with
no AI and no API calls, and saves the result next to the text the user typed:

```json
"destination": "Greenville",
"destination_place": {"id": "us-sc-greenville", "city": "Greenville", "state": "SC",
                      "latitude": 34.8526, "longitude": -82.394,
                      "airports": ["GSP", "AVL"], "dot_market_id": 31871}
```

Hotel search accepts this saved place, and fare-trend functions accept its market ID.
The flight-trends node uses the local location and fare services. Flight and hotel
search retain the planning contracts described below.

- `airports`: airports with scheduled flights within 120 km, closest first (for the
  flight search). A driving trip keeps the place but doesn't use them.
- `dot_market_id`: the DOT Table 6 market of the closest of those airports that has one
  ("Fort Worth" -> "Dallas/Fort Worth, TX"), or `null` (no fare history then). The very
  closest airport can be a general-aviation field that DOT puts in no market (Meacham,
  FTW, for Fort Worth; BJC for Denver), so it is skipped for the market but still
  listed in `airports`.
  This deliberately differs from Issue #21's literal closest-airport rule. Approval
  of this interpretation is still required; the behavior is preserved in this change.
- Only code fills `*_place`. The AI never sees or sets them, and changing the city text
  clears the old place, so a new city never keeps the old city's airports.

### How a city is matched

Whole names only, never part of one ("Charlotte" never finds "Charlottesville").
Case, extra spaces, "St."/"Saint", "Ft."/"Fort" and a trailing ", USA" don't matter. A
state can follow the city as a code or a name ("Greenville, SC", "Greenville, South
Carolina"; the AI keeps it when the user gives one). A few nicknames work too: NYC, LA,
SF, DC, Philly, Vegas, NOLA. A state's name alone ("Florida") isn't a city. Every
Census place counts, however small: "Atlantis" is Atlantis, FL (about 2,000 people).

| Matches | What happens |
|---|---|
| One ("Fort Worth", "Chicago", "NYC", "Greenville, SC") | Saved; no card |
| Several ("Greenville", "Charlotte", "Saint Louis", "Denver") | "Which Greenville do you mean?" card, biggest first |
| None ("Narnia") | The city card again, with the error below |

Most big cities share their name with a small town somewhere (Charlotte, MI; Denver,
PA), so a city name alone usually gets the card, with the big city first. Typing the
state ("Denver, CO") skips it.

A state does not guarantee a unique place: `Greenville, NY` offers
`Greenville (CDP; Westchester County), NY` and
`Greenville (CDP; Greene County), NY`. Likewise, `Waukesha, WI` offers its city and
village separately. These labels use the existing `city` field; the ordinary name
still matches both. Each option has a distinct GEOID-suffixed ID, and selecting it
saves that entity's own coordinates, airports and market. The question remains
"Which Greenville do you mean?". A complete qualified label can also be looked up
exactly (case and whitespace aside); an unknown qualifier is rejected. Partial
county-name interpretation is not implemented.

### The place card

```json
{"type": "required_question",
 "card": {"field": "destination_place", "text": "Which Greenville do you mean?",
          "input": "choice", "allow_text": true,
          "options": [{"value": "us-nc-greenville", "label": "Greenville, NC"},
                      {"value": "us-sc-greenville", "label": "Greenville, SC"},
                      {"value": "us-tx-greenville", "label": "Greenville, TX"},
                      {"value": "us-ms-greenville", "label": "Greenville, MS"},
                      {"value": "us-wi-greenville", "label": "Greenville, WI"}]},
 "error": null}
```

- It comes right after its city card, with the five biggest matches, biggest first.
- Picking an option (`{"value": "us-sc-greenville"}`) is saved directly, with no AI
  call. An id that isn't one of the card's options shows the card again with "Choose
  one of these places, or type the city with its state."
- Typed text (`{"text": "the one in Texas"}`) goes through the AI as a new city
  ("Greenville, TX") and is looked up again.
- A city that isn't found shows the city card with: *We couldn't find that city in
  the US. Try adding the state, like "Greenville, SC".*

The terminal demo reads the location tables from the database in `DATABASE_URL`, so
load them first; if they are missing or empty it stops with the command that loads them.

## Planning nodes

After intake, three nodes gather prices. Each one writes **one** key of `TripState`
and never changes `constraints` or another node's key:

```
intake done ─┬─ fly ──► flight_trends ─► flight_search ─┐
             └─ drive ──────────────────────────────────┴─► hotel_search ─► planning (evaluator later)
```

| Node (file in `nodes/`) | Writes | Contract in `state.py` | Data | Tests |
|---|---|---|---|---|
| `flight_trends.py` | `flight_trends` | `FlightTrends` | DOT Table 6 (our DB) | `tests/test_flight_trends.py` |
| `flight_search.py` | `flight_offers` | `FlightOffers` | Duffel | `tests/test_flight_search.py` |
| `hotel_search.py` | `hotel_options` | `HotelOptions` | LiteAPI | `tests/test_hotel_search.py` |

`flight_trends` implements local historical analysis. `flight_search` remains a
stub, and `hotel_search` computes its input dates, rooms and budget before returning
an empty stub result. Their contracts and consumers are unchanged by Issue #34.
`tests/planning_fixtures.py` has sample trips and `assert_contract`, which checks
status, required top-level keys, allowed keys and non-success reasons. The node
tests also assert every successful payload field and its nested contract.

**Shared rules** (the comment block above the contracts in `state.py`):

- **Status:** every result has `status`: `"ok"`, `"skipped"` (the node doesn't apply,
  e.g. flights on a driving trip) or `"empty"` (it ran and found nothing), plus a
  `reason` when it isn't `"ok"`. An `"ok"` result has every key of its contract.
- **Fallbacks:** each node works without the result before it.
  - Flights without trends: the places' own airports, on the trip's dates.
  - Hotels without flights: the full budget, and dates from the trip.
- **Data only:** prices come from Table 6, Duffel or LiteAPI, never from the AI. Live
  results have `fetched_at`. Dates are ISO strings and money is USD.
- **Window → dates:** `flight_trends` turns a departure window into up to 3 concrete
  departure dates (`candidate_dates`), each returning `trip_nights` later. Flights and
  hotels use those dates.

**What each node reads:**

- **`flight_trends`**: the places' DOT markets and coordinates, the dates, budget and
  travelers. It tells flights where and when to search (`search_airports`,
  `candidate_dates`).
- **`flight_search`**: `search_airports` and `candidate_dates` (fallback: the places'
  airports and the trip's dates), travelers and `max_connections`. All Duffel calls
  go through one guarded client limited by `DUFFEL_DAILY_SEARCH_LIMIT`. It writes a
  shortlist of about 5 offers chosen in code, each with measured `metrics`, and
  `cheapest_total`.
- **`hotel_search`**: the stay dates (from the cheapest shortlisted flight, else trends'
  first candidate dates, else the trip's own dates), rooms (one per 2 travelers) and
  the hotel budget (budget minus `cheapest_total`). With no stay length (no
  `trip_nights` and no return date, e.g. a one-way trip), it is `"skipped"`. These
  inputs already work in the stub.


## Issue #34: flight trends and alternative airports

`flight_trends` reads the resolved places, trip dates, travelers and budget and
returns only `{"flight_trends": result}`. It does not change constraints or another
node's state. The result follows the existing `FlightTrends` contract: a successful
analysis includes `home`, `options`, `chart`, `search_airports`, `candidate_dates`
and `budget_check`. The home route remains present when it has no fare history.
All computation uses local data and code; this node makes no model or external API
calls.

### Candidate markets and fare selection

Each side measures from its own resolved city center to the nearest eligible
airport in each DOT market. A market qualifies within an inclusive **400 km
straight-line radius**, excluding the home market and any market smaller than it.
Equal-size markets qualify. Without a home market, the size filter is omitted.
ORD and MDW are one Chicago candidate, using the nearer eligible airport's
unrounded distance. Arrival distances use the destination's coordinates.

Market size uses one shared set of the dataset's newest four loaded year/quarter
pairs, or all loaded pairs if fewer than four exist. An absent market contributes
zero in that window. Normalizing unordered market pairs before counting their
passengers prevents duplicate routes or multiple airports from inflating a market.
The sum is a relative passengers/day index, not annual unique travelers. Details
and airport-evidence limitations are in [data.md](data.md#issue-34-comparison-data).

Selection fares average usable, positive, finite each-way route fares in the trip's
relevant quarters across all loaded years. Rows have equal weight; the calculation
is neither passenger-weighted nor an average of quarter means. An all-year mean is
used only when no usable fare exists in the requested quarters. Exact round trips
cover departure through return; one-way exact trips use the departure quarter.
Flexible trips use the departure-window quarters for comparison. `home.avg_fare`,
`options[].avg_fare`, savings and the budget estimate all use this selection-fare
basis across available years; the chart separately shows each seasonal mean.

For each side, select at most one alternative against the **original home fare**.
It must cost at most `0.80 * home_fare`, inclusively; if the home fare is missing,
any usable alternative fare can qualify, with unavailable savings left null.
Start with the cheapest candidate, breaking equal-fare ties by market ID. A closer
candidate replaces that original cheapest only when both conditions hold:

- `cheapest_fare >= 0.85 * closer_fare`
- `closer_distance <= 0.80 * cheapest_distance`

Among qualifying replacements, distance wins, then fare, then market ID. These
tests always use the original cheapest, never successive replacements. A closer
$100 route can replace $85, but cannot replace $84; the distance reduction must
also reach 20%. Comparisons use unrounded values. Named constants define the
radius and percentage thresholds; dollars are rounded only for presentation.

### The five decisions

1. **Dates:** preserve an exact normalized departure/return pair. For a flexible
   departure window, split its inclusive bounds at calendar-quarter and year
   boundaries. Use home seasonal fares if any relevant home quarter has history;
   otherwise use the lowest seasonal fare among selected alternatives. Ignore
   missing fares, choose the earliest segment on a fare tie, and choose the earliest
   segment if all fares are missing. Return that segment's first, lower-middle and
   last departures, deduplicated and chronological (at most three). Round-trip
   returns are departure plus `trip_nights`, even when they extend beyond the
   departure window. A saved fixed return next to a window is normalized away by
   `TripConstraints`; it is not an extra return bound. One-way returns are null.
   Missing trip type, departure inputs or a required round-trip stay produce
   `empty` before data access, rather than a fabricated departure or stay.
2. **Combined route:** compare departure alternative → home destination and home
   origin → arrival alternative independently against the same home route. The
   result lists selected departure then selected arrival alternatives, and the
   chart keeps home first. There is no combined-alternative route or new `side`
   value. Both selected airport groups can be offered to the future flight search;
   historical savings do not claim a fare for every possible airport combination.
3. **Status:** preserve the existing flight skip rules and exact reasons:
   `"not a flying trip"` and `"origin or destination not resolved to a place"`.
   Skip before accessing local data. Completed analysis is `ok` even without fare
   history. Required-data access failures produce `empty` with a reason, including
   database failures and missing, unreadable or malformed required Table 1a
   metadata; programming errors are not hidden by a blanket exception handler. Non-success results do not claim a completed analysis.
4. **Search airports:** a local airport row must agree with a market mapping from
   local Table 1a or the loader's checked `MARKET_FIXES`, with `RENAMED_CODES`
   aliases applied. Home airports precede the selected alternative's airports;
   each group is sorted by city-to-airport distance and IATA, then deduplicated.
   Without a home market, only verified place-associated codes enter the home
   group. The same eligibility rule governs candidate distances and search lists.
   A non-null market or membership in `Place.airports` alone is insufficient.
5. **Budget:** estimate flights using the home route's selection fare times the
   number of travelers, multiplied by two for a round trip and one for one-way.
   Round-trip doubling is an estimate. `typical_trip_total` is rounded to cents;
   `fits` compares the unrounded flight estimate with the whole trip budget.
   Missing fare or travelers leaves the estimate null; a missing budget leaves
   `fits` null. The service also returns no estimate without a trip type; the node
   reports that incomplete trip as `empty` before date resolution. A true result covers flights only and does not imply that
   hotels or other trip costs fit. The hotel consumer still subtracts actual
   `flight_offers.cheapest_total`, never this historical estimate.

### Chart and downstream contracts

The chart has Q1–Q4 arithmetic means across loaded years, with explicit null gaps.
It never interpolates or substitutes the all-year mean for a missing quarter.
Home remains the first series even when all four values are null, followed by
selected alternatives. Its note identifies DOT Table 6, average each-way USD and
the historical nature of the figures. Its year range is the union of years in
the displayed routes, or `"No fare history"` when none have data. Distances in
options are straight-line km.
The separate service heuristic `straight_line_km * 1.3 / 80` gives approximate
driving hours; `RouteOption` has no driving-time field, so the node does not extend
the contract or report ground-transport costs.

`flight_search.py`, `hotel_search.py`, `state.py` and their existing schemas remain
unchanged. The current flight-search stub accepts state containing the result but
does not yet execute airport/date searches. Contract tests cover its compatibility;
they do not claim live Duffel searches. The existing `hotel_search.stay_dates`
consumes the first candidate from an `ok` trend result, unless concrete flight
results take precedence. Candidate dates are ISO strings; one-way null returns
remain compatible with the hotel's existing no-stay behavior.

### Offline verification

Run from `backend/` with the existing installed dependencies:

```bash
.venv/bin/python -W always::ResourceWarning -m unittest discover -s tests
```

The service tests cover market-size aggregation, inclusive thresholds, tie
boundaries, airport eligibility and seasonal sampling. Node tests exercise the
actual adapter, preserve skip behavior, inject database failures, validate the
complete payload with `assert_contract` and deeper field assertions, and pass its
date pairs to the unchanged downstream helpers. No live API call is required.

Recorded verification on November 6, 2026, ran directly in the repository on
`feat/issue-34-flight-trends`, based on commit
`110fa94dbc496f48d10beff2b6da69a32d890dff`. The full suite passed **309 tests in
1.517 seconds**, with exit status 0 and `ResourceWarning` enabled as above. No
resource warnings, failures or skips were reported. This includes all 29 node
tests and the 74 trip-loop tests. The earlier 144-test targeted run also passed.
The successful full run supersedes the earlier missing-dependency import failure.

Trip-loop tests now bind both `extract` and `flight_trends` to the fixture's
in-memory database and supply fixture airport metadata. Teardown disposes that
database. The flying-trip regression check rejects connections to the application
engine; it caught the original isolation defect before the fix. The graph nodes
and their queries still execute, with no dependency substitutes or test skips.

Final submission preparation reused these recorded results after reviewing the
source changes and verification evidence. Only documentation changed during that
preparation; the full suite was not rerun. The existing environment's installed
distributions match the unchanged `backend/requirements.txt` pins.

Run the actual node against already available files without copying the database:

```bash
python -m scripts.verify_flight_trends_node --database /path/to/database.db --table1a /path/to/dot_table1a_airports.csv
```

The default inputs are Urbana, IL → New York, NY, January 15–22, 2027, two travelers,
round trip and a $2,000 whole-trip budget. The report includes explicit constraints,
the actual node result, `assert_contract` validation and service diagnostics. It
opens SQLite with `mode=ro` and `query_only` and verifies the database and metadata
hashes are unchanged. The recorded [real-data output](issue-34-real-data.json) is
separate from fixture tests. No data download, ingestion or external call is needed.

The node run repeated in this checkout used 105,115 Table 6 rows spanning 2022 Q1–2026 Q1,
31,828 place rows and 502 airports. The shared market-size window was 2025
Q2–2026 Q1. Home fare was $343.95 each way; Chicago was selected at $179.09,
saving $164.86 each way over 190.17 straight-line km. No arrival alternative met
the market-size filter. Search codes were CMI, MDW, ORD at departure and LGA, EWR,
JFK, HPN, ISP, SWF at arrival. The exact January 15–22 pair was retained, and the
home flight estimate was $1,375.79 with `fits: true` against $2,000. The output
passed `assert_contract`, preserved the input state and left both data hashes
unchanged. The JSON includes all four chart quarters and raw service diagnostics.
