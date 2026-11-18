# Pipeline internals

## Readers and converter

`readers.py` turns any supported source into an iterator of records
(header → values): csv / delimited text with delimiter sniffing, Excel via
openpyxl, JSON (list of objects, nested objects, `record_path`, JSON lines),
XML (one element per record). `converter.py` maps the records to the seven
default columns - explicit map, explicit sequence or the alias table in
`formats.COLUMN_ALIASES` - normalises dates to `YYYYMMDD`, writes to
`data/work/` and moves the finished file into the loading folder
(rename / overwrite / error on name clashes). Conversion never opens the
database.

## Loader

Per file, `StageLoader.load_file()`:

1. sha-256 fingerprint, market and trade date from the file name (regexes in
   `[loader]`), no-trading-day check against `TradingCalendar` → *skipped*;
2. header check, row validation (ticker present, date `YYYYMMDD`, numbers);
3. **one transaction**: delete the file's previous rows, insert the new
   ones in batches (`batch_size`), record `Load_History`;
4. data-quality strategies (`quality.py`) → `Data_Error`;
5. move the file to `archive` / `error` / `skipped`.

Bulk loads (`load_folder`) repeat this per file and keep the known tickers
per market in memory so the missing-market-day check stays cheap.

## Promotion procedures

SQLite has no stored procedures, so `promotion.py` builds each procedure as
a list of SQL statements executed inside `BEGIN IMMEDIATE … COMMIT`:

* **promote_daily** - fill temporary tables with the market calendar and
  symbol windows, select candidate staging rows (all, a filter, or rows
  loaded / corrected since the last completed unfiltered run), classify each
  row with the first failing rule, write rejects to `Data_Error`, pick the
  newest row per stock/day, upsert into `Stock_Daily` (update only when a
  value changed → `UpdatedAt`), count inserted / updated / unchanged /
  rejected / skipped.
* **promote_weekly / monthly** - determine the periods touched (all, or
  periods containing daily rows with `UpdatedAt` after the last run),
  aggregate per ticker and period, compute expected trading days from the
  calendar and the symbol window, mark incomplete periods and record
  `incomplete_week` / `incomplete_month` findings, upsert; `--full` also
  deletes aggregates whose daily rows are gone.

* **promote_eod_daily** - `Stock_EOD_Stage` → `Stock_Daily`: select EOD rows
  (all, a filter, or rows with `UpdatedAt` after the last run plus rows
  without a daily row), resolve the market (parameter → `Stock_Symbol` →
  the ticker's market in `Stock_Daily` → `[eod_api] default_market`), fill
  the market calendar, convert (`rate = AdjustedClose / Close`; open / high /
  low × rate; close = adjusted close; rounded to 6 decimals by the
  application function `eod_round`, because SQLite's `round()` can be one
  ulp off), validate the converted values (daily rules + `market_unknown`,
  `market_mismatch`), newest EOD row per ticker/day, upsert on
  `(Ticker, TradeDate)` with `StageId` NULL (it references `Stock_Stage`),
  resolve its earlier findings of the promoted days.

Every run writes `Promotion_History`; a failure rolls back, is recorded in
`App_Error` and raises `PromotionError`. The exact SQL is exported to
`sql/promote_*_sqlite.sql` and a test keeps the export in sync.

## Incremental logic

The watermark is the `StartedAt` of the last completed, unfiltered run of
the same procedure: daily looks at `LoadedAt` of staging rows and at
`Data_Correction` timestamps; weekly / monthly at `UpdatedAt` of daily rows.
Filtered runs (ticker, file, date range) never move the watermark.
`promote_eod_daily` looks at `UpdatedAt` of `Stock_EOD_Stage` and also
retries every EOD row that has no daily row yet (e.g. rejected because its
market was unknown); a run with `--market` is a filtered run.
