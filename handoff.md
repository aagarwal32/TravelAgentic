# Handoff: Issue #9 — Make trip intake transactional

Issue: https://github.com/aagarwal32/TravelAgentic/issues/9
Branch: `9-make-trip-intake-transactional` (based on `main` at `e214f1a`)

The issue is the spec. This file covers what's already done, what's left, and the traps we found while planning. Read the issue first, especially **"The new flow"**, **"Questions and inputs"** and **"Frontend contract"**.

---

## The idea in one paragraph

Intake used to be a chat: `clarify` asked the AI to write a question, and `extract` asked the AI to read the whole conversation. Now `clarify` shows **predefined question cards** and never calls the AI. Every answer goes back to `extract`:
- a **fixed answer** (button, number picker or date picker, sent as `{"value": ...}`) is saved **without the AI**
- a **typed answer** (`{"text": ...}`) and the first trip request go to the AI with `EXTRACT_PROMPT`

`extract` is the **only** node that writes `constraints`, so all validation happens there.

```
first request ──► extract ──► route
                    ▲           ├─ missing not empty ──────────────► clarify ──────┐ (one card)
                    │           ├─ nothing missing, optional not shown ► ask_optional ┤ (all optional cards)
                    └───────────┼──────────────────────────────────────────────────┘
                                └─ otherwise ──► planning
```

---

## Status

| Step | File | Status |
|---|---|---|
| 1 | `backend/services/agent/state.py` | ✅ committed `92d7022` |
| 2 | `backend/services/agent/prompts.py` | ✅ committed `3be70d2` |
| 3 | `backend/services/agent/nodes/clarify.py` | ⬜ to do |
| 4 | `backend/services/agent/nodes/extract.py` | ⬜ to do |
| 5 | `backend/services/agent/graph.py` | ⬜ to do |
| 6 | `backend/tests/test_trip_loop.py` | ⬜ to do |

**The tests are broken right now, and that's expected.** `test_text_loop_preserves_state_and_corrects_fields` fails with `KeyError: 'errors'`, because Step 1 removed `errors` and `ready` from `TripState` but `graph.py` and `extract.py` still use them. It stays broken until Steps 4–6 are done.

### What Step 1 did (`state.py`)
- Removed `driver_age`, `needs_car` and `budget_scope` from `TripConstraints`. `extra="forbid"` means those keys are now rejected if the AI returns them.
- `OPTIONAL = ["max_connections", "activities", "hotel_preferences"]`.
- `compute_missing()` no longer returns `driver_age`.
- In `TripState`: removed `ready` and `errors`, and added:
  - `pending_answers: NotRequired[list[dict]]`: answers waiting for `extract`
  - `optional_asked: NotRequired[bool]`: whether the optional stack has been shown
  - `field_error: NotRequired[Optional[str]]`: the error shown on the next card

### What Step 2 did (`prompts.py`)
- `INTAKE_PROMPT` → `EXTRACT_PROMPT`, copied exactly from the issue. It's a **plain constant with no `{{placeholders}}`**: it goes in the `instructions` slot of `generate()`, and the data goes separately as JSON in the user message.
- Deleted `CLARIFY_PROMPT`, `CONSTRAINT_KEYS`, `REQUIRED_FIELDS`, `CLARIFY_ORDER` and `OPTIONAL_FIELDS`.
- Kept `render_prompt()` and `TRENDS_PROMPT` unchanged. `TRENDS_PROMPT` still mentions `flexibility`/`budget_scope`; **that's on purpose**, because the trends issue will fix it. Say so in the PR description.
- **Nothing imports `EXTRACT_PROMPT` yet.** Step 4 does that.

---

## Setup

```bash
git fetch origin
git checkout 9-make-trip-intake-transactional
cd backend
source .venv/bin/activate
pip install -r requirements.txt
python -m unittest tests.test_trip_loop    # expect 1 error until Step 6 (see above)
```

Commit after each step, as the README says to.

---

## Step 3: `nodes/clarify.py` (no more AI)

**Keep as is:** `DATE_FIELDS`, `bad_fields()`, `merge_answers()`. `extract` uses them.
**Delete:** `build_questions()` and the current `clarify()` body (the AI call).

### 3a. `QUESTIONS`
Rewrite it from the table in the issue. The shape:

