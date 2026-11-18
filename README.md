# Stock_DataLoad

Load stock trading data into a SQLite database, check and promote it, and sell it through a
token-metered data API. A Python port of the SSIS package `Stock_DataLoad/LoadStage.dtsx`, plus three
web front ends:

| Part | Start | Address | For |
|------|-------|---------|-----|
| **Pipeline** (CLI) | `python -m stock_dataload <command>` | - | convert / load files, fetch prices from the EOD API, promote, housekeeping |
| **Dashboard** | `python -m stock_dataload dashboard` | http://127.0.0.1:8050 | staff: pipeline, API loading, active tables, calendar, symbols, monitoring, users |
| **Portal** | `python -m stock_dataload portal` | http://127.0.0.1:8060 | customers: token shop, accounts, the data API (`/api/v1/...`) |
| **Chart dashboard** | `cd chart_dashboard && python -m stock_chart` | http://127.0.0.1:8070 | candlestick charts of the daily / weekly / monthly tables ([its README](chart_dashboard/README.md)) |

All of them share one database file, `data/stock_trade.db`.

```
 source files (csv/text/xlsx/json/xml) ─► convert ─► loading folder ─► load ─► Stock_Stage ─┐
 EOD API (eodhd.com or the built-in stub) ─► eod-fetch / eod-fetch-market ─► Stock_EOD_Stage ─┤
                                                                                               ▼
                             promote daily / eod ─► Stock_Daily ─► promote weekly / monthly ─► Stock_Weekly / Stock_Monthly
                                                         │
                                                         └─► portal data API, chart dashboard
```

The full documentation (user manual, portal guide, technical design, operations, reference) is in
[`docs/`](docs/index.md) and built into the dashboard (**Docs** tab).

---

## 1. Install

Python 3.12 is tested (the Visual Studio project targets 3.14). Use the **same interpreter** to install
and to run:

```bash
cd Stock_DataLoadGit
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt                       # flask, openpyxl, coverage
python -m pip install -r chart_dashboard/requirements.txt       # the chart dashboard (flask only)
# optional: python -m pip install -r requirements-dev.txt && python -m playwright install chromium
#           (real-browser tests, QR codes when enrolling an authenticator app)
```

## 2. Run

```bash
python -m stock_dataload init-db       # creates any missing table (safe on an existing database)
python -m stock_dataload dashboard     # http://127.0.0.1:8050 (also runs the daily bulk schedules)
python -m stock_dataload portal        # http://127.0.0.1:8060   (second terminal)
cd chart_dashboard && python -m stock_chart                      # http://127.0.0.1:8070   (third terminal)
```

### Signing in to the dashboard

* **Empty user table** (a new database): the dashboard creates the administrator `admin` at the first
  start and prints a random **one-time password to the console**; it must be changed at the first
  sign-in. `$STOCK_DATALOAD_ADMIN_PASSWORD` sets it instead.
* **The existing database** already has its user accounts - sign in with yours. To try the
  application locally without them, add a throw-away administrator (dummy credentials - choose your
  own password for anything real; without `--password` the command prompts for it):

  ```bash
  python -m stock_dataload user add dummy_admin --role admin --password Dummy-Admin-Pass-1
  python -m stock_dataload user delete dummy_admin          # afterwards
  ```

* E-mail and SMS are **stubs** by default (`[mail] provider = console`, `[sms] provider = console`):
  verification codes, sign-in codes and password-reset links are **printed to the server console**,
  nothing is sent. For a phone number in tests use a dummy one such as `+1 555 010 1234`.

### Out of the box nothing real is called or charged

| Service | Default | Real one |
|---------|---------|----------|
| Payments (portal) | `[portal] payment_gateway = fake` - built-in Stripe / PayPal stub pages with *Pay* / *Cancel*; **no card data is asked for** | `live` + the secrets below |
| Sign in with Google / Apple / Facebook | `[oauth] gateway = fake` - a built-in consent page | `live` + client ids / secrets |
| E-mail / SMS | console stubs | `smtp` / `twilio` |
| EOD price API | `[eod_api] gateway = live`, but the token is the placeholder **`dummy`**, which the live API refuses - the dashboard then refuses live loads and says why | set `$STOCK_DATALOAD_EOD_API_TOKEN`; or use the built-in stub (`--fake`, or *use the built-in stub* on the API loading page, or `gateway = fake`) |

If you test with Stripe's own **test mode**, use Stripe's test card `4242 4242 4242 4242` - never a real card.

## 3. Configuration and secrets

