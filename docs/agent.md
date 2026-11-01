# Trip-intake agent

The LangGraph agent in `backend/services/agent/` turns a user's trip request into
validated trip details (`TripConstraints` in `state.py`) before planning starts.

> The card-based intake (issue #9) will change how follow-up questions work. Update
> this page when it merges.

## Terminal demo

From `backend/`, with the virtual environment active:

```bash
python -m services.agent.graph
```

- **Sign-in:** the demo reuses the `flight-agent` ChatGPT OAuth sign-in and streaming
  model call. No OpenAI API key is needed, but your account must support that OAuth
  flow. The first run opens a browser to sign in and lists the available models.
- **Model:** choose one from that list, or pass one directly with `--model <slug>`.
- **Credentials** are stored under `services/agent/.chatgpt-auth/`. Git ignores this
  folder, and credentials never enter the graph state. Never commit it.

Enter a trip request, then answer the follow-up questions in the same terminal. The
model proposes JSON field updates; Python validates and merges them before checking
which required details are still missing. Invalid values lead to another follow-up
question. Enter `/quit` at a follow-up to exit.

When every required detail is known, the demo prints the validated constraints and
calls a placeholder planning node. It does not search or book anything.

## Graph image

The demo saves `services/agent/trip_intake_graph.png` at startup. To render it
without signing in or calling a model:

```bash
python -m services.agent.graph --graph-only
```

Rendering uses the online Mermaid service, so it needs network access. Only the
graph's structure is sent. Git ignores the generated image.

## Using the graph from a route

The graph uses in-memory checkpoints: conversation state lasts while the process
runs and is lost on restart. A chat route will start a conversation with
`graph.invoke(initial_state, config)` and resume after each pause with
`graph.invoke(Command(resume=...), config)`, using the same unique thread ID each
time. The demo uses one local OAuth account, so it is not a multi-user endpoint yet.

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
These are service-level interfaces: the intake graph still ends at a planning
placeholder and does not call either service. Flight integration is also future work.

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
