"""Tests for the historical airfare trends service (DOT Table 6).

They run against an in-memory SQLite database filled with a few hand-made rows,
never the real dataset or database.db. Run from the repo root:
    PYTHONPATH=backend backend/.venv/bin/python -m unittest discover -s backend/tests -v
"""

import os
import unittest
from datetime import date
from itertools import count

# core.config requires these settings when db.database is imported; give test
# defaults so the tests work without a .env file.
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from db.database import Base
from models.airfare import AirfareQuarter
from services.fare_trends_service import (
    FARE_TYPE,
    SOURCE,
    _route_filter,
    find_city_market,
    get_carrier_baseline,
    get_cheapest_quarters,
    get_price_trend,
    quarters_in_window,
)

# (DOT city market id, DOT market name)
CHICAGO = (30977, "Chicago, IL")
DENVER = (30325, "Denver, CO")
PORTLAND_OR = (34057, "Portland, OR")
PORTLAND_ME = (34321, "Portland, ME")
MIAMI = (32467, "Miami, FL (Metropolitan Area)")
TAMPA = (33195, "Tampa, FL (Metropolitan Area)")

_tbl6pk = count(1)


def fare_row(city1, city2, year, quarter, fare, **changes) -> AirfareQuarter:
    """One Table 6 row with plausible carrier values, with optional overrides."""
    values = {
        "tbl6pk": next(_tbl6pk), "year": year, "quarter": quarter,
        "citymarketid_1": city1[0], "citymarketid_2": city2[0], "city1": city1[1], "city2": city2[1],
        "nsmiles": 900, "passengers": 500.0, "fare": fare,
        "carrier_lg": "UA", "large_ms": 0.45, "fare_lg": fare + 10,
        "carrier_low": "WN", "lf_ms": 0.30, "fare_low": fare - 30,
    }
    values.update(changes)
    return AirfareQuarter(**values)


def sample_rows() -> list[AirfareQuarter]:
    return [
        # Chicago-Denver, stored in BOTH orientations (either city can be city1).
        fare_row(CHICAGO, DENVER, 2023, 1, 180.0, fare_low=150.0),
        fare_row(DENVER, CHICAGO, 2024, 1, 200.0, fare_low=170.0),
        fare_row(CHICAGO, DENVER, 2023, 2, 220.0, fare_low=190.0),
        fare_row(DENVER, CHICAGO, 2024, 2, 241.0, carrier_low=None, lf_ms=None, fare_low=None),
        fare_row(CHICAGO, DENVER, 2023, 3, 150.0, fare_low=120.0),
        fare_row(CHICAGO, DENVER, 2024, 4, 260.0, carrier_lg="UA", large_ms=0.50, fare_lg=270.0,
                 carrier_low="NK", lf_ms=0.10, fare_low=210.0),
        # Newest quarter of the route: 2025 Q1 is newer than 2024 Q4 even though 1 < 4.
        fare_row(DENVER, CHICAGO, 2025, 1, 191.0, carrier_lg="WN", large_ms=0.48, fare_lg=195.5,
                 carrier_low="F9", lf_ms=0.12, fare_low=161.0),
        # Rows that share ONE city with Chicago-Denver; they must never count for it.
        fare_row(PORTLAND_OR, DENVER, 2024, 1, 150.0, carrier_low=None, lf_ms=None, fare_low=None),
        fare_row(CHICAGO, PORTLAND_ME, 2024, 3, 400.0),
        # Tampa only appears as city2.
        fare_row(MIAMI, TAMPA, 2024, 1, 90.0),
    ]


class FareTrendsTestCase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine, tables=[AirfareQuarter.__table__])
        self.db = Session(self.engine)
        self.db.add_all(sample_rows())
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def assertNoHistory(self, result, *reason_parts):
        self.assertEqual(set(result), {"no_history", "reason"})
        self.assertIs(result["no_history"], True)
        for part in reason_parts:
            self.assertIn(part, result["reason"])


class FindCityMarketTests(FareTrendsTestCase):
    def test_case_insensitive_starts_with_match(self):
        self.assertEqual(find_city_market(self.db, "chicago"), CHICAGO[0])
        self.assertEqual(find_city_market(self.db, "DENVER"), DENVER[0])
        self.assertEqual(find_city_market(self.db, "  Chi "), CHICAGO[0])
        self.assertEqual(find_city_market(self.db, "portland, or"), PORTLAND_OR[0])
        self.assertEqual(find_city_market(self.db, "Miami, FL (Metropolitan Area)"), MIAMI[0])

    def test_city_found_in_either_column(self):
        self.assertEqual(find_city_market(self.db, "Tampa"), TAMPA[0])  # only ever city2

    def test_unknown_city_returns_none(self):
        for name in ("Atlantis", "", "   ", "Chicago, OH", "_hicago", "%"):
            with self.subTest(name=name):
                self.assertIsNone(find_city_market(self.db, name))

    def test_ambiguous_city_returns_none(self):
        self.assertIsNone(find_city_market(self.db, "Portland"))
        self.assertIsNone(find_city_market(self.db, "p"))