Settings are in [`config.ini`](config.ini) (also editable on the dashboard's **Settings** page, admin
only); relative paths are resolved against the folder of the file; `--config FILE` or
`$STOCK_DATALOAD_CONFIG` selects another file. **No real secret is stored in the repository**: every
secret in `config.ini` is empty or the placeholder `dummy`, and each one can come from an environment
variable instead (the variable wins):

| Variable | Replaces |
|----------|----------|
| `STOCK_DATALOAD_SECRET` | the session-signing secret (otherwise generated once into the git-ignored `data/.secret_key`) |
| `STOCK_DATALOAD_EOD_API_TOKEN` | `[eod_api] api_token` (placeholder `dummy`) |
| `STOCK_DATALOAD_STRIPE_SECRET_KEY`, `STOCK_DATALOAD_PAYPAL_CLIENT_SECRET` | the `[portal]` payment secrets |
| `STOCK_DATALOAD_GOOGLE_CLIENT_SECRET`, `STOCK_DATALOAD_FACEBOOK_APP_SECRET`, `STOCK_DATALOAD_APPLE_CLIENT_SECRET` | the `[oauth]` secrets |
| `STOCK_DATALOAD_SMTP_PASSWORD`, `STOCK_DATALOAD_TWILIO_AUTH_TOKEN` | `[mail]` / `[sms]` secrets |
| `STOCK_DATALOAD_ADMIN_PASSWORD` | the one-time password of the initial `admin` |
| `STOCK_DATALOAD_PASSWORD` | the password for `user add` / `user passwd` without a prompt |

Example for a shell session (dummy values - put your own in):

```bash
export STOCK_DATALOAD_EOD_API_TOKEN=dummy-replace-with-your-eodhd-token
export STOCK_DATALOAD_SECRET=dummy-replace-with-a-long-random-string
```

Passwords, API keys, one-time codes and reset tokens are stored **only as hashes** (PBKDF2-SHA256 for
passwords, SHA-256 for keys and codes). Details: [Accounts and security](docs/tech-design/40-accounts-and-security.md),
[Configuration](docs/operations/10-configuration.md).

## 4. One day of every stock of a market (bulk)

The function `fetch_bulk_last_day` ([`stock_dataload/eod_bulk.py`](stock_dataload/eod_bulk.py)) gets one
trading day of **all stocks of a market** in a single call of the EODHD *bulk last day* endpoint and loads
them into the same staging table as every other EOD load, `Stock_EOD_Stage`:

```
https://eodhd.com/api/eod-bulk-last-day/US?api_token=YOUR_API_TOKEN&fmt=json&date=2026-10-12
```

```python
from stock_dataload.eod_bulk import fetch_bulk_last_day

# parameters: market (US = every US exchange), API token, format (json | csv), date (YYYY-MM-DD; None = last trading day)
result = fetch_bulk_last_day("US", api_token="dummy-replace-with-your-token", fmt="json", date="2026-10-12")
print(result)             # US 2026-10-12: 5,641 record(s), 5,641 stock(s) loaded into Stock_EOD_Stage - ...
print(result.warnings())  # stocks skipped (daily limit), rejected records, no data ...

result = fetch_bulk_last_day("US", fmt="csv", date="2026-10-12", fake=True)   # the built-in stub - no network
```

```bash
python -m stock_dataload eod-bulk-fetch US --date 2026-10-12 [--fmt csv] [--token T] [--promote]
python -m stock_dataload eod-bulk-fetch US --date 2026-10-12 --fake               # the stub
python -m stock_dataload eod-bulk-load docs/SampleAPI_BulkLastDay_US.json --market US   # a saved answer
```

and on the dashboard: *API loading* → tab **One day, all stocks**.

* Every record `{"code": "MSFT", "exchange_short_name": "US", "date": ..., "open", "high", "low", "close",
  "adjusted_close", "volume"}` (or the CSV `Code,Ex,Date,Open,High,Low,Close,Adjusted_close,Volume`)
  becomes one row with the symbol `MSFT.US` (ticker `MSFT`, exchange `US`) - upserted per stock and day,
  the whole answer in one transaction with one `LoadId`; the answer is also saved in `data/api_downloads/`.
* Checked **before the call**: the date (a real date, not in the future, a trading day - weekends and US
  holidays are refused unless `--force`), the token, and the daily limits.
