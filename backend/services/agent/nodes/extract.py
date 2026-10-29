"""Save pending answers into constraints: fixed answers directly, typed ones via the AI."""
import json
from datetime import date
from services.agent.llm import generate
from services.agent.prompts import EXTRACT_PROMPT
from services.agent.state import TripConstraints, TripState, compute_missing
from services.agent.nodes.clarify import COULD_NOT_UNDERSTAND, DATE_FIELDS, ERROR_TEXT, merge_answers


# Keys an answer to this card can fill (a departure can be a date or a window)
ANSWER_KEYS = {"depart_date": {"depart_date", "flexible_start", "flexible_end"}}


def fixed_patch(field: str, value) -> dict:
    """Turn one picker/button answer into fields to save; validation happens on merge."""
    if field == "depart_date":
        if isinstance(value, dict):  # range from the picker
            return {"flexible_start": value.get("start"), "flexible_end": value.get("end"),
                    "depart_date": None}
        return {"depart_date": value, "flexible_start": None, "flexible_end": None}
    return {field: value}


def parse_json_object(raw: str) -> dict:
    """Parse the AI's JSON reply, tolerating a ```json fence or text around the object.

    Returns {} (nothing saved) when there is no JSON object to read.
    """
    text = raw.strip()
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        # Fall back to the outermost {...}: covers ```json fences and a sentence before/after
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end < start:
            return {}
        try:
            data = json.loads(text[start:end + 1])
        except ValueError:
            return {}
    return data if isinstance(data, dict) else {}


def ai_patch(model: str, current: TripConstraints, typed: list[dict]) -> dict:
    """Read every typed answer in one AI call; return only known fields, unvalidated."""
    typed = [a for a in typed if a["text"].strip()]
    if not typed:
        return {}
    raw = generate(model, EXTRACT_PROMPT,
        json.dumps({"today": date.today().isoformat(),
                    "schema": TripConstraints.model_json_schema(),
                    "constraints": current.model_dump(mode="json"),
                    "answers": [{"field": a["field"], "question": a["question"], "text": a["text"]}
                                for a in typed]}))
    patch = parse_json_object(raw)
    # null is only allowed for date fields (switching between an exact date and a window);
    # any other null would erase a saved answer, so it's ignored like a left-out field
    return {k: v for k, v in patch.items()
            if k in TripConstraints.model_fields and (v is not None or k in DATE_FIELDS)}


def extract(state: TripState) -> dict:
    current = TripConstraints(**state.get("constraints", {}))
    pending = state.get("pending_answers", [])
    fixed = [a for a in pending if "value" in a]
    typed = [a for a in pending if "text" in a]

    patch = {}
    for a in fixed:
        patch.update(fixed_patch(a["field"], a["value"]))
    merged = merge_answers(current, patch)
    # Pass merged so the AI sees fixed answers saved this run (e.g. travelers for "$250 each")
    ai = ai_patch(state["model"], merged, typed) if typed else {}
    merged = merge_answers(merged, ai)
    missing = compute_missing(merged)

    field_error = None
    skipped = list(state.get("skipped", []))
    for a in pending:
        field = a["field"]
        if field is None:  # the first request asked about nothing in particular
            continue
        if field in ERROR_TEXT:
            if field in missing:
                # The AI read a typed answer but the value was invalid: show the card's own
                # message. COULD_NOT_UNDERSTAND is only for replies with nothing usable.
                # A rejected answer (wrong kind for the card) counts as a fixed answer here
                understood = ("value" in a or a.get("rejected")
                              or ANSWER_KEYS.get(field, {field}) & ai.keys())
                field_error = ERROR_TEXT[field] if understood else COULD_NOT_UNDERSTAND
        elif getattr(merged, field) is None and field not in skipped:
            skipped.append(field)

    return {"constraints": merged.model_dump(mode="json"), "missing": missing,
            "skipped": skipped, "field_error": field_error, "pending_answers": []}
