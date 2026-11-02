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
                          └─ done → planning (placeholder for now)
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
saved trip details. It does not search or book anything. Each typed answer costs one
AI call; picker answers cost none.

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
