import unittest

from planning_fixtures import PLACE, TRIP, assert_contract, state
from services.agent.nodes.flight_trends import flight_trends
from services.agent.state import FlightTrends


class FlightTrendsTests(unittest.TestCase):
    """The flight trends node: the stub's contract and skip rules.

    The real comparison (alternative airports, chart, candidate dates) adds its tests here.
    """

    def run_node(self, constraints):
        result = flight_trends(state(constraints))["flight_trends"]
        assert_contract(self, result, FlightTrends)
        return result

    def test_flying_trip_returns_a_contract_result(self):
        self.assertEqual(self.run_node(TRIP)["status"], "empty")

    def test_driving_trip_is_skipped(self):
        result = self.run_node({**TRIP, "mode": "drive"})
        self.assertEqual((result["status"], result["reason"]), ("skipped", "not a flying trip"))

    def test_unresolved_place_is_skipped(self):
        for field in ("origin_place", "destination_place"):
            with self.subTest(field=field):
                result = self.run_node({**TRIP, field: None})
                self.assertEqual(result["status"], "skipped")
                self.assertIn("not resolved", result["reason"])

    def test_one_way_trip_still_runs(self):
        self.assertEqual(self.run_node({**TRIP, "trip_type": "one_way", "return_date": None})["status"], "empty")


if __name__ == "__main__":
    unittest.main()
