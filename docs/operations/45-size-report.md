# Where the space goes - and how to shrink it

Measured on the shipped project folder (`Stock_DataLoadGit/`, **392 MB**
before this change; `.git`, `.venv` and caches excluded). Re-create the
table any time with `python -m stock_dataload db size` and `du`.

## Top 10 space consumers

| # | Item | Size | What it is |
|---|------|------|------------|
| 1 | `data/stock_trade.db` → table `Stock_Daily` | 136.4 MB | 836,309 validated daily rows (the product) |
| 2 | `data/stock_trade.db` → table `Stock_Stage` | 107.0 MB | the same 836,309 rows **again, as text** - the landing zone of the loader |
| 3 | `data/stock_trade.db` → table `Stock_Weekly` | 27.6 MB | 180,074 weekly aggregates |
| 4 | `data/stock_trade.db` → index `IX_Stock_Stage_SourceFile` | 25.5 MB | index on the staging table's file name |
| 5 | `data/stock_trade.db` → index `IX_Stock_Daily_UpdatedAt` | 25.5 MB | index on the text timestamp used by incremental promotion |
| 6 | `data/stock_trade.db` → index `sqlite_autoindex_Stock_Daily_1` | 21.8 MB | the `UNIQUE (Ticker, TradeDate)` constraint |
| 7 | `data/stock_trade.db` → index `IX_Stock_Daily_TradeDate` | 17.1 MB | index on the trade date |
| 8 | `data/stock_trade.db` → table `Stock_Monthly` + `Data_Error` | 7.7 MB + 4.7 MB | monthly aggregates; 18,354 data-quality findings |
| 9 | `samples/` | 2.5 MB | the same one-day delivery in 9 file formats (used by the tests) |
| 10 | `docs/*.png` | 2.4 MB → **0** | screenshots (removed from the tree) |

Everything else - the package (1.6 MB), tests (0.5 MB), `logs/` (0.3 MB),
`data/archive/` (0.36 MB), `.coverage` (52 KB) - is below 2 MB together.

In short: **98 % of the folder is one SQLite file**, and inside it the
staging table plus its index (132 MB) duplicate what the daily table
already holds, while indexes make up another 95 MB.

## Suggestions, largest win first

1. **Purge promoted staging rows** (−132 MB, done in 15 s):
   `python -m stock_dataload db purge-stage --all --vacuum` takes the file
   from 385.5 MB to **244 MB**. The daily table keeps the data,
   `Load_History` keeps the registry; staged copies are only needed for
   corrections / re-promotion of recent deliveries, so the default keeps
   the last 30 days (`--keep-days`). Schedule it after `promote`.
2. **Keep the database out of the code folder.** It is git-ignored already;
   point `[database] sqlite_path` at a data drive (or back it up with
   `sqlite3 data/stock_trade.db ".backup ..."`) and ship the repository with
   an empty database - `init-db` recreates the schema in a second.
3. **Fewer / smaller indexes** (~45 MB): `IX_Stock_Daily_UpdatedAt` (25 MB)
   only serves the incremental weekly / monthly promotion; a watermark on
   the integer `DailyId` (rows changed since the last run have a higher
   id or a flag) would replace it. `IX_Stock_Stage_SourceFile` shrinks with
   the purge; with `Load_History` holding the file → `LoadId` mapping the
   staging index could be on the integer load id instead of the file name.
4. **Narrower daily rows** (~40-50 MB): every daily row repeats
   `SourceFile` (e.g. `NASDAQ_20230103.txt`), `LoadId` (a UUID) and two ISO
   timestamps as text - ~60 bytes of provenance per 8-column row. Storing
   an integer `LoadHistoryId` instead of file name + UUID, and timestamps
   as integers, roughly halves the table. (Schema change: do it with a
   migration + full re-promotion.)
5. **`VACUUM` after big deletes** - SQLite does not return free pages by
   itself (`db vacuum`). WAL files (`*.db-wal`) can also grow between
   checkpoints; `db vacuum` checkpoints first.
6. **Samples**: `samples/make_samples.py` regenerates the csv / tsv / xlsx /
   json / xml variants from `NASDAQ_20250123.txt`; keeping only the text
   file and the script saves ~2.2 MB (the tests would then call the script
   in `setUpClass`).
7. **Runtime artefacts**: `logs/*.log`, `.coverage`, `data/archive/*`,
   `__pycache__/` are git-ignored but sit in the working folder - clean them
   before zipping (`git clean -Xn` lists them).
8. **Screenshots**: removed from the tree (they belong in the wiki / release
   assets, not in the source folder).

## What was applied to the shipped folder

`python -m stock_dataload db reset-data --yes` emptied `Stock_Stage`,
`Stock_Daily`, `Stock_Weekly`, `Stock_Monthly`, `Data_Error` (+ load /
promotion history), dropped and re-created `IX_Stock_Stage_SourceFile`,
`IX_Stock_Daily_UpdatedAt`, `IX_Stock_Daily_TradeDate` and the other
secondary indexes, `REINDEX`ed the unique constraints (incl.
`Stock_Daily (Ticker, TradeDate)`) and vacuumed: **385.5 MB → 0.9 MB**,
schema and reference data (calendar, symbols, users) intact. All
`docs/*.png` files were deleted.
