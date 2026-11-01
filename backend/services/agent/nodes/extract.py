"""Save pending answers into constraints: fixed answers directly, typed ones via the AI.

Then each origin/destination whose text is new is resolved ONCE into an exact US place
(services/location_service.py: our own tables, no AI, no API calls) and saved next to
the text. Hotels, fare trends and flights all read that saved place, so they agree on
which city the trip is about. The lookup happens here, not in clarify, because clarify
re-runs on resume and must only read state before its interrupt().
"""
import json
from datetime import date
from db.database import SessionLocal
from services.agent.llm import generate
from services.agent.prompts import EXTRACT_PROMPT
from services.agent.state import PLACE_FIELDS, TripConstraints, TripState, compute_missing
from services.agent.nodes.clarify import (COULD_NOT_UNDERSTAND, DATE_FIELDS, ERROR_TEXT,
                                          PLACE_NOT_FOUND, merge_answers)
from services.location_service import find_places, get_place, normalize_name, place_label

PLACE_CARDS = set(PLACE_FIELDS.values())   # origin_place, destination_place

# Keys an answer to this card can fill: typed text on a "Which Greenville?" card is a new
# city name, saved as origin/destination and resolved again
ANSWER_KEYS = {"origin_place": {"origin"}, "destination_place": {"destination"}}


def fixed_patch(field: str, value) -> dict:
    """Turn one picker/button answer into fields to save; validation happens on merge."""
    if field == "depart_date":
        if isinstance(value, dict):  # range from the picker
            return {"flexible_start": value.get("start"), "flexible_end": value.get("end"),
                    "depart_date": None}
        return {"depart_date": value, "flexible_start": None, "flexible_end": None}
    return {field: value}


def ai_schema() -> dict:
    """The TripConstraints schema the AI sees: without the *_place fields, which only code fills."""
    schema = TripConstraints.model_json_schema()
    for field in PLACE_CARDS:
        schema["properties"].pop(field, None)
    schema.get("$defs", {}).pop("Place", None)
    if not schema.get("$defs"):
        schema.pop("$defs", None)
    return schema


def ai_patch(model: str, current: TripConstraints, typed: list[dict]) -> dict:
    """Read every typed answer in one AI call; return only known fields, unvalidated."""
    typed = [a for a in typed if a["text"].strip()]
    if not typed:
        return {}
    raw = generate(model, EXTRACT_PROMPT,
        json.dumps({"today": date.today().isoformat(),
                    "schema": ai_schema(),
                    "constraints": current.model_dump(mode="json", exclude=PLACE_CARDS),
                    "answers": [{"field": a["field"], "question": a["question"], "text": a["text"]}
                                for a in typed]}))
    try:
        patch = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(patch, dict):
        return {}
    # The AI never sets a place: those come only from our own lookup (or the user's pick).
    # null is only allowed for date fields (switching between an exact date and a window);
    # any other null would erase a saved answer, so it's ignored like a left-out field
    return {k: v for k, v in patch.items()
            if k in TripConstraints.model_fields and k not in PLACE_CARDS
            and (v is not None or k in DATE_FIELDS)}


def same_city(a: str | None, b: str | None) -> bool:
    """Whether two city texts name the same city ("St. Louis" = "saint louis")."""
    return a == b or (a is not None and b is not None and normalize_name(a) == normalize_name(b))


def resolve_places_for(current: TripConstraints, merged: TripConstraints, picked: list[dict],
                       choices: dict) -> tuple[TripConstraints, list[str]]:
    """Fill origin_place/destination_place in merged; return (merged, fields with no match).

    - A changed city text drops its old place and card choices, so a new city never keeps
      the old city's airports.
    - picked: fixed answers to a place card ({"value": "<place id>"}). Saved only if the id
      was one of that card's options; otherwise the card stays open (extract shows its error).
    - A city text without a place is looked up: no match clears the text (so its card is
      asked again with PLACE_NOT_FOUND), one match is saved, several become the card's
      options in `choices` (changed in place).
    """
    updates, not_found = {}, []
    for text_field, place_field in PLACE_FIELDS.items():
        if not same_city(getattr(merged, text_field), getattr(current, text_field)):
            updates[place_field] = None
            choices.pop(place_field, None)
    merged = merged.model_copy(update=updates)

    todo = [f for f, p in PLACE_FIELDS.items()
            if getattr(merged, f) is not None and getattr(merged, p) is None and p not in choices]
    if not picked and not todo:
        return merged, not_found
    with SessionLocal() as db:
        for answer in picked:
            field = answer["field"]
            offered = [option["value"] for option in choices.get(field, [])]
            place = get_place(db, answer["value"]) if answer["value"] in offered else None
            if place is not None:
                merged = merged.model_copy(update={field: place})
                choices.pop(field)
        for text_field in todo:
            place_field = PLACE_FIELDS[text_field]
            places = find_places(db, getattr(merged, text_field))
            if not places:
                merged = merged.model_copy(update={text_field: None})
                not_found.append(text_field)
            elif len(places) == 1:
                merged = merged.model_copy(update={place_field: places[0]})
            else:
                choices[place_field] = [{"value": p.id, "label": place_label(p)} for p in places]
    return merged, not_found


def extract(state: TripState) -> dict:
    current = TripConstraints(**state.get("constraints", {}))
    pending = state.get("pending_answers", [])
    fixed = [a for a in pending if "value" in a and a["field"] not in PLACE_CARDS]
    picked = [a for a in pending if "value" in a and a["field"] in PLACE_CARDS]
    typed = [a for a in pending if "text" in a]

    patch = {}
    for a in fixed:
        patch.update(fixed_patch(a["field"], a["value"]))
    merged = merge_answers(current, patch)
    # Pass merged so the AI sees fixed answers saved this run (e.g. travelers for "$250 each")
    ai = ai_patch(state["model"], merged, typed) if typed else {}
    merged = merge_answers(merged, ai)
    choices = dict(state.get("place_choices", {}))
    merged, not_found = resolve_places_for(current, merged, picked, choices)
    missing = compute_missing(merged)

    field_error = None
    skipped = list(state.get("skipped", []))
    for a in pending:
        field = a["field"]
        if field is None:  # the first request asked about nothing in particular
            continue
        if field in ERROR_TEXT:
            if field in missing:
                # A typed new city that is still ambiguous gets the card's own message;
                # COULD_NOT_UNDERSTAND is only for replies with nothing usable
                understood = "value" in a or ANSWER_KEYS.get(field, set()) & ai.keys()
                field_error = ERROR_TEXT[field] if understood else COULD_NOT_UNDERSTAND
        elif getattr(merged, field) is None and field not in skipped:
            skipped.append(field)
    if not_found:
        # Ask for the city that wasn't found first, with the US-only message
        field_error = PLACE_NOT_FOUND
        missing = not_found + [f for f in missing if f not in not_found]

    return {"constraints": merged.model_dump(mode="json"), "missing": missing,
            "skipped": skipped, "field_error": field_error, "pending_answers": [],
            "place_choices": choices}
