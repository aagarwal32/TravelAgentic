from datetime import date
from typing import Annotated, Literal, Optional, TypedDict, NotRequired
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field, model_validator, field_validator, ConfigDict


class Place(BaseModel):
    """One exact US place that a typed city resolved to (services/location_service.py).

    Intake resolves origin and destination once and saves the Place, so hotels, fare
    trends and flights all use the same city instead of each interpreting the name.
    """
    model_config = ConfigDict(extra="forbid")
    id: str                    # our id, e.g. "us-sc-greenville"
    city: str                  # "Greenville"
    state: str                 # "SC"
    latitude: float
    longitude: float
    airports: list[str]        # nearby airports with scheduled flights, closest first: ["GSP"]
    dot_market_id: Optional[int] = None   # DOT Table 6 market of the closest airport that has one


class TripConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin: Optional[str] = None               # what the user typed
    destination: Optional[str] = None
    # What the text resolved to. Only code fills these (never the AI), and a changed
    # origin/destination text clears its place so old airports never stick to a new city.
    origin_place: Optional[Place] = None
    destination_place: Optional[Place] = None
    mode: Optional[Literal["fly", "drive"]] = None
    trip_type: Optional[Literal["one_way", "round_trip"]] = None
    depart_date: Optional[date] = None
    flexible_start: Optional[date] = None   # "sometime in March" → Mar 1
    flexible_end: Optional[date] = None     #                     → Mar 31
    budget: Optional[float] = Field(None, gt=0)   
    travelers: Optional[int] = Field(None, ge=1, le=9)
    return_date: Optional[date] = None
    max_connections: Optional[int] = Field(None, ge=0, le=2)
    activities: Optional[list[str]] = None
    hotel_preferences: Optional[str] = None

    @field_validator("origin", "destination", "hotel_preferences")
    @classmethod
    def nonempty_text(cls, value):
        if value is not None:
            value = value.strip()
            if not value:
                raise ValueError("Text must not be empty")
        return value


    @model_validator(mode="after")
    def check_dates(self):
        today = date.today()
        if self.flexible_start and self.flexible_start < today:
            raise ValueError("flexible_start is in the past")
        if self.flexible_end and self.flexible_end < today:
            raise ValueError("flexible_end is in the past")
        if self.depart_date and self.depart_date < today:
          raise ValueError("depart_date is in the past")
        if self.return_date and self.return_date < today:
          raise ValueError("return_date is in the past")
        if self.depart_date and self.return_date and self.return_date < self.depart_date:
            raise ValueError("return_date before depart_date")
        if self.flexible_start and self.flexible_end and self.flexible_end < self.flexible_start:
            raise ValueError("flexible window is backwards")
        if self.return_date and not self.depart_date and self.flexible_end and self.return_date < self.flexible_end:
            raise ValueError("return_date before the end of the departure window")
        return self

REQUIRED = ["origin", "destination", "mode", "trip_type", "budget", "travelers"]
OPTIONAL = ["max_connections", "activities", "hotel_preferences"]
# The place field that belongs to each city text field
PLACE_FIELDS = {"origin": "origin_place", "destination": "destination_place"}

def compute_missing(c: TripConstraints) -> list[str]:
    missing = []
    for f in REQUIRED:
        if getattr(c, f) is None:
            missing.append(f)
        elif f in PLACE_FIELDS and getattr(c, PLACE_FIELDS[f]) is None:
            # The city is typed but not resolved yet (several matches): ask right after it
            missing.append(PLACE_FIELDS[f])
    has_dates = c.depart_date or (c.flexible_start and c.flexible_end)
    if not has_dates:
        missing.append("depart_date")
    if c.trip_type == "round_trip" and not c.return_date:
        missing.append("return_date")
    return missing

class TripState(TypedDict):
    model: NotRequired[str]
    pending_answers: NotRequired[list[dict]]   # answers waiting for extract
    optional_asked: NotRequired[bool]          # optional stack already shown
    field_error: NotRequired[Optional[str]]    # error text for the next card
    # Places offered on an open "Which Greenville?" card, as card options
    # ({"destination_place": [{"value": "us-sc-greenville", "label": "Greenville, SC"}, ...]}).
    # Saved by extract so clarify builds the card without a database lookup.
    place_choices: NotRequired[dict[str, list[dict]]]
    messages: Annotated[list, add_messages]   
    constraints: dict                       
    missing: list[str]
    skipped: list[str]
    trends: NotRequired[dict]
    offers: NotRequired[dict]
    hotel_matches: NotRequired[list]
    evaluation: NotRequired[dict]
