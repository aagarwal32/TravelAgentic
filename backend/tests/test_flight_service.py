"""Offline coverage of Duffel flight-result normalization."""

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import httpx

with patch.dict(os.environ, {
    "SECRET_KEY": "test-only", "DATABASE_URL": "sqlite://", "DEBUG": "false",
}):
    from services.duffel_client import DuffelError
    from services import duffel_client, flight_service
    from services.agent.state import TripConstraints
    from services.flight_service import (
        _flight_metrics, _initial_departure_dates, _parse_offer, _parse_slice,
        _search_offers, _shortlist_offers, refresh_offer,
    )


NONSTOP = {
    # Slice-level locations may describe a city; use the flown segment airports.
    "origin": {"iata_code": "CHI"},
    "destination": {"iata_code": "NYC"},
    "duration": "PT2H10M59S",
    "segments": [{
        "origin": {"iata_code": "ORD"},
        "destination": {"iata_code": "JFK"},
        "departing_at": "2027-03-01T08:00:00",
        "arriving_at": "2027-03-01T11:10:59",
        "operating_carrier": {"name": "American Airlines", "iata_code": "AA"},
        "marketing_carrier": {"name": "British Airways", "iata_code": "BA"},
        "stops": [],
    }],
}

METRICS_NONSTOP = deepcopy(NONSTOP)
METRICS_NONSTOP["segments"][0]["origin"]["time_zone"] = "America/Chicago"
METRICS_NONSTOP["segments"][0]["destination"]["time_zone"] = "America/New_York"
METRICS_NONSTOP["segments"][0]["passengers"] = [{
    "passenger_id": "pas_one",
    "baggages": [{"type": "carry_on", "quantity": 1}],
}]

METRICS_CONNECTION = deepcopy(METRICS_NONSTOP)
METRICS_CONNECTION["duration"] = "PT7H30M"
METRICS_CONNECTION["segments"].append({
    "origin": {"iata_code": "JFK", "time_zone": "America/New_York"},
    "destination": {"iata_code": "BOS", "time_zone": "America/New_York"},
    "departing_at": "2027-03-01T15:00:00",
    "arriving_at": "2027-03-01T16:30:00",
    "operating_carrier": {"name": "JetBlue", "iata_code": "B6"},
    "passengers": [{"passenger_id": "pas_one", "baggages": [{"type": "carry_on", "quantity": 1}]}],
})

OFFER_NOW = datetime(2027, 2, 1, 12, tzinfo=timezone.utc)
OFFER = {
    "id": "off_example",
    "total_amount": "420.00",
    "total_currency": "USD",
    "expires_at": "2027-02-01T12:30:00Z",
    "slices": [METRICS_NONSTOP],
}


class InitialDepartureDatesTests(unittest.TestCase):
    def test_initial_dates_prefer_weekdays_and_cover_the_window(self):
        c = TripConstraints(trip_type="round_trip", flexible_start="2099-03-01",
                            flexible_end="2099-03-31", trip_nights=2)
        result = _initial_departure_dates(c)
        self.assertEqual(result, [date(2099, 3, 3), date(2099, 3, 17), date(2099, 3, 31)])
        self.assertTrue(all(day.weekday() in (1, 2, 5) for day in result))
        self.assertLessEqual(result[0].day, 10)
        self.assertGreaterEqual(result[-1].day, 25)

    def test_actual_and_observed_holidays_and_adjacent_days_are_avoided(self):
        # Independence Day 2099 is Saturday, observed Friday July 3.
        c = TripConstraints(trip_type="one_way", flexible_start="2099-07-01", flexible_end="2099-07-10")
        result = _initial_departure_dates(c, preferred_dates=[date(2099, 7, 3), date(2099, 7, 4)])
        self.assertEqual(len(result), 3)
        self.assertFalse(any(date(2099, 7, 2) <= day <= date(2099, 7, 5) for day in result))

    def test_next_year_observance_is_recognized_in_december(self):
        # Jan 1, 2101 is Saturday, so its observed holiday is Dec 31, 2100.
        c = TripConstraints(trip_type="one_way", flexible_start="2100-12-27", flexible_end="2100-12-31")
        self.assertEqual(_initial_departure_dates(c), [date(2100, 12, day) for day in (27, 28, 29)])

    def test_round_trip_returns_are_checked_for_holiday_periods(self):
        c = TripConstraints(trip_type="round_trip", flexible_start="2099-06-20",
                            flexible_end="2099-06-30", trip_nights=7)
        result = _initial_departure_dates(c)
        self.assertTrue(result)
        for depart in result:
            back = depart + timedelta(days=7)
            self.assertFalse(date(2099, 7, 2) <= back <= date(2099, 7, 5))

    def test_exact_and_implied_departures_override_calendar_preferences(self):
        exact = TripConstraints(trip_type="round_trip", depart_date="2099-07-04", trip_nights=2)
        self.assertEqual(_initial_departure_dates(exact), [date(2099, 7, 4)])
        implied = TripConstraints(trip_type="round_trip", flexible_start="2099-07-01",
                                  flexible_end="2099-07-10", return_date="2099-07-06", trip_nights=2)
        # Exercise the service's explicit-return support independently of main's
        # intake normalization, which currently clears window return dates.
        implied = implied.model_copy(update={"return_date": date(2099, 7, 6)})
        self.assertEqual(_initial_departure_dates(implied), [date(2099, 7, 4)])

    def test_fixed_return_bounds_departures_and_one_way_ignores_hotel_stay(self):
        fields = {"flexible_start": "2099-03-01", "flexible_end": "2099-03-31",
                  "return_date": "2099-03-15"}
        round_trip = TripConstraints(trip_type="round_trip", **fields)
        round_trip = round_trip.model_copy(update={"return_date": date(2099, 3, 15)})
        self.assertTrue(all(day <= date(2099, 3, 15) for day in _initial_departure_dates(round_trip)))
        one_way = TripConstraints(trip_type="one_way", trip_nights=2, **fields)
        self.assertEqual(_initial_departure_dates(one_way)[-1], date(2099, 3, 31))

    def test_narrow_holiday_only_and_leap_day_windows_still_return_distinct_dates(self):
        for start, end in (("2099-07-03", "2099-07-05"), ("2099-03-01", "2099-03-02"),
                           ("2104-02-28", "2104-03-01"), ("2099-07-04", "2099-07-04")):
            with self.subTest(start=start, end=end):
                c = TripConstraints(trip_type="one_way", flexible_start=start, flexible_end=end)
                result = _initial_departure_dates(c)
                self.assertEqual(len(result), min(3, (c.flexible_end - c.flexible_start).days + 1))
                self.assertEqual(result, sorted(set(result)))
                self.assertTrue(all(c.flexible_start <= day <= c.flexible_end for day in result))

    def test_valid_trend_seeds_break_ties_without_overriding_calendar_quality(self):
        c = TripConstraints(trip_type="one_way", flexible_start="2099-03-01", flexible_end="2099-03-31")
        seeds = [date(2099, 3, 4), date(2099, 3, 18), date(2099, 3, 28)]
        original = deepcopy(seeds)
        self.assertEqual(_initial_departure_dates(c, preferred_dates=seeds), seeds)
        self.assertEqual(seeds, original)
        self.assertEqual(_initial_departure_dates(c, preferred_dates=[date(2099, 3, 5)])[0], date(2099, 3, 3))

    def test_missing_window_returns_no_dates_without_api_calls(self):
        with patch.object(flight_service, "_request") as request:
            self.assertEqual(_initial_departure_dates(TripConstraints(trip_type="one_way")), [])
        request.assert_not_called()


