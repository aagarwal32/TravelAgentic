"""Offline node integration against real SQL queries and the unchanged planning contracts."""

import copy
from datetime import date
import importlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from pydantic import ValidationError
from sqlalchemy import create_engine, delete, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from db.database import Base
from models.airfare import AirfareQuarter
from models.location import Airport
from planning_fixtures import assert_contract, state
from services.agent.nodes.flight_search import flight_search
from services.agent.nodes.hotel_search import hotel_budget, hotel_search, stay_dates
from services.agent.state import (BudgetCheck, CandidateDates, ChartSeries, FareChart,
                                  FlightOffers, FlightTrends, HotelOptions, RouteOption,
                                  RouteTrend, SearchAirports)

node = importlib.import_module("services.agent.nodes.flight_trends")

ORIGIN = {"id": "test-origin", "city": "Origin", "state": "IL", "latitude": 40,
          "longitude": -88, "airports": ["BAD", "CMI", "MDW", "CMI"], "dot_market_id": 1}
DESTINATION = {"id": "test-destination", "city": "Destination", "state": "NY",
               "latitude": 40, "longitude": -74, "airports": ["TEB", "JFK", "EWR"],
               "dot_market_id": 2}
TRIP = {"origin": "Origin, IL", "destination": "Destination, NY", "origin_place": ORIGIN,
        "destination_place": DESTINATION, "mode": "fly", "trip_type": "round_trip",
        "depart_date": "2099-03-10", "return_date": "2099-03-14", "travelers": 2,
        "budget": 1000}


class FlightTrendsTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine, tables=[Airport.__table__, AirfareQuarter.__table__])
        self.factory = sessionmaker(bind=self.engine)
        self.verified = {"CMI": 1, "JFK": 2, "EWR": 2, "MDW": 3, "ORD": 3,
                         "STL": 4, "PHL": 5}
        with Session(self.engine) as db:
            for code, market, lat, lon in (
                    ("CMI", 1, 40, -88), ("JFK", 2, 40, -74), ("EWR", 2, 40, -74.1),
                    ("MDW", 3, 41, -88), ("ORD", 3, 41.2, -88),
                    ("STL", 4, 38.5, -89), ("PHL", 5, 39.8, -75),
                    # A mapped field is still ineligible without independent evidence.
                    ("TEB", 6, 40, -74.05)):
                db.add(Airport(iata=code, name=code, city=f"Market {market}", state="IL",
                               latitude=lat, longitude=lon, dot_market_id=market))
            self.row_id = 0
            for a, b, quarter, fare in ((1, 2, 1, 200), (1, 2, 2, 100), (1, 2, 4, 300),
                                        (3, 2, 1, 150), (3, 2, 2, 60),
                                        (4, 2, 1, 140), (4, 2, 2, 75),
                                        (1, 5, 1, 160), (1, 5, 2, 75),
                                        (3, 5, 1, 1)):
                db.add(self.fare_row(a, b, quarter, fare))
            # Give candidate markets sufficient common-window passenger size. The
            # far endpoint has no nearby airport, so it is not a route suggestion.
            for market in (3, 4, 5, 6):
                db.add(self.fare_row(market, 99, 1, 900, passengers=10000))
            db.commit()
        self.session_patch = patch.object(node, "SessionLocal", self.factory)
        self.mapping_patch = patch.object(node, "read_verified_airport_markets", return_value=self.verified)
        self.session_patch.start()
        self.mapping_patch.start()
        self.addCleanup(self.session_patch.stop)
        self.addCleanup(self.mapping_patch.stop)
        self.addCleanup(self.engine.dispose)

    def fare_row(self, a, b, quarter, fare, passengers=100, year=2025):
        self.row_id += 1
        return AirfareQuarter(tbl6pk=self.row_id, year=year, quarter=quarter,
                              citymarketid_1=a, citymarketid_2=b,
                              city1=f"Market {a}, IL", city2=f"Market {b}, NY",
                              nsmiles=700, passengers=passengers, fare=fare)

    def run_node(self, constraints=None, **prior):
        input_state = state(copy.deepcopy(TRIP if constraints is None else constraints), **prior)
        before = copy.deepcopy(input_state)
        update_result = node.flight_trends(input_state)
        self.assertEqual(input_state, before, "The node must not mutate constraints or other state")
        self.assertEqual(set(update_result), {"flight_trends"})
        result = update_result["flight_trends"]
        assert_contract(self, result, FlightTrends)
        if result["status"] == "ok":
            self.assertEqual(set(result), {"status", "home", "options", "chart", "search_airports",
                                           "candidate_dates", "budget_check"})
            for value, contract in ((result["home"], RouteTrend), (result["chart"], FareChart),
                                    (result["search_airports"], SearchAirports),
                                    (result["budget_check"], BudgetCheck)):
                self.assertEqual(set(value), set(contract.__annotations__))
            self.assertTrue(result["home"]["route"])
            self.assertIs(type(result["home"]["has_data"]), bool)
            self.assertEqual(result["home"]["has_data"], result["home"]["avg_fare"] is not None)
            self.assertLessEqual(len(result["options"]), 4)
            self.assertEqual(len({(o["side"], o["suggested"]) for o in result["options"]}),
                             len(result["options"]))
            for option in result["options"]:
                self.assertEqual(set(option), set(RouteOption.__annotations__))
                self.assertIn(option["side"], ("departure", "arrival"))
                self.assertTrue(option["route"])
                self.assertIs(type(option["suggested"]), bool)
                self.assertGreater(option["avg_fare"], 0)
                self.assertGreaterEqual(option["distance_km"], 0)
                self.assertLessEqual(option["distance_km"], 400)
            suggested = [o for o in result["options"] if o["suggested"]]
            self.assertEqual(len(result["chart"]["series"]), len(suggested) + 1)
            self.assertEqual([s["home"] for s in result["chart"]["series"]],
                             [True] + [False] * len(suggested))
            for series in result["chart"]["series"]:
                self.assertEqual(set(series), set(ChartSeries.__annotations__))
                self.assertEqual(len(series["values"]), 4)
            self.assertTrue(result["chart"]["note"])
            for codes in result["search_airports"].values():
                self.assertEqual(len(codes), len(set(codes)))
                self.assertTrue(all(code in self.verified for code in codes))
            self.assertGreaterEqual(len(result["candidate_dates"]), 1)
            self.assertLessEqual(len(result["candidate_dates"]), 3)
            departures = []
            for pair in result["candidate_dates"]:
                self.assertEqual(set(pair), set(CandidateDates.__annotations__))
                departures.append(date.fromisoformat(pair["depart_date"]))
                if pair["return_date"] is not None:
                    self.assertGreaterEqual(date.fromisoformat(pair["return_date"]), departures[-1])
            self.assertEqual(departures, sorted(set(departures)))
        json.dumps(result, allow_nan=False)
        return result

    def window(self, start="2099-03-20", end="2099-04-30", nights=4, **updates):
        return {**TRIP, "depart_date": None, "return_date": None, "flexible_start": start,
                "flexible_end": end, "trip_nights": nights, **updates}

    def test_full_contract_and_independent_alternatives_against_home(self):
        result = self.run_node(flight_offers={"status": "empty", "reason": "untouched"},
                               hotel_options={"status": "skipped", "reason": "untouched"},
                               messages=[{"role": "user", "content": "keep this"}])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["home"]["avg_fare"], 200)
        self.assertEqual([(o["side"], o["airports"], o["avg_fare"], o["savings_each_way"])
                          for o in result["options"]],
                         [("departure", ["MDW", "ORD"], 150, 50), ("arrival", ["PHL"], 160, 40)])
        # The $1 combined route cannot influence independent home-baseline comparisons.
        self.assertEqual(len(result["chart"]["series"]), 3)

    def test_existing_skip_rules_and_reasons_do_not_touch_database_or_metadata(self):
        cases = [({**TRIP, "mode": "drive"}, "not a flying trip"),
                 ({**TRIP, "mode": None}, "not a flying trip")]
        cases += [({**TRIP, field: None}, "origin or destination not resolved to a place")
                  for field in ("origin_place", "destination_place")]
        with patch.object(node, "SessionLocal") as sessions, \
                patch.object(node, "read_verified_airport_markets") as metadata:
            for constraints, reason in cases:
                with self.subTest(reason=reason, constraints=constraints):
                    self.assertEqual(self.run_node(constraints), {"status": "skipped", "reason": reason})
            sessions.assert_not_called()
            metadata.assert_not_called()

    def test_missing_required_table_maps_to_empty_and_preserves_state(self):
        for table in (AirfareQuarter.__table__, Airport.__table__):
            with self.subTest(table=table.name):
                table.drop(self.engine)
                result = self.run_node()
                self.assertEqual(result["status"], "empty")
                self.assertTrue(result["reason"])
                table.create(self.engine)

    def test_database_connection_failure_returns_empty_without_exception_details(self):
        error = OperationalError("secret query", {}, Exception("private database location"))
        with patch.object(node, "SessionLocal", side_effect=error):
            result = self.run_node()
        self.assertEqual(result["status"], "empty")
        self.assertNotIn("private database location", result["reason"])
        self.assertNotIn("secret query", result["reason"])

    def test_database_query_failure_is_not_no_history_ok(self):
        error = OperationalError("query", {}, Exception("locked"))
        with patch.object(node, "get_market_passenger_totals", side_effect=error):
            self.assertEqual(self.run_node()["status"], "empty")

    def test_programming_errors_are_not_disguised_as_database_failures(self):
        with patch.object(node, "get_market_passenger_totals", side_effect=ValueError("programming error")):
            with self.assertRaisesRegex(ValueError, "programming error"):
                self.run_node()

    def test_airport_metadata_comes_from_verified_market_sources_in_the_database(self):
        self.mapping_patch.stop()
        with Session(self.engine) as db:
            for code, source in (("CMI", "fixed"), ("JFK", "table1a"), ("EWR", "table1a"),
                                 ("MDW", "table1a"), ("ORD", "table1a"), ("STL", "table1a"),
                                 ("PHL", "table1a"), ("TEB", "name")):
                db.get(Airport, code).market_source = source
            db.commit()
            mappings = node.read_verified_airport_markets(db)
        self.assertEqual(mappings, self.verified)  # TEB's name match is not trusted
        result = self.run_node()
        self.assertEqual(result["status"], "ok")
        self.assertNotIn("TEB", json.dumps(result))

    def test_no_verified_airport_returns_empty_with_a_reload_hint(self):
        self.mapping_patch.stop()
        result = self.run_node()  # the fixture airports have no market_source
        self.assertEqual(result["status"], "empty")
        self.assertIn("ingest_locations", result["reason"])

    def test_airports_table_from_an_older_load_returns_empty_with_a_reload_hint(self):
        self.mapping_patch.stop()
        Airport.__table__.drop(self.engine)
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE airports (iata VARCHAR(3) PRIMARY KEY, name VARCHAR, city VARCHAR, "
                "state VARCHAR(2), latitude FLOAT, longitude FLOAT, dot_market_id INTEGER)")
        result = self.run_node()
        self.assertEqual(result["status"], "empty")
        self.assertIn("ingest_locations", result["reason"])

    def test_no_fare_history_still_has_complete_ok_result_and_null_chart(self):
        with Session(self.engine) as db:
            db.execute(delete(AirfareQuarter))
            db.commit()
        result = self.run_node(self.window())
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["home"]["has_data"], False)
        self.assertIsNone(result["home"]["avg_fare"])
        self.assertEqual(result["options"], [])
        self.assertEqual(result["chart"]["series"][0]["values"], [None] * 4)
        self.assertEqual(result["budget_check"], {"typical_trip_total": None, "fits": None})
        self.assertEqual(result["candidate_dates"], [
            {"depart_date": "2099-03-20", "return_date": "2099-03-24"},
            {"depart_date": "2099-03-25", "return_date": "2099-03-29"},
            {"depart_date": "2099-03-31", "return_date": "2099-04-04"}])

    def test_missing_home_history_selects_valid_alternatives_without_savings(self):
        with Session(self.engine) as db:
            db.execute(delete(AirfareQuarter).where(AirfareQuarter.citymarketid_1 == 1,
                                                    AirfareQuarter.citymarketid_2 == 2))
            db.commit()
        result = self.run_node()
        self.assertIsNone(result["home"]["avg_fare"])
        self.assertEqual(len(result["options"]), 2)
        self.assertTrue(all(o["savings_each_way"] is None for o in result["options"]))
        self.assertEqual(result["chart"]["series"][0]["values"], [None] * 4)
        self.assertEqual(result["budget_check"], {"typical_trip_total": None, "fits": None})

    def test_no_home_market_uses_verified_associated_airports_then_alternative(self):
        constraints = {**TRIP, "origin_place": {**ORIGIN, "dot_market_id": None,
                                               "airports": ["BAD", "CMI", "CMI"]}}
        result = self.run_node(constraints)
        self.assertIsNone(result["home"]["avg_fare"])
        # Home: the associated verified airports; the alternative adds only its main airport
        self.assertEqual(result["search_airports"]["origin"], ["CMI", "MDW"])
        self.assertEqual([o["side"] for o in result["options"]], ["departure"])
        self.assertEqual(result["options"][0]["airports"], ["MDW", "ORD"])
        self.assertIsNone(result["options"][0]["savings_each_way"])

    def test_search_airports_are_each_markets_main_airport_home_first(self):
        result = self.run_node()
        # Without sizes, the closest airport is the main one. EWR is in the home market
        # but neither its main airport nor the place's own airport, so it isn't searched.
        self.assertEqual(result["search_airports"], {"origin": ["CMI", "MDW"],
                                                    "destination": ["JFK", "PHL"]})
        self.assertNotIn("TEB", json.dumps(result))
        self.assertNotIn("BAD", json.dumps(result))

    def set_passengers(self, **sizes):
        with Session(self.engine) as db:
            for code, passengers in sizes.items():
                db.get(Airport, code).passengers = passengers
            db.commit()

    def test_main_airport_is_the_busiest_and_the_places_own_airport_is_kept(self):
        self.set_passengers(JFK=1000, EWR=5000, MDW=2000, ORD=9000)
        result = self.run_node()
        # EWR is the main home airport; JFK stays as the place's own (first in its list)
        self.assertEqual(result["search_airports"], {"origin": ["CMI", "ORD"],
                                                    "destination": ["EWR", "JFK", "PHL"]})
        self.assertEqual(result["options"][0]["airports"], ["ORD", "MDW"])

    def test_own_airport_comes_from_flight_airports_first(self):
        self.set_passengers(JFK=1000, EWR=5000)
        constraints = {**TRIP, "destination_place": {**DESTINATION, "flight_airports": ["EWR", "JFK"]}}
        result = self.run_node(constraints)
        self.assertEqual(result["search_airports"]["destination"], ["EWR", "PHL"])

    def test_near_empty_airports_are_dropped_unless_the_market_has_nothing_bigger(self):
        self.set_passengers(MDW=40, ORD=9000)
        self.assertEqual(self.run_node()["options"][0]["airports"], ["ORD"])
        self.set_passengers(MDW=40, ORD=60)
        self.assertEqual(self.run_node()["options"][0]["airports"], ["ORD"])

    def add_near_miss_market(self, fare=170, lat=40.45):
        """Market 7 (BMI), about 50 km from the origin: 15% cheaper than home in Q1."""
        self.verified["BMI"] = 7
        with Session(self.engine) as db:
            db.add(Airport(iata="BMI", name="BMI", city="Market 7", state="IL",
                           latitude=lat, longitude=-88, dot_market_id=7))
            db.add(self.fare_row(7, 2, 1, fare))
            db.add(self.fare_row(7, 99, 1, 900, passengers=10000))
            db.commit()

    def test_near_miss_is_searched_but_not_suggested_or_charted(self):
        self.add_near_miss_market()
        result = self.run_node()
        departure = [(o["airports"], o["avg_fare"], o["suggested"])
                     for o in result["options"] if o["side"] == "departure"]
        self.assertEqual(departure, [(["MDW", "ORD"], 150, True), (["BMI"], 170, False)])
        self.assertEqual(result["search_airports"]["origin"], ["CMI", "MDW", "BMI"])
        self.assertNotIn("BMI", json.dumps(result["chart"]))

    def test_near_miss_needs_to_be_close_and_cheaper_enough(self):
        self.add_near_miss_market(lat=41.9)  # 15% cheaper but about 210 km away
        self.assertNotIn("BMI", json.dumps(self.run_node()))

    def test_chart_matches_quarterly_means_and_keeps_null_gaps(self):
        with Session(self.engine) as db:
            db.add(self.fare_row(2, 1, 1, 202.009, year=2024))
            db.commit()
        result = self.run_node()
        self.assertEqual(result["chart"]["x"], ["Q1 (Jan-Mar)", "Q2 (Apr-Jun)",
                                                "Q3 (Jul-Sep)", "Q4 (Oct-Dec)"])
        self.assertEqual([s["values"] for s in result["chart"]["series"]],
                         [[201.0, 100.0, None, 300.0], [150.0, 60.0, None, None],
                          [160.0, 75.0, None, None]])
        self.assertIn("2024", result["chart"]["years"])
        self.assertIn("2025", result["chart"]["years"])
        self.assertIn("Table 6", result["chart"]["note"])

    def test_exact_pair_preserved_and_nights_derived_without_mutating_constraints(self):
        self.assertEqual(self.run_node()["candidate_dates"],
                         [{"depart_date": "2099-03-10", "return_date": "2099-03-14"}])
        self.assertEqual(self.run_node({**TRIP, "return_date": None, "trip_nights": 3})["candidate_dates"],
                         [{"depart_date": "2099-03-10", "return_date": "2099-03-13"}])
        self.assertEqual(self.run_node({**TRIP, "return_date": "2099-03-10"})["candidate_dates"],
                         [{"depart_date": "2099-03-10", "return_date": "2099-03-10"}])

    def test_exact_dates_take_precedence_and_quarters_span_the_stay(self):
        constraints = {**TRIP, "depart_date": "2099-03-30", "return_date": "2099-04-02",
                       "flexible_start": "2099-06-01", "flexible_end": "2099-06-30"}
        result = self.run_node(constraints)
        self.assertEqual(result["home"]["avg_fare"], 150)
        self.assertEqual(result["candidate_dates"],
                         [{"depart_date": "2099-03-30", "return_date": "2099-04-02"}])

    def test_flexible_window_selects_cheapest_quarter_and_derives_returns_beyond_window(self):
        result = self.run_node(self.window())
        self.assertEqual(result["candidate_dates"], [
            {"depart_date": "2099-04-01", "return_date": "2099-04-05"},
            {"depart_date": "2099-04-15", "return_date": "2099-04-19"},
            {"depart_date": "2099-04-30", "return_date": "2099-05-04"}])

    def test_flexible_window_with_fixed_return_departs_before_it(self):
        # Cheapest quarter (Apr) up to the day before the Apr 20 return; every trip ends Apr 20
        result = self.run_node(self.window(nights=None, return_date="2099-04-20"))
        self.assertEqual(result["candidate_dates"], [
            {"depart_date": "2099-04-01", "return_date": "2099-04-20"},
            {"depart_date": "2099-04-10", "return_date": "2099-04-20"},
            {"depart_date": "2099-04-19", "return_date": "2099-04-20"}])

    def test_flexible_window_with_return_and_nights_has_one_implied_departure(self):
        result = self.run_node(self.window(return_date="2099-04-20", nights=4))
        self.assertEqual(result["candidate_dates"],
                         [{"depart_date": "2099-04-16", "return_date": "2099-04-20"}])

    def test_flexible_window_without_return_or_nights_is_empty(self):
        result = self.run_node(self.window(nights=None))
        self.assertEqual(result["status"], "empty")
        self.assertIn("return date or stay length is missing", result["reason"])

    def test_one_and_two_day_windows_produce_distinct_valid_pairs(self):
        for last in (1, 2):
            with self.subTest(last=last):
                result = self.run_node(self.window("2099-04-01", f"2099-04-0{last}", nights=2))
                self.assertEqual(result["candidate_dates"], [
                    {"depart_date": f"2099-04-0{day}", "return_date": f"2099-04-0{day+2}"}
                    for day in range(1, last + 1)])

    def test_cross_year_window_uses_cheapest_quarter_and_breaks_ties_by_earliest_segment(self):
        constraints = self.window("2099-12-29", "2100-01-20", nights=2)
        result = self.run_node(constraints)
        self.assertEqual([p["depart_date"] for p in result["candidate_dates"]],
                         ["2100-01-01", "2100-01-10", "2100-01-20"])
        with Session(self.engine) as db:
            db.execute(update(AirfareQuarter).where(AirfareQuarter.citymarketid_1 == 1,
                                                   AirfareQuarter.citymarketid_2 == 2,
                                                   AirfareQuarter.quarter == 4).values(fare=200))
            db.commit()
        result = self.run_node(constraints)
        self.assertEqual(result["candidate_dates"], [
            {"depart_date": "2099-12-29", "return_date": "2099-12-31"},
            {"depart_date": "2099-12-30", "return_date": "2100-01-01"},
            {"depart_date": "2099-12-31", "return_date": "2100-01-02"}])

    def test_missing_home_seasonality_uses_selected_alternative_seasonality(self):
        with Session(self.engine) as db:
            db.execute(delete(AirfareQuarter).where(AirfareQuarter.citymarketid_1 == 1,
                                                    AirfareQuarter.citymarketid_2 == 2))
            db.commit()
        result = self.run_node(self.window())
        self.assertEqual([p["depart_date"] for p in result["candidate_dates"]],
                         ["2099-04-01", "2099-04-15", "2099-04-30"])

    def test_one_way_exact_and_window_returns_remain_null_even_with_nights(self):
        for constraints in ({**TRIP, "trip_type": "one_way", "trip_nights": 4},
                            self.window(trip_type="one_way"),
                            self.window(trip_type="one_way", trip_nights=None)):
            with self.subTest(constraints=constraints):
                result = self.run_node(constraints)
                self.assertEqual(result["status"], "ok")
                self.assertTrue(all(pair["return_date"] is None for pair in result["candidate_dates"]))
        self.assertEqual(self.run_node({**TRIP, "trip_type": "one_way"})["home"]["avg_fare"], 200)

    def test_missing_date_inputs_do_not_invent_stay_lengths_or_touch_database(self):
        cases = [{**TRIP, "trip_type": None}, {**TRIP, "return_date": None},
                 {**TRIP, "depart_date": None, "return_date": None},
                 self.window(nights=None), self.window(flexible_end=None)]
        with patch.object(node, "SessionLocal") as sessions:
            for constraints in cases:
                with self.subTest(constraints=constraints):
                    self.assertEqual(self.run_node(constraints)["status"], "empty")
            sessions.assert_not_called()

    def test_flexible_dates_respect_representable_return_bounds(self):
        result = self.run_node(self.window("9999-12-28", "9999-12-31", nights=2))
        self.assertEqual(result["candidate_dates"], [
            {"depart_date": "9999-12-28", "return_date": "9999-12-30"},
            {"depart_date": "9999-12-29", "return_date": "9999-12-31"}])
        self.assertEqual(self.run_node(self.window("9999-12-30", "9999-12-31", nights=2))["status"],
                         "empty")
        result = self.run_node(self.window("9999-12-31", "9999-12-31", trip_type="one_way"))
        self.assertEqual(result["candidate_dates"], [{"depart_date": "9999-12-31", "return_date": None}])

    def test_invalid_dates_retain_constraint_validation(self):
        with self.assertRaises(ValidationError):
            self.run_node(self.window("2099-04-30", "2099-04-01"))
        with self.assertRaises(ValidationError):
            self.run_node({**TRIP, "depart_date": "2000-01-01"})

    def test_budget_uses_home_fare_all_travelers_directions_and_inclusive_boundary(self):
        result = self.run_node({**TRIP, "budget": 800})
        self.assertEqual(result["budget_check"], {"typical_trip_total": 800, "fits": True})
        self.assertEqual(self.run_node({**TRIP, "budget": 799.99})["budget_check"],
                         {"typical_trip_total": 800, "fits": False})
        self.assertEqual(self.run_node({**TRIP, "trip_type": "one_way", "travelers": 3})["budget_check"],
                         {"typical_trip_total": 600, "fits": True})

    def test_budget_compares_unrounded_estimate_before_presentation(self):
        with Session(self.engine) as db:
            db.execute(update(AirfareQuarter).where(AirfareQuarter.citymarketid_1 == 1,
                                                   AirfareQuarter.citymarketid_2 == 2,
                                                   AirfareQuarter.quarter == 1).values(fare=200.001))
            db.commit()
        self.assertEqual(self.run_node({**TRIP, "budget": 800})["budget_check"],
                         {"typical_trip_total": 800.0, "fits": False})

    def test_missing_budget_or_travelers_has_unknown_fit(self):
        self.assertEqual(self.run_node({**TRIP, "budget": None})["budget_check"],
                         {"typical_trip_total": 800, "fits": None})
        self.assertEqual(self.run_node({**TRIP, "travelers": None})["budget_check"],
                         {"typical_trip_total": None, "fits": None})

    def test_actual_downstream_consumers_accept_output_unchanged(self):
        constraints = self.window()
        trends = self.run_node(constraints)
        downstream_state = state(constraints, flight_trends=trends)
        before = copy.deepcopy(downstream_state)
        flights_update = flight_search(downstream_state)
        assert_contract(self, flights_update["flight_offers"], FlightOffers)
        self.assertEqual(flights_update["flight_offers"]["status"], "empty")
        downstream_state.update(flights_update)
        hotel_result = hotel_search(downstream_state)["hotel_options"]
        assert_contract(self, hotel_result, HotelOptions)
        self.assertEqual((hotel_result["checkin"], hotel_result["checkout"]),
                         ("2099-04-01", "2099-04-05"))
        self.assertEqual(stay_dates(downstream_state), (date(2099, 4, 1), date(2099, 4, 5)))
        self.assertEqual(hotel_budget(downstream_state), 1000)
        self.assertEqual(hotel_result["hotel_budget"], 1000,
                         "Historical flight estimates must not reduce actual hotel budget")
        self.assertEqual(downstream_state["constraints"], before["constraints"])
        self.assertEqual(downstream_state["flight_trends"], before["flight_trends"])

    def test_one_way_trends_allow_hotel_nights_without_inventing_return_flight(self):
        constraints = self.window(trip_type="one_way", trip_nights=3)
        result = self.run_node(constraints)
        self.assertIsNone(result["candidate_dates"][0]["return_date"])
        self.assertEqual(stay_dates(state(constraints, flight_trends=result)),
                         (date(2099, 4, 1), date(2099, 4, 4)))


if __name__ == "__main__":
    unittest.main()
