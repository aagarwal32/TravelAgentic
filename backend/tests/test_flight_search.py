import unittest

from planning_fixtures import TRIP, assert_contract, state
from services.agent.nodes.flight_search import flight_search
from services.agent.state import FlightOffers


class FlightSearchTests(unittest.TestCase):
    """The flight search node: the stub's contract and skip rules.

    The real search (guarded Duffel client, metrics, shortlist) adds its tests here,
    with the Duffel client mocked: tests never call the real API.
    """

    def run_node(self, constraints, **results):
        result = flight_search(state(constraints, **results))["flight_offers"]
        assert_contract(self, result, FlightOffers)
        return result

    def test_flying_trip_returns_a_contract_result(self):
        self.assertEqual(self.run_node(TRIP)["status"], "empty")

    def test_driving_trip_is_skipped(self):
        result = self.run_node({**TRIP, "mode": "drive"})
        self.assertEqual((result["status"], result["reason"]), ("skipped", "not a flying trip"))

    def test_unresolved_place_is_skipped(self):
        self.assertEqual(self.run_node({**TRIP, "destination_place": None})["status"], "skipped")

    def test_runs_without_a_trends_result(self):
        # The fallback rule: a missing, skipped or empty trends result never stops the search
        for trends in (None, {"status": "skipped", "reason": "x"}, {"status": "empty", "reason": "x"}):
            with self.subTest(trends=trends):
                results = {} if trends is None else {"flight_trends": trends}
                self.assertEqual(self.run_node(TRIP, **results)["status"], "empty")


if __name__ == "__main__":
    unittest.main()