* **At most 3 calls for the same market and day**: the bulk endpoint is called at most 3 times for the
  same market and trading day (the `date` asked for; without a date: the latest trading day, then the
  day of the answer). Every call counts - also with `--symbols`, save-only and failed calls; a 4th is
  refused before anything is requested (`REFUSED: US 2026-10-12: the bulk endpoint may be called at most
  3 times for the same market and day ...`, `App_Log` `bulk_call_refused`); the bulk tab shows the count
  (*US 2026-10-12: 1 of 3 used*) and blocks the start at 3.
* A bulk load of a market **is a whole-market load** (at most 3 a day per market) and counts one load per
  stock (5 a day - stocks at their limit are skipped with a warning). Invalid records are rejected into
  `Data_Error` and the rest loaded (`--strict`: nothing). Failed calls are retried like every EOD call.
* **Stub** for validations (`--fake`, `fake=True` or `[eod_api] gateway = fake`): answers `US` / `NYSE`
  with one record per symbol of the NYSE sample list (the same invented prices as the single-stock stub,
  MRK = the real sample), an empty list for days without data; `--fake-faults "US:503x2"` simulates
  failures. `docs/SampleAPI_BulkLastDay_US.json` is a small saved answer of real records only (MSFT = the
  API's documentation example, MRK = the real sample answer) for `eod-bulk-load`.
* **Daily schedule** (*API loading* → card **Daily schedule**, or `eod-bulk-schedule`): pick an exchange and
  a **time**; the load then runs **once per day on every trading day** of the exchange (weekends and
  holidays skipped) at or after that time - loading that trading day (run it after the close) or the
  previous one. The scheduler is a thread of the dashboard (`python -m stock_dataload dashboard`, checks
  every 30 s; `--no-scheduler` turns it off) - or run `python -m stock_dataload eod-bulk-schedule run-due`
  from cron. Each run is an ordinary bulk load (shown under *Current load*, all limits apply); its outcome
  is kept with the schedule (*Last run*, *Status*) and in `App_Log`; a refused / failed run is not repeated
  that day; *Run now* starts it at once.

  ```bash
  python -m stock_dataload eod-bulk-schedule set US --time 18:30          # every trading day at 18:30
  python -m stock_dataload eod-bulk-schedule list
  python -m stock_dataload eod-bulk-schedule run-due --loop               # without the dashboard
  ```
* Settings `[eod_api] bulk_url`, `bulk_fmt`, `bulk_file_name`. Details: [user manual](docs/user-manual/35-eod-api-downloads.md#one-day-of-every-stock-bulk).
  Tests: [`tests/test_eod_bulk.py`](tests/test_eod_bulk.py).

## 5. Daily load limits

Five rules limit the loads from the EOD API per **day** (local calendar date; for the bulk endpoint: per trading day asked for). They are fixed business
rules (constants in [`load_limits.py`](stock_dataload/load_limits.py) and `eod_market.py`, not settings)
and hold for the dashboard, the command line and the Python API alike:

| Rule | Counts | Does not count |
|------|--------|----------------|
| **a stock at most 5 loads a day** (`MAX_LOADS_PER_STOCK_PER_DAY`) | every *successful* load of the stock into `Stock_EOD_Stage`: `eod-fetch`, `eod-load`, `eod-fetch-market`, `eod-bulk-fetch` / `eod-bulk-load` (each stock of the answer), the API loading page (`MRK` and `MRK.US` are one stock) | failed / refused loads, `eod-fetch --no-load` |
| **the bulk endpoint at most 3 calls for the same market and trading day** (`MAX_BULK_CALLS_PER_MARKET_DAY`) | every call of `eod-bulk-last-day` for the market and the `date` (no date: the latest trading day / the day of the answer) - also with `--symbols`, save-only, failed, scheduled | loads of saved answers (`eod-bulk-load`), retries within one call |
| **a whole market at most 3 loads a day** per market (`MAX_MARKET_LOADS_PER_DAY`) | every run over the market's symbol list (*Whole market* tab, `eod-fetch-market NYSE` without `--symbols`) and every one-day bulk load (*One day, all stocks* tab, `eod-bulk-fetch US`, `eod-bulk-load`), any outcome, live or stub, also with `--limit` | selected stocks (also `eod-bulk-fetch --symbols`), text files, resume, other markets |
| **a text file at most 100 symbols** (`MAX_FILE_SYMBOLS`) | the different valid symbols of the file | invalid tokens (left out and named) |
| **text-file loads at most 5 a day** (`MAX_FILE_LOADS_PER_DAY`, all markets) | every load from a file (*From a text file* tab, `eod-fetch-market --symbols-file FILE`) | typed-in stocks, resume |

### Warnings and errors

| Situation | Dashboard (*API loading* page) | Command line | Audit trail (`App_Log`, level WARNING) |
|-----------|-------------------------------|--------------|----------------------------------------|
| some stocks of a load reached their 5 loads | **warning** (yellow) before the start: *Daily limit per stock - 2 stock(s) will be skipped (not called): HD, JNJ*; after the start a warning message and warning lines in the live log; the load runs, those stocks end as *skipped* (`daily limit: ...`) | `[3/10] WARNING HD: SKIPPED - daily limit: ...` and `WARNING: 2 stock(s) skipped ...`, exit code 0 | `stock_load_skipped` |
| every stock of a load reached its limit / `eod-fetch`, `eod-load` of such a stock | **error** (red) *Nothing to load ...*, start disabled; server: *Not started - nothing to load: ...* | `REFUSED ...`, exit code 1 | `stock_load_refused` |
| 4th call of the bulk endpoint for a market and trading day | red *Call limit reached - US 2026-10-12: ...*, start disabled; server: *Not started - US 2026-10-12: the bulk endpoint may be called at most 3 times for the same market and day ...*; a schedule records *refused* | `REFUSED: US 2026-10-12: ...`, exit code 1 | `bulk_call_refused` |
| 4th whole-market load of a market | red *Daily limit reached* notice, start disabled for that market; server: *Not started - NYSE: the bulk load of all stocks of a market may run at most 3 times a day ...* | `REFUSED: NYSE: ...`, exit code 1 | `market_run_refused` |
| a text file with more than 100 symbols | the file is read in the browser: red *Too many stock symbols - big.txt lists 120 ...*, start disabled; server: *Not started - big.txt lists 120 stock symbols - a text file may list at most 100 ...* | `REFUSED: big.txt lists 120 ...`, exit code 1 | `file_too_many_symbols` |
| 6th text-file load of the day | red *Daily limit reached - loading from a text file may run at most 5 times a day ...*, start disabled | `REFUSED: loading from a text file ...`, exit code 1 | `file_load_refused` |

A refusal happens **before any API call** and registers nothing (no market or file load is used up). It
is a warning, not an operational error - `App_Error` stays clean. Every check is repeated inside the
database transaction that registers the run / writes the rows (`BEGIN IMMEDIATE`), so the dashboard and
the command line together cannot exceed a limit even at the same moment. A stock skipped for its limit is
tried again by a later `--resume` / *Resume*.

Today's use: the status strip of the *API loading* page (*Whole-market loads today: NYSE 2/3 · Text-file
loads today: 1/5 · Stocks at their daily limit: 2*), `python -m stock_dataload eod-limits [SYMBOL ...]
[--market NYSE]`, and JSON at `/api/api-load/limits?symbols=HD,JNJ&market=NYSE` and
`/api/api-load/quota?market=NYSE`. The per-stock count is the ledger table `Eod_Stock_Load`
([`sql/create_eod_stock_load_sqlite.sql`](sql/create_eod_stock_load_sqlite.sql)).

Tests: [`tests/test_load_limits.py`](tests/test_load_limits.py), [`tests/test_market_load_limit.py`](tests/test_market_load_limit.py),
the browser flows in `tests/test_dashboard_browser.py`, requirement R6 in `tests/test_requirements.py`.

## 6. The data in this repository

`data/stock_trade.db` (git-ignored, 4.3 MB) keeps stock trade data for **PFE, HD, MRK and JNJ only**.
The rows of every other ticker (520,883 rows of 2,146 other NYSE stocks in `Stock_EOD_Stage`, JPM in the
daily / weekly / monthly tables and `Data_Error`) were deleted with the new housekeeping command, and
`data/api_downloads/JPM.json` was removed:

```bash
python -m stock_dataload db keep-tickers PFE HD MRK JNJ          # the plan: rows per table that would go
python -m stock_dataload db keep-tickers PFE HD MRK JNJ --yes    # delete them, then compact (131.8 MB -> 4.3 MB)
```

It touches `Stock_Stage`, `Stock_EOD_Stage`, `Stock_Daily`, `Stock_Weekly`, `Stock_Monthly`,
`Data_Error` and `Data_Correction`; users, customers, symbol lists, `Stock_Symbol`, the trading calendar,
the run history and the chart lists stay. Reference data that is not trade data also stays:
`docs/SampleAPI_NYSE_List.json` (the NYSE symbol list), `docs/SampleAPI_JasonData.txt` (the sample MRK
answer the stub serves) and the converter test fixtures in `samples/`.

To load more stocks again: the dashboard's *API loading* page, or
`python -m stock_dataload eod-fetch-market NYSE --symbols HD,JNJ,MRK,PFE --promote` (real token needed,
or add `--fake` for invented prices in a test database).

## 7. Tests

```bash
python -m unittest discover -s tests -t . -v                 # ~650 tests, a few minutes (browser tests skip without Chromium)
python -m unittest tests.test_load_limits -v                 # one module
python run_coverage.py --html                                # coverage report
cd chart_dashboard && python -m unittest discover -s tests -t .   # the chart dashboard (68 tests)
```

The tests need no network and no credentials (memory e-mail / SMS senders, fake payment, identity and
EOD gateways); every account, password, token and key in them is a dummy (`dummy_admin`,
`Dummy-Admin-Pass-1`, `dummy-...-token`). `tests/test_nasdaq_2023.py` runs only when the real 2023
deliveries are available (`$STOCK_DATALOAD_NASDAQ_2023` or `../NASDAQ_2023`).

## 8. Project layout

```
stock_dataload/          the package: config, readers / converter, loader, promotion, quality, calendar, symbols,
                         auth (staff users), accounts (2FA), messaging (e-mail / sms), customers, payments, oauth,
                         eod_api / eod_stage / eod_symbols / eod_market / eod_bulk (EOD API), load_limits (the daily limits),
                         api_jobs, dataapi, docs, cli
stock_dataload/dashboard the staff dashboard (Flask): app.py, templates/, static/ (api_load.js: the API loading page)
stock_dataload/portal    the public portal (Flask): app.py, templates/, static/
chart_dashboard/         the separate chart application (own README, config, tests)
config.ini, formats.json configuration and the custom file formats
sql/                     reference DDL of every table and the promotion procedures
samples/                 one NASDAQ trading day in every supported format (make_samples.py regenerates them)
docs/                    the documentation (also the dashboard's Docs tab)
data/                    database, folders of the pipeline, api_downloads/ (git-ignored content)
tests/                   the test-suite
```

## 9. What was added or changed in this revision

* **Restored missing files** so the application runs:
  `stock_dataload/auth.py` (staff users, roles, PBKDF2 hashing, bootstrap admin, session secret - compatible
  with the existing `App_User` rows and hashes), `stock_dataload/messaging.py` (console / memory stubs,
  SMTP, Twilio, `BrokenSender`), the portal pages `templates/index.html` and `templates/docs.html`,
  `docs/portal/20-using-the-api.md`, the samples `NASDAQ_20250123_reordered.txt`, `_broker.csv`,
  `_noheader.txt`, `.xlsx`, `.xml` (generated by `samples/make_samples.py`), this README, and the test
  modules other tests import or the project lists (`test_dashboard`, `test_eod_api`, `test_accounts`,
  `test_portal`, `test_oauth`, `test_config_cli`, `test_nasdaq_2023`).
* **Bulk endpoint: at most 3 calls for the same market and day** and a **daily schedule** of *One day, all
  stocks* (section 4): new `bulk_schedule.py`, table `Eod_Bulk_Schedule`, the *Daily schedule* card on
  `/api-load`, `eod-bulk-schedule`, `dashboard --no-scheduler`, `/api/api-load/bulk-quota`,
  `/api/api-load/schedules`.
* **One day of every stock of a market** (section 4): `fetch_bulk_last_day(market, api_token, fmt, date)` for
  the EODHD `eod-bulk-last-day` endpoint, loaded into `Stock_EOD_Stage`; the stub answers it; new: `eod_bulk.py`,
  `eod-bulk-fetch` / `eod-bulk-load`, the *One day, all stocks* tab, `[eod_api] bulk_url / bulk_fmt /
  bulk_file_name`, `docs/SampleAPI_BulkLastDay_US.json`.
* **Daily load limits** (section 5): a whole market at most 3 loads a day per market; a stock at most 5
  loads a day; a text file at most 100 symbols and at most 5 text-file loads a day - with warnings
  (stocks skipped) and errors (refusals) on the dashboard, the command line and in `App_Log`; new:
  `load_limits.py`, the ledger `Eod_Stock_Load`, `eod-fetch-market --symbols-file`, `eod-limits`,
  `/api/api-load/limits`, the *Kind* column of the runs table.
* **New command** `db keep-tickers`, and the database trimmed to PFE, HD, MRK, JNJ (section 6).
