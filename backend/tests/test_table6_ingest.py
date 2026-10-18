"""Tests for the DOT Table 6 model and ingestion script.

They run against an in-memory SQLite database and a tiny fixture CSV, never the
real dataset or database.db. Run from the repo root:
    PYTHONPATH=backend backend/.venv/bin/python -m unittest discover -s backend/tests -v
"""

import os
import unittest
from pathlib import Path
from unittest import mock

# core.config requires these settings when db.database is imported; give test
# defaults so the tests work without a .env file.
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db.database import Base
from models.airfare import AirfareQuarter
from scripts import ingest_table6

FIXTURE = Path(__file__).parent / "fixtures" / "table6_sample.csv"

# What the fixture contains. If you replace it with rows copied from the real
# file, update these numbers:
#   6 good rows (>= 2022), 2 rows before 2022 (filtered out silently),
#   4 bad rows (empty fare, "n/a" fare, repeated tbl6pk, quarter 5) that get skipped.
FIXTURE_LOADED = 6
FIXTURE_SKIPPED = 4


def good_row(**changes) -> dict:
    """A valid raw CSV row (all values are text, like csv.DictReader gives), with optional overrides."""
    row = {
        "tbl": "Table 6", "table_1_flag": "", "location_1": "POINT (0 0)", "location_2": "POINT (1 1)",
        "tbl6pk": "20233324673319", "year": "2023", "quarter": "3",
        "citymarketid_1": "32467", "citymarketid_2": "33195",
        "city1": "Miami, FL (Metropolitan Area)", "city2": "Tampa, FL (Metropolitan Area)",
        "nsmiles": "204", "passengers": "298.1", "fare": "174.0",
        "carrier_lg": "AA", "large_ms": "0.52", "fare_lg": "239.0",
        "carrier_low": "NK", "lf_ms": "0.31", "fare_low": "90.0",
    }
    row.update(changes)
    return row


class ParseRowTests(unittest.TestCase):
    def test_valid_row_has_right_types(self):
        row = ingest_table6.parse_row(good_row())
        self.assertIsInstance(row["year"], int)
        self.assertIsInstance(row["quarter"], int)
        self.assertIsInstance(row["tbl6pk"], int)
        self.assertIsInstance(row["fare"], float)
        self.assertEqual(row["year"], 2023)
        self.assertEqual(row["fare"], 174.0)
        self.assertEqual(row["carrier_low"], "NK")

    def test_rows_before_min_year_are_left_out(self):
        self.assertIsNone(ingest_table6.parse_row(good_row(year="2021")))
        self.assertIsNotNone(ingest_table6.parse_row(good_row(year=str(ingest_table6.MIN_YEAR))))

    def test_missing_or_non_numeric_fare_is_skipped(self):
        self.assertIsNone(ingest_table6.parse_row(good_row(fare="")))
        self.assertIsNone(ingest_table6.parse_row(good_row(fare="n/a")))

    def test_dropped_columns_are_not_in_the_parsed_row(self):
        row = ingest_table6.parse_row(good_row())
        for dropped in ("tbl", "table_1_flag", "location_1", "location_2"):
            self.assertNotIn(dropped, row)
        self.assertEqual(set(row), set(ingest_table6.KEPT_COLUMNS))

    def test_blank_carrier_columns_become_none(self):
        row = ingest_table6.parse_row(good_row(carrier_low="", lf_ms="", fare_low=""))
        self.assertIsNone(row["carrier_low"])
        self.assertIsNone(row["lf_ms"])
        self.assertIsNone(row["fare_low"])

    def test_export_style_numbers_are_accepted(self):
        row = ingest_table6.parse_row(good_row(year="2,026", fare="$346.29"))
        self.assertEqual(row["year"], 2026)
        self.assertEqual(row["fare"], 346.29)

    def test_quarter_outside_1_to_4_is_skipped(self):
        self.assertIsNone(ingest_table6.parse_row(good_row(quarter="5")))


class ReadRowsTests(unittest.TestCase):
    def test_fixture_counts(self):
        rows, skipped = ingest_table6.read_rows(FIXTURE)
        self.assertEqual(len(rows), FIXTURE_LOADED)
        self.assertEqual(skipped, FIXTURE_SKIPPED)  # old rows are NOT counted as skipped
        self.assertTrue(all(r["year"] >= 2022 for r in rows))

    def test_wrong_file_raises_clear_error(self):
        tmp = Path(__file__).parent / "fixtures" / "_wrong_header.csv"
        tmp.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
        try:
            with self.assertRaises(ValueError):
                ingest_table6.read_rows(tmp)
        finally:
            tmp.unlink()


class LoadRowsTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine, tables=[AirfareQuarter.__table__])
        self.session = Session(self.engine)
        self.rows, _ = ingest_table6.read_rows(FIXTURE)

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def count(self) -> int:
        return self.session.scalar(select(func.count()).select_from(AirfareQuarter))

    def test_loading_fixture_fills_the_table(self):
        loaded = ingest_table6.load_rows(self.session, self.rows)
        self.assertEqual(loaded, FIXTURE_LOADED)
        self.assertEqual(self.count(), FIXTURE_LOADED)

    def test_loading_twice_does_not_duplicate(self):
        ingest_table6.load_rows(self.session, self.rows)
        ingest_table6.load_rows(self.session, self.rows)
        self.assertEqual(self.count(), FIXTURE_LOADED)

    def test_failed_load_keeps_old_rows(self):
        ingest_table6.load_rows(self.session, self.rows)
        before = set(self.session.scalars(select(AirfareQuarter.tbl6pk)))

        # Repeat a tbl6pk so the INSERT fails in the second batch, after the
        # old rows were already deleted inside the transaction.
        bad_rows = self.rows[:3] + [dict(self.rows[0])]
        with mock.patch.object(ingest_table6, "BATCH_SIZE", 2):
            with self.assertRaises(IntegrityError):
                ingest_table6.load_rows(self.session, bad_rows)

        after = set(self.session.scalars(select(AirfareQuarter.tbl6pk)))
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
