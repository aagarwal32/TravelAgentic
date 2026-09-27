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
