"""Tests for the location tables' ingestion script (scripts/ingest_locations.py).

They run against an in-memory SQLite database and tiny fixture files, never the real
datasets or database.db. The fixtures in tests/fixtures/locations/ are rows copied
from the real files, with a few edited on purpose:
  gazetteer_sample.txt  2024 Census Gazetteer: 12 places load; Puerto Rico is filtered;
                        "Nowhere city, TX" (edited) has a bad latitude and is skipped;
                        Waukesha city and village (WI), and two Greenville CDPs (NY),
                        share names but have distinct Census and GNIS identities.
  population_sample.dat ACS B01003: every place except Village of Oak Creek, plus a
                        nation and a county row that must be ignored.
  gnis_sample.txt       GNIS Federal Codes: San Francisco, Saint Louis and Greenville SC
                        centers (matched by Census code); Nashville (old code, matched by
                        its unique name in the state); Greenville NC's point was moved to
                        South Dakota so the distance guard rejects it; an added second
                        "Milford" in CT makes that name ambiguous; a Civil and a
                        second-county row are ignored for centers. Civil/Census
                        entity records supply counties by exact ANSI feature ID.
  airports_sample.csv   OurAirports: 10 airports kept; a heliport, a foreign, an
                        unscheduled and a small airport are filtered; edited rows: no
                        IATA code (filtered), a bad latitude and a repeated DFW (skipped).
  table1a_sample.csv    DOT Table 1a airport pairs; an edited 2022 row puts GSP in
                        another market, and the newer row must win.
"""

import io
import os
import shutil
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

# core.config requires these settings when db.database is imported; give test
# defaults so the tests work without a .env file.
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from db.database import Base
from models.airfare import AirfareQuarter
from models.location import Airport, USPlace
from scripts import ingest_locations as ingest

FIXTURES = Path(__file__).parent / "fixtures" / "locations"
GAZETTEER = FIXTURES / "gazetteer_sample.txt"
POPULATION = FIXTURES / "population_sample.dat"
CENTERS = FIXTURES / "gnis_sample.txt"
AIRPORTS = FIXTURES / "airports_sample.csv"
TABLE1A = FIXTURES / "table1a_sample.csv"

# DOT Table 6 market names the name match reads (real ids and names)
MARKETS = {30194: "Dallas/Fort Worth, TX", 30977: "Chicago, IL", 31871: "Greenville/Spartanburg, SC",
           31454: "Orlando, FL", 34761: "Sanford, FL", 34027: "West Palm Beach/Palm Beach, FL",
           31049: "College Station/Bryan, TX", 33367: "Quad Cities, IL (Metropolitan Area)"}


def read_fixture_places():
    return ingest.read_places(GAZETTEER, ingest.read_population(POPULATION), ingest.read_city_centers(CENTERS))


class PlaceNameTests(unittest.TestCase):
    def test_census_names_become_the_names_people_type(self):
        cases = {("Greenville city", "4530850"): "Greenville",
                 ("Milford city (balance)", "0947515"): "Milford",
                 ("Juneau city and borough", "0236400"): "Juneau",
                 ("Village of Oak Creek (Big Park) CDP", "0480095"): "Village of Oak Creek",
                 ("North Attleborough Town city", "2546598"): "North Attleborough Town",
                 ("Carson City", "3209700"): "Carson City",
                 ("Urban Honolulu CDP", "1571550"): "Honolulu",
                 ("Nashville-Davidson metropolitan government (balance)", "4752006"): "Nashville",
                 ("Boise City city", "1608830"): "Boise",
                 ("Boise City city", "4007300"): "Boise City"}  # the Oklahoma one keeps its name
        for (name, geoid), expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(ingest.clean_place_name(name, geoid), expected)

    def test_place_ids(self):
        self.assertEqual(ingest.place_id("SC", "Greenville"), "us-sc-greenville")
        self.assertEqual(ingest.place_id("MO", "St. Louis"), "us-mo-saint-louis")
        self.assertEqual(ingest.place_id("NC", "Winston-Salem"), "us-nc-winston-salem")