class ParseSliceTests(unittest.TestCase):
    def test_nonstop_uses_actual_airports_and_preserves_local_times(self):
        original = deepcopy(NONSTOP)
        self.assertEqual(_parse_slice(NONSTOP), {
            "origin": "ORD", "destination": "JFK",
            "depart_at": "2027-03-01T08:00:00",
            "arrive_at": "2027-03-01T11:10:59",
            "duration_min": 130, "stops": 0,
            "carriers": ["American Airlines"],
        })
        self.assertEqual(NONSTOP, original)

    def test_connection_includes_layover_and_keeps_carriers_in_flight_order(self):
        value = deepcopy(NONSTOP)
        value["duration"] = "PT7H30M"
        value["segments"].append({
            "origin": {"iata_code": "JFK"},
            "destination": {"iata_code": "BOS"},
            "departing_at": "2027-03-01T15:00:00",
            "arriving_at": "2027-03-01T16:30:00",
            "operating_carrier": {"name": "JetBlue", "iata_code": "B6"},
        })
        result = _parse_slice(value)
        self.assertEqual(result["destination"], "BOS")
        self.assertEqual(result["arrive_at"], "2027-03-01T16:30:00")
        self.assertEqual(result["duration_min"], 450)
        self.assertEqual(result["stops"], 1)
        self.assertEqual(result["carriers"], ["American Airlines", "JetBlue"])
        value["segments"][1]["operating_carrier"]["name"] = "American Airlines"
        self.assertEqual(_parse_slice(value)["carriers"], ["American Airlines"] * 2)

    def test_intermediate_stop_is_not_reported_as_nonstop(self):
        value = deepcopy(NONSTOP)
        value["segments"][0]["stops"] = [{"airport": {"iata_code": "DTW"}}]
        self.assertEqual(_parse_slice(value)["stops"], 1)

    def test_carrier_code_and_marketing_fallbacks(self):
        for operating, marketing, expected in (
            ({"name": None, "iata_code": "AA"}, None, "AA"),
            (None, {"name": "British Airways", "iata_code": "BA"}, "British Airways"),
            ({}, {"name": " ", "iata_code": "BA"}, "BA"),
        ):
            with self.subTest(operating=operating, marketing=marketing):
                value = deepcopy(NONSTOP)
                value["segments"][0].update(
                    operating_carrier=operating, marketing_carrier=marketing,
                )
                self.assertEqual(_parse_slice(value)["carriers"], [expected])

    def test_overnight_and_time_zone_crossing_keep_provider_duration(self):
        value = deepcopy(NONSTOP)
        value["duration"] = "P1DT2H5M"
        value["segments"][0].update(
            departing_at="2027-03-01T23:30:00",
            arriving_at="2027-03-03T04:35:00",
        )
        result = _parse_slice(value)
        self.assertEqual(result["duration_min"], 1565)
        self.assertEqual(result["arrive_at"], "2027-03-03T04:35:00")

    def test_malformed_slice_and_segment_fields_raise_duffel_error(self):
        # Build malformed cases independently so a failure identifies one field.
        invalid = [None, [], "private-payload", {}]
        for segments in (None, [], {}, [None], ["private-payload"]):
            invalid.append({**NONSTOP, "segments": segments})
        for field, values in (
            ("origin", (None, {}, {"iata_code": None}, {"iata_code": ""}, {"iata_code": "ORDX"})),
            ("destination", ([], {"iata_code": 123}, {"iata_code": "jfk"})),
            ("departing_at", (None, "", "2027-03-01", "2027-02-30T08:00:00")),
            ("arriving_at", (123, "private-payload", "2027-03-01T25:00:00")),
            ("stops", (None, {}, [None])),
        ):
            value = deepcopy(NONSTOP)
            del value["segments"][0][field]
            if field != "stops":
                invalid.append(value)
            for replacement in values:
                value = deepcopy(NONSTOP)
                value["segments"][0][field] = replacement
                invalid.append(value)
        value = deepcopy(NONSTOP)
        value["segments"][0].update(operating_carrier=None, marketing_carrier={})
        invalid.append(value)
        invalid.append({"segments": NONSTOP["segments"]})
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaisesRegex(DuffelError, "invalid flight slice"):
                    _parse_slice(value)

    def test_invalid_duration_raises_duffel_error(self):
        for duration in (None, "", "P", "PT", "-PT1H", "private-payload"):
            with self.subTest(duration=duration):
                with self.assertRaisesRegex(DuffelError, "invalid duration"):
                    _parse_slice({**NONSTOP, "duration": duration})


