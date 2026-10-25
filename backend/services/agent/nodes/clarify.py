from langgraph.types import interrupt
from pydantic import ValidationError

from services.agent.state import TripConstraints, TripState

# "input" tells the frontend which widget to render for each field
QUESTIONS = {
    "origin":            {"text": "Where are you leaving from?", "input": "text"},
    "destination":       {"text": "Where do you want to go?", "input": "text"},
    "mode":              {"text": "Do you want to fly or drive?", "input": "choice", "options": [{"value": "fly", "label": "Fly"}, {"value": "drive", "label": "Drive"}]},
    "trip_type":         {"text": "One-way or round trip?", "input": "choice", "options": [{"value": "one_way", "label": "One-way"}, {"value": "round_trip", "label": "Round trip"}]},
    "depart_date":       {"text": "When do you want to leave?", "input": "date_or_range", "allow_text": True},
    "return_date":       {"text": "When do you want to come back?", "input": "date"},
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
    "mode":        "Choose Fly or Drive.",
    "trip_type":   "Choose One-way or Round trip.",
    "depart_date": "Pick a departure date or range that isn't in the past.",
    "return_date": "Pick a return date after your departure.",
    "travelers":   "Enter a number from 1 to 9.",
    "budget":      "Enter a budget of at least $1.",
}

# Shown when a typed answer couldn't be used
COULD_NOT_UNDERSTAND = "Sorry, I couldn't understand that. Please try again."


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


def merge_answers(current: TripConstraints, answers: dict) -> TripConstraints:
    """Merge the user's form answers into the current constraints.

    Bad answers are dropped so compute_missing() asks for them again.
    """
    answers = dict(answers)
    while True:
        try:
            return TripConstraints(**{**current.model_dump(), **answers})
        except ValidationError as e:
            to_drop = bad_fields(e) & answers.keys()
            if not to_drop:
                return current
            for field in to_drop:
                answers.pop(field)

def build_card(field: str, c: TripConstraints) -> dict:
    card = {"field": field, **QUESTIONS[field]}

    if field == "return_date":
        if c.depart_date is not None:
            card["min"] = c.depart_date.isoformat()
        elif c.flexible_end is not None:
            card["min"] = c.flexible_end.isoformat()
            card["text"] = "What's the latest date you could be back by?"

    return card


def to_pending(card: dict, answer) -> dict:
    """Tag a resume answer with the card it answers, for extract."""
    if not isinstance(answer, dict) or ("value" in answer) + ("text" in answer) != 1:
        raise ValueError('Resume with {"value": ...} or {"text": ...}.')
    kind = "value" if "value" in answer else "text"
    return {"field": card["field"], "question": card["text"], kind: answer[kind]}

def clarify(state: TripState) -> dict:
    """Pause with a card for the first missing field; pass the answer to extract."""
    # Resuming re-runs this node, so everything before interrupt() only reads state
    c = TripConstraints(**state["constraints"])
    card = build_card(state["missing"][0], c)
    answer = interrupt({"type": "required_question", "card": card,
                        "error": state.get("field_error")})
    return {"pending_answers": [to_pending(card, answer)]}