class ReadPlacesTests(unittest.TestCase):
    def setUp(self):
        rows, self.counts = read_fixture_places()
        self.places = {row["id"]: row for row in rows}

    def test_counts(self):
        self.assertEqual(len(self.places), 12)
        self.assertEqual(self.counts, {"skipped": 1, "ambiguous": 4, "no_population": 1, "centers": 4})
        self.assertNotIn("us-pr-san-juan-zona-urbana", self.places)
        self.assertTrue(all(row["state"] != "PR" for row in self.places.values()))

    def test_row_values(self):
        row = self.places["us-mo-saint-louis"]
        self.assertEqual((row["geoid"], row["city"], row["state"], row["name_key"], row["population"]),
                         ("2965000", "St. Louis", "MO", "saint louis", 288512))
        self.assertEqual(set(row), {"id", "geoid", "city", "state", "name_key", "latitude", "longitude", "population"})
        self.assertEqual(self.places["us-tn-nashville"]["population"], 690130)
        self.assertEqual(self.places["us-hi-honolulu"]["city"], "Honolulu")
        self.assertEqual(self.places["us-az-village-of-oak-creek"]["population"], 0)  # no ACS row

    def test_same_state_names_preserve_distinct_entities(self):
        for geoid, label, lat in (
            ("5584250", "Waukesha (city; Waukesha County)", 43.009084),
            ("5584275", "Waukesha (village; Waukesha County)", 42.960422),
            ("3630609", "Greenville (CDP; Greene County)", 42.413789),
            ("3630642", "Greenville (CDP; Westchester County)", 40.998643),
        ):
            with self.subTest(geoid=geoid):
                row = next(r for r in self.places.values() if r["geoid"] == geoid)
                self.assertTrue(row["id"].endswith("-" + geoid))
                self.assertEqual(row["city"], label)
                self.assertEqual(row["name_key"], label.split(" (")[0].lower())
                self.assertEqual(row["latitude"], lat)

    def test_same_state_ids_do_not_depend_on_order_or_population(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reversed.txt"
            lines = GAZETTEER.read_text().splitlines()
            path.write_text("\n".join([lines[0], *reversed(lines[1:])]) + "\n")
            populations = ingest.read_population(POPULATION)
            populations.update({"5584250": 1, "5584275": 100000, "3630609": 100000, "3630642": 1})
            rows, _ = ingest.read_places(path, populations, ingest.read_city_centers(CENTERS))
        self.assertEqual({r["geoid"]: (r["id"], r["city"]) for r in rows},
                         {r["geoid"]: (r["id"], r["city"]) for r in self.places.values()})

    def test_missing_counties_use_distinct_coordinate_labels(self):
        centers = ingest.read_city_centers(CENTERS)
        centers.counties.clear()
        rows, _ = ingest.read_places(GAZETTEER, ingest.read_population(POPULATION), centers)
        greenvilles = [r for r in rows if r["name_key"] == "greenville" and r["state"] == "NY"]
        self.assertEqual(len({r["city"] for r in greenvilles}), 2)
        self.assertTrue(all("lat " in r["city"] and "lon " in r["city"] for r in greenvilles))

    def test_same_name_places_do_not_share_a_name_only_gnis_center(self):
        centers = ingest.read_city_centers(CENTERS)
        centers.by_name[("55", "waukesha")] = [(43.01, -88.24)]
        rows, _ = ingest.read_places(GAZETTEER, ingest.read_population(POPULATION), centers)
        self.assertEqual({r["latitude"] for r in rows if r["name_key"] == "waukesha"},
                         {43.009084, 42.960422})

    def test_repeated_geoid_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "duplicate.txt"
            lines = GAZETTEER.read_text().splitlines()
            path.write_text("\n".join([*lines, lines[1]]) + "\n")
            with self.assertRaisesRegex(ValueError, "Duplicate Census GEOID"):
                ingest.read_places(path, ingest.read_population(POPULATION), ingest.read_city_centers(CENTERS))

    def test_gnis_city_center_replaces_the_gazetteer_point(self):
        san_francisco = self.places["us-ca-san-francisco"]  # Gazetteer point is in the ocean
        self.assertEqual((san_francisco["latitude"], san_francisco["longitude"]), (37.775, -122.4194444))
        st_louis = self.places["us-mo-saint-louis"]  # GNIS writes "Saint Louis"; the Civil row is ignored
        self.assertEqual((st_louis["latitude"], st_louis["longitude"]), (38.6272733, -90.1978889))

    def test_center_found_by_unique_name_when_the_code_differs(self):
        nashville = self.places["us-tn-nashville"]  # GNIS still has the pre-consolidation code
        self.assertEqual((nashville["latitude"], nashville["longitude"]), (36.1658899, -86.7844432))

    def test_implausible_ambiguous_or_missing_center_keeps_the_gazetteer_point(self):
        greenville_nc = self.places["us-nc-greenville"]  # its GNIS point was moved 1,500 km away
        self.assertEqual((greenville_nc["latitude"], greenville_nc["longitude"]), (35.596431, -77.375256))
        milford = self.places["us-ct-milford"]  # two GNIS "Milford"s in CT, neither with its code
        self.assertEqual((milford["latitude"], milford["longitude"]), (41.223644, -73.064556))
        honolulu = self.places["us-hi-honolulu"]  # no GNIS row at all
        self.assertEqual((honolulu["latitude"], honolulu["longitude"]), (21.324347, -157.84764))

    def test_zipped_files_are_read_like_the_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            gazetteer_zip = Path(tmp) / "gazetteer.zip"
            with zipfile.ZipFile(gazetteer_zip, "w") as archive:
                archive.write(GAZETTEER, "2024_Gaz_place_national.txt")
            centers_zip = Path(tmp) / "gnis.zip"
            with zipfile.ZipFile(centers_zip, "w") as archive:
                archive.write(CENTERS, "Text/FederalCodes_National.txt")
                archive.writestr("FedCodes_National_Text.xml", "<metadata/>")
            zipped = ingest.read_places(gazetteer_zip, ingest.read_population(POPULATION),
                                        ingest.read_city_centers(centers_zip))
        self.assertEqual(zipped, read_fixture_places())

    def test_wrong_file_raises_clear_error(self):
        for reader in (ingest.read_population, ingest.read_airports, ingest.read_table1a_markets,
                       ingest.read_city_centers):
            with self.subTest(reader=reader.__name__), self.assertRaises(ValueError):
                reader(POPULATION if reader is not ingest.read_population else AIRPORTS)


class ReadAirportsTests(unittest.TestCase):
    def test_only_us_airports_with_scheduled_service(self):
        rows, skipped = ingest.read_airports(AIRPORTS)
        self.assertEqual(sorted(row["iata"] for row in rows),
                         ["CLL", "DAL", "DFW", "DJT", "FTW", "GSP", "MCO", "MLI", "ORD", "SFB"])
        self.assertEqual(skipped, 2)  # bad latitude, repeated DFW
        gsp = next(row for row in rows if row["iata"] == "GSP")
        self.assertEqual(gsp, {"iata": "GSP", "name": "Greenville-Spartanburg International Airport",
                               "city": "Greenville/Greer/Spartanburg", "state": "SC",
                               "latitude": 34.895699, "longitude": -82.218903, "dot_market_id": None})


class LinkMarketsTests(unittest.TestCase):
    def setUp(self):
        rows, _ = ingest.read_airports(AIRPORTS)
        self.counts = ingest.link_markets(rows, ingest.read_table1a_markets(TABLE1A), MARKETS)
        self.markets = {row["iata"]: row["dot_market_id"] for row in rows}

    def test_table1a_gives_metro_airports_their_shared_market(self):
        self.assertEqual(self.markets["DFW"], 30194)
        self.assertEqual(self.markets["DAL"], 30194)
        self.assertEqual(self.markets["ORD"], 30977)
        self.assertEqual(self.markets["GSP"], 31871)  # the newest Table 1a row wins
        self.assertEqual(self.markets["DJT"], 34027)  # renamed from PBI

    def test_hand_checked_fix_and_name_match(self):
        self.assertEqual(self.markets["MLI"], 33367)  # Moline -> Quad Cities
        self.assertEqual(self.markets["CLL"], 31049)  # College Station -> College Station/Bryan, TX

    def test_name_match_never_joins_a_table1a_market(self):
        self.assertIsNone(self.markets["SFB"])  # "Orlando" is MCO's market; DOT has a separate Sanford one
        self.assertIsNone(self.markets["FTW"])  # "Fort Worth" is DFW's and DAL's market
        self.assertEqual(self.counts, {"table1a": 6, "fixed": 1, "name": 1, "none": 2})

    def test_without_table6_nothing_is_linked_by_name(self):
        rows, _ = ingest.read_airports(AIRPORTS)
        counts = ingest.link_markets(rows, ingest.read_table1a_markets(TABLE1A), {})
        self.assertEqual(counts["name"], 0)


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", poolclass=StaticPool)
        Base.metadata.create_all(self.engine, tables=[USPlace.__table__, Airport.__table__,
                                                      AirfareQuarter.__table__])
        self.session = Session(self.engine)
        self.places, _ = read_fixture_places()
        self.airports, _ = ingest.read_airports(AIRPORTS)

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def count(self, model) -> int:
        return self.session.scalar(select(func.count()).select_from(model))

    def test_loading_twice_replaces_instead_of_duplicating(self):
        ingest.load_rows(self.session, self.places, self.airports)
        ingest.load_rows(self.session, self.places, self.airports)
        self.assertEqual((self.count(USPlace), self.count(Airport)), (12, 10))

    def test_failed_load_keeps_both_old_tables(self):
        ingest.load_rows(self.session, self.places, self.airports)
        # A repeated airport fails the INSERT after both tables were already emptied
        with mock.patch.object(ingest, "BATCH_SIZE", 2), self.assertRaises(IntegrityError):
            ingest.load_rows(self.session, self.places[:3], self.airports[:3] + [dict(self.airports[0])])
        self.assertEqual((self.count(USPlace), self.count(Airport)), (12, 10))

    def test_main_loads_links_and_reports(self):
        self.session.add(AirfareQuarter(
            tbl6pk=1, year=2025, quarter=1, citymarketid_1=31049, citymarketid_2=30977,
            city1="College Station/Bryan, TX", city2="Chicago, IL", nsmiles=900, passengers=50.0, fare=250.0))
        self.session.commit()
        args = ["--gazetteer", str(GAZETTEER), "--population", str(POPULATION), "--centers", str(CENTERS),
                "--airports", str(AIRPORTS), "--table1a", str(TABLE1A)]
        output = io.StringIO()
        with mock.patch.object(ingest, "SessionLocal", sessionmaker(bind=self.engine)), \
             mock.patch.object(ingest, "create_tables"), redirect_stdout(output):
            self.assertEqual(ingest.main(args), 0)
        self.assertEqual((self.count(USPlace), self.count(Airport)), (12, 10))
        self.assertEqual(self.session.get(Airport, "CLL").dot_market_id, 31049)  # matched by Table 6 name
        self.assertIn("DFW  30194  Dallas/Fort Worth, TX", output.getvalue())
        self.assertIn("6 from Table 1a, 1 hand-checked, 1 by Table 6 name, 2 without a market", output.getvalue())

    def test_main_with_a_missing_file_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "airports.csv"
            output = io.StringIO()
            with mock.patch.object(ingest, "SessionLocal") as sessions, redirect_stdout(output):
                code = ingest.main(["--gazetteer", str(GAZETTEER), "--population", str(POPULATION),
                                    "--centers", str(CENTERS), "--airports", str(missing),
                                    "--table1a", str(TABLE1A)])
        self.assertEqual(code, 1)
        sessions.assert_not_called()
        self.assertIn("download_datasets.py ourairports", output.getvalue())

    def test_main_with_an_empty_file_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "airports.csv"
            shutil.copy(AIRPORTS, empty)
            empty.write_text(AIRPORTS.read_text().splitlines()[0] + "\n")  # header only
            with mock.patch.object(ingest, "SessionLocal") as sessions, redirect_stdout(io.StringIO()):
                code = ingest.main(["--gazetteer", str(GAZETTEER), "--population", str(POPULATION),
                                    "--centers", str(CENTERS), "--airports", str(empty),
                                    "--table1a", str(TABLE1A)])
        self.assertEqual(code, 1)
        sessions.assert_not_called()


if __name__ == "__main__":
    unittest.main()