class FlightMetricsTests(unittest.TestCase):
    def test_daytime_nonstop_has_no_connection_metrics(self):
        original = deepcopy(METRICS_NONSTOP)
        self.assertEqual(_flight_metrics([METRICS_NONSTOP]), {
            "longest_layover_min": None, "shortest_connection_min": None,
            "overnight_layover": False, "airport_change": False,
            "red_eye": False, "carry_on_included": True,
        })
        self.assertEqual(METRICS_NONSTOP, original)

    def test_round_trip_aggregates_connections_without_counting_destination_stay(self):
        outbound = deepcopy(METRICS_CONNECTION)
        outbound["segments"][0]["arriving_at"] = "2027-03-01T11:10:00"
        inbound = deepcopy(METRICS_CONNECTION)
        inbound["segments"][0].update(
            origin={"iata_code": "BOS", "time_zone": "America/New_York"},
            destination={"iata_code": "JFK", "time_zone": "America/New_York"},
            departing_at="2027-03-08T08:00:00", arriving_at="2027-03-08T09:00:00",
        )
        inbound["segments"][1].update(
            destination={"iata_code": "ORD", "time_zone": "America/Chicago"},
            departing_at="2027-03-08T09:40:00", arriving_at="2027-03-08T11:00:00",
        )
        result = _flight_metrics([outbound, inbound])
        self.assertEqual(result["shortest_connection_min"], 40)
        self.assertEqual(result["longest_layover_min"], 230)
        self.assertFalse(result["overnight_layover"])
        self.assertFalse(result["airport_change"])

    def test_airport_change_uses_elapsed_time_across_zones(self):
        value = deepcopy(METRICS_CONNECTION)
        value["segments"][0]["arriving_at"] = "2027-03-01T11:00:00"
        value["segments"][1].update(
            origin={"iata_code": "ORD", "time_zone": "America/Chicago"},
            departing_at="2027-03-01T12:00:00",
        )
        result = _flight_metrics([value])
        self.assertTrue(result["airport_change"])
        self.assertEqual(result["shortest_connection_min"], 120)

    def test_overnight_connection_uses_arrival_airport_calendar(self):
        value = deepcopy(METRICS_CONNECTION)
        value["segments"][0]["arriving_at"] = "2027-03-01T23:30:00"
        value["segments"][1].update(
            departing_at="2027-03-02T06:00:00", arriving_at="2027-03-02T07:30:00",
        )
        result = _flight_metrics([value])
        self.assertTrue(result["overnight_layover"])
        self.assertEqual(result["longest_layover_min"], 390)

        # Different airport calendars alone must not invent an overnight wait.
        value["segments"][0]["arriving_at"] = "2027-03-01T22:00:00"
        value["segments"][1].update(
            origin={"iata_code": "LHR", "time_zone": "Europe/London"},
            departing_at="2027-03-02T04:30:00", arriving_at="2027-03-02T08:00:00",
        )
        result = _flight_metrics([value])
        self.assertFalse(result["overnight_layover"])
        self.assertEqual(result["longest_layover_min"], 90)

    def test_dst_spring_forward_counts_elapsed_time_not_wall_clock_time(self):
        value = deepcopy(METRICS_CONNECTION)
        value["segments"][0].update(
            departing_at="2027-03-13T22:00:00", arriving_at="2027-03-14T01:30:00",
        )
        value["segments"][1].update(
            departing_at="2027-03-14T03:30:00", arriving_at="2027-03-14T05:00:00",
        )
        self.assertEqual(_flight_metrics([value])["longest_layover_min"], 60)

    def test_explicit_offsets_disambiguate_dst_fall_back(self):
        value = deepcopy(METRICS_CONNECTION)
        value["segments"][0].update(
            departing_at="2027-11-06T22:00:00-05:00", arriving_at="2027-11-07T01:15:00-04:00",
        )
        value["segments"][1].update(
            departing_at="2027-11-07T01:45:00-05:00", arriving_at="2027-11-07T03:00:00-05:00",
        )
        self.assertEqual(_flight_metrics([value])["shortest_connection_min"], 90)

    def test_red_eye_boundaries_and_connecting_segments(self):
        for departure, arrival, expected in (
            ("2027-03-01T21:59:00", "2027-03-02T06:00:00", False),
            ("2027-03-01T22:00:00", "2027-03-02T06:00:00", True),
            ("2027-03-01T08:00:00", "2027-03-02T05:59:00", True),
            ("2027-03-01T05:59:00", "2027-03-01T09:00:00", True),
            ("2027-03-01T06:00:00", "2027-03-01T09:00:00", False),
        ):
            with self.subTest(departure=departure, arrival=arrival):
                value = deepcopy(METRICS_NONSTOP)
                value["segments"][0].update(departing_at=departure, arriving_at=arrival)
                self.assertEqual(_flight_metrics([value])["red_eye"], expected)
        value = deepcopy(METRICS_CONNECTION)
        value["segments"][1].update(
            departing_at="2027-03-01T22:00:00", arriving_at="2027-03-02T06:00:00",
        )
        self.assertTrue(_flight_metrics([value])["red_eye"])

    def test_carry_on_requires_explicit_allowance_for_every_passenger_and_segment(self):
        for baggages, expected in (
            ([{"type": "carry_on", "quantity": 1}], True),
            ([{"type": "carry_on", "quantity": 0}], False),
            ([{"type": "checked", "quantity": 1}], None),
            ([], None), (None, None),
            ([{"type": "carry_on", "quantity": True}], None),
            ([{"type": "carry_on", "quantity": -1}], None),
            ([{"type": "carry_on", "quantity": "1"}], None),
        ):
            with self.subTest(baggages=baggages):
                value = deepcopy(METRICS_CONNECTION)
                value["segments"][1]["passengers"].append({"baggages": baggages})
                self.assertIs(_flight_metrics([value])["carry_on_included"], expected)
        value = deepcopy(METRICS_CONNECTION)
        value["segments"][0].pop("passengers")
        self.assertIsNone(_flight_metrics([value])["carry_on_included"])
        value["segments"][1]["passengers"][0]["baggages"][0]["quantity"] = 0
        self.assertIs(_flight_metrics([value])["carry_on_included"], False)

    def test_invalid_times_zones_and_negative_connections_raise_duffel_error(self):
        invalid = [None, [], {}, [None]]
        for timestamp in ("2027-03-14T02:30:00", "2027-11-07T01:30:00"):
            value = deepcopy(METRICS_NONSTOP)
            value["segments"][0]["arriving_at"] = timestamp
            invalid.append([value])
        for zone in (None, "", "Not/A_Zone", "/private-path"):
            value = deepcopy(METRICS_NONSTOP)
            value["segments"][0]["origin"]["time_zone"] = zone
            invalid.append([value])
        value = deepcopy(METRICS_NONSTOP)
        del value["segments"][0]["origin"]["time_zone"]
        invalid.append([value])
        value = deepcopy(METRICS_CONNECTION)
        value["segments"][1]["departing_at"] = "2027-03-01T10:00:00"
        invalid.append([value])
        value = deepcopy(METRICS_NONSTOP)
        value["segments"][0]["arriving_at"] = "2027-03-01T07:00:00"
        invalid.append([value])
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(DuffelError):
                    _flight_metrics(value)