class RouteFilterTests(FareTrendsTestCase):
    def count_route(self, a, b) -> int:
        return self.db.scalar(select(func.count()).select_from(AirfareQuarter).where(_route_filter(a, b)))

    def test_matches_both_orientations_and_nothing_else(self):
        self.assertEqual(self.count_route(CHICAGO[0], DENVER[0]), 7)
        self.assertEqual(self.count_route(DENVER[0], CHICAGO[0]), 7)
        self.assertEqual(self.count_route(CHICAGO[0], MIAMI[0]), 0)


class PriceTrendTests(FareTrendsTestCase):
    def test_quarterly_aggregates(self):
        result = get_price_trend(self.db, "Chicago", "Denver")
        self.assertEqual(result["origin"], "Chicago, IL")
        self.assertEqual(result["destination"], "Denver, CO")
        self.assertEqual(result["source"], SOURCE)
        self.assertEqual(result["fare_type"], FARE_TYPE)
        self.assertEqual(result["avg_fare"], 206.0)  # (180+200+191+220+241+150+260) / 7
        self.assertEqual(result["years_covered"], [2023, 2024, 2025])
        self.assertEqual(result["newest_quarter"], {"year": 2025, "quarter": 1})
        self.assertEqual(result["by_quarter"], [
            # Q1: (180+200+191)/3 = 190.333..., fare_low (150+170+161)/3 = 160.333...
            {"quarter": 1, "months": "Jan-Mar", "avg_fare": 190.33, "min_fare": 180.0, "max_fare": 200.0,
             "avg_lowest_carrier_fare": 160.33, "data_points": 3},
            # Q2: the 2024 row has no lowest-fare carrier, so its average uses the 2023 row only.
            {"quarter": 2, "months": "Apr-Jun", "avg_fare": 230.5, "min_fare": 220.0, "max_fare": 241.0,
             "avg_lowest_carrier_fare": 190.0, "data_points": 2},
            {"quarter": 3, "months": "Jul-Sep", "avg_fare": 150.0, "min_fare": 150.0, "max_fare": 150.0,
             "avg_lowest_carrier_fare": 120.0, "data_points": 1},
            {"quarter": 4, "months": "Oct-Dec", "avg_fare": 260.0, "min_fare": 260.0, "max_fare": 260.0,
             "avg_lowest_carrier_fare": 210.0, "data_points": 1},
        ])

    def test_route_is_the_same_in_both_directions(self):
        there = get_price_trend(self.db, "Chicago", "Denver")
        back = get_price_trend(self.db, "denver", "chicago")
        self.assertEqual((back["origin"], back["destination"]), ("Denver, CO", "Chicago, IL"))
        for key in ("avg_fare", "years_covered", "newest_quarter", "by_quarter"):
            self.assertEqual(back[key], there[key])

    def test_quarters_without_data_are_left_out(self):
        result = get_price_trend(self.db, "Portland, OR", "Denver")
        self.assertEqual([q["quarter"] for q in result["by_quarter"]], [1])
        self.assertIsNone(result["by_quarter"][0]["avg_lowest_carrier_fare"])


class CheapestQuartersTests(FareTrendsTestCase):
    def test_ranked_cheapest_first(self):
        result = get_cheapest_quarters(self.db, "Chicago", "Denver")
        self.assertEqual(result["source"], SOURCE)
        self.assertEqual(result["fare_type"], FARE_TYPE)
        self.assertEqual(result["ranking"], [
            {"quarter": 3, "months": "Jul-Sep", "avg_fare": 150.0, "data_points": 1},
            {"quarter": 1, "months": "Jan-Mar", "avg_fare": 190.33, "data_points": 3},
            {"quarter": 2, "months": "Apr-Jun", "avg_fare": 230.5, "data_points": 2},
            {"quarter": 4, "months": "Oct-Dec", "avg_fare": 260.0, "data_points": 1},
        ])

    def test_quarter_filter(self):
        result = get_cheapest_quarters(self.db, "Denver", "Chicago", quarters=[4, 2])
        self.assertEqual([q["quarter"] for q in result["ranking"]], [2, 4])
        result = get_cheapest_quarters(self.db, "Chicago", "Denver", quarters=quarters_in_window(
            date(2026, 2, 10), date(2026, 5, 5)))
        self.assertEqual([(q["quarter"], q["avg_fare"]) for q in result["ranking"]], [(1, 190.33), (2, 230.5)])

    def test_filter_with_no_data_returns_no_history(self):
        result = get_cheapest_quarters(self.db, "Portland, OR", "Denver", quarters=[3])
        self.assertNoHistory(result, "Portland, OR", "Denver, CO", "[3]")


