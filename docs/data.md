# Datasets

Datasets are too large for Git, so each person downloads them into
`backend/datasets/` with a script. The local database (`backend/database.db`) isn't
committed either; everyone builds their own from the datasets.

All commands run from `backend/` with the virtual environment active.

```bash
python scripts/download_datasets.py          # all datasets
python scripts/download_datasets.py --list   # see what's available
```

## DOT Table 6 (historical airfares)

Historical airfares from the DOT Consumer Airfare Report, Table 6, 2022 and newer.
The trends agent uses them. The data is quarterly, city pairs have no direction, and
fares are average each-way fares, not real ticket prices.

Source: https://data.transportation.gov/d/yj5y-b2ir

### Load it into the database

```bash
python scripts/download_datasets.py dot_table6   # downloads datasets/dot_table6.csv
python -m scripts.ingest_table6                  # loads it into dot_table6_fares
```

The second command replaces the table's contents, so running it twice never creates
duplicates. It prints the rows loaded, the rows skipped, and the newest year and
quarter. When DOT publishes a new quarter, run both commands again.

By default the script reads `datasets/dot_table6.csv`. To load a CSV from somewhere
else, pass its path with `--csv`:

```bash
python -m scripts.ingest_table6 --csv path/to/dot_table6.csv
```