```python
QUESTIONS = {
    "origin":      {"text": "Where are you leaving from?", "input": "text"},
    "mode":        {"text": "Do you want to fly or drive?", "input": "choice",
                    "options": [{"value": "fly", "label": "Fly"}, {"value": "drive", "label": "Drive"}]},
    "depart_date": {"text": "When do you want to leave?", "input": "date_or_range", "allow_text": True},
    "travelers":   {"text": "How many people are traveling?", "input": "number", "min": 1, "max": 9},
    "budget":      {"text": "What's your total budget for the whole trip (everyone), in USD?",
                    "input": "number", "min": 1},
    # ... destination, trip_type, return_date, max_connections, activities, hotel_preferences
}
```
- **No `needs_car` or `driver_age` entries.**
- `allow_text: True` goes **only** on `depart_date`; a test checks this.
- `activities` changes from `"input": "list"` to `"input": "text"`.

### 3b. `ERROR_TEXT` and `COULD_NOT_UNDERSTAND`
- Add one fixed message for each **required** field: `origin`, `destination`, `mode`, `trip_type`, `depart_date`, `return_date`, `travelers`, `budget`. Examples from the issue:
  - travelers: `"Enter a number from 1 to 9."`
  - return_date: `"Pick a return date after your departure."`
- Add `COULD_NOT_UNDERSTAND = "Sorry, I couldn't understand that. Please try again."` for typed answers the AI couldn't use.

### 3c. `build_card(field, c: TripConstraints) -> dict`
Copy `QUESTIONS[field]`, add `"field": field`, then handle the one card whose limits depend on the trip:
- `return_date` with an exact `depart_date` → `min` = `depart_date`
- `return_date` with a range (`flexible_end` and no `depart_date`) → `min` = `flexible_end`, and change the text to `"What's the latest date you could be back by?"`

`ask_optional` in `graph.py` reuses this, so make it a separate function.

### 3d. `to_pending(card, answer) -> dict`
This checks the resume answer and tags it for `extract`:
```python
{"field": card["field"], "question": card["text"], "value": ...}   # or "text": ...
```
Raise `ValueError` if the answer isn't a dict with exactly one of `value` or `text`. The old `wait_for_reply` raised on bad input the same way.

### 3e. The new `clarify(state)`
```python
c = TripConstraints(**state["constraints"])
card = build_card(state["missing"][0], c)
answer = interrupt({"type": "required_question", "card": card, "error": state.get("field_error")})
return {"pending_answers": [to_pending(card, answer)]}
```
- Import `interrupt` from `langgraph.types`.
- **Don't write `constraints` here.** That's `extract`'s job.

> ⚠️ **Resuming re-runs the node from line 1.** When the user answers, LangGraph calls `clarify` again, and `interrupt()` returns the answer instead of pausing. Everything before `interrupt()` must have no side effects and give the same result both times.

---

## Step 4: `nodes/extract.py` (two paths)

Imports you'll need: `EXTRACT_PROMPT` from `prompts`, `OPTIONAL` from `state`, and `QUESTIONS`, `ERROR_TEXT`, `COULD_NOT_UNDERSTAND`, `merge_answers` from `clarify`.

### Input
`state["pending_answers"]` holds a list of items like these:
```python
{"field": None,      "question": None,                 "text": "Chicago to NYC Dec 18-22"}  # first request
{"field": "travelers", "question": "How many people…", "value": 2}                       # fixed
{"field": "origin",  "question": "Where are you leaving from?", "text": "O'Hare"}          # typed
```

### Algorithm
1. **Fixed answers → patch, no AI.** Use a helper `fixed_patch(field, value)`:
   - `depart_date` with a dict `{"start", "end"}` → `{"flexible_start": start, "flexible_end": end, "depart_date": None}`
   - `depart_date` with a string → `{"depart_date": v, "flexible_start": None, "flexible_end": None}`
   - anything else → `{field: value}`

   Merge with `merge_answers(current, fixed)`.
2. **Typed answers → one AI call.** Collect *every* item with non-empty `text`, including the first request, and call `generate(state["model"], EXTRACT_PROMPT, json.dumps({...}))` **once**. The JSON has `today`, `schema`, `constraints` and **`answers`** (replacing the old `"conversation"`), where each answer is `{field, question, text}`. Turn the reply into a dict, dropping keys not in `TripConstraints.model_fields` and treating bad JSON or a non-dict as `{}`. Then merge with `merge_answers()`.
3. `missing = compute_missing(merged)`.
4. **Check each field that was asked** (skip `field: None`):
   - required and still in `missing` → `field_error = ERROR_TEXT[field]` if it was a fixed answer, else `COULD_NOT_UNDERSTAND`
   - optional and still `None` → add it to `skipped`
