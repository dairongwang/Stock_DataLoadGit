# Active tables and promotion

The staging table holds files *as delivered* (every column text). The
**active tables** hold validated, typed data:

| Table | One row per | Content |
|-------|-------------|---------|
| `Stock_Daily` | stock and trading day | open, high, low, close (numbers), volume, provenance (file, load, stage row) |
| `Stock_Weekly` | stock and ISO week (Mon-Sun) | open of the first day, highest high, lowest low, close of the last day, summed volume, trading days, completeness |
| `Stock_Monthly` | stock and calendar month | the same per month |

## Running a promotion

On the *Active tables* page choose **daily**, **weekly**, **monthly**,
**all** or **eod** (the EOD API data, see below) and press *Promote*. Options:

* **full** - re-promote everything (default: only what changed since the last completed run);
* **ticker / file / load id / date range** - limit a daily promotion to part of the staging table.

From the command line: `python -m stock_dataload promote all [--full] [--ticker AAPL]`.

## Validation (staging → daily)

Every staged row must pass, in this order:

| Rule | Rejects the row when |
|------|----------------------|
| `ticker_required` | the ticker is empty |
| `date_is_valid` | the date is not a real `YYYYMMDD` (or `YYYY-MM-DD`) date |
| `numeric_columns` | open, high, low, close or vol is missing / not a number |
| `positive_price` | a price is 0 or negative |
| `volume_invalid` | vol is negative or not a whole number |
| `high_above_low` | high < low |
| `price_range` | open or close lies outside low..high (`[promotion] check_price_range`) |
| `before_first_trade_date` / `after_last_trade_date` | the date lies outside the stock's trading window (`Stock_Symbol`; `check_trading_window`) |
| `market_closed` | the market was closed that day (`No_Trading_Day`; `reject_market_closed_days`) |

Rejected rows become **data errors** (Monitoring → Data errors) with a
*Correct* link; fix them on the [Data correction](60-monitoring-and-correction.md)
page and promote again. If the same stock/day is staged twice (two files, a
manual row) the most recently loaded row wins.

## The EOD API data (Stock_EOD_Stage → daily)

Prices downloaded from the EOD API ([Downloading prices from the EOD API](35-eod-api-downloads.md))
reach the daily table with **promote eod**. The prices are converted to the
adjusted basis of `adjusted_close`, per symbol and trade day:

| Daily column | Value |
|--------------|-------|
| `Open` | EOD `Open` × rate |
| `High` | EOD `High` × rate |
| `Low` | EOD `Low` × rate |
| `Close` | EOD `AdjustedClose` |
| `Volume` | EOD `Volume` |

with **rate = AdjustedClose / Close**, rounded to 6 decimals. Example (MRK,
2025-11-14): open 92.2, high 95.08, low 91.49, close 92.92, adjusted close
90.2693 → rate 0.971473, daily open 89.569839, high 92.367682, low 88.880093,
close 90.2693, volume 19,632,000.

**Symbol, trade date and market are matched.** The daily row of an EOD row is
the one with the same ticker (the symbol without an exchange suffix: `MRK.US`
→ `MRK`) and the same trade date. Its market is taken from, in this order:

1. the market given for the run (*Market* field / `--market NYSE`);
2. the symbol table - the ticker registered in exactly one market
   (`python -m stock_dataload symbols set MRK --market NYSE`, or the *Symbols* page);
3. the market the ticker already has in the daily table;
4. `[eod_api] default_market` (for tickers the symbol table does not know).

Rows without a market are rejected (`market_unknown`). A daily row of the
same ticker and day that belongs to a **different** market is never
overwritten (`market_mismatch`); a daily row of the **same** market is
updated with the converted values (also one that came from a delivery file).
The daily validation rules then apply to the converted values
(`positive_price` - including a close of 0, which has no rate -,
`high_above_low`, `price_range`, `market_closed`, trading window).
Findings resolve themselves once the day is promoted.

```bash
python -m stock_dataload promote eod --market NYSE            # everything, market NYSE
python -m stock_dataload promote eod                          # incremental: changed EOD rows + rows not promoted yet
python -m stock_dataload promote eod --symbol MRK --from 2026-01-01 --full
python -m stock_dataload eod-fetch MRK --promote --market NYSE   # call the API, load, promote in one go
python -m stock_dataload promote weekly && python -m stock_dataload promote monthly   # then the aggregates
```

When the same ticker is staged under two symbols (`MRK` and `MRK.US`), the
most recently updated EOD row wins. Promote the same ticker from delivery
files *and* from the API only if you want the later run to win - the daily
table keeps one row per ticker and day.

## Completeness (daily → weekly / monthly)

Every *trading day* of a week / month - market open, inside the stock's
trading window, between the first and the last date loaded for the market -
must have a daily row. If one is missing the aggregate is still written but
marked **incomplete** (`Complete = 0`, `MissingDays`, `MissingDates`) and a
data error `incomplete_week` / `incomplete_month` is recorded; it resolves
itself once the gap is filled and the period promoted again.

## Browsing

*Browse* opens a table with filters (ticker, date range, incomplete only);
the same data is available as JSON under `/api/active/daily|weekly|monthly`.
The run history (`Promotion_History`) lists every run with rows read /
inserted / updated / rejected.
