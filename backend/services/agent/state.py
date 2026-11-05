from datetime import date, timedelta
from typing import Annotated, Literal, Optional, TypedDict, NotRequired
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field, model_validator, field_validator, ConfigDict

# How long a round trip can be, whether the user gives nights or a return date
TRIP_NIGHTS_MIN, TRIP_NIGHTS_MAX = 1, 30


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

# --- Planning node results ---------------------------------------------------------
#
# After intake, three nodes each write ONE key of TripState and never change
# constraints or another node's key:
#     flight_trends (fly only) -> flight_trends: FlightTrends
#     flight_search (fly only) -> flight_offers: FlightOffers
#     hotel_search             -> hotel_options: HotelOptions
#
# Shared rules:
# - Every result has "status": "ok", "skipped" (the node doesn't apply to this trip)
#   or "empty" (it ran and found nothing), and a "reason" when it isn't "ok". The
#   other keys are always present when "ok"; they may be missing otherwise.
# - A node must still work when the node before it has no result (missing,
#   "skipped" or "empty"):
#     flights without trends: search the places' own airports on the trip's dates
#     hotels without flights: the full budget, and dates from the trip itself
# - Prices come only from data (DOT Table 6, Duffel, LiteAPI), never from the AI.
#   Live results carry "fetched_at" (UTC, ISO 8601).
# - Dates are ISO strings ("2027-03-10") and money is USD rounded to cents, so state
#   stays JSON-friendly like constraints.
# - A departure window becomes concrete dates once, in flight_trends: up to 3
#   departure dates inside the window (in its cheapest quarter, spread out), each
#   with return_date = depart_date + trip_nights. An exact date has one candidate,
#   (depart_date, return_date). One-way trips have return_date None.

ResultStatus = Literal["ok", "skipped", "empty"]


def empty_result(status: ResultStatus, reason: str) -> dict:
    """A node result with nothing found yet: {"status": ..., "reason": ...}."""
    return {"status": status, "reason": reason}


def flight_skip_reason(c: "TripConstraints") -> Optional[str]:
    """Why the flight nodes don't apply to this trip, or None if they do."""
    if c.mode != "fly":
        return "not a flying trip"
    if c.origin_place is None or c.destination_place is None:
        return "origin or destination not resolved to a place"
    return None


class RouteTrend(TypedDict):
    route: str                       # "Champaign/Urbana, IL → New York City, NY"
    avg_fare: Optional[float]        # average each-way fare over all years; None without data
    has_data: bool


class RouteOption(TypedDict):
    route: str
    side: Literal["departure", "arrival"]   # which end of the trip uses the other airport
    airports: list[str]              # the alternative market's airports: ["MDW", "ORD"]
    distance_km: float               # from the place's center to the closest of them
    avg_fare: Optional[float]
    savings_each_way: Optional[float]   # vs the home route; None if either has no data
    suggested: bool                  # cheap enough to suggest (and to chart)


class ChartSeries(TypedDict):
    label: str                       # "From Chicago (ORD/MDW)"
    values: list[Optional[float]]    # Q1..Q4 averages over all years; None = no data
    home: bool                       # the route the user asked for


class FareChart(TypedDict):
    x: list[str]                     # ["Q1 (Jan-Mar)", "Q2 (Apr-Jun)", "Q3 (Jul-Sep)", "Q4 (Oct-Dec)"]
    series: list[ChartSeries]
    years: str                       # "2022–2026"
    note: str                        # what the numbers are and their source


class SearchAirports(TypedDict):
    origin: list[str]                # home airports first, then suggested alternatives
    destination: list[str]


class CandidateDates(TypedDict):
    depart_date: str
    return_date: Optional[str]       # None for one-way


class BudgetCheck(TypedDict):
    typical_trip_total: Optional[float]   # typical round-trip fares x travelers, from Table 6
    fits: Optional[bool]                  # None when there is no fare history


class FlightTrends(TypedDict):
    status: ResultStatus
    reason: NotRequired[str]
    home: NotRequired[RouteTrend]                 # always the route the user asked for
    options: NotRequired[list[RouteOption]]
    chart: NotRequired[FareChart]
    search_airports: NotRequired[SearchAirports]  # what flight_search should search
    candidate_dates: NotRequired[list[CandidateDates]]
    budget_check: NotRequired[BudgetCheck]


class FlightSlice(TypedDict):
    origin: str                      # airport codes: "ORD"
    destination: str
    depart_at: str                   # local time, ISO 8601
    arrive_at: str
    duration_min: int
    stops: int
    carriers: list[str]              # airline names or codes, in flight order


class FlightMetrics(TypedDict):
    # Measured in code from the offer's segments, so the evaluator never does time math
    longest_layover_min: Optional[int]       # None for nonstop
    shortest_connection_min: Optional[int]   # under ~45 min domestic is risky
    overnight_layover: bool
    airport_change: bool                     # e.g. land at LGA, leave from JFK
    red_eye: bool                            # departs late night or arrives before ~6 am
    carry_on_included: Optional[bool]        # None when the fare rules don't say


class FlightOffer(TypedDict):
    offer_id: str                    # Duffel's id; the evaluator picks by id only
    total_amount: float              # all passengers
    per_person: float
    currency: str
    expires_at: str                  # Duffel offers expire (~30 min); never show after this
    from_alternative_airport: bool
    tags: list[str]                  # why it's shortlisted: "cheapest", "fastest", "best_balance", "alternative_airport"
    outbound: FlightSlice
    inbound: Optional[FlightSlice]   # None for one-way
    metrics: FlightMetrics


class FlightOffers(TypedDict):
    status: ResultStatus
    reason: NotRequired[str]
    searches_made: NotRequired[int]          # Duffel searches this node made
    fetched_at: NotRequired[str]
    cheapest_total: NotRequired[Optional[float]]   # hotel_search subtracts it from the budget
    shortlist: NotRequired[list[FlightOffer]]      # about 5, chosen in code


class HotelPick(TypedDict):
    hotel_id: str
    name: Optional[str]
    total_price: float               # whole stay, all rooms
    per_night: float
    rating: Optional[float]          # guest rating, 0-10
    distance_km: float               # from the destination's center
    matched_amenities: NotRequired[list[str]]
    missing_amenities: NotRequired[list[str]]


class HotelOptions(TypedDict):
    status: ResultStatus
    reason: NotRequired[str]
    fetched_at: NotRequired[str]
    sandbox: NotRequired[bool]               # LiteAPI sandbox prices: real, but not bookable
    checkin: NotRequired[str]
    checkout: NotRequired[str]
    rooms: NotRequired[int]
    hotel_budget: NotRequired[float]         # trip budget minus the cheapest flight, if any
    over_budget: NotRequired[bool]           # even the cheapest hotel doesn't fit
    budget: NotRequired[Optional[HotelPick]] # cheap / middle / nicer, all within hotel_budget
    mid: NotRequired[Optional[HotelPick]]
    upper: NotRequired[Optional[HotelPick]]
    radius_m: NotRequired[int]
    unknown_amenities: NotRequired[list[str]]
    unmet_hard: NotRequired[list[str]]
    rating_floor_applied: NotRequired[bool]


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
    # Planning node results (see the shared rules above)
    flight_trends: NotRequired[FlightTrends]
    flight_offers: NotRequired[FlightOffers]
    hotel_options: NotRequired[HotelOptions]
    evaluation: NotRequired[dict]
