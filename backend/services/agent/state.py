from datetime import date, timedelta
from typing import Annotated, Literal, Optional, TypedDict, NotRequired
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field, model_validator, field_validator, ConfigDict

# How long a round trip can be, whether the user gives nights or a return date
TRIP_NIGHTS_MIN, TRIP_NIGHTS_MAX = 1, 30

class TripConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin: Optional[str] = None
    destination: Optional[str] = None
    mode: Optional[Literal["fly", "drive"]] = None
    trip_type: Optional[Literal["one_way", "round_trip"]] = None
    depart_date: Optional[date] = None
    # A departure window: the user can leave on ANY day from flexible_start to
    # flexible_end ("sometime in March" -> Mar 1 to Mar 31). It only describes the
    # departure, so a window goes with trip_nights, never with a fixed return_date.
    flexible_start: Optional[date] = None
    flexible_end: Optional[date] = None
    budget: Optional[float] = Field(None, gt=0)   
    travelers: Optional[int] = Field(None, ge=1, le=9)
    # Round trips: after an exact departure date normalize_dates derives one of these
    # from the other (nights only when the stay is 1 to 30, the range the nights card
    # can express); after a departure window only trip_nights is set.
    return_date: Optional[date] = None
    trip_nights: Optional[int] = Field(None, ge=TRIP_NIGHTS_MIN, le=TRIP_NIGHTS_MAX)
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
    def normalize_dates(self):
        """Check the dates, then derive return_date and trip_nights from each other.

        The date math lives here, in code: the AI only extracts what the user said
        ("4 nights" -> trip_nights: 4) and never computes a return date itself. This
        runs on every validation, so a saved trip that still has a return date next
        to a departure window is cleaned up when it is loaded.
        """
        today = date.today()
        if self.depart_date is None and self.flexible_start and self.flexible_end and self.return_date:
            # A fixed return date does not fit "leave any day in the window". The stay
            # length is trip_nights instead, so drop it (from the AI or an old saved state)
            self.return_date = None
        if self.flexible_start and self.flexible_start < today:
            raise ValueError("flexible_start is in the past")
        if self.flexible_end and self.flexible_end < today:
            raise ValueError("flexible_end is in the past")
        if self.depart_date and self.depart_date < today:
            raise ValueError("depart_date is in the past")
        if self.return_date and self.return_date < today:
            raise ValueError("return_date is in the past")
        if self.flexible_start and self.flexible_end and self.flexible_end < self.flexible_start:
            raise ValueError("flexible window is backwards")
        if self.depart_date and self.return_date:
            if self.return_date < self.depart_date:
                raise ValueError("return_date before depart_date")
            # Exact departure + return date: return_date wins and trip_nights follows from
            # it. A same-day return or a stay over 30 nights is still a valid return date
            # (as before this field existed); it just has no trip_nights.
            nights = (self.return_date - self.depart_date).days
            self.trip_nights = nights if TRIP_NIGHTS_MIN <= nights <= TRIP_NIGHTS_MAX else None
        elif self.depart_date and self.trip_nights:
            # Exact departure + nights: the return date is departure + nights
            self.return_date = self.depart_date + timedelta(days=self.trip_nights)
        return self

REQUIRED = ["origin", "destination", "mode", "trip_type", "budget", "travelers"]
OPTIONAL = ["max_connections", "activities", "hotel_preferences"]

def compute_missing(c: TripConstraints) -> list[str]:
    missing = [f for f in REQUIRED if getattr(c, f) is None]
    has_dates = c.depart_date or (c.flexible_start and c.flexible_end)
    if not has_dates:
        missing.append("depart_date")
    if c.trip_type == "round_trip":
        # Asked right after the departure, as the return date was before
        if c.depart_date:
            # Exact departure: a return date OR a number of nights is enough, since
            # normalize_dates fills in the other one
            if c.return_date is None and c.trip_nights is None:
                missing.append("return_date")
        elif c.trip_nights is None:
            # Departure window (or no departure yet): ask how long the trip is, never
            # for a return date, because "leave any day in the window" has no fixed end
            missing.append("trip_nights")
    return missing

class TripState(TypedDict):
    model: NotRequired[str]
    pending_answers: NotRequired[list[dict]]   # answers waiting for extract
    optional_asked: NotRequired[bool]          # optional stack already shown
    field_error: NotRequired[Optional[str]]    # error text for the next card
    messages: Annotated[list, add_messages]   
    constraints: dict                       
    missing: list[str]
    skipped: list[str]
    trends: NotRequired[dict]
    offers: NotRequired[dict]
    hotel_matches: NotRequired[list]
    evaluation: NotRequired[dict]