class ParseOfferTests(unittest.TestCase):
    def test_one_way_combines_price_slice_and_metrics_without_mutating_response(self):
        original = deepcopy(OFFER)
        result = _parse_offer(OFFER, 3, now=OFFER_NOW)
        self.assertEqual(result, {
            "offer_id": "off_example", "total_amount": 420.0, "per_person": 140.0,
            "currency": "USD", "expires_at": "2027-02-01T12:30:00Z",
            "from_alternative_airport": False, "tags": [],
            "outbound": {
                "origin": "ORD", "destination": "JFK",
                "depart_at": "2027-03-01T08:00:00", "arrive_at": "2027-03-01T11:10:59",
                "duration_min": 130, "stops": 0, "carriers": ["American Airlines"],
            },
            "inbound": None,
            "metrics": {
                "longest_layover_min": None, "shortest_connection_min": None,
                "overnight_layover": False, "airport_change": False,
                "red_eye": False, "carry_on_included": True,
            },
        })
        self.assertEqual(OFFER, original)

    def test_round_trip_uses_one_total_and_combines_baggage_metrics(self):
        value = deepcopy(OFFER)
        inbound = deepcopy(METRICS_NONSTOP)
        inbound["segments"][0].update(
            origin={"iata_code": "JFK", "time_zone": "America/New_York"},
            destination={"iata_code": "ORD", "time_zone": "America/Chicago"},
            departing_at="2027-03-08T08:00:00", arriving_at="2027-03-08T09:10:00",
        )
        inbound["segments"][0]["passengers"][0]["baggages"][0]["quantity"] = 0
        value["slices"].append(inbound)
        result = _parse_offer(value, 2, from_alternative_airport=True, now=OFFER_NOW)
        self.assertEqual(result["total_amount"], 420.0)
        self.assertEqual(result["per_person"], 210.0)
        self.assertEqual(result["inbound"]["origin"], "JFK")
        self.assertEqual(result["inbound"]["destination"], "ORD")
        self.assertTrue(result["from_alternative_airport"])
        self.assertEqual(result["tags"], [])
        self.assertIs(result["metrics"]["carry_on_included"], False)
        self.assertIsNone(result["metrics"]["longest_layover_min"])
        self.assertFalse(result["metrics"]["overnight_layover"])

    def test_prices_round_half_up_without_binary_float_division(self):
        for amount, travelers, total, per_person in (
            ("0.29", 2, 0.29, 0.15),
            ("10.005", 2, 10.01, 5.01),
            ("100.00", 3, 100.0, 33.33),
            ("0.00", 1, 0.0, 0.0),
        ):
            with self.subTest(amount=amount, travelers=travelers):
                result = _parse_offer({**OFFER, "total_amount": amount}, travelers, now=OFFER_NOW)
                self.assertEqual(result["total_amount"], total)
                self.assertEqual(result["per_person"], per_person)

    def test_non_usd_offers_are_rejected_without_currency_conversion(self):
        for currency in ("GBP", "EUR", "usd", "", None):
            with self.subTest(currency=currency):
                with self.assertRaisesRegex(DuffelError, "non-USD"):
                    _parse_offer({**OFFER, "total_currency": currency}, 1, now=OFFER_NOW)

    def test_expiry_compares_instants_and_rejects_the_exact_expiry_boundary(self):
        for expires_at in ("2027-02-01T11:59:59Z", "2027-02-01T12:00:00Z",
                           "2027-02-01T07:00:00-05:00"):
            with self.subTest(expires_at=expires_at):
                with self.assertRaisesRegex(DuffelError, "has expired"):
                    _parse_offer({**OFFER, "expires_at": expires_at}, 1, now=OFFER_NOW)
        expires_at = "2027-02-01T07:00:00.000001-05:00"
        result = _parse_offer({**OFFER, "expires_at": expires_at}, 1, now=OFFER_NOW)
        self.assertEqual(result["expires_at"], expires_at)
        local_now = OFFER_NOW.astimezone(timezone(timedelta(hours=9)))
        self.assertEqual(_parse_offer(OFFER, 1, now=local_now)["offer_id"], "off_example")

    def test_missing_and_malformed_offer_data_raise_duffel_error(self):
        invalid = [None, [], "private-payload", {}]
        for field, replacements in (
            ("id", (None, "", "   ", 123)),
            ("expires_at", (None, "", "private-payload", "2027-02-01",
                            "2027-02-01T12:30:00", "2027-02-30T12:30:00Z")),
            ("slices", (None, [], {}, [None], [METRICS_NONSTOP] * 3)),
            ("total_amount", (None, True, "private-payload", "NaN", "Infinity", "-1.00")),
            ("total_currency", ()),
        ):
            value = deepcopy(OFFER)
            del value[field]
            invalid.append(value)
            invalid.extend({**OFFER, field: replacement} for replacement in replacements)
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(DuffelError) as caught:
                    _parse_offer(value, 1, now=OFFER_NOW)
                self.assertNotIn("private-payload", str(caught.exception))

    def test_malformed_return_slice_is_not_silently_treated_as_one_way(self):
        with self.assertRaises(DuffelError):
            _parse_offer({**OFFER, "slices": [METRICS_NONSTOP, {}]}, 1, now=OFFER_NOW)

    def test_invalid_caller_inputs_raise_value_error(self):
        for travelers in (None, True, 0, -1, 1.5, "2"):
            with self.subTest(travelers=travelers):
                with self.assertRaisesRegex(ValueError, "travelers"):
                    _parse_offer(OFFER, travelers, now=OFFER_NOW)
        for now in ("2027-02-01T12:00:00Z", datetime(2027, 2, 1, 12)):
            with self.subTest(now=now):
                with self.assertRaisesRegex(ValueError, "timezone-aware"):
                    _parse_offer(OFFER, 1, now=now)
        with self.assertRaisesRegex(ValueError, "from_alternative_airport"):
            _parse_offer(OFFER, 1, from_alternative_airport="false", now=OFFER_NOW)