5. Return `{"constraints": merged.model_dump(mode="json"), "missing": missing, "skipped": skipped, "field_error": field_error, "pending_answers": []}`

### Traps
- **Set `field_error` on every run**, to `None` when nothing was rejected. Otherwise an old error shows up on the next card.
- **Clear `pending_answers`.** Otherwise the same answers get processed again on the next pass.
- **Empty or whitespace text:** don't send it to the AI. It stays missing and gets `COULD_NOT_UNDERSTAND`.
- **A whole-model date error** (from `check_dates`, which has an empty `loc`) makes `bad_fields()` blame every `DATE_FIELDS` entry. So an invalid range drops all the date keys in that patch, and the old values stay. That's the behavior you want.
- **Remove `ready` and `errors`** from the return value; they're no longer in `TripState`.

---

## Step 5: `graph.py`

- **Delete `wait_for_reply`.**
- **Add `ask_optional(state)`:**
  1. `fields` = the `OPTIONAL` fields that are `None` in `constraints` and not in `skipped`; leave out `max_connections` if `mode == "drive"`.
  2. If `fields` is empty → `return {"optional_asked": True}`.
  3. `cards = [build_card(f, c) for f in fields]`, then `reply = interrupt({"type": "optional_cards", "cards": cards})`.
  4. `answers = reply.get("answers", {})`. Answered cards go to `pending_answers` via `to_pending()`; unanswered ones get added to `skipped`; set `optional_asked: True`.
- **Add `route_after_extract(state) -> str`:**
  - `state["missing"]` not empty → `"clarify"`
  - `optional_asked` not set → `"ask_optional"`
  - otherwise → `"planning"`
- **Edges:**
  ```python
  builder.add_edge(START, "extract")
  builder.add_conditional_edges("extract", route_after_extract, ["clarify", "ask_optional", "planning"])
  builder.add_edge("clarify", "extract")
  builder.add_edge("ask_optional", "extract")
  builder.add_edge("planning", END)
  ```
- **Start state** (useful for the demo and tests):
  ```python
  {"pending_answers": [{"field": None, "question": None, "text": request}],
   "model": model, "constraints": {}, "missing": [], "skipped": []}
  ```
- **`main()` demo:**
  - For a `required_question`: print `error` if there is one, print the card, read a line, and resume with `{"value": ...}` if the line fits the card's input (a number, a choice label or value, an ISO date, or `YYYY-MM-DD to YYYY-MM-DD` for a range). Otherwise resume with `{"text": line}`.
  - For `optional_cards`: ask each card in turn, an empty line skips it, and resume with `{"answers": {...}}`.

---

## Step 6: `tests/test_trip_loop.py`

- **Delete:** `test_text_loop_preserves_state_and_corrects_fields`, `test_invalid_patch_keeps_previous_values`, `test_malformed_json_and_unknown_fields`. They test `ready`, `errors` and chat messages.
- **Keep:** `test_flexible_round_trip_requires_return`, `test_oauth_transport`.
- **Add** the 11 checklist tests from the issue.
  - Mock only `services.agent.nodes.extract.generate`. Patch it once in `setUp`, and use `assert_not_called()` for the "no AI" checks.
  - Use `2099-…` dates so the past-date validator never fails.
  - Small helpers make the tests short: `start(first_patch)` invokes the start state, `answer(resume, ai_patch=None)` resumes, and both return `result["__interrupt__"][0].value`, or `None` once the graph finishes.
- **Worth adding as well:** "typing `flying out of O'Hare` on the origin card sets origin and mode, and the mode card is skipped". It tests the "one answer can fill several fields" behavior from the issue.

---

## Done when

1. `python -m unittest tests.test_trip_loop` passes.
2. `grep -rn "needs_car\|driver_age\|budget_scope\|wait_for_reply\|build_questions\|CLARIFY_PROMPT\|\"ready\"" services tests` finds nothing.
3. `python -m services.agent.graph --graph-only` draws extract ↔ clarify, extract ↔ ask_optional, extract → planning.
4. `python -m services.agent.graph`: walk through the issue's Flows 1–3 by hand, including the `idk` retry and the date-range return card.
5. Open a PR into `main` with `Closes #9`, and mention that `TRENDS_PROMPT` still uses the old fields on purpose.
