"""Load DOT Table 6 (airfares by city pair and quarter) into dot_table6_fares.

Usage, from backend/:
    python scripts/download_datasets.py dot_table6     # get the CSV first
    python -m scripts.ingest_table6                    # load backend/datasets/dot_table6.csv
    python -m scripts.ingest_table6 --csv path/to.csv  # load another file

The script REPLACES the table's contents instead of appending. DOT republishes
the whole file every quarter, so replacing means running it twice gives the same
result (no duplicate rows) and a new quarter is just "download, run again".
"""

import argparse
import csv
import math
import sys
from pathlib import Path

from sqlalchemy import delete, insert
from sqlalchemy.orm import Session

from db.database import SessionLocal, create_tables
from models.airfare import AirfareQuarter

MIN_YEAR = 2022      # older rows are ignored; change here to load more history
BATCH_SIZE = 5000    # rows per INSERT; one row per query would be very slow for ~105,000 rows
DEFAULT_CSV = Path(__file__).resolve().parent.parent / "datasets" / "dot_table6.csv"

# The columns we keep, grouped by how they are converted. Everything else in the
# CSV (tbl, table_1_flag, location_* ...) is dropped.
INT_COLUMNS = ["tbl6pk", "year", "quarter", "citymarketid_1", "citymarketid_2", "nsmiles"]
FLOAT_COLUMNS = ["passengers", "fare"]
TEXT_COLUMNS = ["city1", "city2"]
OPTIONAL_TEXT_COLUMNS = ["carrier_lg", "carrier_low"]                      # blank -> None
OPTIONAL_FLOAT_COLUMNS = ["large_ms", "fare_lg", "lf_ms", "fare_low"]      # blank -> None
KEPT_COLUMNS = (INT_COLUMNS + FLOAT_COLUMNS + TEXT_COLUMNS
                + OPTIONAL_TEXT_COLUMNS + OPTIONAL_FLOAT_COLUMNS)


def _to_float(text: str | None) -> float:
    """Convert text to a float. Raises ValueError if it is missing or not a real number."""
    if text is None:
        raise ValueError("missing value")
    # The API link gives plain numbers, but we also accept "$346.29" / "2,026"
    # (the style of the site's Export button) in case someone loads that file.
    cleaned = text.strip().replace("$", "").replace(",", "")
    if cleaned == "":
        raise ValueError("empty value")
    value = float(cleaned)
    if not math.isfinite(value):  # float() accepts "nan" and "inf"
        raise ValueError("not a finite number")
    return value


def _to_int(text: str | None) -> int:
    """Convert text to an int ("2025" or "2025.0"). Raises ValueError otherwise."""
    if text is not None:
        cleaned = text.strip().replace(",", "")
        try:
            return int(cleaned)  # exact for long ids; going through float could round them
        except ValueError:
            pass
    value = _to_float(text)
    if not value.is_integer():
        raise ValueError("not a whole number")
    return int(value)


def parse_row(raw: dict) -> dict | None:
    """Turn one CSV row into clean typed values, or None if the row must be left out.

    Left out: years before MIN_YEAR, a missing or unparseable required value,
    a quarter outside 1-4, or a fare that is not positive. Only the kept
    columns are returned.
    """
    try:
        row = {}
        for col in INT_COLUMNS:
            row[col] = _to_int(raw.get(col))
        for col in FLOAT_COLUMNS:
            row[col] = _to_float(raw.get(col))
        for col in TEXT_COLUMNS:
            text = (raw.get(col) or "").strip()
            if not text:
                raise ValueError(f"{col} is empty")
            row[col] = text
        for col in OPTIONAL_TEXT_COLUMNS:
            row[col] = (raw.get(col) or "").strip() or None
        for col in OPTIONAL_FLOAT_COLUMNS:
            text = raw.get(col)
            # Blank is fine (None); a non-blank value that is not a number is a bad row.
            row[col] = _to_float(text) if text and text.strip() else None
    except ValueError:
        return None

    if row["year"] < MIN_YEAR or not 1 <= row["quarter"] <= 4 or row["fare"] <= 0:
        return None
    return {col: row[col] for col in KEPT_COLUMNS}


def _is_before_min_year(raw: dict) -> bool:
    """True if the row's year is readable and older than MIN_YEAR (filtered, not 'bad')."""
    try:
        return _to_int(raw.get("year")) < MIN_YEAR
    except ValueError:
        return False


def check_header(fieldnames: list[str] | None) -> None:
    """Raise ValueError if the CSV lacks any column we need.

    Without this, a wrong file would silently skip every row.
    """
    missing = [c for c in KEPT_COLUMNS if c not in (fieldnames or [])]
    if missing:
        raise ValueError(f"CSV is missing columns: {', '.join(missing)}")


def read_rows(csv_path: Path) -> tuple[list[dict], int]:
    """Read the CSV; return (clean rows, number of bad rows skipped).

    Rows older than MIN_YEAR are filtered out silently and not counted as skipped.
    A repeated tbl6pk counts as skipped, because the table requires it to be unique.
    """
    rows: list[dict] = []
    skipped = 0
    seen: set[int] = set()
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        check_header(reader.fieldnames)
        for raw in reader:
            row = parse_row(raw)
            if row is None:
                if not _is_before_min_year(raw):
                    skipped += 1
                continue
            if row["tbl6pk"] in seen:
                skipped += 1
                continue
            seen.add(row["tbl6pk"])
            rows.append(row)
    return rows, skipped


def load_rows(session: Session, rows: list[dict]) -> int:
    """Replace everything in dot_table6_fares with `rows`, in ONE transaction.

    If anything fails the transaction is rolled back, so the table keeps its
    old rows. Returns the number of rows inserted.
    """
    try:
        session.execute(delete(AirfareQuarter))
        for start in range(0, len(rows), BATCH_SIZE):
            session.execute(insert(AirfareQuarter), rows[start:start + BATCH_SIZE])
        session.commit()
    except Exception:
        session.rollback()
        raise
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point. Returns the process exit code."""
    parser = argparse.ArgumentParser(description="Load DOT Table 6 into the database.")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="path to the CSV file")
    args = parser.parse_args(argv)

    if not args.csv.exists():
        print(f"CSV not found: {args.csv}\nRun: python scripts/download_datasets.py dot_table6")
        return 1

    rows, skipped = read_rows(args.csv)
    if not rows:
        # Never wipe the table because of an empty or wrong file.
        print("No valid rows found; the table was NOT changed.")
        return 1

    create_tables()
    with SessionLocal() as session:
        loaded = load_rows(session, rows)

    newest_year, newest_quarter = max((r["year"], r["quarter"]) for r in rows)
    print(f"Rows loaded:  {loaded}")
    print(f"Rows skipped: {skipped}")
    print(f"Newest data:  {newest_year} Q{newest_quarter}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
