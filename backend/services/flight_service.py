from calendar import monthrange
from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
from typing import TYPE_CHECKING
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from services.duffel_client import DuffelError, _request

if TYPE_CHECKING:
    from services.agent.state import FlightMetrics, FlightOffer, FlightSlice, TripConstraints

MAX_SHORTLIST_OFFERS = 5


def _parse_amount(value) -> float:
    """Parse a finite, nonnegative price and round half-up to cents."""
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError
        return float(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        raise DuffelError("Duffel returned an invalid monetary amount") from None


def _duration_minutes(value: str) -> int:
    """Convert an ISO duration's days/hours/minutes/seconds to whole minutes."""
    match = re.fullmatch(
        r"P(?:(\d+)D)?(?:T(?=\d)(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?",
        value,
    ) if isinstance(value, str) else None
    if match is None or not any(part is not None for part in match.groups()):
        raise DuffelError("Duffel returned an invalid duration")
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    # FlightSlice stores integer minutes; discard any remaining seconds.
    return days * 1440 + hours * 60 + minutes + seconds // 60


def _initial_departure_dates(
    constraints: "TripConstraints", preferred_dates: list[date] | None = None,
) -> list[date]:
    """Pick up to three initial probes using calendar heuristics, never guessed fares.

    Exact departures and return-minus-nights are fixed. Flexible windows prefer
    days outside actual/observed US federal holidays and their adjacent days, on
    both flight legs. Split the least holiday-affected dates into chronological
    thirds, choosing one per third: avoid March 8–24 where possible (a coarse
    spring-break heuristic), prefer Tue/Wed/Sat, then valid trend seeds, then
    closeness to the start/midpoint/end. These are soft preferences: a restricted
    window still produces dates, even if every choice is holiday-affected.
    """
    c = constraints
    if c.depart_date is not None:
        return [c.depart_date]
    start, end = c.flexible_start, c.flexible_end
    if start is None or end is None:
        return []
    if c.trip_type == "round_trip":
        if c.return_date is not None:
            if c.trip_nights is not None:
                start = end = c.return_date - timedelta(days=c.trip_nights)
            else:
                end = min(end, c.return_date)
        elif c.trip_nights is not None:
            end = min(end, date.max - timedelta(days=c.trip_nights))
    if end < start:
        return []
    if start == end:
        return [start]

    latest_return = end
    if c.trip_type == "round_trip":
        latest_return = c.return_date or end + timedelta(days=c.trip_nights or 0)
    holidays = set()
    # OPM federal calendar: actual dates plus Friday/Monday weekend observances.
    # https://www.opm.gov/policy-data-oversight/pay-leave/federal-holidays/
    for year in range(max(1, start.year - 1), min(9999, latest_return.year + 1) + 1):
        for month, day in ((1, 1), (6, 19), (7, 4), (11, 11), (12, 25)):
            actual = date(year, month, day)
            offset = -1 if actual.weekday() == 5 else 1 if actual.weekday() == 6 else 0
            holidays.update((actual, actual + timedelta(days=offset)))
        for month, weekday, occurrence in ((1, 0, 3), (2, 0, 3), (5, 0, -1),
                                           (9, 0, 1), (10, 0, 2), (11, 3, 4)):
            if occurrence == -1:
                last = date(year, month, monthrange(year, month)[1])
                holiday = last - timedelta(days=(last.weekday() - weekday) % 7)
            else:
                first = date(year, month, 1)
                holiday = first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (occurrence - 1))
            holidays.add(holiday)
    busy_days = {holiday + timedelta(days=offset) for holiday in holidays for offset in (-1, 0, 1)
                 if date.min.toordinal() <= holiday.toordinal() + offset <= date.max.toordinal()}
    scored = []
    for offset in range((end - start).days + 1):
        depart = start + timedelta(days=offset)
        back = None
        if c.trip_type == "round_trip":
            back = c.return_date or (depart + timedelta(days=c.trip_nights) if c.trip_nights else None)
        scored.append((int(depart in busy_days) + int(back in busy_days), depart))
    least_risk = min(risk for risk, _ in scored)
    eligible = [depart for risk, depart in scored if risk == least_risk]
    preferred = {day for day in (preferred_dates or []) if type(day) is date and start <= day <= end}
    count = min(3, len(eligible))
    chosen = []
    for index in range(count):
        bucket = eligible[index * len(eligible) // count:(index + 1) * len(eligible) // count]
        target = start + timedelta(days=(end - start).days * index // max(1, count - 1))
        chosen.append(min(bucket, key=lambda day: (
            day.month == 3 and 8 <= day.day <= 24,
            day.weekday() not in (1, 2, 5), day not in preferred, abs((day - target).days), day,
        )))
    return chosen


def _parse_slice(value: dict) -> "FlightSlice":
    """Summarize a Duffel slice using its actual airports and local timestamps.

    Use the provider's full duration, which includes layovers. Count connections
    and intermediate stops within segments. Prefer operating-carrier names (then
    codes), falling back to marketing carriers; retain one entry per segment.
    """
    try:
        if not isinstance(value, dict):
            raise ValueError
        segments = value["segments"]
        if not isinstance(segments, list) or not segments:
            raise ValueError
        carriers = []
        stops = len(segments) - 1
        for segment in segments:
            if not isinstance(segment, dict):
                raise ValueError
            for field in ("origin", "destination"):
                airport = segment[field]
                if not isinstance(airport, dict):
                    raise ValueError
                code = airport["iata_code"]
                if not isinstance(code, str) or re.fullmatch(r"[A-Z]{3}", code) is None:
                    raise ValueError
            for field in ("departing_at", "arriving_at"):
                timestamp = segment[field]
                if not isinstance(timestamp, str) or "T" not in timestamp:
                    raise ValueError
                datetime.fromisoformat(timestamp)
            carrier = None
            for field in ("operating_carrier", "marketing_carrier"):
                airline = segment.get(field)
                if not isinstance(airline, dict):
                    continue
                for key in ("name", "iata_code"):
                    label = airline.get(key)
                    if isinstance(label, str) and label.strip():
                        carrier = label.strip()
                        break
                if carrier is not None:
                    break
            if carrier is None:
                raise ValueError
            carriers.append(carrier)
            intermediate_stops = segment.get("stops", [])
            if (not isinstance(intermediate_stops, list)
                    or any(not isinstance(stop, dict) for stop in intermediate_stops)):
                raise ValueError
            stops += len(intermediate_stops)
        return {
            "origin": segments[0]["origin"]["iata_code"],
            "destination": segments[-1]["destination"]["iata_code"],
            "depart_at": segments[0]["departing_at"],
            "arrive_at": segments[-1]["arriving_at"],
            "duration_min": _duration_minutes(value["duration"]),
            "stops": stops,
            "carriers": carriers,
        }
    except (KeyError, TypeError, ValueError):
        raise DuffelError("Duffel returned an invalid flight slice") from None


def _flight_metrics(slices: list[dict]) -> "FlightMetrics":
    """Measure connections within each slice, never the stay between slices.

    Elapsed times use UTC; overnight connections cross midnight in the arrival
    airport's zone. A red-eye departs at/after 22:00 or before 06:00, or arrives
    before 06:00, in airport-local time. Carry-on is included only if explicitly
    included for every passenger on every segment; any explicit exclusion wins.
    Missing baggage information stays unknown. Ambiguous/nonexistent local times
    without an explicit UTC offset are rejected rather than guessed.
    """
    try:
        if not isinstance(slices, list) or not slices:
            raise ValueError
        layovers = []
        overnight = airport_change = red_eye = False
        carry_on = []
        for value in slices:
            _parse_slice(value)
            previous_arrival = None
            previous_airport = None
            for segment in value["segments"]:
                times = []
                for field, airport_field in (("departing_at", "origin"), ("arriving_at", "destination")):
                    zone = ZoneInfo(segment[airport_field]["time_zone"])
                    local = datetime.fromisoformat(segment[field])
                    if local.tzinfo is None:
                        aware = local.replace(tzinfo=zone)
                        # Round-tripping detects a clock time skipped by DST.
                        if aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != local:
                            raise ValueError
                        if aware.utcoffset() != local.replace(tzinfo=zone, fold=1).utcoffset():
                            raise ValueError
                    else:
                        aware = local.astimezone(zone)
                    times.append(aware)
                departure, arrival = times
                if arrival.astimezone(timezone.utc) < departure.astimezone(timezone.utc):
                    raise ValueError
                red_eye |= departure.hour >= 22 or departure.hour < 6 or arrival.hour < 6
                if previous_arrival is not None:
                    seconds = (departure.astimezone(timezone.utc)
                               - previous_arrival.astimezone(timezone.utc)).total_seconds()
                    if seconds < 0:
                        raise ValueError
                    layovers.append(int(seconds // 60))
                    overnight |= departure.astimezone(previous_arrival.tzinfo).date() > previous_arrival.date()
                    airport_change |= previous_airport != segment["origin"]["iata_code"]
                previous_arrival = arrival
                previous_airport = segment["destination"]["iata_code"]

                passengers = segment.get("passengers")
                if not isinstance(passengers, list) or not passengers:
                    carry_on.append(None)
                    continue
                for passenger in passengers:
                    bags = passenger.get("baggages") if isinstance(passenger, dict) else None
                    if not isinstance(bags, list):
                        carry_on.append(None)
                        continue
                    quantities = [bag.get("quantity") for bag in bags
                                  if isinstance(bag, dict) and bag.get("type") == "carry_on"]
                    if quantities and all(type(quantity) is int and quantity >= 0 for quantity in quantities):
                        carry_on.append(sum(quantities) > 0)
                    else:
                        carry_on.append(None)
        return {
            "longest_layover_min": max(layovers) if layovers else None,
            "shortest_connection_min": min(layovers) if layovers else None,
            "overnight_layover": overnight,
            "airport_change": airport_change,
            "red_eye": red_eye,
            "carry_on_included": False if False in carry_on else True if all(carry_on) else None,
        }
    except (KeyError, TypeError, ValueError, OverflowError, ZoneInfoNotFoundError):
        raise DuffelError("Duffel returned invalid flight metrics data") from None


def _parse_offer(
    value: dict,
    travelers: int,
    *,
    from_alternative_airport: bool = False,
    now: datetime | None = None,
) -> "FlightOffer":
    """Build an unranked one-way/round-trip offer; reject unusable provider data.

    Prices are USD for all travelers. Divide the rounded total using Decimal,
    rounding per-person prices half-up to cents. Expiry must include a UTC offset
    and be strictly later than now (an aware datetime, defaulting to UTC now).
    The caller identifies alternative-airport searches; shortlist tags start empty.
    """
    if type(travelers) is not int or travelers < 1:
        raise ValueError("travelers must be a positive integer")
    if not isinstance(from_alternative_airport, bool):
        raise ValueError("from_alternative_airport must be a boolean")
    if now is not None and (not isinstance(now, datetime) or now.utcoffset() is None):
        raise ValueError("now must be a timezone-aware datetime")
    now = datetime.now(timezone.utc) if now is None else now
    try:
        if not isinstance(value, dict):
            raise ValueError
        offer_id = value["id"]
        if not isinstance(offer_id, str) or not offer_id.strip():
            raise ValueError
        if value["total_currency"] != "USD":
            raise DuffelError("Duffel returned a non-USD offer")
        expires_at = value["expires_at"]
        if not isinstance(expires_at, str) or "T" not in expires_at:
            raise ValueError
        expiry = datetime.fromisoformat(expires_at)
        if expiry.utcoffset() is None:
            raise ValueError
        if expiry <= now:
            raise DuffelError("Duffel offer has expired")
        slices = value["slices"]
        if not isinstance(slices, list) or len(slices) not in (1, 2):
            raise ValueError
        total = _parse_amount(value["total_amount"])
        return {
            "offer_id": offer_id,
            "total_amount": total,
            "per_person": _parse_amount(Decimal(str(total)) / travelers),
            "currency": "USD",
            "expires_at": expires_at,
            "from_alternative_airport": from_alternative_airport,
            "tags": [],
            "outbound": _parse_slice(slices[0]),
            "inbound": _parse_slice(slices[1]) if len(slices) == 2 else None,
            "metrics": _flight_metrics(slices),
        }
    except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
        raise DuffelError("Duffel returned an invalid flight offer") from None


def _shortlist_offers(
    offers: list["FlightOffer"], *, now: datetime | None = None,
) -> list["FlightOffer"]:
    """Select up to five unexpired offers, cheapest first, without changing inputs."""
    if not isinstance(offers, list):
        raise ValueError("offers must be a list of parsed offers")
    if now is not None and (not isinstance(now, datetime) or now.utcoffset() is None):
        raise ValueError("now must be a timezone-aware datetime")
    now = datetime.now(timezone.utc) if now is None else now
    by_id = {}
    for offer in offers:
        try:
            expiry = datetime.fromisoformat(offer["expires_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if expiry.utcoffset() is not None and expiry > now:
            by_id.setdefault(offer["offer_id"], offer)
    if not by_id:
        return []

    features, itineraries, clean_options = {}, {}, defaultdict(list)
    for key, offer in by_id.items():
        legs = [leg for leg in (offer["outbound"], offer["inbound"]) if leg is not None]
        metrics = offer["metrics"]
        itinerary = tuple((leg["origin"], leg["destination"], leg["depart_at"][:10]) for leg in legs)
        price = Decimal(str(offer["total_amount"]))
        duration = sum(leg["duration_min"] for leg in legs)
        stops = sum(leg["stops"] for leg in legs)
        features[key] = (price, duration, stops, metrics["longest_layover_min"] or 0)
        itineraries[key] = itinerary
        if not (metrics["overnight_layover"] or metrics["airport_change"]):
            clean_options[itinerary].append((price, duration, stops))

    eligible = []
    for key, offer in by_id.items():
        price, duration, stops, _ = features[key]
        metrics = offer["metrics"]
        if (metrics["overnight_layover"] or metrics["airport_change"]) and any(
            other_price <= price + 10 and other_duration <= duration + 60 and other_stops <= stops
            for other_price, other_duration, other_stops in clean_options[itineraries[key]]
        ):
            continue
        eligible.append(key)

    # Normalize each dimension across the eligible offers. Price and elapsed time
    # carry twice the weight of stops and the longest layover.
    scores = dict.fromkeys(eligible, Decimal(0))
    for index, weight in enumerate((2, 2, 1, 1)):
        values = [features[key][index] for key in eligible]
        minimum, maximum = min(values), max(values)
        if maximum > minimum:
            for key in eligible:
                scores[key] += weight * Decimal(features[key][index] - minimum) / (maximum - minimum)

    def by_price(key):
        return (*features[key][:2], key)

    cheapest = min(eligible, key=by_price)
    fastest = min(eligible, key=lambda key: (features[key][1], features[key][0], key))
    ranked = sorted(eligible, key=lambda key: (scores[key], by_price(key)))
    alternative = min((key for key in eligible if by_id[key]["from_alternative_airport"]),
                      key=by_price, default=None)

    selected = {}
    for key, tag in ((cheapest, "cheapest"), (fastest, "fastest"), (ranked[0], "best_balance"),
                     (alternative, "alternative_airport")):
        if key is None:
            continue
        if key not in selected:
            selected[key] = deepcopy(by_id[key])
            selected[key]["tags"] = []
        selected[key]["tags"].append(tag)
    for key in ranked:
        if len(selected) == MAX_SHORTLIST_OFFERS:
            break
        if key not in selected:
            selected[key] = deepcopy(by_id[key])
            selected[key]["tags"] = []
    return [selected[key] for key in sorted(selected, key=by_price)]


def _search_offers(
    origin: str,
    destination: str,
    depart_date: date,
    travelers: int,
    *,
    return_date: date | None = None,
    max_connections: int | None = None,
    from_alternative_airport: bool = False,
    now: datetime | None = None,
) -> list["FlightOffer"]:
    """Run one guarded airport/date search and parse all usable USD offers.

    Dates are date objects; airport codes are trimmed and uppercased. Travelers
    are 1–9 adults, matching TripConstraints. Omit max_connections when unspecified
    (Duffel defaults to 1); otherwise allow 0–2. Request the full flat offer list,
    leaving ranking to _shortlist_offers after searches have been combined.
    Invalid, expired, partial, or wrong-slice-count offers are skipped; malformed
    response envelopes and client failures raise DuffelError without retries.
    """
    for name, code in (("origin", origin), ("destination", destination)):
        if not isinstance(code, str) or re.fullmatch(r"[A-Z]{3}", code.strip().upper()) is None:
            raise ValueError(f"{name} must be a three-letter airport code")
    origin, destination = origin.strip().upper(), destination.strip().upper()
    if origin == destination:
        raise ValueError("origin and destination must be different airports")
    if type(depart_date) is not date:
        raise ValueError("depart_date must be a datetime.date object")
    if return_date is not None:
        if type(return_date) is not date or return_date < depart_date:
            raise ValueError("return_date must be a datetime.date on or after depart_date")
    if type(travelers) is not int or not 1 <= travelers <= 9:
        raise ValueError("travelers must be an integer from 1 to 9")
    if max_connections is not None and (type(max_connections) is not int or not 0 <= max_connections <= 2):
        raise ValueError("max_connections must be an integer from 0 to 2")
    if not isinstance(from_alternative_airport, bool):
        raise ValueError("from_alternative_airport must be a boolean")
    if now is not None and (not isinstance(now, datetime) or now.utcoffset() is None):
        raise ValueError("now must be a timezone-aware datetime")

    slices = [{"origin": origin, "destination": destination, "departure_date": depart_date.isoformat()}]
    if return_date is not None:
        slices.append({"origin": destination, "destination": origin, "departure_date": return_date.isoformat()})
    payload = {"slices": slices, "passengers": [{"type": "adult"} for _ in range(travelers)]}
    if max_connections is not None:
        payload["max_connections"] = max_connections
    # return_offers includes all returned offers, without a second paginated call.
    body = _request(
        "POST", "/air/offer_requests",
        params={"return_offers": True, "view": "offers"}, json={"data": payload},
    )
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get("offers"), list):
        raise DuffelError("Duffel returned an invalid offer search response")
    offers = []
    for value in data["offers"]:
        if isinstance(value, dict) and value.get("partial") is True:
            continue
        try:
            offer = _parse_offer(
                value, travelers, from_alternative_airport=from_alternative_airport, now=now,
            )
        except DuffelError:
            continue
        if (offer["inbound"] is not None) == (return_date is not None):
            offers.append(offer)
    return sorted(offers, key=lambda offer: offer["total_amount"])


def refresh_offer(
    offer_id: str,
    travelers: int,
    *,
    from_alternative_airport: bool = False,
    now: datetime | None = None,
) -> "FlightOffer":
    """Refetch one offer through the daily guard and return its latest parsed data.

    At most one GET per invocation, without retries. The caller supplies the original
    traveler count and alternative-airport flag. Return an unranked offer because
    prices may have changed; selection tags must be recomputed against other offers.
    Unavailable, expired, non-USD, partial, or malformed results raise DuffelError.
    """
    if not isinstance(offer_id, str) or not offer_id.strip():
        raise ValueError("offer_id must be a nonempty string")
    offer_id = offer_id.strip()
    if type(travelers) is not int or not 1 <= travelers <= 9:
        raise ValueError("travelers must be an integer from 1 to 9")
    if not isinstance(from_alternative_airport, bool):
        raise ValueError("from_alternative_airport must be a boolean")
    if now is not None and (not isinstance(now, datetime) or now.utcoffset() is None):
        raise ValueError("now must be a timezone-aware datetime")
    body = _request(
        "GET", f"/air/offers/{quote(offer_id, safe='')}",
        params={"return_available_services": False},
    )
    value = body.get("data") if isinstance(body, dict) else None
    if (not isinstance(value, dict) or value.get("id") != offer_id
            or value.get("partial") is True):
        raise DuffelError("Duffel returned an invalid offer refresh response")
    return _parse_offer(
        value, travelers, from_alternative_airport=from_alternative_airport, now=now,
    )