class ShortlistOfferTests(unittest.TestCase):
    def setUp(self):
        self.offers = []
        for offer_id, price, minutes in (
            ("off_cheap", 100, 600), ("off_fast", 400, 60),
            ("off_balance", 150, 180), ("off_runner_up", 180, 200),
            ("off_more_value", 200, 220), ("off_slow", 500, 700),
            ("off_costly", 450, 650),
        ):
            offer = _parse_offer(
                {**OFFER, "id": offer_id, "total_amount": str(price)}, 1, now=OFFER_NOW,
            )
            offer["outbound"]["duration_min"] = minutes
            self.offers.append(offer)

    def test_keeps_cheapest_fastest_and_tradeoffs_up_to_twenty(self):
        extras = []
        for index in range(25):
            offer = deepcopy(self.offers[0])
            offer.update(offer_id=f"off_extra_{index:02d}", total_amount=600.0 + index)
            offer["outbound"]["duration_min"] = 800 + index
            extras.append(offer)
        # The best offers occur after the first twenty input rows.
        result = _shortlist_offers(extras + self.offers, now=OFFER_NOW)
        self.assertEqual(len(result), 20)
        self.assertEqual([offer["offer_id"] for offer in result], [
            "off_cheap", "off_balance", "off_runner_up", "off_more_value", "off_fast",
            "off_costly", "off_slow",
        ] + [f"off_extra_{index:02d}" for index in range(13)])
        tags = {offer["offer_id"]: offer["tags"] for offer in result}
        self.assertEqual(tags["off_cheap"], ["cheapest"])
        self.assertEqual(tags["off_fast"], ["fastest"])
        self.assertEqual(tags["off_balance"], ["best_balance"])
        self.assertEqual(tags["off_runner_up"], [])
        self.assertEqual(tags["off_more_value"], [])

    def test_drops_risky_offer_only_when_a_similar_clean_option_exists(self):
        clean = {**self.offers[2], "total_amount": 155.0}
        for flag in ("overnight_layover", "airport_change"):
            with self.subTest(flag=flag):
                risky = deepcopy(self.offers[2])
                risky["offer_id"] = "off_risky"
                risky["metrics"][flag] = True
                result = _shortlist_offers([risky, clean], now=OFFER_NOW)
                self.assertEqual([offer["offer_id"] for offer in result], [clean["offer_id"]])
                expensive = {**clean, "total_amount": 200.0}
                self.assertEqual(len(_shortlist_offers([risky, expensive], now=OFFER_NOW)), 2)

    def test_guarantees_cheapest_alternative_and_only_tags_that_alternative(self):
        self.offers[-1]["from_alternative_airport"] = True
        self.offers[-2]["from_alternative_airport"] = True
        result = _shortlist_offers(self.offers, now=OFFER_NOW)
        self.assertEqual([offer["offer_id"] for offer in result if "alternative_airport" in offer["tags"]],
                         ["off_costly"])

    def test_balance_prefers_fewer_stops_and_shorter_layovers(self):
        indirect, direct = deepcopy(self.offers[:2])
        indirect["outbound"].update(duration_min=180, stops=1)
        direct["outbound"].update(duration_min=180, stops=0)
        indirect["total_amount"] = direct["total_amount"] = 200.0
        indirect["metrics"]["longest_layover_min"] = 90
        result = _shortlist_offers([indirect, direct], now=OFFER_NOW)
        self.assertNotIn("best_balance", result[0]["tags"])
        self.assertIn("best_balance", result[1]["tags"])

    def test_rechecks_expiry_including_offset_and_exact_boundary(self):
        for expires_at in ("2027-02-01T11:59:59Z", "2027-02-01T12:00:00Z",
                           "2027-02-01T07:00:00-05:00"):
            with self.subTest(expires_at=expires_at):
                expired = {**self.offers[0], "expires_at": expires_at}
                result = _shortlist_offers([expired, self.offers[1]], now=OFFER_NOW)
                self.assertEqual([offer["offer_id"] for offer in result], ["off_fast"])
        live = {**self.offers[0], "expires_at": "2027-02-01T07:00:00.000001-05:00"}
        self.assertEqual(len(_shortlist_offers([live], now=OFFER_NOW)), 1)

    def test_unknown_or_invalid_expiry_is_excluded(self):
        for expires_at in (None, "not-a-time", "2027-02-01T12:30:00"):
            with self.subTest(expires_at=expires_at):
                unknown = {**self.offers[0], "expires_at": expires_at}
                self.assertEqual(_shortlist_offers([unknown], now=OFFER_NOW), [])
        unknown = deepcopy(self.offers[0])
        del unknown["expires_at"]
        self.assertEqual(_shortlist_offers([unknown], now=OFFER_NOW), [])

    def test_duplicate_ids_do_not_fill_shortlist_or_hide_a_live_copy(self):
        expired = {**self.offers[0], "expires_at": OFFER_NOW.isoformat()}
        result = _shortlist_offers(
            [expired, self.offers[0], deepcopy(self.offers[0]), self.offers[1]], now=OFFER_NOW,
        )
        self.assertEqual([offer["offer_id"] for offer in result], ["off_cheap", "off_fast"])

    def test_small_result_sets_are_not_padded_and_one_offer_can_have_all_roles(self):
        self.assertEqual(_shortlist_offers([], now=OFFER_NOW), [])
        for count in range(1, len(self.offers) + 1):
            with self.subTest(count=count):
                result = _shortlist_offers(self.offers[:count], now=OFFER_NOW)
                self.assertEqual(len(result), count)
                self.assertEqual(len({offer["offer_id"] for offer in result}), count)
        result = _shortlist_offers(self.offers[:1], now=OFFER_NOW)
        self.assertEqual(result[0]["tags"], ["cheapest", "fastest", "best_balance"])

    def test_round_trip_speed_includes_both_directions_but_not_destination_stay(self):
        offers = deepcopy(self.offers[:2])
        offers[0]["outbound"]["duration_min"] = 60
        offers[0]["inbound"] = {**offers[0]["outbound"], "duration_min": 600}
        offers[1]["outbound"]["duration_min"] = 120
        offers[1]["inbound"] = {**offers[1]["outbound"], "duration_min": 120}
        result = _shortlist_offers(offers, now=OFFER_NOW)
        tags = {offer["offer_id"]: offer["tags"] for offer in result}
        self.assertNotIn("fastest", tags["off_cheap"])
        self.assertIn("fastest", tags["off_fast"])

    def test_ties_zero_prices_and_constant_durations_are_deterministic(self):
        offers = [{**self.offers[0], "offer_id": f"off_{index:02d}", "total_amount": 0.0}
                  for index in range(25)]
        result = _shortlist_offers(offers, now=OFFER_NOW)
        reversed_result = _shortlist_offers(list(reversed(offers)), now=OFFER_NOW)
        self.assertEqual(result, reversed_result)
        self.assertEqual([offer["offer_id"] for offer in result], [f"off_{index:02d}" for index in range(20)])
        self.assertEqual(result[0]["tags"], ["cheapest", "fastest", "best_balance"])

    def test_ranking_is_independent_of_input_order(self):
        self.assertEqual(
            _shortlist_offers(self.offers, now=OFFER_NOW),
            _shortlist_offers(list(reversed(self.offers)), now=OFFER_NOW),
        )

    def test_labels_alternatives_without_changing_or_sharing_input_state(self):
        self.offers[0]["tags"] = ["fastest"]  # Discard labels from an earlier ranking.
        self.offers[2]["from_alternative_airport"] = True
        original = deepcopy(self.offers)
        result = _shortlist_offers(self.offers, now=OFFER_NOW)
        self.assertEqual(self.offers, original)
        self.assertEqual(result[0]["tags"], ["cheapest"])
        self.assertEqual(result[1]["tags"], ["best_balance", "alternative_airport"])
        result[0]["tags"].append("changed")
        result[0]["outbound"]["carriers"].append("changed")
        self.assertEqual(self.offers, original)

    def test_invalid_caller_inputs_raise_value_error(self):
        with self.assertRaisesRegex(ValueError, "offers must be a list"):
            _shortlist_offers(None, now=OFFER_NOW)
        for now in ("2027-02-01T12:00:00Z", datetime(2027, 2, 1, 12)):
            with self.subTest(now=now):
                with self.assertRaisesRegex(ValueError, "timezone-aware"):
                    _shortlist_offers(self.offers, now=now)


class SearchOffersTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(flight_service, "_request")
        self.request = patcher.start()
        self.addCleanup(patcher.stop)
        self.request.return_value = {"data": {"offers": [deepcopy(OFFER)]}}

    def test_trimmed_duffel_test_mode_response(self):
        fixture = Path(__file__).parent / "fixtures" / "duffel" / "round_trip.json"
        self.request.return_value = json.loads(fixture.read_text(encoding="utf-8"))
        result = _search_offers(
            "CMI", "JFK", date(2027, 3, 2), 2, return_date=date(2027, 3, 6),
            now=datetime(2026, 11, 16, 13, 37, tzinfo=timezone.utc),
        )
        self.assertEqual(len(result), 1)
        offer = result[0]
        self.assertEqual((offer["total_amount"], offer["per_person"], offer["currency"]), (223.34, 111.67, "USD"))
        self.assertEqual((offer["outbound"]["origin"], offer["outbound"]["destination"]), ("CMI", "JFK"))
        self.assertEqual((offer["inbound"]["origin"], offer["inbound"]["destination"]), ("JFK", "CMI"))

    def test_one_way_sends_normalized_airports_and_adults_and_parses_response(self):
        result = _search_offers(" ord ", "jfk", date(2027, 3, 1), 2, now=OFFER_NOW)
        self.request.assert_called_once_with(
            "POST", "/air/offer_requests",
            params={"return_offers": True, "view": "offers"},
            json={"data": {
                "slices": [{"origin": "ORD", "destination": "JFK", "departure_date": "2027-03-01"}],
                "passengers": [{"type": "adult"}, {"type": "adult"}],
            }},
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["offer_id"], "off_example")
        self.assertEqual(result[0]["total_amount"], 420.0)
        self.assertEqual(result[0]["per_person"], 210.0)
        self.assertIsNone(result[0]["inbound"])
        self.assertIs(result[0]["metrics"]["carry_on_included"], True)

    def test_round_trip_reverses_airports_preserves_zero_connections_and_marks_alternatives(self):
        value = deepcopy(OFFER)
        inbound = deepcopy(METRICS_NONSTOP)
        inbound["segments"][0].update(
            origin={"iata_code": "JFK", "time_zone": "America/New_York"},
            destination={"iata_code": "ORD", "time_zone": "America/Chicago"},
            departing_at="2027-03-08T08:00:00", arriving_at="2027-03-08T09:10:00",
        )
        value["slices"].append(inbound)
        self.request.return_value = {"data": {"offers": [value]}}
        result = _search_offers(
            "ORD", "JFK", date(2027, 3, 1), 2,
            return_date=date(2027, 3, 8), max_connections=0,
            from_alternative_airport=True, now=OFFER_NOW,
        )
        self.request.assert_called_once_with(
            "POST", "/air/offer_requests",
            params={"return_offers": True, "view": "offers"},
            json={"data": {
                "slices": [
                    {"origin": "ORD", "destination": "JFK", "departure_date": "2027-03-01"},
                    {"origin": "JFK", "destination": "ORD", "departure_date": "2027-03-08"},
                ],
                "passengers": [{"type": "adult"}, {"type": "adult"}],
                "max_connections": 0,
            }},
        )
        self.assertEqual(result[0]["inbound"]["destination"], "ORD")
        self.assertTrue(result[0]["from_alternative_airport"])
        self.assertEqual(result[0]["tags"], [])

    def test_supported_traveler_and_connection_limits_are_sent(self):
        for connections in (1, 2):
            with self.subTest(connections=connections):
                _search_offers("ORD", "JFK", date(2027, 3, 1), 9,
                               max_connections=connections, now=OFFER_NOW)
                payload = self.request.call_args.kwargs["json"]["data"]
                self.assertEqual(len(payload["passengers"]), 9)
                self.assertEqual(payload["max_connections"], connections)

    def test_all_offers_are_parsed_and_sorted_without_applying_the_shortlist_cap(self):
        self.request.return_value = {"data": {"offers": [
            {**OFFER, "id": f"off_{index:02d}", "total_amount": str(100 + index)}
            for index in reversed(range(25))
        ]}}
        result = _search_offers("ORD", "JFK", date(2027, 3, 1), 1, now=OFFER_NOW)
        self.assertEqual(len(result), 25)
        self.assertEqual([offer["offer_id"] for offer in result], [f"off_{index:02d}" for index in range(25)])
        self.request.assert_called_once()

    def test_unusable_offers_are_skipped_without_discarding_valid_results(self):
        malformed_metrics = deepcopy(OFFER)
        del malformed_metrics["slices"][0]["segments"][0]["origin"]["time_zone"]
        self.request.return_value = {"data": {"offers": [
            None, {}, {**OFFER, "expires_at": OFFER_NOW.isoformat()},
            {**OFFER, "total_currency": "GBP"}, {**OFFER, "total_amount": "NaN"},
            {**OFFER, "partial": True}, malformed_metrics, OFFER,
        ]}}
        original = deepcopy(self.request.return_value)
        result = _search_offers("ORD", "JFK", date(2027, 3, 1), 1, now=OFFER_NOW)
        self.assertEqual([offer["offer_id"] for offer in result], ["off_example"])
        self.assertEqual(self.request.return_value, original)

    def test_wrong_slice_counts_do_not_turn_round_trips_into_one_way_prices(self):
        result = _search_offers("ORD", "JFK", date(2027, 3, 1), 1,
                                return_date=date(2027, 3, 8), now=OFFER_NOW)
        self.assertEqual(result, [])
        self.request.return_value = {"data": {"offers": [{**OFFER, "slices": [METRICS_NONSTOP] * 2}]}}
        self.assertEqual(_search_offers("ORD", "JFK", date(2027, 3, 1), 1, now=OFFER_NOW), [])

    def test_empty_or_entirely_unusable_offer_list_returns_empty(self):
        for offers in ([], [None, {}, {**OFFER, "total_currency": "EUR"}]):
            with self.subTest(offers=offers):
                self.request.return_value = {"data": {"offers": offers}}
                self.assertEqual(_search_offers("ORD", "JFK", date(2027, 3, 1), 1, now=OFFER_NOW), [])

    def test_malformed_response_envelope_raises_duffel_error(self):
        for body in (None, [], {}, {"data": None}, {"data": []}, {"data": {}},
                     {"data": {"offers": None}}, {"data": {"offers": {}}}):
            with self.subTest(body=body):
                self.request.return_value = body
                with self.assertRaisesRegex(DuffelError, "invalid offer search response"):
                    _search_offers("ORD", "JFK", date(2027, 3, 1), 1, now=OFFER_NOW)

    def test_guarded_client_failure_propagates_without_retry(self):
        for message in ("Duffel daily search limit reached", "Duffel request timed out"):
            with self.subTest(message=message):
                self.request.reset_mock()
                error = DuffelError(message)
                self.request.side_effect = error
                with self.assertRaises(DuffelError) as caught:
                    _search_offers("ORD", "JFK", date(2027, 3, 1), 1, now=OFFER_NOW)
                self.assertIs(caught.exception, error)
                self.request.assert_called_once()

    def test_invalid_inputs_are_rejected_before_sending_a_request(self):
        base = {"origin": "ORD", "destination": "JFK", "depart_date": date(2027, 3, 1),
                "travelers": 1, "now": OFFER_NOW}
        for field, values in (
            ("origin", (None, "", "OR", "ORDX", "O1D")),
            ("destination", (123, "", "ORD")),
            ("depart_date", (None, "2027-03-01", datetime(2027, 3, 1))),
            ("return_date", ("2027-03-08", date(2027, 2, 28), datetime(2027, 3, 8))),
            ("travelers", (None, True, 0, -1, 10, 1.5, "2")),
            ("max_connections", (True, -1, 3, 1.5, "1")),
            ("from_alternative_airport", (None, "false", 1)),
            ("now", ("2027-02-01T12:00:00Z", datetime(2027, 2, 1, 12))),
        ):
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        _search_offers(**{**base, field: value})
        self.request.assert_not_called()


class RefreshOfferTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(flight_service, "_request")
        self.request = patcher.start()
        self.addCleanup(patcher.stop)
        self.request.return_value = {"data": deepcopy(OFFER)}

    def test_refresh_gets_latest_price_expiry_flight_details_and_baggage(self):
        latest = self.request.return_value["data"]
        latest.update(total_amount="215.00", expires_at="2027-02-01T12:45:00Z")
        latest["slices"][0]["duration"] = "PT3H"
        latest["slices"][0]["segments"][0]["arriving_at"] = "2027-03-01T12:00:00"
        latest["slices"][0]["segments"][0]["passengers"][0]["baggages"][0]["quantity"] = 0
        original = deepcopy(self.request.return_value)
        result = refresh_offer(" off_example ", 2, now=OFFER_NOW)
        self.request.assert_called_once_with(
            "GET", "/air/offers/off_example", params={"return_available_services": False},
        )
        self.assertEqual(result["offer_id"], "off_example")
        self.assertEqual(result["total_amount"], 215.0)
        self.assertEqual(result["per_person"], 107.5)
        self.assertEqual(result["expires_at"], "2027-02-01T12:45:00Z")
        self.assertEqual(result["outbound"]["duration_min"], 180)
        self.assertEqual(result["outbound"]["arrive_at"], "2027-03-01T12:00:00")
        self.assertIs(result["metrics"]["carry_on_included"], False)
        self.assertEqual(result["tags"], [])
        self.assertEqual(self.request.return_value, original)

    def test_round_trip_and_alternative_airport_flag_are_preserved(self):
        inbound = deepcopy(METRICS_NONSTOP)
        inbound["segments"][0].update(
            origin={"iata_code": "JFK", "time_zone": "America/New_York"},
            destination={"iata_code": "ORD", "time_zone": "America/Chicago"},
            departing_at="2027-03-08T08:00:00", arriving_at="2027-03-08T09:10:00",
        )
        self.request.return_value["data"]["slices"].append(inbound)
        result = refresh_offer("off_example", 3, from_alternative_airport=True, now=OFFER_NOW)
        self.assertEqual(result["per_person"], 140.0)
        self.assertEqual(result["inbound"]["origin"], "JFK")
        self.assertEqual(result["inbound"]["destination"], "ORD")
        self.assertTrue(result["from_alternative_airport"])
        self.assertEqual(result["tags"], [])

    def test_offer_id_is_escaped_as_one_path_component(self):
        offer_id = "off_example/extra?query=1#fragment"
        self.request.return_value["data"]["id"] = offer_id
        result = refresh_offer(offer_id, 1, now=OFFER_NOW)
        self.assertEqual(result["offer_id"], offer_id)
        self.request.assert_called_once_with(
            "GET", "/air/offers/off_example%2Fextra%3Fquery%3D1%23fragment",
            params={"return_available_services": False},
        )

    def test_invalid_envelopes_mismatched_ids_and_partial_offers_are_rejected(self):
        for body in (None, [], {}, {"data": None}, {"data": []}, {"data": {}},
                     {"data": {**OFFER, "id": "off_other"}},
                     {"data": {**OFFER, "partial": True}}):
            with self.subTest(body=body):
                self.request.return_value = body
                with self.assertRaisesRegex(DuffelError, "invalid offer refresh response"):
                    refresh_offer("off_example", 1, now=OFFER_NOW)

    def test_expired_non_usd_and_malformed_results_raise_parser_errors(self):
        for fields, message in (
            ({"expires_at": OFFER_NOW.isoformat()}, "has expired"),
            ({"total_currency": "GBP"}, "non-USD"),
            ({"total_amount": "NaN"}, "invalid monetary amount"),
            ({"slices": []}, "invalid flight offer"),
        ):
            with self.subTest(fields=fields):
                self.request.return_value = {"data": {**OFFER, **fields}}
                with self.assertRaisesRegex(DuffelError, message):
                    refresh_offer("off_example", 1, now=OFFER_NOW)

    def test_unavailable_offer_or_client_failure_propagates_without_retry(self):
        for message in ("Duffel GET /air/offers/off_example returned HTTP 404",
                        "Duffel GET /air/offers/off_example timed out",
                        "Duffel daily search limit reached"):
            with self.subTest(message=message):
                self.request.reset_mock()
                error = DuffelError(message)
                self.request.side_effect = error
                with self.assertRaises(DuffelError) as caught:
                    refresh_offer("off_example", 1, now=OFFER_NOW)
                self.assertIs(caught.exception, error)
                self.request.assert_called_once()

    def test_refresh_uses_one_counted_http_call_and_daily_guard_blocks_the_next(self):
        self.request.side_effect = duffel_client._request
        response = httpx.Response(
            200, json={"data": OFFER},
            request=httpx.Request("GET", "https://api.duffel.com/air/offers/off_example"),
        )
        with patch.object(duffel_client.settings, "DUFFEL_API_KEY", "offline-test-key"), \
             patch.object(duffel_client.settings, "DUFFEL_DAILY_SEARCH_LIMIT", 1), \
             patch.object(duffel_client, "_usage", {"day": None, "count": 0}), \
             patch.object(duffel_client.httpx, "request", return_value=response) as http:
            refresh_offer("off_example", 1, now=OFFER_NOW)
            self.assertEqual(duffel_client._usage["count"], 1)
            with self.assertRaisesRegex(DuffelError, "daily search limit"):
                refresh_offer("off_example", 1, now=OFFER_NOW)
            http.assert_called_once()
            self.assertEqual(http.call_args.args, ("GET", "https://api.duffel.com/air/offers/off_example"))
            self.assertEqual(duffel_client._usage["count"], 1)

    def test_invalid_inputs_are_rejected_before_any_request(self):
        base = {"offer_id": "off_example", "travelers": 1, "now": OFFER_NOW}
        for field, values in (
            ("offer_id", (None, "", "  ", 123)),
            ("travelers", (None, True, 0, -1, 10, 1.5, "2")),
            ("from_alternative_airport", (None, "false", 1)),
            ("now", ("2027-02-01T12:00:00Z", datetime(2027, 2, 1, 12))),
        ):
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        refresh_offer(**{**base, field: value})
        self.request.assert_not_called()
