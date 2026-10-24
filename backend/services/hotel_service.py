"""Small hotel-search results for agents, backed by LiteAPI's guarded client.

Prices are USD for the entire requested stay and all requested rooms. Geographic
distances are straight-line kilometres, not walking or driving distances.
"""

from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from math import asin, cos, isfinite, radians, sin, sqrt
import re
from typing import Optional
from urllib.parse import quote

from core.config import settings
from services.liteapi_client import LiteAPIError, _request


def _positive_int(value: int, name: str, maximum: Optional[int] = None) -> int:
    """Validate a positive integer input, optionally with an API upper bound."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return value


def _number(value) -> Optional[float]:
    """Read a finite API number; missing or malformed values remain unknown."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _coordinates(latitude, longitude) -> tuple[float, float]:
    """Validate latitude/longitude in degrees without treating zero as missing."""
    lat, lon = _number(latitude), _number(longitude)
    if lat is None or lon is None or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError("latitude/longitude must be finite coordinates in degrees")
    return lat, lon


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return unrounded great-circle distance in kilometres using haversine."""
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 6371.0088 * 2 * asin(sqrt(min(1.0, max(0.0, a))))


def _rows(body: dict) -> list[dict]:
    """Read an endpoint's data array; distinguish malformed data from no results."""
    data = body.get("data")
    if data is None:
        return []
    if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
        raise LiteAPIError("LiteAPI returned an unexpected data array")
    return data


def _text(value) -> str:
    """Read Google-style display names as well as ordinary text fields."""
    return str(value.get("text", "")) if isinstance(value, dict) else str(value or "")


def _hotel_id(value: str) -> str:
    """Validate a nonempty hotel identifier before making a request."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("hotel_id must be a nonempty string")
    return value.strip()


def _occupancies(adults: int, rooms: int = 1) -> list[dict]:
    """Spread adults evenly across rooms, with at least one adult in every room."""
    _positive_int(adults, "adults")
    _positive_int(rooms, "rooms")
    if rooms > adults:
        raise ValueError("rooms must not exceed adults")
    per_room, extra = divmod(adults, rooms)
    return [{"adults": per_room + (index < extra)} for index in range(rooms)]


def _stay_payload(checkin: date, checkout: date, adults: int, rooms: int) -> tuple[dict, int]:
    """Validate a stay and build the shared dates, guests, and USD rate payload."""
    if type(checkin) is not date or type(checkout) is not date:
        raise ValueError("checkin and checkout must be datetime.date objects")
    nights = (checkout - checkin).days
    if nights < 1:
        raise ValueError("checkout must be after checkin")
    return {
        "checkin": checkin.isoformat(), "checkout": checkout.isoformat(),
        "occupancies": _occupancies(adults, rooms),
        "currency": "USD", "guestNationality": "US", "timeout": 10,
    }, nights


def _money(value) -> Optional[Decimal]:
    """Read a finite nonnegative monetary amount without binary-float arithmetic."""
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
        return amount if amount.is_finite() and amount >= 0 else None
    except (InvalidOperation, ValueError):
        return None


def _usd_amount(value) -> Optional[Decimal]:
    """Select the USD amount from a rate's currency object or currency array."""
    entries = value if isinstance(value, list) else [value]
    for entry in entries:
        if isinstance(entry, dict) and entry.get("currency") == "USD":
            return _money(entry.get("amount"))
    return None


def _prices(total: Decimal, nights: int) -> dict:
    """Return total USD and average nightly USD, rounded to cents."""
    cent = Decimal("0.01")
    return {
        "total_price": float(total.quantize(cent, rounding=ROUND_HALF_UP)),
        "per_night": float((total / nights).quantize(cent, rounding=ROUND_HALF_UP)),
        "currency": "USD",
    }


