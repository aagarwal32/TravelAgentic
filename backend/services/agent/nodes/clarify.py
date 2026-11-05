from langgraph.types import interrupt
from pydantic import ValidationError

from services.agent.state import TRIP_NIGHTS_MAX, TRIP_NIGHTS_MIN, TripConstraints, TripState

# "input" tells the frontend which widget to render for each field
QUESTIONS = {
    "origin":            {"text": "Where are you leaving from?", "input": "text"},
    "destination":       {"text": "Where do you want to go?", "input": "text"},
    # Asked right after their city when it fits several US places; build_card fills in the
    # city and the options from state["place_choices"]. Typing another city is allowed.
    "origin_place":      {"text": "Which {city} do you mean?", "input": "choice", "allow_text": True, "options": []},
    "destination_place": {"text": "Which {city} do you mean?", "input": "choice", "allow_text": True, "options": []},
    "mode":              {"text": "Do you want to fly or drive?", "input": "choice", "options": [{"value": "fly", "label": "Fly"}, {"value": "drive", "label": "Drive"}]},
    "trip_type":         {"text": "One-way or round trip?", "input": "choice", "options": [{"value": "one_way", "label": "One-way"}, {"value": "round_trip", "label": "Round trip"}]},
    "depart_date":       {"text": "When do you want to leave?", "input": "date_or_range", "allow_text": True},
    "return_date":       {"text": "When do you want to come back?", "input": "date"},
    # Asked instead of a return date when the departure is a window ("any day in
    # December"): the trip's length is what fits a flexible departure. A number box,
    # so the answer is saved without an AI call.
    "trip_nights":       {"text": "How many nights do you want to stay?", "input": "number", "min": TRIP_NIGHTS_MIN, "max": TRIP_NIGHTS_MAX},
    "travelers":         {"text": "How many people are traveling?", "input": "number", "min": 1, "max": 9},
    "budget":            {"text": "What's your total budget for the whole trip (everyone), in USD?", "input": "number", "min": 1},
    "max_connections":   {"text": "Most layovers you're okay with?", "input": "choice", "options": [{"value": 0, "label": "Nonstop only"}, {"value": 1, "label": "Up to 1 stop"}, {"value": 2, "label": "Up to 2 stops"}]},
    "activities":        {"text": "Any activities you're interested in?", "input": "text"},
    "hotel_preferences": {"text": "Anything you want in a hotel?", "input": "text"},
}

# Shown on the same card again when a fixed answer is rejected
ERROR_TEXT = {
    "origin":      "Enter the city you're leaving from.",
    "destination": "Enter the city you want to go to.",
    "origin_place":      "Choose one of these places, or type the city with its state.",
    "destination_place": "Choose one of these places, or type the city with its state.",
    "mode":        "Choose Fly or Drive.",
    "trip_type":   "Choose One-way or Round trip.",
    "depart_date": "Pick a date or range from today on, with the end after the start.",
    "return_date": "Pick a return date after your departure.",
    "trip_nights": f"Enter a number of nights from {TRIP_NIGHTS_MIN} to {TRIP_NIGHTS_MAX}.",
    "travelers":   "Enter a number from 1 to 9.",
    "budget":      "Enter a budget of at least $1.",
}

# Shown when a typed answer couldn't be used
COULD_NOT_UNDERSTAND = "Sorry, I couldn't understand that. Please try again."

# Shown on the city card again when a typed city isn't a US place we know
PLACE_NOT_FOUND = "We couldn't find that city in the US. Try adding the state, like \"Greenville, SC\"."


DATE_FIELDS = ["depart_date", "return_date", "flexible_start", "flexible_end"]


def bad_fields(e: ValidationError) -> set[str]:
    """Return the names of the fields that caused this validation error."""
    bad = set()
    for err in e.errors():
        if not err["loc"]:
            bad.update(DATE_FIELDS)
        else:
            bad.add(err["loc"][0])
    return bad


def stale_dates(current: TripConstraints, answers: dict) -> dict:
    """Null the return date or trip length that a new value for the other makes stale.

    TripConstraints.normalize_dates derives one from the other, and lets return_date
    win when both are set. Without this, a saved return date computed from "4 nights"
    would win over a new "actually 6 nights" instead of being derived again from it.
    A return date only replaces the nights after an exact departure: with a window
    (or when switching to one) the return date is dropped and the nights are kept.
    """
    if "trip_nights" in answers and "return_date" not in answers:
        return {"return_date": None}
    exact = answers.get("depart_date", current.depart_date) is not None
    if "return_date" in answers and "trip_nights" not in answers and exact:
        return {"trip_nights": None}
    return {}


def merge_answers(current: TripConstraints, answers: dict) -> TripConstraints:
    """Merge the user's form answers into the current constraints.

    Bad answers are dropped so compute_missing() asks for them again.
    """
    answers = dict(answers)
    while True:
        try:
            return TripConstraints(**{**current.model_dump(), **answers, **stale_dates(current, answers)})
        except ValidationError as e:
            to_drop = bad_fields(e) & answers.keys()
            if not to_drop:
                return current
            for field in to_drop:
                answers.pop(field)

def build_card(field: str, c: TripConstraints, place_choices: dict | None = None) -> dict:
    card = {"field": field, **QUESTIONS[field]}

    if field in ("origin_place", "destination_place"):
        # Built from what extract saved: no database lookup before interrupt()
        options = list((place_choices or {}).get(field, []))
        city = options[0]["label"].split(" (")[0].split(",")[0] if options else "city"
        card.update(text=card["text"].format(city=city), options=options)
    # The return card is only asked after an exact departure date (a departure window
    # asks for trip_nights instead), so its earliest pick is that date
    elif field == "return_date" and c.depart_date is not None:
        card["min"] = c.depart_date.isoformat()

    return card


def to_pending(card: dict, answer) -> dict:
    """Tag a resume answer with the card it answers, for extract."""
    if not isinstance(answer, dict) or ("value" in answer) + ("text" in answer) != 1:
        raise ValueError('Resume with {"value": ...} or {"text": ...}.')
    kind = "value" if "value" in answer else "text"
    # Enforce the card's input type here, not only in the frontend: picker cards never
    # cost an AI call, and text cards always get the AI's cleanup ("O'Hare" -> "Chicago")
    if kind == "text" and card["input"] != "text" and not card.get("allow_text"):
        raise ValueError(f'{card["field"]} takes a picked value: resume with {{"value": ...}}.')
    if kind == "value" and card["input"] == "text":
        raise ValueError(f'{card["field"]} takes typed text: resume with {{"text": ...}}.')
    return {"field": card["field"], "question": card["text"], kind: answer[kind]}

def clarify(state: TripState) -> dict:
    """Pause with a card for the first missing field; pass the answer to extract."""
    # Resuming re-runs this node, so everything before interrupt() only reads state
    c = TripConstraints(**state["constraints"])
    card = build_card(state["missing"][0], c, state.get("place_choices"))
    answer = interrupt({"type": "required_question", "card": card,
                        "error": state.get("field_error")})
    try:
        pending = to_pending(card, answer)
    except ValueError:
        # Raising here would leave the chat stuck; extract shows the card again with its error
        pending = {"field": card["field"], "question": card["text"], "rejected": True}
    return {"pending_answers": [pending]}
