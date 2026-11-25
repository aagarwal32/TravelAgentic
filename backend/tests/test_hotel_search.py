import unittest

from planning_fixtures import TRIP, WINDOW_TRIP, assert_contract, state
from services.agent.nodes.hotel_search import hotel_search, room_count
from services.agent.state import HotelOptions


def offer(total, arrive_at, inbound_depart_at=None):
    """A shortlisted flight with only the fields hotel_search reads."""
    inbound = {"depart_at": inbound_depart_at} if inbound_depart_at else None
    return {"total_amount": total, "outbound": {"arrive_at": arrive_at}, "inbound": inbound}


def flights(*shortlist, cheapest_total=None):
    totals = [o["total_amount"] for o in shortlist]
    return {"status": "ok", "shortlist": list(shortlist),
            "cheapest_total": cheapest_total if cheapest_total is not None else min(totals)}


class HotelSearchTests(unittest.TestCase):
    """The hotel search node's inputs: stay dates, rooms and hotel budget, with every fallback.

    The real search (search_hotels_with_prices, then the picks) adds its tests here,
    with LiteAPI mocked.
    """

    def run_node(self, constraints, **results):
        result = hotel_search(state(constraints, **results))["hotel_options"]
        assert_contract(self, result, HotelOptions)
        return result

    def stay(self, result):
        return result["checkin"], result["checkout"]

    def test_exact_dates_and_full_budget_without_flights(self):
        result = self.run_node(TRIP)
        self.assertEqual(result["status"], "empty")
        self.assertEqual(self.stay(result), ("2099-03-10", "2099-03-14"))
        self.assertEqual((result["rooms"], result["hotel_budget"]), (2, 900.0))

    def test_window_uses_its_start_plus_nights(self):
        self.assertEqual(self.stay(self.run_node(WINDOW_TRIP)), ("2099-03-01", "2099-03-03"))

    def test_window_with_fixed_return_and_no_nights_keeps_checkout(self):
        trip = {**WINDOW_TRIP, "trip_nights": None, "return_date": "2099-03-15"}
        self.assertEqual(self.stay(self.run_node(trip)), ("2099-03-01", "2099-03-15"))

    def test_window_with_return_and_nights_uses_the_implied_departure(self):
        # With no concrete flights or trends, both user constraints still apply:
        # a 2-night stay returning March 15 starts March 13, not the window's start.
        for back, expected_departure in (("2099-03-03", "2099-03-01"),
                                         ("2099-03-15", "2099-03-13"),
                                         ("2099-04-02", "2099-03-31")):
            with self.subTest(return_date=back):
                trip = {**WINDOW_TRIP, "return_date": back}
                self.assertEqual(self.stay(self.run_node(trip)), (expected_departure, back))

    def test_rooms_are_one_per_two_travelers(self):
        for travelers, rooms in ((1, 1), (2, 1), (3, 2), (4, 2), (9, 5)):
            with self.subTest(travelers=travelers):
                self.assertEqual(room_count(travelers), rooms)

    def test_flights_set_the_dates_and_the_budget(self):
        # The cheapest offer decides: check in on its arrival day, out on its return day
        shortlist = [offer(520.0, "2099-03-11T09:00", "2099-03-15T17:00"),
                     offer(412.5, "2099-03-12T23:40", "2099-03-14T08:00")]
        result = self.run_node(WINDOW_TRIP, flight_offers=flights(*shortlist))
        self.assertEqual(self.stay(result), ("2099-03-12", "2099-03-14"))
        self.assertEqual(result["hotel_budget"], 487.5)

    def test_one_way_flight_uses_arrival_plus_nights(self):
        result = self.run_node({**WINDOW_TRIP, "trip_type": "one_way"},
                               flight_offers=flights(offer(150.0, "2099-03-05T14:00")))
        self.assertEqual(self.stay(result), ("2099-03-05", "2099-03-07"))

    def test_trends_dates_are_used_without_flights(self):
        trends = {"status": "ok", "candidate_dates": [{"depart_date": "2099-03-18", "return_date": "2099-03-20"}]}
        self.assertEqual(self.stay(self.run_node(WINDOW_TRIP, flight_trends=trends)), ("2099-03-18", "2099-03-20"))
        # Flights win over trends: they are the dates the user would actually travel
        result = self.run_node(WINDOW_TRIP, flight_trends=trends,
                               flight_offers=flights(offer(300.0, "2099-03-25T10:00", "2099-03-27T10:00")))
        self.assertEqual(self.stay(result), ("2099-03-25", "2099-03-27"))

    def test_empty_or_skipped_upstream_results_are_ignored(self):
        for status in ("empty", "skipped"):
            with self.subTest(status=status):
                result = self.run_node(TRIP, flight_trends={"status": status, "reason": "x"},
                                       flight_offers={"status": status, "reason": "x", "cheapest_total": 400.0})
                self.assertEqual(self.stay(result), ("2099-03-10", "2099-03-14"))
                self.assertEqual(result["hotel_budget"], 900.0)

    def test_budget_never_goes_below_zero(self):
        result = self.run_node(TRIP, flight_offers=flights(offer(1200.0, "2099-03-10T09:00", "2099-03-14T09:00")))
        self.assertEqual(result["hotel_budget"], 0.0)

    def test_driving_trip_searches_hotels(self):
        self.assertEqual(self.run_node({**TRIP, "mode": "drive"})["status"], "empty")

    def test_no_stay_length_is_skipped(self):
        one_way = {**TRIP, "trip_type": "one_way", "return_date": None}
        result = self.run_node(one_way)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("no stay length", result["reason"])
        # With nights, a one-way trip does get hotels (the rule is the stay length, not trip_type)
        self.assertEqual(self.stay(self.run_node({**one_way, "trip_nights": 3})), ("2099-03-10", "2099-03-13"))

    def test_unresolved_destination_is_skipped(self):
        result = self.run_node({**TRIP, "destination_place": None})
        self.assertEqual((result["status"], result["reason"]), ("skipped", "destination not resolved to a place"))


if __name__ == "__main__":
    unittest.main()
