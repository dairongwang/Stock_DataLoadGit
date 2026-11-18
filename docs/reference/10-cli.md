# Command line

```
python -m stock_dataload [--config FILE] [-v | -q] COMMAND ...
```

## Pipeline

| Command | Options |
|---------|---------|
| `load [FILE ...]` | `--market M`, `--truncate`, `--skip-bad-rows`, `--keep`, `--include-no-trading-days` |
| `load-folder FOLDER` | `--pattern GLOB` (repeatable), `--recursive`, `--market M`, `--truncate`, `--move`, `--skip-bad-rows`, `--stop-on-error`, `--promote`, `--include-no-trading-days` |
| `convert SOURCE ...` | `--format NAME`, `--type auto|csv|text|excel|json|xml`, `--columns a,b,...`, `--map col=field,...`, `--delimiter`, `--no-header`, `--skip-rows N`, `--date-format`, `--encoding`, `--sheet`, `--record-path`, `--record-tag`, `--output-name`, `--overwrite | --rename`, `--move-source | --no-move`, `--skip-bad-rows` |
| `convert-folder` | `--format NAME`, `--on-conflict rename|overwrite|error`, `--keep-source` |
| `inspect SOURCE` | dry run |
| `formats` | list the saved formats |
| `promote daily|weekly|monthly|all` | `--full`, `--ticker T`, `--file F`, `--load-id ID`, `--from YYYYMMDD`, `--to YYYYMMDD` |
| `promote eod` | `Stock_EOD_Stage` → `Stock_Daily` (adjusted prices): `--market M`, `--symbol S`, `--ticker T`, `--load-id ID`, `--from`, `--to`, `--full` |
| `promotions` | `--procedure NAME`, `--limit N` |
| `status`, `logs`, `errors`, `data-errors`, `history` | `--limit N`; `logs --level L --component C`; `data-errors --file F --alerts`; `history --file F` |
| `init-db` | create all tables, generate this and next year's no-trading days |

## EOD API downloads

```
eod-fetch SYMBOL [SYMBOL ...] [--fmt json|csv|...] [--token T] [--base-url URL] [--from YYYY-MM-DD] [--to YYYY-MM-DD]
                              [--period d|w|m] [--order a|d] [--param NAME=VALUE ...] [--output-dir DIR] [--fake]
                              [--no-load] [--skip-bad-rows] [--promote [--market M]]
```
Calls `<base_url><SYMBOL>?api_token=<token>&fmt=<fmt>` for every symbol and saves each answer in
`[eod_api] download_dir`, then loads json / csv answers into `Stock_EOD_Stage` (`--no-load` only saves;
`--skip-bad-rows` loads the valid records of an answer with invalid ones). Options override `[eod_api]`
for this call; `--fake` uses the built-in stub.

```
eod-load FILE [FILE ...] [--symbol SYMBOL] [--fmt json|csv] [--skip-bad-rows] [--promote [--market M]]
eod-stage [--symbol SYMBOL [--from YYYY-MM-DD] [--to YYYY-MM-DD] [--limit N]] | --delete SYMBOL
```
```
eod-symbols-fetch MARKET [MARKET ...] [--fmt json|csv] [--token T] [--base-url URL] [--type TYPE] [--param NAME=VALUE ...]
                                      [--output-dir DIR] [--fake] [--no-load] [--skip-bad-rows]
eod-symbols-load FILE [FILE ...] [--market M] [--fmt json|csv] [--skip-bad-rows]
eod-symbols [--q TEXT] [--market M] [--type T] [--limit N] | --delete MARKET
```
`eod-symbols-fetch` calls `<symbol_list_url><MARKET>?api_token=<token>&fmt=<fmt>` per market, saves the list and loads it
into `Stock_Symbol_Stage`; `eod-symbols-load` loads saved lists (market from names like `NYSE_List.json`);
`eod-symbols` summarises / searches the staged lists.

