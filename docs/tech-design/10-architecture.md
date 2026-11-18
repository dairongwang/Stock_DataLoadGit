# Architecture

## Processes

```
 python -m stock_dataload <command>      CLI: load / convert / promote / calendar / symbols / users / customers / db / eod-fetch
 python -m stock_dataload dashboard      Flask app, 127.0.0.1:8050  - internal, staff accounts (App_User); runs the daily bulk schedules
 python -m stock_dataload portal         Flask app, 127.0.0.1:8060  - public, customer accounts (Api_Customer)
 python -m stock_chart (chart_dashboard/) Flask app, 127.0.0.1:8070  - SEPARATE application, reads Stock_Daily / Weekly / Monthly read-only,
                                                                     keeps its stock lists in Chart_List / Chart_List_Item
                      │                        │                              │
                      └────────────────────────┴──────── one SQLite file ─────┘   data/stock_trade.db (WAL mode)
```

All three are thin front ends over the same library classes; anything the
dashboard does can be scripted (`StageLoader`, `PromotionService`,
`CustomerStore`, …). The two web apps are separate Flask applications with
separate session cookies so the portal can be published on the internet
while the dashboard stays inside.

The chart dashboard in `chart_dashboard/` is a separate application (own
package `stock_chart`, config `chart.ini`, requirements, tests, README). It
does not import `stock_dataload`; its only interface is the data contract -
read-only access to `Stock_Daily` (`Ticker`, `TradeDate`, `Market`, `Open`,
`High`, `Low`, `Close`, `Volume`) and, for the weekly / monthly candles, to
`Stock_Weekly` / `Stock_Monthly` (`WeekStart` / `MonthStart`, the same price
columns, `TradingDays`, `FirstTradeDate`, `LastTradeDate`, `Complete`) - when
those are missing or older than the daily rows, the chart aggregates the daily
rows itself (same rules as `promote weekly` / `promote monthly`). Changing these
tables' columns therefore needs a matching change in
`chart_dashboard/stock_chart/repository.py`. Its own stock lists (watchlist,
Selected, screen lists) are the only thing it writes: the tables `Chart_List` /
`Chart_List_Item`, in the same database unless `[lists] sqlite_path` names
another file; the pipeline never touches them.

## Packages and modules

| Module | Responsibility |
|--------|----------------|
| `config` | `Settings` dataclass ↔ `config.ini` (load, validate, save) |
| `formats`, `file_formats`, `readers`, `converter` | the default format, custom formats (`formats.json`), csv / text / excel / json / xml readers, conversion into the loading folder |
| `loader`, `db` | loading folder → `Stock_Stage`, `Load_History`; bulk folder loads |
| `quality`, `correction`, `audit` | data-quality strategies → `Data_Error`; corrections → `Data_Correction`; `App_Log` / `App_Error` |
| `trading_calendar`, `market_calendar`, `symbols` | `Market` / `No_Trading_Day`; US holiday rules; `Stock_Symbol` |
| `promotion` | the three promotion procedures (set-based SQL in one transaction each) and the active tables |
| `auth` | staff users, roles, PBKDF2 password hashing, self-registration, external (OAuth) users |
| `customers` | portal customers, API keys (hashed) with token balances, orders, usage ledger, roles |
| `accounts` | 2FA (TOTP / e-mail / SMS), one-time codes, recovery codes, e-mail verification, reset tokens |
| `oauth` | Google / Apple / Facebook providers, signed state, identities, fake identity gateway |
| `payments` | Stripe Checkout, PayPal Checkout, fake gateway |
| `messaging` | e-mail / SMS senders: console + memory stubs, SMTP, Twilio |
| `httpclient` | the JSON-over-HTTP transport the providers share |
| `eod_api` | downloads end-of-day prices from the EOD API (`[eod_api]`) into a local folder |
| `eod_market` | a whole market from the EOD API (at most 3 whole-market runs per market and day - `MAX_MARKET_LOADS_PER_DAY`): one call per stock of the symbol list, straight into `Stock_EOD_Stage`, retries, run registry + resume |
| `eod_bulk` | one trading day of every stock of a market in ONE call (`eod-bulk-last-day`): request from market / token / format / date, JSON or CSV, validation, one transaction into `Stock_EOD_Stage` (`CODE.EXCHANGE`), the run registry (`source = bulk`), the daily limits |
| `bulk_schedule` | the daily schedules of the one-day bulk load (`Eod_Bulk_Schedule`): next run on trading days, the atomic once-per-day claim, `BulkScheduler` (a thread of the dashboard / `eod-bulk-schedule run-due`) running them through `ApiLoadManager` |
| `api_jobs` | the dashboard's *API loading* page: runs `eod_market` loads in a background thread (one at a time), live progress + log for `/api/api-load`, stop after the current stock, resume, promotion afterwards |
| `load_limits` | the daily limits of the EOD API loads: a stock 5 loads a day (ledger `Eod_Stock_Load`, checked in the load transaction), a text file 100 symbols and 5 loads a day; the errors (`LoadLimitError` ...) and the audit of refusals |
| `eod_symbols` | the staging table of the exchange symbol lists (`Stock_Symbol_Stage`): validate, upsert per market and code |
| `eod_stage` | the staging table of the API data (`Stock_EOD_Stage`): validate, type and upsert the records |
| `dataapi` | queries behind the data API, public field names, catalogue |
| `docs` | this documentation: markdown tree and renderer |
| `dashboard/`, `portal/` | the Flask apps (routes + templates + css) |
| `cli` | argument parsing and the command handlers |

## Design principles

* **Standard library first.** Only Flask (web) and openpyxl (Excel) are
  required; hashing, TOTP, HTTP clients, markdown rendering are stdlib.
* **Everything external is replaceable.** Payments, identity providers,
  e-mail and SMS go through small interfaces with in-process stubs, so the
  whole system runs and is tested without credentials or network
  ([External services and stubs](70-external-services-and-stubs.md)).
* **One copy, one transaction.** A file is loaded in one transaction that
  replaces its previous rows; a promotion is one transaction; token charges
  are one atomic statement.
* **Nothing secret in the repository.** Passwords, API keys, codes and reset
  tokens are stored as hashes; secrets come from environment variables or
  a git-ignored file; the session secret is generated on first start.
* **Audit everything.** Loads, promotions, corrections, sign-ins, 2FA
  changes, orders and grants are written to `App_Log` with details.

## Request flow of the two web apps

Both apps are built by a `create_app(settings)` factory holding a *state*
object (settings + service instances). A `before_request` hook enforces
authentication (dashboard: every page except the public ones; portal:
only the account / admin pages) and, on the portal, CSRF tokens for every
form post (except the OAuth callback, which is protected by the signed
state). Roles are checked per endpoint (`required_role()` on the dashboard,
`admin_required` on the portal).
