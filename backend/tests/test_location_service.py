"""Tests for resolving typed cities into exact US places (services/location_service.py).

They run against an in-memory SQLite database filled with a few hand-made places and
airports (tests/location_fixtures.py), never the real datasets or database.db.
"""

import os
import unittest

# core.config requires these settings when db.database is imported; give test
# defaults so the tests work without a .env file.
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from location_fixtures import (CHICAGO_MARKET, DALLAS_FORT_WORTH_MARKET, GREENVILLE_SC_MARKET,
                               NEW_YORK_MARKET, ST_LOUIS_MARKET, WASHINGTON_MARKET, location_engine)
from services.agent.state import Place
from services.location_service import (MAX_CHOICES, NO_LOCATION_DATA, LocationDataMissing, build_place,
                                       find_places, get_place, nearby_airports, normalize_name,
                                       place_label)
from models.location import Airport, USPlace


class LocationTestCase(unittest.TestCase):
    def setUp(self):
        self.engine = location_engine()
        self.db = Session(self.engine)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def ids(self, text: str, **kwargs) -> list[str]:
        return [place.id for place in find_places(self.db, text, **kwargs)]


class FindPlacesTests(LocationTestCase):
    def test_unique_city_is_one_complete_place(self):
        [chicago] = find_places(self.db, "Chicago")
        self.assertIsInstance(chicago, Place)
        self.assertEqual((chicago.id, chicago.city, chicago.state), ("us-il-chicago", "Chicago", "IL"))
        self.assertAlmostEqual(chicago.latitude, 41.85)
        self.assertAlmostEqual(chicago.longitude, -87.6501)
        self.assertEqual(chicago.airports, ["MDW", "ORD"])  # closest first
        self.assertEqual(chicago.dot_market_id, CHICAGO_MARKET)

    def test_ambiguous_city_lists_the_biggest_first_at_most_five(self):
        self.assertEqual(self.ids("Greenville"), ["us-nc-greenville", "us-sc-greenville", "us-tx-greenville",
                                                  "us-ms-greenville", "us-wi-greenville"])
        self.assertEqual(MAX_CHOICES, 5)  # the sixth Greenville (OH, the smallest) is left out
        self.assertEqual(self.ids("Greenville", limit=2), ["us-nc-greenville", "us-sc-greenville"])

    def test_state_code_or_state_name_picks_one(self):
        for text in ("Greenville, SC", "Greenville, South Carolina", "greenville ,  s.c.",
                     "Greenville SC", "Greenville South Carolina", "Greenville, SC, USA"):
            with self.subTest(text=text):
                self.assertEqual(self.ids(text), ["us-sc-greenville"])

    def test_same_state_names_keep_distinct_places_and_airports(self):
        places = find_places(self.db, "Greenville, New York")
        self.assertEqual([p.id for p in places], ["us-ny-greenville-3630642", "us-ny-greenville-3630609"])
        self.assertEqual([place_label(p) for p in places],
                         ["Greenville (CDP; Westchester County), NY", "Greenville (CDP; Greene County), NY"])
        self.assertEqual([p.latitude for p in places], [40.998643, 42.413789])
        self.assertEqual(places[0].dot_market_id, NEW_YORK_MARKET)
        self.assertTrue(places[0].airports)
        self.assertIsNone(places[1].dot_market_id)
        self.assertEqual(places[1].airports, [])
        for place in places:
            self.assertEqual(get_place(self.db, place.id), place)
        # A stale, formerly shared ID must not silently select either entity.
        self.assertIsNone(get_place(self.db, "us-ny-greenville"))

    def test_qualified_card_labels_match_exactly_before_limiting(self):
        for text in ("Greenville, NY", "Waukesha, WI"):
            for place in find_places(self.db, text):
                with self.subTest(place=place.id):
                    self.assertEqual(self.ids(place_label(place).lower(), limit=1), [place.id])
        for text in ("Greenville (CDP; Wrong County), NY", "Greenville (Greene), NY",
                     "Greenvill (CDP; Greene County), NY"):
            self.assertEqual(self.ids(text), [])

    def test_case_spaces_and_trailing_usa_do_not_matter(self):
        for text in ("  chicago ", "CHICAGO", "Chicago, USA", "Chicago, IL, United States", "chicago usa"):
            with self.subTest(text=text):
                self.assertEqual(self.ids(text), ["us-il-chicago"])

    def test_aliases(self):
        expected = {"NYC": "us-ny-new-york", "nyc": "us-ny-new-york", "New York City": "us-ny-new-york",
                    "LA": "us-ca-los-angeles", "SF": "us-ca-san-francisco", "DC": "us-dc-washington",
                    "Philly": "us-pa-philadelphia", "Vegas": "us-nv-las-vegas", "NOLA": "us-la-new-orleans",
                    "NYC, NY": "us-ny-new-york"}
        for text, place_id in expected.items():
            with self.subTest(text=text):
                self.assertEqual(self.ids(text), [place_id])

    def test_saint_and_st_are_the_same(self):
        expected = ["us-mo-saint-louis", "us-mi-saint-louis"]
        for text in ("Saint Louis", "St. Louis", "st louis", "ST. LOUIS"):
            with self.subTest(text=text):
                self.assertEqual(self.ids(text), expected)
        self.assertEqual(self.ids("Saint Louis, MO"), ["us-mo-saint-louis"])
        self.assertEqual(normalize_name("Ft. Worth"), normalize_name("Fort Worth"))
        self.assertEqual(self.ids("Ft. Worth"), ["us-tx-fort-worth"])

    def test_washington_dc_spellings(self):
        for text in ("Washington, DC", "Washington DC", "Washington, D.C.", "Washington, District of Columbia"):
            with self.subTest(text=text):
                self.assertEqual(self.ids(text), ["us-dc-washington"])
        self.assertEqual(self.ids("Washington"), ["us-dc-washington", "us-pa-washington"])

    def test_never_matches_part_of_a_name(self):
        self.assertEqual(self.ids("Charlotte"), ["us-nc-charlotte", "us-mi-charlotte"])  # no Charlottesville
        self.assertEqual(self.ids("Charlottesville"), ["us-va-charlottesville"])
        for text in ("Charlott", "Chi", "Green", "York", "Louis"):
            with self.subTest(text=text):
                self.assertEqual(self.ids(text), [])

    def test_unknown_city_or_state_finds_nothing(self):
        for text in ("Narnia", "", "   ", ", USA", "Chicago, OH", "Greenville, Narnia", "Chicago, Ontario"):
            with self.subTest(text=text):
                self.assertEqual(self.ids(text), [])

    def test_atlantis_is_a_real_town(self):
        # Issue #21 expected "Atlantis" -> [], but Atlantis, FL is in the Census data
        [atlantis] = find_places(self.db, "Atlantis")
        self.assertEqual((atlantis.id, atlantis.state), ("us-fl-atlantis", "FL"))
        self.assertEqual(atlantis.airports, ["DJT"])
        self.assertEqual(atlantis.dot_market_id, 34027)  # "West Palm Beach/Palm Beach, FL"

    def test_state_name_alone_is_not_a_city(self):
        for text in ("Florida", "florida", "Florida, USA"):
            with self.subTest(text=text):
                self.assertEqual(self.ids(text), [])
        self.assertEqual(self.ids("Florida, NY"), ["us-ny-florida"])  # the town, when asked for
        self.assertEqual(self.ids("New York"), ["us-ny-new-york"])   # state names that are big cities
        self.assertEqual(self.ids("Washington")[0], "us-dc-washington")

    def test_every_match_is_returned_however_small(self):
        # No population shortcut: Charlotte, MI (9k) is listed next to Charlotte, NC (904k),
        # so intake asks which one
        self.assertEqual(self.ids("Charlotte"), ["us-nc-charlotte", "us-mi-charlotte"])
        self.assertEqual(self.ids("Portland"), ["us-or-portland", "us-me-portland"])

    def test_empty_table_is_an_error_not_an_unknown_city(self):
        empty = location_engine(filled=False)
        with Session(empty) as db, self.assertRaises(LocationDataMissing):
            find_places(db, "Chicago")
        empty.dispose()

    def test_missing_tables_are_an_error_not_an_unknown_city(self):
        # A database where scripts.ingest_locations never ran has no us_places table
        bare = create_engine("sqlite://", poolclass=StaticPool)
        with Session(bare) as db, self.assertRaises(LocationDataMissing) as raised:
            find_places(db, "Chicago")
        self.assertEqual(str(raised.exception), NO_LOCATION_DATA)
        self.assertIn("python -m scripts.ingest_locations", NO_LOCATION_DATA)
        bare.dispose()