```
eod-fetch-market MARKET [--types T1,T2 | --all-types] [--symbols A,B | --symbols-file FILE] [--limit N] [--from D] [--to D] [--period d|w|m]
                        [--param NAME=VALUE ...] [--retries N] [--backoff S] [--pause S] [--refresh-list]
                        [--resume [RUN_ID]] [--skip-bad-rows] [--promote] [--token T] [--base-url URL]
                        [--fake [--fake-faults SPEC] [--allow-fake-data]] [--summary-only]
eod-market-runs [MARKET] [--run RUN_ID [--all]] [--limit N]
eod-limits [SYMBOL ...] [--market MARKET ...]
eod-bulk-fetch [MARKET] [--date YYYY-MM-DD] [--fmt json|csv] [--token T] [--base-url URL] [--symbols A,B] [--force]
               [--strict] [--no-save] [--no-load] [--output-dir DIR] [--retries N] [--promote [--aggregates]]
               [--fake [--fake-faults SPEC] [--allow-fake-data]]
eod-bulk-load FILE [--market M] [--date D] [--fmt json|csv] [--strict] [--promote [--aggregates]]
eod-bulk-schedule list | set MARKET --time HH:MM [--previous-day] [--fmt json|csv] [--symbols A,B] [--no-promote]
                  [--aggregates] [--strict] [--fake [--allow-fake-data]] [--disabled]
                  | enable MARKET | disable MARKET | delete MARKET | run MARKET | run-due [--loop] [--interval S]
dashboard [--host H] [--port P] [--no-scheduler]
```
`eod-bulk-fetch US --date 2026-10-12` calls `<bulk_url>US?api_token=..&fmt=json&date=2026-10-12` once (default market
`US`, default date: the API's last trading day), saves the answer and loads every record into `Stock_EOD_Stage` as
`CODE.EXCHANGE` (`MSFT.US`). A weekend / holiday / future date is refused before the call (`--force` calls anyway);
invalid records are rejected into `Data_Error` (`--strict`: load nothing). It counts as a whole-market load (3 a day),
every stock as a load of that stock (5 a day; stocks at their limit are skipped with a `WARNING`), and the
endpoint is called at most 3 times for the same market and trading day. `eod-bulk-schedule` keeps one daily
schedule per exchange (run once per trading day at its time by the dashboard's scheduler, or by `run-due`). `eod-bulk-load`
loads a saved answer (e.g. `docs/SampleAPI_BulkLastDay_US.json`). See
[One day of every stock](../user-manual/35-eod-api-downloads.md#one-day-of-every-stock-bulk).
`eod-fetch-market` calls the same endpoint once per stock of the market's symbol list (`Stock_Symbol_Stage`,
fetched first when missing; types of `[eod_api] market_types`) and loads every answer straight into
`Stock_EOD_Stage` - no files. Failed calls are repeated (`--retries`, default 3); 401 / 402 / 403 or
`max_consecutive_failures` failures in a row stop the run; `--resume` continues it. A whole market (no
`--symbols`, no `--resume`) may be loaded **at most 3 times a day per market** - a 4th run is refused
before any API call (`REFUSED: ...`, exit code 1). `--symbols-file FILE` loads the symbols of a text file
(comma separated): at most **100** symbols per file and at most **5** text-file loads a day. A stock is
loaded at most **5 times a day** (`eod-fetch`, `eod-load`, `eod-fetch-market`): a run skips a stock at its
limit (`WARNING` lines, the run still succeeds), a load of only such stocks is refused. `eod-limits`
shows the limits and today's use. `--promote` runs
`promote eod --market MARKET` for exactly the loaded stocks. `eod-market-runs` lists the runs
(`Eod_Market_Run`) and, with `--run`, the failed / skipped / pending stocks of one; with a market it
also shows today's whole-market loads (`NYSE 2/3`).

`eod-load` loads saved answers (e.g. `eod-load docs/SampleAPI_JasonData.txt --symbol MRK`; the symbol is
taken from names like `MRK_20251114_101500.json` or from a file named after the stock, `PFE.json`). `eod-stage` lists the staged symbols, the rows of one
symbol, or deletes a symbol's rows.
See [Downloading prices from the EOD API](../user-manual/35-eod-api-downloads.md).

## Calendar and symbols

```
no-trading-days update [--year YYYY ...] [--from YYYY --to YYYY] [--market M ...]
no-trading-days list MARKET [--year YYYY] [--all]
no-trading-days check MARKET DATE
no-trading-days add MARKET DATE [--reason TEXT]   |   remove MARKET DATE
no-trading-days markets | add-market CODE [--name N] [--rules us|weekends] | enable CODE | disable CODE
symbols list [--ticker T] [--market M] [--missing-dates] [--suspicious] [--limit N]
symbols show TICKER [--market M] | set TICKER [--market M] [--first-trade-date D] [--last-trade-date D] [--name N]
symbols import FILE.csv [--market M] | export FILE.csv | delete TICKER [--market M]
```
(`calendar` is an alias of `no-trading-days`.)

## Web apps

```
dashboard [--host H] [--port P] [--debug]
portal    [--host H] [--port P] [--debug]
```

## Staff users

```
user add NAME [--role viewer|operator|admin] [--name "Full Name"] [--email ADDR] [--password PW] [--must-change]
user list | passwd NAME [--password PW] [--must-change] | role NAME ROLE | email NAME [ADDR] | reset-2fa NAME
user enable NAME | disable NAME | delete NAME
```
Passwords: `--password`, `$STOCK_DATALOAD_PASSWORD`, or an interactive
prompt (only when a terminal is attached).

## Portal customers

```
customers list [--q TEXT] [--limit N] | orders [--status paid|pending|failed|cancelled] | usage [--key PREFIX] | stats
customers add-admin EMAIL [--password PW] [--name N] | role EMAIL user|admin | reset-2fa EMAIL | verify EMAIL
customers set-password EMAIL [--password PW] | grant KEYPREFIX TOKENS [--note TEXT] | new-key EMAIL [--label L] [--tokens N]
customers disable-key PREFIX | enable-key PREFIX | disable EMAIL | enable EMAIL
```

## Database

```
db size
db purge-stage [--keep-days N] [--all] [--vacuum] [--dry-run]
db vacuum
db reset-data [--yes] [--keep-history]
db keep-tickers TICKER [TICKER ...] [--yes]
```
`db keep-tickers PFE HD MRK JNJ` keeps the stock data of those tickers only: it shows how many rows of
other tickers `Stock_Stage`, `Stock_EOD_Stage`, `Stock_Daily`, `Stock_Weekly`, `Stock_Monthly`,
`Data_Error` and `Data_Correction` hold, and with `--yes` deletes them and compacts the file (rows
without a ticker stay; users, customers, symbols and the calendar are untouched).

Exit codes: 0 success · 1 a file / procedure / command failed · 2 configuration error · 130 interrupted.