class CarrierBaselineTests(FareTrendsTestCase):
    def test_uses_newest_year_and_quarter(self):
        result = get_carrier_baseline(self.db, "Chicago", "Denver")
        self.assertEqual(result["source"], SOURCE)
        self.assertEqual(result["fare_type"], FARE_TYPE)
        # 2025 Q1, not 2024 Q4 (bigger quarter number) and not the stored orientation's order.
        self.assertEqual((result["year"], result["quarter"], result["months"]), (2025, 1, "Jan-Mar"))
        self.assertEqual(result["largest_carrier"], {"airline": "WN", "market_share": 0.48, "avg_fare": 195.5})
        self.assertEqual(result["lowest_fare_carrier"], {"airline": "F9", "market_share": 0.12, "avg_fare": 161.0})
        self.assertEqual(get_carrier_baseline(self.db, "Denver", "Chicago")["largest_carrier"],
                         result["largest_carrier"])

    def test_missing_carrier_values_are_none(self):
        result = get_carrier_baseline(self.db, "Portland, OR", "Denver")
        self.assertEqual(result["largest_carrier"], {"airline": "UA", "market_share": 0.45, "avg_fare": 160.0})
        self.assertEqual(result["lowest_fare_carrier"], {"airline": None, "market_share": None, "avg_fare": None})


class NoHistoryTests(FareTrendsTestCase):
    FUNCTIONS = (get_price_trend, get_cheapest_quarters, get_carrier_baseline)

    def test_known_cities_without_a_route(self):
        for function in self.FUNCTIONS:
            with self.subTest(function=function.__name__):
                self.assertNoHistory(function(self.db, "Chicago", "Miami"), "Chicago, IL", "Miami, FL")

    def test_unknown_city(self):
        for function in self.FUNCTIONS:
            with self.subTest(function=function.__name__):
                self.assertNoHistory(function(self.db, "Chicago", "Atlantis"), "Atlantis")
                self.assertNoHistory(function(self.db, "", "Denver"))

    def test_ambiguous_city_is_not_guessed(self):
        for function in self.FUNCTIONS:
            with self.subTest(function=function.__name__):
                self.assertNoHistory(function(self.db, "Portland", "Denver"),
                                     "Portland", "Portland, ME", "Portland, OR")

    def test_empty_table(self):
        self.db.query(AirfareQuarter).delete()
        self.db.commit()
        for function in self.FUNCTIONS:
            with self.subTest(function=function.__name__):
                self.assertNoHistory(function(self.db, "Chicago", "Denver"))


class QuartersInWindowTests(unittest.TestCase):
    def test_window_inside_one_quarter(self):
        self.assertEqual(quarters_in_window(date(2026, 7, 4), date(2026, 7, 4)), [3])
        self.assertEqual(quarters_in_window(date(2026, 1, 1), date(2026, 3, 31)), [1])

    def test_window_spanning_several_quarters(self):
        self.assertEqual(quarters_in_window(date(2026, 2, 10), date(2026, 5, 5)), [1, 2])
        self.assertEqual(quarters_in_window(date(2026, 3, 31), date(2026, 10, 1)), [1, 2, 3, 4])

    def test_window_crossing_new_year(self):
        self.assertEqual(quarters_in_window(date(2026, 11, 20), date(2027, 1, 5)), [4, 1])
        self.assertEqual(quarters_in_window(date(2026, 8, 1), date(2027, 2, 1)), [3, 4, 1])

    def test_long_window_touches_every_quarter_once(self):
        self.assertEqual(quarters_in_window(date(2026, 5, 1), date(2028, 5, 1)), [2, 3, 4, 1])

    def test_end_before_start_raises(self):
        with self.assertRaises(ValueError):
            quarters_in_window(date(2026, 5, 5), date(2026, 2, 10))


if __name__ == "__main__":
    unittest.main()
