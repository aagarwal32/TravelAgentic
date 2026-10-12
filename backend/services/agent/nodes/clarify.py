from langgraph.types import interrupt
from pydantic import ValidationError

from services.agent.state import OPTIONAL, TripConstraints, TripState, compute_missing

# "input" tells the frontend which widget to render for each field
QUESTIONS = {
    "origin":            {"text": "Where are you leaving from?", "input": "text"},
    "destination":       {"text": "Where do you want to go?", "input": "text"},
    "mode":              {"text": "Do you want to fly or drive?", "input": "choice", "options": ["fly", "drive"]},
    "trip_type":         {"text": "One-way or round trip?", "input": "choice", "options": ["one_way", "round_trip"]},
    "depart_date":       {"text": "When do you want to leave?", "input": "date"},
    "return_date":       {"text": "When do you want to come back?", "input": "date"},
    "travelers":         {"text": "How many people are traveling?", "input": "number"},
    "budget":            {"text": "What's your budget in USD?", "input": "number"},
    "driver_age":        {"text": "How old is the driver?", "input": "number"},
    "needs_car":         {"text": "Do you need a rental car?", "input": "boolean"},
    "max_connections":   {"text": "Most layovers you're okay with?", "input": "choice", "options": [0, 1, 2]},
    "activities":        {"text": "Any activities you're interested in?", "input": "list"},
    "hotel_preferences": {"text": "Anything you want in a hotel?", "input": "text"},
}


def build_questions(c: TripConstraints, skipped: list[str]) -> list[dict]:
    required = compute_missing(c)
    optional = [f for f in OPTIONAL if getattr(c, f) is None and f not in skipped]
    if c.mode == "drive":
        optional = [f for f in optional if f != "max_connections"]
    return (
        [{"field": f, **QUESTIONS[f], "required": True} for f in required]
        + [{"field": f, **QUESTIONS[f], "required": False} for f in optional]
    )


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


def clarify(state: TripState) -> dict:
    constraints = TripConstraints(**state["constraints"])
    skipped = state.get("skipped", [])
    questions = build_questions(constraints, skipped)
    if not questions:
        return {"missing": []}

    answers = interrupt({"questions": questions})

    merged = merge_answers(constraints, answers)
    newly_skipped = [q["field"] for q in questions
                     if not q["required"] and q["field"] not in answers]
    return {
        "constraints": merged.model_dump(mode="json"),
        "missing": compute_missing(merged),
        "skipped": skipped + newly_skipped,
    }
