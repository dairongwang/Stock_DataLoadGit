# Downloading prices from the EOD API

`eod-fetch` downloads the end-of-day prices of one or more stock symbols
from an EOD data API (by default [EODHD](https://eodhd.com)), saves each
answer as a file in a local folder and loads it into the staging table
`Stock_EOD_Stage`.

```bash
python -m stock_dataload eod-fetch MRK
# MRK: 251 record(s), 30,096 bytes (json) saved to .../data/api_downloads/MRK_20251114_101500.json
```

## What is called

With the shipped configuration the request for `MRK` is

```
https://eodhd.com/api/eod/MRK?api_token=dummy&fmt=json
```

* the **symbol** is the argument of the command (`MRK`, `AAPL.US`, `BRK-B.US` …);
* the **API address** is `[eod_api] base_url`;
* the **token** is `$STOCK_DATALOAD_EOD_API_TOKEN`, else `[eod_api] api_token`
  (the shipped value `dummy` is a placeholder - the real API answers 401
  until you configure your own token);
* the **format** is `[eod_api] fmt` (`json`; `csv` and others work too).

See [Operations › Configuration](../operations/10-configuration.md) for the
whole `[eod_api]` section.

## Examples

```bash
# several symbols at once - one failing symbol does not stop the others
python -m stock_dataload eod-fetch MRK AAPL.US MSFT.US

# CSV instead of JSON, a date range, weekly bars, newest first
python -m stock_dataload eod-fetch MRK --fmt csv --from 2025-01-01 --to 2025-06-30 --period w --order d

# another token / API address for one call, any further query parameter
python -m stock_dataload eod-fetch MRK --token YOUR_TOKEN --base-url https://eodhd.com/api/eod/ --param filter=last_close

# save somewhere else
python -m stock_dataload eod-fetch MRK --output-dir D:\downloads

# try it without network or token: the built-in stub serves the sample answer
python -m stock_dataload eod-fetch MRK --fake
```

## Where the files go

Into `[eod_api] download_dir` (default `data/api_downloads`), named by
`[eod_api] file_name` (default `{symbol}_{timestamp}.{ext}`, e.g.
`MRK_20251114_101500.json`). The body is saved exactly as the API sent it.
Nothing is saved when the API answers with an error (wrong token, unknown
symbol) or, for `fmt=json`, with something that is not JSON.

Every download and every failure is recorded in the activity log
(*Monitoring → Activity log*, component `eod_api`) - with the token hidden.

## From Python

```python
from stock_dataload.eod_api import fetch_eod

result = fetch_eod("MRK")                                   # settings from config.ini
result = fetch_eod("MRK", fmt="csv", params={"from": "2025-01-01"})
print(result.path, result.records)
print(result.stage)                  # the load into Stock_EOD_Stage (inserted / updated / unchanged)

from stock_dataload.config import load_settings
from stock_dataload.eod_stage import EodStageLoader

loader = EodStageLoader(load_settings())
print(loader.load_file("docs/SampleAPI_JasonData.txt", symbol="MRK"))
print(loader.rows("MRK", limit=3))
```

## The staging table Stock_EOD_Stage

Every JSON (or CSV) answer is loaded into the staging table
**`Stock_EOD_Stage`** right after it was saved - one row per symbol and
trading day:

| API field | Column | Type |
|-----------|--------|------|
| *(the requested symbol)* | `Symbol` (`MRK`, `MRK.US`), `Ticker` (`MRK`), `Exchange` (`US` or empty) | text |
| `date` | `TradeDate` | text `YYYY-MM-DD` |
| `open`, `high`, `low`, `close` | `Open`, `High`, `Low`, `Close` | number |
| `adjusted_close` | `AdjustedClose` | number |
| `volume` | `Volume` | whole number |

plus where the values came from: `SourceFile` (the saved answer), `LoadId`,
`LoadedAt` and `UpdatedAt`.

```bash
python -m stock_dataload eod-fetch MRK
# MRK: 251 record(s), 30,096 bytes (json) saved to .../data/api_downloads/MRK_20251114_101500.json
# MRK: 251 record(s) 2025-11-14 .. 2026-11-13, loaded into Stock_EOD_Stage - 251 inserted, 0 updated, 0 unchanged
```

* **Calling again is safe.** The API returns the whole history each time;
  dates already staged with the same values stay *unchanged*, changed values
  (typically `adjusted_close` after a dividend or split) are *updated*, new
  dates are *inserted*.
* **Invalid records** (a missing field, text where a number belongs, a
  negative volume, a date twice) are listed and recorded on *Monitoring →
  Data errors* (rule `eod_record`). By default such an answer is **not
  loaded at all**; `--skip-bad-rows` loads the valid records. An error body
  from the API (e.g. `{"message": "Invalid API token"}`) is reported as such.
* Every load appears in *Monitoring → Load history*.
* `--no-load` (or `[eod_api] load_to_stage = false`) only saves the files.

### Loading saved answers and the sample data

`eod-load` loads answers that are already on disk. The response has no symbol
field, so the symbol comes from the file name - the name `eod-fetch` gives it
(`MRK_20251114_101500.json`) or just the stock (`PFE.json`, `BRK-B.US.csv`) - or
from `--symbol`. The sample answer shipped in `docs/SampleAPI_JasonData.txt`
(251 trading days of MRK) loads like this:

```bash
python -m stock_dataload eod-load docs/SampleAPI_JasonData.txt --symbol MRK
python -m stock_dataload eod-load data/api_downloads/*.json          # symbols from the file names
```

### Into the daily table

`promote eod` moves the staged data into `Stock_Daily`, converted to adjusted
prices (rate = AdjustedClose / Close) and matched on symbol, trade date and
market - see [Active tables and promotion](40-promotion-active-tables.md):

```bash
python -m stock_dataload promote eod --market NYSE
python -m stock_dataload eod-fetch MRK --promote --market NYSE     # fetch, load and promote in one go
```

### Looking at the staged data

```bash
python -m stock_dataload eod-stage                                   # per symbol: rows, first / last date
python -m stock_dataload eod-stage --symbol MRK --limit 5            # the newest rows of MRK
python -m stock_dataload eod-stage --delete MRK                      # remove MRK again
```

or in SQL: `SELECT * FROM Stock_EOD_Stage WHERE Symbol = 'MRK' ORDER BY TradeDate`.

## Stock lists per market (exchange symbol list)

`eod-symbols-fetch` asks the API which symbols are traded on a market and
loads the answer into the staging table **`Stock_Symbol_Stage`**:

```bash
python -m stock_dataload eod-symbols-fetch NYSE
# NYSE: 5,641 record(s), 834,623 bytes (json) saved to .../data/api_downloads/NYSE_symbols_20251115_101500.json
# NYSE: 5,641 symbol(s) loaded into Stock_Symbol_Stage - 5,641 inserted, 0 updated, 0 unchanged
#   types: ETF 2,710, Common Stock 2,150, Preferred Stock 357, FUND 229, Notes 78, Warrant 61, Unit 52, ...
```

The request is `<symbol_list_url><MARKET>?api_token=<token>&fmt=json` - with
the shipped configuration `https://eodhd.com/api/exchange-symbol-list/NYSE?api_token=dummy&fmt=json`:

* the **market** is the argument (`NYSE`, `NASDAQ`, `LSE`, `XETRA` ... - several at once are fine);
* the **token** is the same as for the prices (`$STOCK_DATALOAD_EOD_API_TOKEN`, else `[eod_api] api_token`; `--token` for one call);
* the **address** is `[eod_api] symbol_list_url` (`{market}` marks the place of the market if it is not the last part);
* the **format** is `[eod_api] symbol_list_fmt` (`json`; `csv` works too - the API's own default);
* `--type common_stock` (or `preferred_stock`, `etf`, `fund`) asks for one type only, `--param NAME=VALUE` adds any other parameter.

Every record becomes one row per market and code:

| API field | Column | |
|-----------|--------|---|
| *(the requested market)* | `Market` | `NYSE` |
| `Code` | `Code` | the ticker on that market, e.g. `MRK`, `BRK-B` |
| `Name` | `Name` | e.g. `Merck & Company Inc` |
| `Country`, `Currency` | `Country`, `Currency` | |
| `Exchange` | `Exchange` | the venue - a NYSE list also holds `NYSE ARCA` (ETFs) and `NYSE MKT` |
| `Type` | `Type` | `Common Stock`, `ETF`, `Preferred Stock`, `FUND`, `Notes`, `Warrant`, `Unit`, ... |
| `Isin` | `Isin` | may be empty |

plus `SourceFile`, `LoadId`, `FirstSeenAt` (first list with the code),
`LastSeenAt` (latest list with it) and `UpdatedAt`. Values are kept as
delivered; only leading / trailing blanks are trimmed.

* **Fetching again is safe**: new codes are inserted, changed names / types
  updated, the rest only get a new `LastSeenAt`. Codes missing from a newer
  list are **reported, not deleted** ("N code(s) of earlier lists are not in
  this one") - their `LastSeenAt` stays at the last list that had them.
* Invalid records (no code, blanks in the code, a code twice) go to
  *Monitoring → Data errors* (rule `symbol_record`); such a list is not
  loaded unless `--skip-bad-rows`.

### The NYSE sample list

`docs/SampleAPI_NYSE_List.json` is a real answer for NYSE (5,641 symbols). Load
a saved list with `eod-symbols-load` - the market comes from the file name
(`NYSE_List.json`, `..._NYSE_List.json`, `NYSE_symbols_<timestamp>.json`) or `--market`:

```bash
python -m stock_dataload eod-symbols-load docs/SampleAPI_NYSE_List.json     # market NYSE from the name
python -m stock_dataload eod-symbols-load D:\downloads\lse.json --market LSE
python -m stock_dataload eod-symbols-fetch NYSE --fake                       # the API stub answers with the sample
```

### Using the lists

```bash
python -m stock_dataload eod-symbols                                   # per market: codes, types, exchanges
python -m stock_dataload eod-symbols --q merck                          # code prefix or part of the name
python -m stock_dataload eod-symbols --market NYSE --type "Common Stock" --limit 100
python -m stock_dataload eod-symbols --delete NYSE
```

In SQL: `SELECT Code, Name, Type FROM Stock_Symbol_Stage WHERE Market = 'NYSE' AND Type = 'Common Stock'`.
From Python: `fetch_symbol_list("NYSE")` (`stock_dataload.eod_api`) and
`SymbolListLoader(settings).search("merck")` (`stock_dataload.eod_symbols`).

## A whole market in one run

`eod-fetch-market` fetches the prices of **every stock of a market** - one call of the same API
endpoint per stock - and loads each answer **straight into `Stock_EOD_Stage`** (no file is saved):

```bash
python -m stock_dataload eod-fetch-market NYSE                        # every common stock of NYSE (2,150)
# [   1/2150] A: 251 record(s) 2025-11-14 .. 2026-11-13, 251 inserted, 0 updated, 0 unchanged
# [   5/2150] AAM: 251 record(s) ... 251 inserted, 0 updated, 0 unchanged after 3 attempt(s)
# [   7/2150] AAP: FAILED - the API answered 404 Not Found - unknown symbol or wrong API address
# ...
# NYSE (live): 2,150 symbol(s) - 2,148 loaded (521,000 rows), 1 without data, 1 failed, 0 skipped; 2,190 API call(s)
#   - run c3e88d5c3020 completed_with_errors
python -m stock_dataload eod-fetch-market NYSE --from 2026-01-01 --promote     # a date range, then into Stock_Daily
python -m stock_dataload eod-fetch-market NYSE --types "Common Stock,ETF"      # more types (or --all-types)
python -m stock_dataload eod-fetch-market NYSE --symbols HD,JNJ,PFE            # just these
python -m stock_dataload eod-fetch-market NYSE --symbols-file my_stocks.txt     # the stocks of a text file (max 100)
python -m stock_dataload eod-fetch-market NYSE --resume                        # continue / repeat the failed ones
python -m stock_dataload eod-market-runs NYSE                                   # the runs; --run ID: the failed stocks
```

The **symbols** come from the market's list in `Stock_Symbol_Stage` (see the next section); a market
without a list - or `--refresh-list` - gets it from the exchange-symbol-list API first. Only the types of
`[eod_api] market_types` are fetched (default `Common Stock`).

**Errors and retries** (per stock - one stock failing never stops the others):

| The API answers | What happens |
|-----------------|--------------|
| a network error, a timeout, 429, 500 / 502 / 503 / 504, an unreadable or cut-off answer | the call is **repeated** up to `retries` times (default 3 - so at most 4 calls), after `retry_backoff` seconds, doubled each time (1, 2, 4 s); a `Retry-After` of the API wins |
| 404 (unknown symbol), 400 | not repeated - the stock is marked *failed* |
| records that are not valid | not repeated (a data problem) - the stock is *failed*, the records are in `data-errors`; `--skip-bad-rows` loads the valid ones |
| an empty list | the stock has *no data* (not an error) |
| 401 / 402 / 403 (token refused, quota used up) | the **run stops** - every further call would fail; fix the token, then `--resume` |
| `max_consecutive_failures` (20) stocks failed in a row | the **run stops** - the API seems to be down; `--resume` later |

### Daily limits

Four limits protect the API quota; "a day" is the local calendar date. They are fixed rules (not
settings) and apply to the command line, the dashboard and Python alike:

| Limit | Counts | Does not count |
|-------|--------|----------------|
| a **stock** is loaded at most **5 times a day** | every *successful* load of the stock into `Stock_EOD_Stage` - `eod-fetch`, `eod-load`, `eod-fetch-market`, the API loading page; `MRK` and `MRK.US` are the same stock | failed or refused loads; `eod-fetch --no-load` (only saved) |
| a **whole market** at most **3 times a day** per market | every run that takes the market's symbol list (`eod-fetch-market NYSE` without `--symbols`, the *Whole market* tab) and every one-day bulk load of the market (`eod-bulk-fetch US`, `eod-bulk-load`, the *One day, all stocks* tab), whatever its outcome, live or stub, also with `--limit` | selected stocks (also `eod-bulk-fetch --symbols`), text files, `--resume`, other markets |
| a **text file** lists at most **100** stock symbols | the different valid symbols of the file (duplicates once; invalid ones are left out and named) | - |
| loads **from a text file** at most **5 times a day** (all markets) | every run from a file (`eod-fetch-market --symbols-file`, the *From a text file* tab) | typed-in stocks, `--resume` |

What happens when a limit is reached:

| Situation | Result |
|-----------|--------|
| a run contains stocks that were loaded 5 times today | they are **skipped** - not called, status *skipped* with the error `daily limit: HD was already loaded 5 times today (at most 5 a day) - not called`; a **warning** (command line: `WARNING ...` lines and a closing `WARNING: 2 stock(s) skipped ...`; dashboard: a yellow notice before the start and warning lines in the live log; `App_Log` event `stock_load_skipped`). The rest of the run loads normally; a later `--resume` tries them again |
| **every** stock of a run reached its limit, or `eod-fetch` / `eod-load` of such a stock | **refused**: `REFUSED: nothing to load: HD, JNJ were already loaded 5 times today - a stock may be loaded at most 5 times a day. Try again tomorrow.` - nothing is called, no run is registered (no market or file load is used up) |
| the 4th whole-market load of a market | **refused**: `REFUSED: NYSE: the bulk load of all stocks of a market may run at most 3 times a day - it already ran 3 time(s) today (at 08:05, 11:30, 14:10). ...` |
| a text file with more than 100 symbols | **refused**: `REFUSED: big.txt lists 120 stock symbols - a text file may list at most 100 (split it into several files; ...)` |
| the 6th text-file load of the day | **refused**: `REFUSED: loading from a text file may run at most 5 times a day - it already ran 5 time(s) today (at ...). Try again tomorrow, or resume an unfinished run.` |

A refusal exits with code 1 and is recorded in `App_Log` as a **WARNING** (events `stock_load_refused`,
`market_run_refused`, `file_too_many_symbols`, `file_load_refused`, with who asked) - it is not an
operational error, so `App_Error` stays clean. The checks happen **before any API call**, and once more
inside the transaction that registers the run or writes the rows, so the dashboard and the command
line together cannot exceed a limit. Today's use:

```bash
python -m stock_dataload eod-limits                     # the limits, text-file / whole-market loads, stocks at their limit
python -m stock_dataload eod-limits HD JNJ --market NYSE
# HD           5/5  LIMIT REACHED - skipped / refused until tomorrow
# JNJ          2/5  3 load(s) left
python -m stock_dataload eod-market-runs NYSE           # ends with the whole-market and text-file counts of today
```

The per-stock count is kept in `Eod_Stock_Load` (one row per successful load).

Every run is registered (`Eod_Market_Run`, one row per stock in `Eod_Market_Run_Item`: status,
attempts, HTTP status, rows, error). `--resume` continues the newest unfinished run of the market (or
`--resume RUN_ID`): it calls only the stocks that are not loaded yet - also after Ctrl+C. Exit code 0
when every stock was loaded (or has no data), 1 otherwise. `--pause 0.2` waits between two stocks (the
API's rate limit; or `[eod_api] request_pause`).

### Trying it without the API

`--fake` serves invented prices per symbol (deterministic; MRK is the sample answer) and the NYSE
sample list, and `--fake-faults` simulates what can go wrong:

```bash
python -m stock_dataload eod-fetch-market NYSE --fake --limit 12 \
       --fake-faults "AAM:503x2,AAP:404,AAT:timeoutx3,AB:503x9,ABBV:empty"
python -m stock_dataload eod-fetch-market NYSE --fake --fake-faults "rate=0.05,seed=42"   # 5% of all calls fail
```

Faults: `SYMBOL:KIND[xN]` (`*` = every symbol; transient kinds happen once unless `xN`, permanent ones
always) with the kinds `500 502 503 504 429 timeout reset badjson truncated` (transient) and
`400 401 402 403 404 empty badrecord` (permanent); `rate=0.05` fails that share of all calls at random
(`seed=` makes it reproducible), `retry_after=2` is the Retry-After of the 429s.

> The fake **invents** prices. Loads from it are marked (`SourceFile` `fake-api/...`), and a fake run
> refuses to write into a `Stock_EOD_Stage` that holds real data - use a test database
> (`--config test.ini`) or `--allow-fake-data`.

From Python: `fetch_market("NYSE")` / `MarketFetcher(settings).fetch_market("NYSE", symbols=[...],
params={"from": "2026-01-01"}, resume=True)` (`stock_dataload.eod_market`).

## One day of every stock (bulk)

The EODHD **bulk last day** endpoint answers one trading day of **every stock of a market in one call**
(instead of one call per stock):

```
https://eodhd.com/api/eod-bulk-last-day/US?api_token=YOUR_API_TOKEN&fmt=json&date=2026-10-12
```

```json
[{"code": "MSFT", "exchange_short_name": "US", "date": "2026-08-18", "open": 481.54, "high": 484.27,
  "low": 477.15, "close": 481.63, "adjusted_close": 481.63, "volume": 24059700}, ...]
```

```bash
python -m stock_dataload eod-bulk-fetch US --date 2026-10-12            # call, save, load into Stock_EOD_Stage
python -m stock_dataload eod-bulk-fetch US                              # the API's last trading day
python -m stock_dataload eod-bulk-fetch US --date 2026-10-12 --fmt csv --promote
python -m stock_dataload eod-bulk-fetch US --date 2026-10-12 --symbols MSFT,HD    # only these stocks
python -m stock_dataload eod-bulk-fetch US --date 2026-10-12 --fake     # the built-in stub: no network, no token
python -m stock_dataload eod-bulk-load docs/SampleAPI_BulkLastDay_US.json --market US   # a saved answer
# GET https://eodhd.com/api/eod-bulk-last-day/US?api_token=***&fmt=json&date=2026-10-12
# answer received: 950,021 bytes after 1 call(s)
# saved to data/api_downloads/US_bulk_2026-10-12_20261117_101500.json
# US 2026-10-12: 5,641 record(s), 5,641 stock(s) loaded into Stock_EOD_Stage - 5,641 inserted, 0 updated, 0 unchanged; run 4c1420d556aa
```

From Python - the parameters are the market, the API token, the format and the date:

```python
from stock_dataload.eod_bulk import fetch_bulk_last_day

result = fetch_bulk_last_day("US", api_token="dummy-replace-with-your-token", fmt="json", date="2026-10-12")
print(result)                 # what was loaded; result.warnings(), result.loaded, result.rejected, result.run_id
result = fetch_bulk_last_day("US", fmt="csv", date="2026-10-12", fake=True)   # against the stub
```

* **Parameters**: the **market** (exchange code; `US` = every US exchange), the **API token** (default
  `$STOCK_DATALOAD_EOD_API_TOKEN`, then `[eod_api] api_token`), the **format** (`json` or `csv`; default
  `[eod_api] bulk_fmt`) and the **date** (`YYYY-MM-DD`; empty = the API's last trading day). Optional:
  `symbols` (only these stocks), `force`, `save` / `load`, `skip_bad_rows`, `retries`. The address is
  `[eod_api] bulk_url` (the market is appended, or replaces `{market}`).
* **Where it goes**: the same staging table `Stock_EOD_Stage`, one row per stock and day with the
  symbol `CODE.EXCHANGE` (`MSFT.US`: ticker `MSFT`, exchange `US`) - the notation of the single-stock
  calls, so `promote eod` treats both alike (one daily row per ticker and day; the newest staging row
  wins). The whole answer is one transaction with one `LoadId`; `--promote` promotes exactly that load.
  The answer is saved in `[eod_api] download_dir` (`bulk_file_name`, default
  `{market}_bulk_{day}_{timestamp}.{ext}`; `--no-save` keeps no file).
* **Markets of the stocks**: for `US` the promotion takes each stock's market from `Stock_Symbol`
  (`symbols set`), the daily table, or `[eod_api] default_market`; stocks with none are rejected as
  `market_unknown` (see *data-errors*). For another exchange (`LSE` ...) that code is the market.
* **Checks before the call** (a bulk call costs many API credits): a date that is not a date, lies in
  the future, or is a day the market does not trade (weekends; US holidays for US markets; `[quality]
  extra_holidays`) is refused - `--force` calls anyway (the API then answers an empty list: *no data*).
* **Invalid records** (no / bad code, a field missing or not a number, a stock twice) are rejected into
  `Data_Error` (rule `eod_record`) and the valid ones loaded; `--strict` loads nothing instead.
* **At most 3 calls for the same market and day**: the bulk endpoint is called at most **3 times for
  the same market and the same trading day** (the `date` asked for - a call without a date counts for
  the latest trading day until the answer tells its day). Every call counts: also with `--symbols`,
  save-only calls and failed ones (retries within one call do not); loading a saved answer
  (`eod-bulk-load`) is no call. A 4th call is refused before anything is requested:
  `REFUSED: US 2026-10-12: the bulk endpoint may be called at most 3 times for the same market and day -
  it was already called 3 time(s) for it (at ...)` (`App_Log` event `bulk_call_refused`). The dashboard's
  bulk tab shows the count of the chosen exchange and day (*US 2026-10-12: 1 of 3 used*) and blocks
  the start at 3; `GET /api/api-load/bulk-quota?market=US&date=2026-10-12` returns it as JSON.
* **Daily limits**: a bulk load of a whole market **is a whole-market load** - at most 3 a day per
  market, together with `eod-fetch-market` runs (with `--symbols` it is none). Every stock loaded counts
  as a load of that stock (5 a day); stocks at their limit are **skipped** with a warning. A saved answer
  loaded with `eod-bulk-load` counts the same way.
* **Errors**: network problems, timeouts, 429 and 5xx are repeated (`[eod_api] retries`); 401 / 402 / 403
  are not. A failed call is registered as an *aborted* run (it still counts against the 3 a day) and
  reported as `ERROR: ...` (exit code 1). A bulk run is one call - it is not resumed; start it again.
* **The stub** (`--fake`, `fake=True`, `[eod_api] gateway = fake`): for `US` / `NYSE` one record per
  symbol of the NYSE sample list with the same invented prices as its single-stock calls (MRK: the real
  sample), `[]` for weekends / holidays / days outside the sample year and other markets;
  `--fake-faults "US:503x2"` simulates failures. `docs/SampleAPI_BulkLastDay_US.json` is a small saved
  answer of real records only (MSFT: the API's documentation example; MRK: the real sample answer).

### Daily schedule

The one-day bulk load can run **automatically once per trading day at a set time** - on the API
loading page, card **Daily schedule**, or on the command line:

```bash
python -m stock_dataload eod-bulk-schedule set US --time 18:30            # every trading day at 18:30, that day's data
python -m stock_dataload eod-bulk-schedule set US --time 07:00 --previous-day   # mornings: the trading day before
python -m stock_dataload eod-bulk-schedule list                           # next / last run, status
python -m stock_dataload eod-bulk-schedule disable US | enable US | delete US | run US
python -m stock_dataload eod-bulk-schedule run-due [--loop]               # run what is due (cron / a service)
```

* **One schedule per exchange**: the time (server local time, 24-hour clock), which day to load -
  *the same trading day* (run it some hours after the close, when the API has published the day) or
  *the previous trading day* - the format, optionally only some stocks, promotion (and weekly /
  monthly), strict mode, the built-in stub for trials, enabled or not. Saving the exchange again
  changes its schedule; *Edit* fills the form with it.
* **When it runs**: on every day the exchange trades (weekends, US holidays for US exchanges,
  `[quality] extra_holidays` are skipped), **once per day**, at or after its time. If the dashboard
  was not running at that time, the run follows as soon as it runs again that day (earlier days are
  not caught up). *Next run* shows when; *due* marks a run that is waiting.
* **Who runs it**: the dashboard process - `python -m stock_dataload dashboard` starts a scheduler
  that checks every 30 seconds (`--no-scheduler` leaves it off; the card says whether it runs). Without
  the dashboard, call `eod-bulk-schedule run-due` regularly (cron, Windows task scheduler) or keep
  `eod-bulk-schedule run-due --loop` running. A run is *claimed* in the database first, so two
  processes never run the same schedule twice a day.
* **What a run is**: an ordinary bulk load - it shows under *Current load* (title *... (bulk, scheduled
  run)*, started by *schedule N (its creator)*), is registered as a run, promoted if ticked, and every
  daily limit applies (3 calls per exchange and trading day, 3 whole-market loads a day, 5 loads per
  stock). The outcome is kept with the schedule (*Last run*, *Status*: loaded / loaded with warnings /
  refused / failed, the run) and in `App_Log` (`schedule_run`, `schedule_run_refused`,
  `schedule_run_failed`). A refused or failed run is **not repeated that day** (to spare the API); a
  run that finds another load running waits for the next check.
* **Token**: a live schedule with the placeholder token `dummy` is refused without calling the API -
  the page warns when it is saved.
* **Run now** starts the schedule's load at once (the day's scheduled run still takes place).

## In the dashboard: the API loading page

**API loading** in the dashboard's menu (http://127.0.0.1:8050/api-load) does the same as
`eod-fetch-market` without the command line: one API call per stock, every answer straight into
`Stock_EOD_Stage`, the same retries, the same run registry - and, if ticked, the promotion into
`Stock_Daily` (and `Stock_Weekly` / `Stock_Monthly`) afterwards. Everybody can watch; starting,
stopping and resuming needs the **operator** role.

Choose **what** to load with the three tabs:

| Tab | Stocks | Example |
|-----|--------|---------|
| **Selected stocks** | typed in, separated by commas (line breaks and blanks work too) | `HD, JNJ, JPM, PFE` |
| **Whole market** | every stock of the market's list in `Stock_Symbol_Stage`, by symbol type - the boxes show how many symbols each type has; *only the first N* tries a few first | NYSE, *Common Stock* = 2,150 calls |
| **One day, all stocks** | every stock of an exchange (`US` = all US exchanges) for one trading day in **one** API call (`eod-bulk-last-day`); optionally only some stocks; *call the API even if the exchange does not trade that day*, *load nothing if a record is invalid*; at most 3 calls per exchange and day (the count is shown); the **Daily schedule** card runs it every trading day at a set time | `US`, 2026-10-12 = thousands of stocks, 1 call |
| **From a text file** | a `.txt` / `.csv` file (at most 1 MB, at most **100** symbols; at most **5** such loads a day) with the symbols separated by commas | `HD, JNJ, JPM, PFE` |

and for all three:

* **Market** - the market the run is registered under and the stocks are promoted into (the list
  shows the markets with a symbol list and its size; *another market ...* takes any code - a market
  without a list gets its list from the API first);
* **Dates** - `from` / `to` (`YYYY-MM-DD`; empty = the whole history the API has);
* **Afterwards** - *promote* (`promote eod` for the loaded stocks only) and *then weekly / monthly*;
* **Invalid records** - load the valid records of an answer that has invalid ones (default: such a
  stock is *failed* and the records are on *Monitoring → Data errors*).

Symbols that are not valid (e.g. `BRK/B`) are left out and named in the message; duplicates are
called once.

**While it runs** the *Current load* card shows a progress bar, the stocks done / total, loaded,
rows, without data, failed, skipped, the API calls and a live log (refreshed every 1.5 s - also in
another browser or after closing the page; the load runs on the server). One load at a time:
*Start the load* is disabled while one runs. **Stop after the current stock** ends the run as
*interrupted*; what was loaded up to then is promoted (if ticked).

**Daily limits** (see [Daily limits](#daily-limits)) - the page shows them and the server enforces
them even if the page is bypassed:

* the strip at the top: *Whole-market loads today* per market (e.g. *NYSE 2/3*), *Text-file loads
  today* (*2/5*) and how many *stocks are at their daily limit*;
* **Whole market**: the count of the chosen market; at 3 a red notice *Daily limit reached ...* and
  *Start the load* is disabled for that market;
* **From a text file**: *at most 100 stock symbols per file and 5 text-file loads a day - 2 of 5 used
  today*. The chosen file is read **in the browser** and its symbols counted (*= 120 stock symbol(s) in
  big.txt*): more than 100 gives the red notice *Too many stock symbols* and blocks the start; after the
  5th text-file load of the day the tab is blocked (*Daily limit reached ...*);
* **Selected stocks** and **From a text file**: stocks that were loaded 5 times today are named in a
  yellow notice (*Daily limit per stock - 2 stock(s) will be skipped (not called): HD, JNJ*) - the load
  can start, those stocks are skipped; when it is **every** stock, the notice turns red (*Nothing to
  load ...*) and the start is blocked;
* after the start the message says which allowed load it is (*Started: ... - text-file load 3 of 5
  today*), a yellow message names the stocks that will be skipped, and the live log shows them as
  warnings. A refused load says *Not started - ...* with the reason.

`GET /api/api-load/quota?market=NYSE` and `GET /api/api-load/limits?symbols=HD,JNJ&market=NYSE` return
the counts as JSON. The **Kind** column of the runs table says what a run loaded: *whole market*,
*selected stocks* or *text file*.

**Runs** lists the runs of the page and of `eod-fetch-market` (newest first). A run that did not
complete has a **Resume** button: it calls only the stocks that are not loaded yet (the failed and
the pending ones) and promotes them. The run id opens the **run page**: parameters, the message,
and every stock with its status (filter: *loaded*, *failed*, *pending* ...), attempts, the HTTP status
of the last failure, the records received / inserted / updated / rejected, the first and last date
and the error.

The token is never shown: the strip at the top says where it comes from (`$STOCK_DATALOAD_EOD_API_TOKEN`
or `config.ini`). While it is missing or still the placeholder `dummy` (which the live API refuses), a
live load - or the resume of a live run - is **not started**; the message says what to set. A token
the API refuses later (401 / 402 / 403) stops the run at that stock - resume it once the token is fixed.

**Testing without the API**: open *Testing without the API (built-in stub)* and tick *use the
built-in stub* - invented prices, no network, no token; *Simulated failures* takes the same faults as
`--fake-faults` (e.g. `HD:503x2, JNJ:404, rate=0.05`). The stub refuses to write into a staging table
that holds real API data unless *allow writing invented prices* is ticked - don't, on a production
database.

The JSON behind the page: `GET /api/api-load` (the current load with progress and log, `?log=N`
lines) and `GET /api/api-load/runs` (`?market=NYSE&limit=20`).