def _source(body: dict) -> dict:
    """Label fetched prices with their UTC retrieval time and sandbox status."""
    sandbox = body.get("sandbox")
    if not isinstance(sandbox, bool):
        sandbox = bool(settings.LITEAPI_API_KEY and settings.LITEAPI_API_KEY.startswith("sand_"))
    return {"sandbox": sandbox, "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def get_city_location(city: str, state: Optional[str] = None) -> Optional[dict]:
    """Return a US city's name, place_id, latitude and longitude in degrees, or None."""
    if not isinstance(city, str) or not city.strip():
        raise ValueError("city must be a nonempty string")
    if state is not None and (not isinstance(state, str) or not state.strip()):
        raise ValueError("state must be a nonempty string when supplied")
    query = ", ".join(part for part in (city.strip(), state.strip() if state else None, "United States") if part)
    candidates = _rows(_request("GET", "/data/places", params={"textQuery": query, "type": "locality"}))
    # Cap disambiguation requests; the shared daily allowance still covers each.
    for candidate in candidates[:5]:
        place_id = candidate.get("placeId")
        if not place_id:
            continue
        if _text(candidate.get("displayName")).casefold() != city.split(",")[0].strip().casefold():
            continue
        data = _request("GET", f"/data/places/{quote(str(place_id), safe='')}").get("data")
        if not data:
            continue
        if not isinstance(data, dict):
            raise LiteAPIError("LiteAPI returned unexpected place details")
        if data.get("countryCode") != "US":
            continue
        if state:
            regions = [part for part in data.get("addressComponents", [])
                       if "administrative_area_level_1" in part.get("types", [])]
            if not any(state.strip().casefold() in {str(part.get("longText", "")).casefold(),
                                                   str(part.get("shortText", "")).casefold()} for part in regions):
                continue
        location = data.get("location") or {}
        try:
            lat, lon = _coordinates(location.get("latitude"), location.get("longitude"))
        except ValueError:
            raise LiteAPIError("LiteAPI returned invalid city coordinates") from None
        return {"name": _text(data.get("displayName")) or city.strip(), "place_id": str(place_id),
                "latitude": lat, "longitude": lon}
    return None


def search_hotels_near(
    latitude: float, longitude: float, radius_m: int = 5000,
    min_rating: Optional[float] = None, facility_ids: Optional[list[int]] = None,
    limit: int = 50,
) -> list[dict]:
    """Return up to limit US hotel candidates inside radius_m, sorted by distance in km.

    The provider limits the candidate set before local sorting. Results are not
    guaranteed to be the globally nearest hotels when that candidate cap is hit.
    Guest min_rating uses the hotel-data endpoint's 0–10 scale, not star ratings.
    """
    lat, lon = _coordinates(latitude, longitude)
    _positive_int(radius_m, "radius_m")
    _positive_int(limit, "limit", 5000)
    params = {"countryCode": "US", "latitude": lat, "longitude": lon,
              "radius": max(radius_m, 1000), "limit": limit}
    if min_rating is not None:
        rating = _number(min_rating)
        if rating is None or not 0 <= rating <= 10:
            raise ValueError("min_rating must be between 0 and 10")
        params["minRating"] = rating
    if facility_ids:
        for facility_id in facility_ids:
            _positive_int(facility_id, "facility_id")
        params.update(facilityIds=",".join(map(str, facility_ids)), strictFacilitiesFiltering="true")
    hotels = []
    for hotel in _rows(_request("GET", "/data/hotels", params=params)):
        if not hotel.get("id"):
            continue
        try:
            hotel_lat, hotel_lon = _coordinates(hotel.get("latitude"), hotel.get("longitude"))
        except ValueError:
            continue  # A missing location cannot satisfy a geographic filter.
        distance = _distance_km(lat, lon, hotel_lat, hotel_lon)
        # LiteAPI's minimum is 1000 m; retain the user's smaller radius locally.
        if distance * 1000 > radius_m:
            continue
        hotels.append({"hotel_id": hotel["id"], "name": hotel.get("name"),
                       "address": hotel.get("address"), "stars": hotel.get("stars"),
                       "rating": hotel.get("rating"), "review_count": hotel.get("reviewCount"),
                       "distance_km": distance, "photo": hotel.get("main_photo")})
    hotels.sort(key=lambda hotel: hotel["distance_km"])
    return [{**hotel, "distance_km": round(hotel["distance_km"], 3)} for hotel in hotels[:limit]]


def get_hotel_details(hotel_id: str) -> dict:
    """Return compact hotel details, amenity names, times and policies; {} if absent."""
    hotel_id = _hotel_id(hotel_id)
    data = _request("GET", "/data/hotel", params={"hotelId": hotel_id}).get("data")
    if not data:
        return {}
    if not isinstance(data, dict):
        raise LiteAPIError("LiteAPI returned unexpected hotel details")
    amenities = [name for name in (data.get("hotelFacilities") or []) if isinstance(name, str)]
    amenities.extend(facility["name"] for facility in (data.get("facilities") or []) if facility.get("name"))
    policies = data.get("policies") or []
    pet_policies = [policy.get("pets_allowed") for policy in policies if policy.get("pets_allowed")]
    parking_policies = [policy.get("description") for policy in policies
                        if "parking" in str(policy.get("policy_type", "")).casefold()
                        or "parking" in str(policy.get("name", "")).casefold()]
    times = data.get("checkinCheckoutTimes") or {}
    return {
        "hotel_id": hotel_id, "name": data.get("name"), "stars": data.get("starRating"),
        "rating": data.get("rating"), "amenities": list(dict.fromkeys(amenities)),
        "check_in": times.get("checkin_start") or times.get("checkin"), "check_out": times.get("checkout"),
        "check_in_end": times.get("checkin_end") or None,
        "parking": data.get("parking"), "parking_policy": " ".join(filter(None, parking_policies)) or None,
        "pets_allowed": data.get("petsAllowed"), "pet_policy": " ".join(pet_policies) or None,
        "photo": data.get("main_photo"),
    }


def find_facility_ids(names: list[str]) -> list[int]:
    """Return facility IDs for exact names/common aliases, ignoring case/punctuation.

    Unknown names have no match. Choose one canonical facility per requested
    name; matching every substring would incorrectly require e.g. a pool bar.
    """
    if any(not isinstance(name, str) or not name.strip() for name in names):
        raise ValueError("facility names must be nonempty strings")
    if not names:
        return []
    # Ignore punctuation so 'WiFi' also matches the provider's 'Wi-Fi'.
    normalize = lambda text: re.sub(r"[^\w]", "", text.casefold())
    aliases = {"wifi": ["wifiavailable", "freewifi"], "pool": ["swimmingpool"],
               "gym": ["fitnesscenter", "fitnessfacilities"]}
    facilities = {}
    for facility in _rows(_request("GET", "/data/facilities")):
        label = normalize(str(facility.get("facility", facility.get("name", ""))))
        identifier = facility.get("facility_id", facility.get("facilityId"))
        try:
            identifier = int(identifier)
        except (TypeError, ValueError):
            continue
        if identifier > 0:
            facilities.setdefault(label, identifier)
    result = []
    for name in names:
        term = normalize(name)
        for label in [term, *aliases.get(term, [])]:
            identifier = facilities.get(label)
            if identifier is not None:
                if identifier not in result:
                    result.append(identifier)
                break
    return result


def get_cheapest_rates(
    hotel_ids: list[str], checkin: date, checkout: date, adults: int, rooms: int = 1,
) -> list[dict]:
    """Return minimum whole-stay/all-room USD totals and average nightly prices, cheapest first."""
    payload, nights = _stay_payload(checkin, checkout, adults, rooms)
    if not hotel_ids:
        return []
    ids = list(dict.fromkeys(_hotel_id(identifier) for identifier in hotel_ids))
    body = _request("POST", "/hotels/min-rates", json={**payload, "hotelIds": ids}, timeout=20.0)
    metadata = _source(body)
    rates = {}
    for row in _rows(body):
        identifier, total = row.get("hotelId"), _money(row.get("price"))
        if identifier not in ids or total is None:
            continue
        # Sandbox verified: price is the requested stay total, not a nightly rate.
        rate = {"hotel_id": identifier, **_prices(total, nights), **metadata}
        if identifier not in rates or rate["total_price"] < rates[identifier]["total_price"]:
            rates[identifier] = rate
    return sorted(rates.values(), key=lambda rate: rate["total_price"])


def get_room_offers(
    hotel_id: str, checkin: date, checkout: date, adults: int, rooms: int = 1,
) -> list[dict]:
    """Return available room offers sorted by whole-stay/all-room USD total.

    A multi-room offer stays grouped. refundable is True only when every room is
    RFN, False if any is NRFN, and None when the provider leaves it unspecified.
    Refundable offers can still carry cancellation deadlines and penalties.
    """
    hotel_id = _hotel_id(hotel_id)
    payload, nights = _stay_payload(checkin, checkout, adults, rooms)
    body = _request("POST", "/hotels/rates", json={**payload, "hotelIds": [hotel_id]}, timeout=20.0)
    metadata, offers = _source(body), []
    for hotel in _rows(body):
        if hotel.get("hotelId") != hotel_id:
            continue
        for offer in hotel.get("roomTypes") or []:
            rates = offer.get("rates") or []
            # Each occupancy must be covered; never quote one room as a whole
            # multi-room stay. Repeated occupancy numbers are invalid here.
            occupancies = [rate.get("occupancyNumber", 1) for rate in rates]
            if sorted(occupancies) != list(range(1, rooms + 1)):
                continue
            total = _usd_amount(offer.get("offerRetailRate"))
            if total is None:
                amounts = [_usd_amount((rate.get("retailRate") or {}).get("total")) for rate in rates]
                if any(amount is None for amount in amounts):
                    continue
                total = sum(amounts, Decimal(0))
            # retailRate.total is a room's stay total; offerRetailRate is the
            # combined total for every requested room (verified in the sandbox).
            tags = [(rate.get("cancellationPolicies") or {}).get("refundableTag") for rate in rates]
            refundable = False if "NRFN" in tags else True if all(tag == "RFN" for tag in tags) else None
            room_names = list(dict.fromkeys(rate.get("name") for rate in rates if rate.get("name")))
            board_names = list(dict.fromkeys(rate.get("boardName") or rate.get("boardType") for rate in rates
                                           if rate.get("boardName") or rate.get("boardType")))
            offers.append({"hotel_id": hotel_id, "room_name": " / ".join(room_names),
                           "board_type": " / ".join(board_names), "refundable": refundable,
                           **_prices(total, nights), **metadata})
    return sorted(offers, key=lambda offer: offer["total_price"])


def get_review_summary(hotel_id: str, limit: int = 20) -> dict:
    """Summarize at most limit reviews: sampled average, headlines and repeated pros/cons.

    Counts describe the fetched sample; total_reviews is the provider's total.
    Common pros/cons count repeated text, without inventing semantic themes.
    """
    hotel_id = _hotel_id(hotel_id)
    _positive_int(limit, "limit", 5000)
    body = _request("GET", "/data/reviews", params={"hotelId": hotel_id, "limit": limit})
    reviews = _rows(body)[:limit]
    scores = [score for review in reviews if (score := _number(review.get("averageScore"))) is not None]
    headlines = list(dict.fromkeys(review["headline"].strip() for review in reviews if review.get("headline")))[:5]

    def common(field: str) -> list[dict]:
        """Return five most common nonempty phrases, preserving their frequency."""
        counts = Counter(" ".join(review[field].split()).casefold() for review in reviews
                         if isinstance(review.get(field), str) and review[field].strip())
        return [{"text": text, "count": count} for text, count in counts.most_common(5)]

    return {"hotel_id": hotel_id, "average_score": round(sum(scores) / len(scores), 2) if scores else None,
            "review_count": len(reviews), "total_reviews": body.get("total", len(reviews)),
            "headlines": headlines, "pros": common("pros"), "cons": common("cons")}


def search_hotels_with_prices(
    city: str, checkin: date, checkout: date, adults: int, rooms: int = 1,
    radius_m: int = 5000, max_total: Optional[float] = None,
) -> list[dict]:
    """Return priced US hotel candidates within radius_m, sorted by total USD.

    max_total applies to the entire stay and every room, not per person/night.
    Uses city lookup -> hotel list -> minimum rates; no booking or agent wiring.
    """
    _stay_payload(checkin, checkout, adults, rooms)
    _positive_int(radius_m, "radius_m")
    budget = _money(max_total) if max_total is not None else None
    if max_total is not None and budget is None:
        raise ValueError("max_total must be a finite nonnegative USD amount")
    location = get_city_location(city)
    if location is None:
        return []
    hotels = search_hotels_near(location["latitude"], location["longitude"], radius_m=radius_m)
    if not hotels:
        return []
    by_id = {hotel["hotel_id"]: hotel for hotel in hotels}
    rates = get_cheapest_rates(list(by_id), checkin, checkout, adults, rooms)
    return [{**by_id[rate["hotel_id"]], **rate} for rate in rates
            if rate["hotel_id"] in by_id and (budget is None or Decimal(str(rate["total_price"])) <= budget)]
