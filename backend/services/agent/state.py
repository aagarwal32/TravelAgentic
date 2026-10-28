from datetime import date
from typing import Annotated, Literal, Optional, TypedDict, NotRequired
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field, model_validator, field_validator, ConfigDict

class TripConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin: Optional[str] = None
    destination: Optional[str] = None
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

def compute_missing(c: TripConstraints) -> list[str]:
    missing = [f for f in REQUIRED if getattr(c, f) is None]
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
    messages: Annotated[list, add_messages]   
    constraints: dict                       
    missing: list[str]
    skipped: list[str]
    trends: NotRequired[dict]
    offers: NotRequired[dict]
    hotel_matches: NotRequired[list]
    evaluation: NotRequired[dict]
