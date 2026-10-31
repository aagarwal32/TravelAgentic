# Trip-intake agent

The LangGraph agent in `backend/services/agent/` turns a user's trip request into
validated trip details (`TripConstraints` in `state.py`) before planning starts.

> The card-based intake (issue #9) will change how follow-up questions work. Update
> this page when it merges.

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