class NearbyAirportsTests(LocationTestCase):
    def test_empty_airport_table_is_a_data_error(self):
        self.db.execute(delete(Airport))
        self.db.commit()
        with self.assertRaisesRegex(LocationDataMissing, "ingest_locations"):
            find_places(self.db, "Chicago")
        with self.assertRaises(LocationDataMissing):
            get_place(self.db, "us-il-chicago")

    def test_missing_airport_table_is_a_data_error(self):
        Airport.__table__.drop(self.engine)
        with self.assertRaisesRegex(LocationDataMissing, "ingest_locations"):
            find_places(self.db, "Chicago")

    def test_closest_first_with_distances(self):
        airports = nearby_airports(self.db, 40.7143, -74.0060)  # New York
        codes = [a["iata"] for a in airports]
        self.assertEqual(set(codes), {"LGA", "EWR", "JFK"})
        distances = [a["distance_km"] for a in airports]
        self.assertEqual(distances, sorted(distances))
        self.assertTrue(all(0 < d < 25 for d in distances), distances)
        self.assertEqual(set(airports[0]), {"iata", "name", "city", "state", "latitude", "longitude",
                                            "dot_market_id", "distance_km"})
        self.assertTrue(all(a["dot_market_id"] == NEW_YORK_MARKET for a in airports))

    def test_only_within_the_radius(self):
        self.assertEqual([a["iata"] for a in nearby_airports(self.db, 41.85, -87.6501)], ["MDW", "ORD"])
        self.assertEqual([a["iata"] for a in nearby_airports(self.db, 41.85, -87.6501, radius_km=15)], ["MDW"])
        self.assertEqual(nearby_airports(self.db, 47.3208, -106.9101), [])  # Jordan, MT
        washington = [a["iata"] for a in nearby_airports(self.db, 38.8951, -77.0364)]
        self.assertEqual(washington, ["DCA", "IAD", "BWI"])
        with self.assertRaises(ValueError):
            nearby_airports(self.db, 0, 0, radius_km=0)


