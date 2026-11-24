from copy import deepcopy
from datetime import date, datetime, timezone
import os
import unittest
from unittest.mock import call, patch

from planning_fixtures import TRIP as BASE_TRIP, WINDOW_TRIP as BASE_WINDOW_TRIP, assert_contract, state

with patch.dict(os.environ, {
    "SECRET_KEY": "test-only", "DATABASE_URL": "sqlite://", "DEBUG": "false",
}):
    from services.agent.nodes.flight_search import MAX_SEARCHES_PER_TRIP, flight_search
    from services.agent.nodes.hotel_search import hotel_search
    from services.agent.state import FlightOffers
    from services.duffel_client import DuffelError, DuffelSearchBlocked
    from services.flight_service import _parse_offer, _search_offers


TRIP = {**BASE_TRIP,
        "origin_place": {**BASE_TRIP["origin_place"], "airports": ["ORD"]},
        "destination_place": {**BASE_TRIP["destination_place"], "airports": ["JFK"]}}
WINDOW_TRIP = {**BASE_WINDOW_TRIP,
               "origin_place": TRIP["origin_place"], "destination_place": TRIP["destination_place"]}


class FlightSearchTests(unittest.TestCase):
    """Offline orchestration: input selection, guarded searches, and result contracts."""

    def setUp(self):
        self.now = datetime(2099, 2, 1, 12, tzinfo=timezone.utc)
        clock = patch("services.agent.nodes.flight_search.datetime", wraps=datetime)
        self.clock = clock.start()
        self.clock.now.return_value = self.now
        self.addCleanup(clock.stop)
        search = patch("services.agent.nodes.flight_search._search_offers", return_value=[])
        self.search = search.start()
        self.addCleanup(search.stop)
        outbound = {
            "duration": "PT2H10M", "segments": [{
                "origin": {"iata_code": "ORD", "time_zone": "America/Chicago"},
                "destination": {"iata_code": "JFK", "time_zone": "America/New_York"},
                "departing_at": "2099-03-10T08:00:00", "arriving_at": "2099-03-10T11:10:00",
                "operating_carrier": {"name": "American Airlines"},
            }],
        }
        inbound = deepcopy(outbound)
        inbound["segments"][0].update(
            origin={"iata_code": "JFK", "time_zone": "America/New_York"},
            destination={"iata_code": "ORD", "time_zone": "America/Chicago"},
            departing_at="2099-03-14T08:00:00", arriving_at="2099-03-14T09:10:00",
        )
        self.raw_offer = {
            "id": "off_example", "total_amount": "420.00", "total_currency": "USD",
            "expires_at": "2099-02-01T12:30:00Z", "slices": [outbound, inbound],
        }
        self.offer = _parse_offer(self.raw_offer, 3, now=self.now)

    def run_node(self, constraints, **results):
        result = flight_search(state(constraints, **results))["flight_offers"]
        assert_contract(self, result, FlightOffers)
        return result

    def test_flying_trip_returns_a_contract_result(self):
        result = self.run_node(TRIP)
        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["searches_made"], 1)
        self.assertEqual(result["shortlist"], [])
        self.assertIsNone(result["cheapest_total"])
        self.assertEqual(result["fetched_at"], "2099-02-01T12:00:00+00:00")

    def test_driving_trip_is_skipped(self):
        result = self.run_node({**TRIP, "mode": "drive"})
        self.assertEqual((result["status"], result["reason"]), ("skipped", "not a flying trip"))
        self.search.assert_not_called()

    def test_unresolved_place_is_skipped(self):
        self.assertEqual(self.run_node({**TRIP, "destination_place": None})["status"], "skipped")
        self.search.assert_not_called()

    def test_runs_without_a_trends_result(self):
        # The fallback rule: a missing, skipped or empty trends result never stops the search
        for trends in (None, {"status": "skipped", "reason": "x"}, {"status": "empty", "reason": "x"}):
            with self.subTest(trends=trends):
                self.search.reset_mock()
                results = {} if trends is None else {"flight_trends": trends}
                self.assertEqual(self.run_node(TRIP, **results)["status"], "empty")
                self.search.assert_called_once_with(
                    "ORD", "JFK", date(2099, 3, 10), 3, return_date=date(2099, 3, 14),
                    max_connections=None, from_alternative_airport=False,
                )

    def test_success_returns_real_shortlist_and_cheapest_total(self):
        self.search.return_value = [self.offer]
        result = self.run_node({**TRIP, "max_connections": 0})
        self.assertEqual(result["status"], "ok")
        self.assertNotIn("reason", result)
        self.assertEqual(result["cheapest_total"], 420.0)
        self.assertEqual(result["shortlist"][0]["per_person"], 140.0)
        self.assertEqual(result["shortlist"][0]["tags"], ["cheapest", "fastest", "best_balance"])
        self.assertEqual(self.search.call_args.kwargs["max_connections"], 0)

    def test_trends_airports_are_deduplicated_and_alternatives_are_marked(self):
        trends = {"status": "ok", "search_airports": {
            "origin": [" ord ", "ORD", "MDW", "MKE"], "destination": ["JFK", "EWR", "EWR"],
        }, "options": [{"side": "departure", "airports": ["MKE"]},
                       {"side": "arrival", "airports": ["EWR"]}]}
        result = self.run_node(TRIP, flight_trends=trends)
        self.assertEqual(result["searches_made"], 6)
        pairs = {(item.args[0], item.args[1]): item.kwargs["from_alternative_airport"]
                 for item in self.search.call_args_list}
        self.assertEqual(pairs, {("ORD", "JFK"): False, ("ORD", "EWR"): True,
                                 ("MDW", "JFK"): False, ("MDW", "EWR"): True,
                                 ("MKE", "JFK"): True, ("MKE", "EWR"): True})

    def test_per_trip_cap_keeps_home_dates_first_and_preserves_an_alternative(self):
        trends = {"status": "ok", "search_airports": {
            "origin": ["ORD", "MKE"], "destination": ["JFK", "EWR"],
        }, "options": [{"side": "departure", "airports": ["MKE"]},
                       {"side": "arrival", "airports": ["EWR"]}]}
        for offers, expected_status in (([], "empty"), ([self.offer], "ok")):
            with self.subTest(status=expected_status):
                self.search.reset_mock()
                self.search.return_value = offers
                result = self.run_node(WINDOW_TRIP, flight_trends=trends)
                self.assertEqual(result["status"], expected_status)
                self.assertEqual(result["searches_made"], MAX_SEARCHES_PER_TRIP)
                self.assertEqual(self.search.call_count, 6)
                self.assertEqual(result["reason"], "per-trip search limit reached")
                tried = [item.args[:3] for item in self.search.call_args_list]
                self.assertEqual(tried[:3], [
                    ("ORD", "JFK", date(2099, 3, 3)),
                    ("ORD", "JFK", date(2099, 3, 17)),
                    ("ORD", "JFK", date(2099, 3, 31)),
                ])
                if offers:
                    self.assertEqual(tried[3:], [
                        ("ORD", "JFK", date(2099, 3, 2)),
                        ("ORD", "JFK", date(2099, 3, 4)),
                        ("ORD", "EWR", date(2099, 3, 3)),
                    ])
                    self.assertEqual(result["cheapest_total"], 420.0)
                    self.assertEqual(result["shortlist"][0]["offer_id"], "off_example")
                else:
                    self.assertEqual(tried[3:], [
                        ("ORD", "EWR", date(2099, 3, 3)),
                        ("MKE", "JFK", date(2099, 3, 3)),
                        ("MKE", "EWR", date(2099, 3, 3)),
                    ])

    def test_fallback_skips_airports_without_a_dot_market(self):
        trip = {**TRIP, "origin_place": {**TRIP["origin_place"], "airports": ["FTW", "DFW"],
                                        "flight_airports": ["DFW"]}}
        self.run_node(trip)
        self.search.assert_called_once()
        self.assertEqual(self.search.call_args.args[:2], ("DFW", "JFK"))

    def test_flexible_followups_use_cheapest_probe_and_preserve_stay_length(self):
        cheaper = {**self.offer, "offer_id": "off_cheaper", "total_amount": 210.0}
        self.search.side_effect = [[self.offer], [cheaper], [], [], [], []]
        result = self.run_node(WINDOW_TRIP)
        self.assertEqual(result["cheapest_total"], 210.0)
        followups = self.search.call_args_list[3:]
        self.assertEqual([item.args[2].day for item in followups], [16, 18, 15])
        self.assertTrue(all((item.kwargs["return_date"] - item.args[2]).days == 2 for item in followups))

    def test_only_trends_airports_are_searched(self):
        # Nearby airports that trends left out (e.g. a near miss like DEN) are never added
        trip = {**TRIP, "origin_place": {**TRIP["origin_place"], "airports": ["ORD", "MDW"],
                                        "flight_airports": ["ORD", "MDW"]}}
        result = self.run_node(trip, flight_trends={"status": "ok", "search_airports": {
            "origin": ["ORD"], "destination": ["JFK"]}, "options": []})
        self.assertEqual(result["searches_made"], 1)
        self.search.assert_called_once()
        self.assertEqual(self.search.call_args.args[:2], ("ORD", "JFK"))
        self.assertFalse(self.search.call_args.kwargs["from_alternative_airport"])

    def test_fallback_without_trends_searches_only_each_places_own_airport(self):
        trip = {**WINDOW_TRIP,
                "origin_place": {**TRIP["origin_place"], "airports": ["ORD", "MDW", "MKE"],
                                 "flight_airports": ["ORD", "MDW", "MKE"]},
                "destination_place": {**TRIP["destination_place"], "airports": ["JFK", "LGA", "EWR"],
                                      "flight_airports": ["JFK", "LGA", "EWR"]}}
        result = self.run_node(trip, flight_trends={"status": "empty", "reason": "no data"})
        self.assertEqual(result["searches_made"], 3)  # the 3 date probes, no follow-ups without fares
        self.assertEqual({item.args[:2] for item in self.search.call_args_list}, {("ORD", "JFK")})
        self.assertTrue(all(not item.kwargs["from_alternative_airport"]
                            for item in self.search.call_args_list))

    def test_fixed_dates_try_trends_alternative_before_home_pairs_fill_budget(self):
        trends = {"status": "ok", "search_airports": {
            "origin": ["ORD", "MDW", "MSN"], "destination": ["JFK", "LGA", "EWR"],
        }, "options": [{"side": "departure", "airports": ["MSN"]}]}
        result = self.run_node(TRIP, flight_trends=trends)
        self.assertEqual(result["searches_made"], MAX_SEARCHES_PER_TRIP)
        self.assertEqual([item.args[:2] for item in self.search.call_args_list[:2]],
                         [("ORD", "JFK"), ("MSN", "JFK")])

    def test_failed_sixth_attempt_counts_and_stops_without_a_seventh_call(self):
        self.search.side_effect = [[] for _ in range(5)] + [DuffelSearchBlocked("Duffel daily search limit reached")]
        trends = {"status": "ok", "search_airports": {
            "origin": ["ORD", "MDW", "MKE"], "destination": ["JFK", "LGA", "EWR"],
        }}
        result = self.run_node(TRIP, flight_trends=trends)
        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["searches_made"], 6)
        self.assertEqual(self.search.call_count, 6)
        self.assertIn("daily search limit", result["reason"])

    def test_duplicates_and_same_airport_pairs_do_not_consume_search_budget(self):
        trends = {"status": "ok", "search_airports": {
            "origin": ["ORD", "ORD", "MDW", "JFK"], "destination": ["ORD", "JFK", "EWR", "EWR"]}}
        result = self.run_node(TRIP, flight_trends=trends)
        pairs = [(item.args[0], item.args[1]) for item in self.search.call_args_list]
        self.assertEqual(result["searches_made"], 6)
        self.assertEqual(len(pairs), 6)
        self.assertEqual(len(set(pairs)), 6)
        self.assertTrue(all(origin != destination for origin, destination in pairs))

    def test_trends_candidate_dates_seed_calendar_probes_and_respect_stay_length(self):
        trends = {"status": "ok", "candidate_dates": [
            {"depart_date": "2099-03-04", "return_date": "2099-03-06"},
            {"depart_date": "2099-03-04", "return_date": "2099-03-06"},
            {"depart_date": "2099-03-18", "return_date": "2099-03-20"},
            {"depart_date": "2099-03-28", "return_date": "2099-03-30"},
        ]}
        result = self.run_node(WINDOW_TRIP, flight_trends=trends)
        self.assertEqual(result["searches_made"], 3)
        self.assertEqual(self.search.call_args_list, [
            call("ORD", "JFK", date(2099, 3, 4), 3, return_date=date(2099, 3, 6),
                 max_connections=None, from_alternative_airport=False),
            call("ORD", "JFK", date(2099, 3, 18), 3, return_date=date(2099, 3, 20),
                 max_connections=None, from_alternative_airport=False),
            call("ORD", "JFK", date(2099, 3, 28), 3, return_date=date(2099, 3, 30),
                 max_connections=None, from_alternative_airport=False),
        ])

    def test_window_fallback_spreads_preferred_weekdays_with_nights(self):
        self.run_node(WINDOW_TRIP)
        self.assertEqual([(item.args[2], item.kwargs["return_date"]) for item in self.search.call_args_list], [
            (date(2099, 3, 3), date(2099, 3, 5)),
            (date(2099, 3, 17), date(2099, 3, 19)),
            (date(2099, 3, 31), date(2099, 4, 2)),
        ])

    def test_window_fixed_return_is_preserved_and_bounds_departures(self):
        trip = {**WINDOW_TRIP, "trip_nights": None, "return_date": "2099-03-15"}
        self.run_node(trip)
        self.assertEqual([item.args[2] for item in self.search.call_args_list],
                         [date(2099, 3, 3), date(2099, 3, 7), date(2099, 3, 14)])
        self.assertTrue(all(item.kwargs["return_date"] == date(2099, 3, 15)
                            for item in self.search.call_args_list))

    def test_window_return_and_nights_use_only_the_implied_departure(self):
        trip = {**WINDOW_TRIP, "return_date": "2099-03-15"}
        self.run_node(trip, flight_trends={"status": "ok", "candidate_dates": [
            {"depart_date": "2099-03-05", "return_date": "2099-03-07"},
        ]})
        self.search.assert_called_once_with(
            "ORD", "JFK", date(2099, 3, 13), 3, return_date=date(2099, 3, 15),
            max_connections=None, from_alternative_airport=False,
        )

    def test_invalid_or_conflicting_trend_dates_fall_back_to_trip_dates(self):
        for candidate in (None, {}, {"depart_date": "not-a-date"},
                          {"depart_date": "2099-03-11", "return_date": "2099-03-14"},
                          {"depart_date": "2099-03-10", "return_date": "2099-03-15"}):
            with self.subTest(candidate=candidate):
                self.search.reset_mock()
                self.run_node(TRIP, flight_trends={"status": "ok", "candidate_dates": [candidate]})
                self.assertEqual(self.search.call_args.args[2], date(2099, 3, 10))
                self.assertEqual(self.search.call_args.kwargs["return_date"], date(2099, 3, 14))
                self.search.assert_called_once()

    def test_invalid_trend_dates_do_not_override_calendar_probes_inside_original_window(self):
        self.run_node(WINDOW_TRIP, flight_trends={"status": "ok", "candidate_dates": [
            {"depart_date": "2099-02-28", "return_date": "2099-03-02"},
            {"depart_date": "2099-03-01", "return_date": "2099-03-04"},  # Wrong nights.
            *[{"depart_date": f"2099-03-{day:02d}", "return_date": f"2099-03-{day + 2:02d}"}
              for day in (4, 8, 12, 16)],
        ]})
        self.assertEqual([item.args[2] for item in self.search.call_args_list],
                         [date(2099, 3, 4), date(2099, 3, 17), date(2099, 3, 31)])

    def test_one_way_does_not_search_return_even_when_nights_are_for_hotels(self):
        self.run_node({**TRIP, "trip_type": "one_way", "return_date": None, "trip_nights": 3})
        self.search.assert_called_once_with(
            "ORD", "JFK", date(2099, 3, 10), 3, return_date=None,
            max_connections=None, from_alternative_airport=False,
        )

    def test_missing_search_details_do_not_make_requests(self):
        for fields in ({"travelers": None}, {"trip_type": None}, {"depart_date": None},
                       {"return_date": None, "trip_nights": None},
                       {"origin_place": {**TRIP["origin_place"], "airports": []}}):
            with self.subTest(fields=fields):
                result = self.run_node({**TRIP, **fields})
                self.assertEqual(result["status"], "empty")
                self.assertEqual(result["searches_made"], 0)
        self.search.assert_not_called()

    def test_past_departure_returns_empty_without_a_request(self):
        result = self.run_node({**TRIP, "depart_date": "2020-03-15"})
        self.assertEqual(result["status"], "empty")
        self.search.assert_not_called()

    def test_same_airport_pairs_are_not_searched(self):
        result = self.run_node(BASE_TRIP)
        self.assertEqual(result["searches_made"], 0)
        self.assertIn("no distinct", result["reason"])
        self.search.assert_not_called()

    def test_client_error_stops_searching_and_returns_an_empty_reason(self):
        self.search.side_effect = DuffelSearchBlocked("Duffel daily search limit reached")
        result = self.run_node(WINDOW_TRIP)
        self.assertEqual(result["status"], "empty")
        self.assertIn("daily search limit", result["reason"])
        self.assertEqual(result["searches_made"], 1)
        self.search.assert_called_once()

    def test_client_error_preserves_offers_from_previous_searches(self):
        self.search.side_effect = [[self.offer], DuffelSearchBlocked("Duffel daily search limit reached")]
        result = self.run_node(WINDOW_TRIP)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["cheapest_total"], 420.0)
        self.assertEqual(result["searches_made"], 2)
        self.assertIn("stopped early", result["reason"])
        self.assertEqual(self.search.call_count, 2)

    def test_recoverable_error_allows_later_route_to_supply_offers(self):
        self.search.side_effect = [DuffelError("Duffel request timed out"), [self.offer]]
        trends = {"status": "ok", "search_airports": {"origin": ["ORD", "MDW"]}}
        result = self.run_node(TRIP, flight_trends=trends)
        self.assertEqual((result["status"], result["searches_made"]), ("ok", 2))
        self.assertIn("1 flight search attempt(s) failed", result["reason"])

    def test_each_search_is_recorded_with_its_offers_or_error(self):
        self.search.side_effect = [DuffelError("Duffel request timed out"), [self.offer]]
        trends = {"status": "ok", "search_airports": {"origin": ["ORD", "MDW"]}}
        result = self.run_node(TRIP, flight_trends=trends)
        self.assertEqual(result["searches"], [
            {"origin": "ORD", "destination": "JFK", "depart_date": "2099-03-10",
             "return_date": "2099-03-14", "offers": 0, "cheapest_total": None,
             "error": "Duffel request timed out"},
            {"origin": "MDW", "destination": "JFK", "depart_date": "2099-03-10",
             "return_date": "2099-03-14", "offers": 1, "cheapest_total": 420.0, "error": None},
        ])

    def test_all_recoverable_failures_return_empty(self):
        self.search.side_effect = DuffelError("Duffel request timed out")
        result = self.run_node(WINDOW_TRIP)
        self.assertEqual((result["status"], result["searches_made"]), ("empty", 3))
        self.assertIn("timed out", result["reason"])

    def test_search_results_are_combined_deduplicated_and_filtered_before_cheapest_total(self):
        offers = []
        for index in range(30):  # different flights (departure minute), so none are fare duplicates
            offer = deepcopy(self.offer)
            offer.update(offer_id=f"off_{index:02d}", total_amount=600.0 - index)
            offer["outbound"]["depart_at"] = f"2099-03-10T08:{index:02d}:00"
            offers.append(offer)
        offers[-1]["total_amount"] = 50.0
        expired = {**self.offer, "offer_id": "off_expired", "total_amount": 1.0,
                   "expires_at": self.now.isoformat()}
        self.search.side_effect = [offers[:15], [offers[0], expired] + offers[15:]]
        trends = {"status": "ok", "search_airports": {"origin": ["ORD", "MDW"]}}
        result = self.run_node(TRIP, flight_trends=trends)
        self.assertEqual(result["searches_made"], 2)
        self.assertEqual(result["cheapest_total"], 50.0)
        self.assertEqual(len(result["shortlist"]), 20)
        self.assertEqual(len({offer["offer_id"] for offer in result["shortlist"]}), 20)
        self.assertNotIn("off_expired", {offer["offer_id"] for offer in result["shortlist"]})

    def test_only_flight_offers_is_written_and_inputs_are_not_mutated(self):
        self.search.return_value = [self.offer]
        incoming = state(TRIP, flight_trends={"status": "empty", "reason": "no trends"},
                         hotel_options={"status": "empty", "reason": "sentinel"})
        original, original_offer = deepcopy(incoming), deepcopy(self.offer)
        result = flight_search(incoming)
        self.assertEqual(set(result), {"flight_offers"})
        self.assertEqual(incoming, original)
        self.assertEqual(self.offer, original_offer)

    def test_duffel_response_flows_through_parsers_and_into_hotel_budget_and_dates(self):
        self.search.side_effect = _search_offers
        with patch("services.flight_service._request", return_value={"data": {"offers": [self.raw_offer]}}) as request:
            result = self.run_node(TRIP)
        request.assert_called_once()
        self.assertEqual(result["status"], "ok")
        hotels = hotel_search(state(TRIP, flight_offers=result))["hotel_options"]
        self.assertEqual((hotels["checkin"], hotels["checkout"]), ("2099-03-10", "2099-03-14"))
        self.assertEqual(hotels["hotel_budget"], 480.0)
        self.assertEqual(hotels["rooms"], 2)


if __name__ == "__main__":
    unittest.main()
