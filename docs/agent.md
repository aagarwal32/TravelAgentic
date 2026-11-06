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
departure; the user can also supply a fixed return date or a number of nights.
Any departure selected later must satisfy those details too.

A round trip needs a return date or a stated stay length. If neither is known,
intake asks for the return date, for both exact and flexible departures:

| Trip | What intake needs | Card asked if missing |
|---|---|---|
| One-way | departure date or window | none |
| Round trip, exact departure date | `return_date` or `trip_nights` (code fills in the other) | "When do you want to come back?" (date picker, from the departure date) |
| Round trip, departure window | `return_date` or `trip_nights`; both are kept if supplied and compatible | "When do you want to come back?" (date picker, from the window's start) |

A fixed return can fall inside the departure window. "Sometime in December,
return December 15" preserves December 1–31 and the December 15 return; it does
not ask for nights. The return picker uses the **start**, not the end, of the
departure window as its minimum. Its fixed answer is saved without an AI call.

For this app, **"return by December 15" means the same exact return date as
"return on December 15"**. It is not a separate latest-return deadline.

If the user supplies nights instead ("sometime in December, stay 2 nights"),
intake accepts `trip_nights` without asking a return-date question. Stated nights
must be 1–30. A window plus return date without nights leaves the length unknown
until a departure is selected; the code does not invent a duration.

Date arithmetic is done in code, not by the AI: `normalize_dates` in `state.py`
runs every time trip details are validated, including when saved details are
loaded. It preserves valid window/return combinations and rejects incompatible ones.

- exact date + `trip_nights` → `return_date` = departure + nights
- exact date + `return_date` → `trip_nights` = nights in between when that is 1 to
  30; a same-day return or a longer stay is still accepted, with no `trip_nights`
- exact departure, both given and they disagree → `return_date` wins, `trip_nights`
  is recalculated
- exact departure, a new value for one of them ("actually 6 nights") replaces the
  value that was derived from the old one
- departure window + `return_date` → both are preserved; the return cannot be
  before the window starts
- departure window + `return_date` + `trip_nights` → return minus nights must fall
  inside the window, including its boundaries; the original window stays saved
- with a window, editing nights does not erase the return date, and editing the
  return date does not erase nights; an incompatible update is rejected

For example, a December 1–31 window with a December 15 return and 2 nights implies
a December 13 departure. A December 2 return with 2 nights would require leaving
November 30, so it is rejected. A January 2 return with 2 nights is valid because
December 31 is inside the window.

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
saved trip details, then one line per planning node with its status (the nodes are
stubs for now, so nothing is searched or booked). Each typed answer costs one AI
call; picker answers cost none.

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
The planning nodes (see "Planning nodes") will call them; for now those nodes are
stubs and call no service.

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

All three are **stubs** for now: they return a valid, empty result, so each one can be
built on its own branch. Replace a stub's body, keep its contract, and add tests to
its own test file. `tests/planning_fixtures.py` has sample trips and
`assert_contract`, which fails if a result has a key its contract doesn't list.

**Shared rules** (the comment block above the contracts in `state.py`):

- **Status:** every result has `status`: `"ok"`, `"skipped"` (the node doesn't apply,
  e.g. flights on a driving trip) or `"empty"` (it ran and found nothing), plus a
  `reason` when it isn't `"ok"`. An `"ok"` result has every key of its contract.
- **Fallbacks:** each node works without the result before it.
  - Flights without trends: the places' own airports, on the trip's dates.
  - Hotels without flights: the full budget, and dates from the trip.
- **Data only:** prices come from Table 6, Duffel or LiteAPI, never from the AI. Live
  results have `fetched_at`. Dates are ISO strings and money is USD.
- **Window → dates:** the future `flight_trends` implementation must produce
  `candidate_dates` that satisfy the original window and the supplied return details:
  - Nights only: up to 3 departures inside the window, returning `trip_nights` later.
  - Fixed return only: up to 3 departures inside the window and no later than the
    fixed return, all using that same return date.
  - Fixed return and nights: the single departure given by return minus nights.
  Flights and hotels use those concrete dates. Flight nodes are still stubs.

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
  inputs already work in the stub. Without flight/trend dates, a window plus
  return and nights uses return minus nights for check-in. Otherwise it uses the
  exact departure or window start, with checkout from the return date or nights.
