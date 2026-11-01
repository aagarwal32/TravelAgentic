"""Download the project's datasets from the web directly into backend/datasets/.

This script enables easy downloading and sharing of big datasets we plan to work with.

Usage (from backend/):
    python scripts/download_datasets.py            # download all datasets
    python scripts/download_datasets.py flights    # download only the named ones
    python scripts/download_datasets.py --force    # re-download even if present
    python scripts/download_datasets.py --list     # show configured datasets
"""

import argparse
import hashlib
import shutil
import sys
import urllib.request
from pathlib import Path

DATASETS_DIR = Path(__file__).resolve().parent.parent / "datasets"

# Add one entry per dataset. `url` must be a direct download link.
# `sha256` is optional; fill it in so everyone can confirm they have the same file
# (get it with: shasum -a 256 datasets/<filename>).
DATASETS = [
    {
        "name": "example",
        "url": "https://example.com/path/to/example.csv",
        "filename": "example.csv",
        "sha256": None,
    },
    {
        "name": "dot_table6",
        # Plain API link (not the site's Export button): numbers come as plain numbers
        # and it is already filtered to 2022+. ">" is written as %3E so it is a valid URL.
        "url": "https://data.transportation.gov/resource/yj5y-b2ir.csv?$where=year%3E=2022&$order=tbl6pk&$limit=500000",
        "filename": "dot_table6.csv",
        # sha256 is None on purpose: DOT adds a new quarter every ~3 months, so the
        # file changes and a fixed checksum would break the download.
        "sha256": None,
    },
    # The five location datasets below are loaded together by scripts/ingest_locations.py
    # (see docs/data.md). All are public domain.
    {
        "name": "us_places_gazetteer",
        # Census Bureau 2024 Gazetteer: every US incorporated place and census-designated
        # place with its coordinates. A fixed release, so it has a checksum.
        "url": "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/2024_Gaz_place_national.zip",
        "filename": "us_places_gazetteer_2024.zip",
        "sha256": "cf262fc92b2326f7a8c62a89d156a60eb17d64d6d35f7a62310c43bb08972c06",
    },
    {
        "name": "us_places_population",
        # Census Bureau ACS 2020-2024 5-year estimates, table B01003 (total population) for
        # every geography; ingestion keeps the places, matching the Gazetteer's 2024 places.
        "url": "https://www2.census.gov/programs-surveys/acs/summary_file/2024/table-based-SF/data/5YRData/acsdt5y2024-b01003.dat",
        "filename": "us_places_population_2024.dat",
        "sha256": "38d1a992bb058d184009b10b9b34987279aee575e4323165cfb5706c69b6ca90",
    },
    {
        "name": "us_places_centers",
        # USGS GNIS Federal Codes: each "Populated Place" (its traditional center, e.g. Civic
        # Center for San Francisco) with its Census place code. Gives cities a downtown point
        # for hotel searches. USGS republishes it every few months, so no checksum.
        "url": "https://prd-tnm.s3.amazonaws.com/StagedProducts/GeographicNames/FederalCodes/FedCodes_National_Text.zip",
        "filename": "us_places_centers_gnis.zip",
        "sha256": None,
    },
    {
        "name": "ourairports",
        # OurAirports: every airport in the world. OurAirports updates it nightly, so no checksum.
        "url": "https://ourairports.com/data/airports.csv",
        "filename": "ourairports_airports.csv",
        "sha256": None,
    },
    {
        "name": "dot_table1a_airports",
        # DOT Consumer Airfare Report Table 1a (airport-pair markets), grouped by the API into
        # one row per airport pair with each airport's DOT city market id (2022+, ~90 KB).
        # It links airports to the market ids Table 6 uses. New quarters change it: no checksum.
        "url": "https://data.transportation.gov/resource/tfrh-tu9e.csv?$select=airport_1,citymarketid_1,city1,airport_2,citymarketid_2,city2,max(year)%20as%20last_year&$where=year%3E=2022&$group=airport_1,citymarketid_1,city1,airport_2,citymarketid_2,city2&$order=airport_1,airport_2&$limit=100000",
        "filename": "dot_table1a_airports.csv",
        "sha256": None,
    },
]

CHUNK_SIZE = 1024 * 1024


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, dest: Path) -> None:
    tmp = dest.with_name(dest.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "travel-agentic-downloader"})
    with urllib.request.urlopen(request) as response, tmp.open("wb") as out:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        while chunk := response.read(CHUNK_SIZE):
            out.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r  {done / 1e6:,.1f} / {total / 1e6:,.1f} MB ({done / total:.0%})", end="", flush=True)
            else:
                print(f"\r  {done / 1e6:,.1f} MB", end="", flush=True)
    print()
    # A dropped connection can end the read early without an error; never keep a cut-off
    # file (datasets without a checksum would otherwise load it silently).
    if total and done != total:
        tmp.unlink()
        raise OSError(f"incomplete download: got {done:,} of {total:,} bytes")
    shutil.move(tmp, dest)


def fetch(dataset: dict, force: bool) -> bool:
    dest = DATASETS_DIR / dataset["filename"]
    expected = dataset.get("sha256")

    if dest.exists() and not force:
        if not expected or sha256_of(dest) == expected:
            print(f"[skip] {dataset['name']}: {dest.name} already present")
            return True
        print(f"[warn] {dataset['name']}: checksum mismatch, re-downloading")

    print(f"[get]  {dataset['name']}: {dataset['url']}")
    try:
        download(dataset["url"], dest)
    except Exception as e:
        print(f"[fail] {dataset['name']}: {e}")
        return False

    if expected:
        actual = sha256_of(dest)
        if actual != expected:
            print(f"[fail] {dataset['name']}: checksum mismatch (got {actual})")
            return False
    print(f"[done] {dataset['name']} -> {dest}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Download project datasets.")
    parser.add_argument("names", nargs="*", help="dataset names to download (default: all)")
    parser.add_argument("--force", action="store_true", help="re-download even if the file exists")
    parser.add_argument("--list", action="store_true", help="list configured datasets and exit")
    args = parser.parse_args()

    if args.list:
        for d in DATASETS:
            print(f"{d['name']:<20} {d['filename']:<30} {d['url']}")
        return 0

    known = {d["name"]: d for d in DATASETS}
    unknown = [n for n in args.names if n not in known]
    if unknown:
        print(f"Unknown dataset(s): {', '.join(unknown)}. Known: {', '.join(known)}")
        return 1

    selected = [known[n] for n in args.names] if args.names else DATASETS
    DATASETS_DIR.mkdir(parents=True, exist_ok=True)
    results = [fetch(d, args.force) for d in selected]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
