"""Contract and edge-case tests using sanitized sandbox response excerpts."""
from datetime import date, datetime
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

with patch.dict(os.environ, {"SECRET_KEY": "test-only", "DATABASE_URL": "sqlite://", "LITEAPI_API_KEY": "test-only"}):
    from services import hotel_service as hotels

FIXTURES = Path(__file__).parent / "fixtures" / "liteapi"
CHECKIN, CHECKOUT = date(2026, 11, 18), date(2026, 11, 21)


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class HotelServiceTests(unittest.TestCase):
    def setUp(self):
        self.transport = patch("services.liteapi_client.httpx.request", side_effect=AssertionError("Network forbidden in unit tests"))
        self.transport.start()
        self.addCleanup(self.transport.stop)
        self.calls = patch("services.hotel_service._request")
        self.request = self.calls.start()
        self.addCleanup(self.calls.stop)

    def test_city_uses_places_details_and_state(self):
        self.request.side_effect = [fixture("places"), fixture("place")]
        location = hotels.get_city_location("Denver", "CO")
        self.assertEqual(location["name"], "Denver")
        self.assertAlmostEqual(location["latitude"], 39.7392358)
        self.assertAlmostEqual(location["longitude"], -104.990251)
        self.assertIn("CO", self.request.call_args_list[0].kwargs["params"]["textQuery"])
        self.assertEqual(self.request.call_args_list[0].kwargs["params"]["type"], "locality")
        self.assertTrue(self.request.call_args_list[1].args[1].startswith("/data/places/"))

    def test_unknown_city_is_none(self):
        self.request.return_value = {"data": []}
        self.assertIsNone(hotels.get_city_location("Unknown city"))
        self.assertEqual(self.request.call_count, 1)

    def test_city_rejects_foreign_or_wrong_state_results(self):
        for change in [{"countryCode": "CA"}, {"addressComponents": []}]:
            with self.subTest(change=change):
                place = fixture("place")
                place["data"].update(change)
                self.request.side_effect = [{"data": fixture("places")["data"][:1]}, place]
                self.assertIsNone(hotels.get_city_location("Denver", "CO"))

    def test_city_supports_display_name_object(self):
        places, place = fixture("places"), fixture("place")
        places["data"][0]["displayName"] = {"text": "Denver"}
        place["data"]["displayName"] = {"text": "Denver"}
        self.request.side_effect = [places, place]
        self.assertEqual(hotels.get_city_location("Denver")["name"], "Denver")

    def test_nearby_hotels_sorted_and_bounded(self):
        self.request.return_value = fixture("hotels")
        result = hotels.search_hotels_near(39.7392358, -104.990251, min_rating=8, facility_ids=[2, 301])
        distances = [hotel["distance_km"] for hotel in result]
        self.assertEqual(distances, sorted(distances))
        self.assertTrue(all(distance <= 5 for distance in distances))
        self.assertTrue(result)
        params = self.request.call_args.kwargs["params"]
        self.assertEqual(params["facilityIds"], "2,301")
        self.assertEqual(params["strictFacilitiesFiltering"], "true")
        self.assertEqual(params["minRating"], 8)
        self.assertNotIn("facilityIds", result[0])

    def test_subkilometre_radius_excludes_outer_candidates_before_rounding(self):
        self.request.return_value = {"data": [
            {"id": "inside", "latitude": 0.004, "longitude": 0},
            {"id": "outside", "latitude": 0.0045, "longitude": 0},
            {"id": "unknown", "latitude": None, "longitude": 0},
        ]}
        result = hotels.search_hotels_near(0, 0, radius_m=500)
        self.assertEqual([hotel["hotel_id"] for hotel in result], ["inside"])
        self.assertEqual(self.request.call_args.kwargs["params"]["radius"], 1000)

    def test_invalid_search_inputs_do_not_call_api(self):
        for kwargs in [{"latitude": 100}, {"longitude": float("nan")}, {"radius_m": 0},
                       {"limit": -1}, {"min_rating": 11}, {"facility_ids": [0]}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                hotels.search_hotels_near(**{"latitude": 0, "longitude": 0, **kwargs})
        self.request.assert_not_called()

    def test_details_return_names_and_actual_checkin_field(self):
        data = fixture("hotel")
        self.request.return_value = data
        result = hotels.get_hotel_details(data["data"]["id"])
        self.assertEqual(result["check_in"], "04:00 PM")
        self.assertEqual(result["check_out"], "11:00 AM")
        self.assertTrue(result["amenities"])
        self.assertTrue(all(isinstance(name, str) for name in result["amenities"]))
        self.assertEqual(len(result["amenities"]), len(set(result["amenities"])))
        self.assertNotIn("email", result)

    def test_missing_details_are_empty(self):
        self.request.return_value = {"data": None}
        self.assertEqual(hotels.get_hotel_details("none"), {})

    def test_details_keep_unknown_policies_unknown(self):
        self.request.return_value = {"data": {"name": "Example", "hotelFacilities": None, "facilities": None, "policies": [
            {"policy_type": "parking", "description": "Paid parking"},
            {"pets_allowed": "Pets on request"},
        ]}}
        result = hotels.get_hotel_details("example")
        self.assertIsNone(result["pets_allowed"])
        self.assertEqual(result["amenities"], [])
        self.assertEqual(result["parking_policy"], "Paid parking")
        self.assertEqual(result["pet_policy"], "Pets on request")

    def test_facilities_match_case_punctuation_and_aliases(self):
        self.request.return_value = fixture("facilities")
        self.assertEqual(hotels.find_facility_ids(["Wi-Fi", "PARKING", "pool", "wifi"]), [47, 2, 301])
        self.assertNotIn(199, hotels.find_facility_ids(["pool"]))  # Pool bar is not a pool.

    def test_facilities_empty_or_unmatched(self):
        self.assertEqual(hotels.find_facility_ids([]), [])
        self.request.assert_not_called()
        self.request.return_value = fixture("facilities")
        self.assertEqual(hotels.find_facility_ids(["nonexistent amenity"]), [])

    def test_cheapest_rates_normalize_sort_and_preserve_sandbox(self):
        self.request.return_value = {"data": [
            {"hotelId": "expensive", "price": 600}, {"hotelId": "cheap", "price": 300}], "sandbox": True}
        result = hotels.get_cheapest_rates(["expensive", "cheap"], CHECKIN, CHECKOUT, 3, 2)
        self.assertEqual([rate["hotel_id"] for rate in result], ["cheap", "expensive"])
        self.assertEqual(result[0]["total_price"], 300)
        self.assertEqual(result[0]["per_night"], 100)
        self.assertTrue(result[0]["sandbox"])
        datetime.fromisoformat(result[0]["fetched_at"])
        payload = self.request.call_args.kwargs["json"]
        self.assertEqual(payload["occupancies"], [{"adults": 2}, {"adults": 1}])
        self.assertEqual(payload["checkin"], "2026-11-18")
        self.assertEqual(payload["currency"], "USD")
        self.assertEqual(payload["guestNationality"], "US")

    def test_sandbox_minimum_response_contract(self):
        self.request.return_value = fixture("min_rates")
        identifiers = [row["hotelId"] for row in fixture("min_rates")["data"]]
        result = hotels.get_cheapest_rates(identifiers, CHECKIN, CHECKOUT, 2)
        self.assertEqual(len(result), 5)
        self.assertEqual(result[0]["total_price"], 332.31)
        self.assertEqual(result[0]["per_night"], 110.77)

    def test_cheapest_ignores_missing_invalid_or_unrequested_prices(self):
        self.request.return_value = {"data": [{"hotelId": "missing"}, {"hotelId": "nan", "price": "NaN"},
            {"hotelId": "unrequested", "price": 1}, {"hotelId": "valid", "price": "300"},
            {"hotelId": "valid", "price": "330"}]}
        result = hotels.get_cheapest_rates(["missing", "nan", "valid"], CHECKIN, CHECKOUT, 2)
        self.assertEqual([row["hotel_id"] for row in result], ["valid"])
        self.assertEqual(result[0]["total_price"], 300)

    def test_empty_hotel_ids_do_not_request_prices(self):
        self.assertEqual(hotels.get_cheapest_rates([], CHECKIN, CHECKOUT, 2), [])
        self.request.assert_not_called()

    def test_invalid_stays_and_occupancies_fail_before_network(self):
        for checkin, checkout, adults, rooms in [(CHECKOUT, CHECKIN, 2, 1), (CHECKIN, CHECKIN, 2, 1),
                ("2026-11-18", CHECKOUT, 2, 1), (CHECKIN, CHECKOUT, 0, 1),
                (CHECKIN, CHECKOUT, 2, 3), (CHECKIN, CHECKOUT, True, 1)]:
            with self.subTest(adults=adults, rooms=rooms), self.assertRaises(ValueError):
                hotels.get_cheapest_rates(["hotel"], checkin, checkout, adults, rooms)
        self.request.assert_not_called()

    def test_adults_spread_evenly_across_rooms(self):
        self.assertEqual(hotels._occupancies(4, 2), [{"adults": 2}, {"adults": 2}])
        self.assertEqual(hotels._occupancies(3, 2), [{"adults": 2}, {"adults": 1}])

    def test_room_offers_parse_real_single_room_response(self):
        body = fixture("rates")
        self.request.return_value = body
        result = hotels.get_room_offers(body["data"][0]["hotelId"], CHECKIN, CHECKOUT, 2)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["total_price"], 610.41)
        self.assertEqual(result[0]["per_night"], 203.47)
        self.assertEqual(result[0]["board_type"], "Room Only")
        self.assertFalse(result[0]["refundable"])

    def test_multi_room_offer_uses_whole_offer_total(self):
        body = fixture("multiroom_rates")
        self.request.return_value = body
        result = hotels.get_room_offers(body["data"][0]["hotelId"], CHECKIN, CHECKOUT, 4, 2)
        self.assertTrue(result)
        self.assertEqual(result[0]["total_price"], 1220.82)
        self.assertEqual(result[0]["per_night"], 406.94)

    def test_multiroom_fallback_sums_rooms_without_mixing_currencies(self):
        body = fixture("multiroom_rates")
        identifier = body["data"][0]["hotelId"]
        offer = body["data"][0]["roomTypes"][0]
        offer.pop("offerRetailRate")
        body["data"][0]["roomTypes"] = [offer]
        self.request.return_value = body
        result = hotels.get_room_offers(identifier, CHECKIN, CHECKOUT, 4, 2)
        self.assertEqual(result[0]["total_price"], 1220.82)
        offer["rates"][0]["retailRate"]["total"][0]["currency"] = "EUR"
        self.assertEqual(hotels.get_room_offers(identifier, CHECKIN, CHECKOUT, 4, 2), [])

    def test_incomplete_room_occupancies_are_not_quoted(self):
        body = fixture("rates")
        self.request.return_value = body
        self.assertEqual(hotels.get_room_offers(body["data"][0]["hotelId"], CHECKIN, CHECKOUT, 4, 2), [])

    def test_refundability_is_conservative_for_multiple_rooms(self):
        body = fixture("multiroom_rates")
        body["data"][0]["roomTypes"] = body["data"][0]["roomTypes"][:1]
        rates = body["data"][0]["roomTypes"][0]["rates"]
        self.request.return_value = body
        identifier = body["data"][0]["hotelId"]
        for tags, expected in [(["RFN", "RFN"], True), (["RFN", "NRFN"], False), (["RFN", None], None)]:
            for rate, tag in zip(rates, tags):
                rate["cancellationPolicies"]["refundableTag"] = tag
            with self.subTest(tags=tags):
                self.assertIs(hotels.get_room_offers(identifier, CHECKIN, CHECKOUT, 4, 2)[0]["refundable"], expected)

    def test_review_summary_uses_sample_and_does_not_expose_authors(self):
        self.request.return_value = fixture("reviews")
        result = hotels.get_review_summary("hotel", 5)
        self.assertEqual(result["average_score"], 8.6)
        self.assertEqual(result["review_count"], 5)
        self.assertEqual(result["total_reviews"], 1185)
        self.assertEqual(result["pros"][0], {"text": "helpful staff", "count": 3})
        self.assertEqual(result["cons"][0], {"text": "small room", "count": 2})
        self.assertEqual(len(result["headlines"]), 5)
        self.assertNotIn("name", result)

    def test_empty_reviews_leave_average_unknown(self):
        self.request.return_value = {"data": [], "total": 0}
        result = hotels.get_review_summary("hotel")
        self.assertIsNone(result["average_score"])
        self.assertEqual(result["pros"], [])
        self.assertEqual(result["headlines"], [])

    def test_combined_search_filters_budget_and_unpriced_hotels(self):
        candidates = [{"hotel_id": "cheap", "name": "Near Hotel", "distance_km": 1},
                      {"hotel_id": "expensive"}, {"hotel_id": "unpriced"}]
        with patch.object(hotels, "get_city_location", return_value={"latitude": 39.7, "longitude": -105}) as city, \
             patch.object(hotels, "search_hotels_near", return_value=candidates) as search, \
             patch.object(hotels, "get_cheapest_rates", return_value=[
                 {"hotel_id": "cheap", "total_price": 300, "per_night": 100, "sandbox": True},
                 {"hotel_id": "expensive", "total_price": 600}]) as rates:
            result = hotels.search_hotels_with_prices("Denver", CHECKIN, CHECKOUT, 3, 2, max_total=300)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "Near Hotel")
        self.assertEqual(result[0]["total_price"], 300)
        self.assertTrue(result[0]["sandbox"])
        self.assertEqual(rates.call_args.args[-2:], (3, 2))
        self.assertEqual(search.call_args.kwargs["radius_m"], 5000)

    def test_unknown_city_or_empty_candidates_short_circuit(self):
        with patch.object(hotels, "get_city_location", return_value=None), \
             patch.object(hotels, "search_hotels_near") as search:
            self.assertEqual(hotels.search_hotels_with_prices("Unknown", CHECKIN, CHECKOUT, 2), [])
        search.assert_not_called()
        with patch.object(hotels, "get_city_location", return_value={"latitude": 0, "longitude": 0}), \
             patch.object(hotels, "search_hotels_near", return_value=[]), \
             patch.object(hotels, "get_cheapest_rates") as rates:
            self.assertEqual(hotels.search_hotels_with_prices("Unknown", CHECKIN, CHECKOUT, 2), [])
        rates.assert_not_called()

    def test_negative_or_nonfinite_budget_fails_before_api(self):
        for budget in [-1, float("nan"), float("inf")]:
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                hotels.search_hotels_with_prices("Denver", CHECKIN, CHECKOUT, 2, max_total=budget)
        self.request.assert_not_called()

    def test_malformed_provider_array_is_not_silently_no_results(self):
        self.request.return_value = {"data": {"unexpected": "object"}}
        with self.assertRaises(hotels.LiteAPIError):
            hotels.search_hotels_near(0, 0)

    def test_api_failure_propagates(self):
        self.request.side_effect = hotels.LiteAPIError("API failure")
        with self.assertRaises(hotels.LiteAPIError):
            hotels.get_city_location("Denver")


if __name__ == "__main__":
    unittest.main()