class BuildPlaceTests(LocationTestCase):
    def test_market_comes_from_the_closest_airport_with_one(self):
        # FTW is closest but has no DOT market; DFW (Dallas/Fort Worth) is next
        [fort_worth] = find_places(self.db, "Fort Worth")
        self.assertEqual(fort_worth.airports, ["FTW", "DFW", "DAL"])
        self.assertEqual(fort_worth.dot_market_id, DALLAS_FORT_WORTH_MARKET)

    def test_right_market_for_aliases_and_spellings(self):
        expected = {"NYC": NEW_YORK_MARKET, "Saint Louis, MO": ST_LOUIS_MARKET, "DC": WASHINGTON_MARKET,
                    "Greenville, SC": GREENVILLE_SC_MARKET, "Greenville, TX": DALLAS_FORT_WORTH_MARKET}
        for text, market in expected.items():
            with self.subTest(text=text):
                self.assertEqual(find_places(self.db, text)[0].dot_market_id, market)
        self.assertEqual(find_places(self.db, "Greenville, SC")[0].airports, ["GSP"])

    def test_place_without_airports_or_market(self):
        [jordan] = find_places(self.db, "Jordan, MT")
        self.assertEqual(jordan.airports, [])
        self.assertIsNone(jordan.dot_market_id)
        self.assertEqual(jordan.latitude, 47.3208)  # still has coordinates for hotels and driving

    def test_build_place_from_a_row(self):
        row = self.db.get(USPlace, "us-sc-greenville")
        place = build_place(self.db, row)
        self.assertEqual(place, get_place(self.db, "us-sc-greenville"))
        self.assertEqual(place_label(place), "Greenville, SC")


class GetPlaceTests(LocationTestCase):
    def test_by_id(self):
        place = get_place(self.db, "us-mo-saint-louis")
        self.assertEqual((place.city, place.state, place.airports), ("St. Louis", "MO", ["STL"]))

    def test_unknown_or_wrong_type_is_none(self):
        for place_id in ("us-xx-nowhere", "", None, 42, {"id": "us-il-chicago"}):
            with self.subTest(place_id=place_id):
                self.assertIsNone(get_place(self.db, place_id))


if __name__ == "__main__":
    unittest.main()
