# Testing

```bash
python -m unittest discover -s tests -t . -v      # the whole suite (about 650 tests, a few minutes)
python -m unittest tests.test_portal              # one module
python run_coverage.py [--html]                   # coverage report (installs coverage if missing)
```

## Principles

* **Unattended.** No test reads from the terminal: CLI tests patch
  `sys.stdin.isatty()` to `False` and clear `$STOCK_DATALOAD_PASSWORD`, so
  password prompts become errors the tests expect.
* **No network, no credentials.** E-mail / SMS use the `memory` senders,
  payments the fake gateway, sign-in the fake identity gateway; the live
  Stripe, PayPal, Google, Apple, Facebook, SMTP and Twilio clients are
  tested against `unittest.mock`-patched `urllib` / `smtplib`.
* **Isolated.** `tests/helpers.TempProjectTestCase` gives every test its own
  folders and SQLite file; `tests/__init__` lowers the PBKDF2 work factor.
* **Reference scripts stay in sync.** Tests compare `sql/*.sql` with the DDL
  and procedures rendered from the modules.

## Map of the test modules

| Module | Covers |
|--------|--------|
| `test_readers`, `test_converter*`, `test_formats`, `test_file_formats` | readers, conversion, formats, byte-identical round trips of the samples |
| `test_loader*`, `test_bulk_load`, `test_quality`, `test_correction`, `test_audit` | loading, tracking, data quality, corrections, audit trail |
| `test_promotion`, `test_dashboard_promotion` | the procedures, validation rules, incremental runs |
| `test_trading_calendar`, `test_market_calendar`, `test_symbols` | calendar and symbols |
| `test_auth`, `test_dashboard`, `test_dashboard_browser` | staff accounts, every dashboard page, real-browser flows (skipped without Chromium) |
| `test_portal` | token shop, receipts, data API metering, payments (fake gateway + mocked HTTP), dashboard Customers page |
| `test_accounts` | TOTP vectors, 2FA service, messaging senders (stubs, SMTP / Twilio against mocks), the `user` and `db keep-tickers` commands |
| `test_oauth` | signed state, providers against mocked HTTP, fake identity gateway, portal and dashboard sign-in flows |
| `test_docs` | the markdown renderer, the documentation tree, every page renders, internal links resolve |
| `test_config_cli`, `test_requirements`, `test_nasdaq_2023` | configuration, CLI, requirement traceability, the real 2023 deliveries (skipped without the folder) |
| `test_eod_promotion` | `promote_eod_daily`: the conversion of every sample row, market sources and matching, validation of the converted values, incremental runs, CLI and dashboard |
| `test_eod_symbols` | exchange symbol lists: the request from configuration, the NYSE sample (every value), upserts, rejected records, CSV, the fake gateway, the `eod-symbols-*` commands |
| `test_eod_stage` | the EOD staging table: the sample response, upserts, rejected records, CSV, loading after an API call, `eod-load` / `eod-stage` |
| `test_eod_market` | whole markets: the fake's per-symbol data and fault plan, one call per stock straight into the stage table (no files), retries / backoff / Retry-After, 404 / empty / bad records, stopping on 401 / 402 and on consecutive failures, resume after Ctrl+C, promotion of exactly the run, the live client against a mocked `urlopen`, the CLI |
| `test_api_load` | the dashboard's API loading: `ApiLoadManager` (selected stocks / a market / resume, promotion, stop after the current stock, one load at a time in the background, loads that cannot start, the fake guard), the page in three modes, form validation, the live-token guard, the run page, resume, roles, the JSON endpoints (browser flow in `test_dashboard_browser`) |
| `test_bulk_schedule` | daily schedules: the store, next run / due on trading days (weekends, holidays), once per day and the claim, the trade date (same / previous), the scheduler (outcomes, refusals of the token and of the call limit, a busy manager, run now, the thread), the Daily schedule card (save / edit / enable / run now / delete, roles, JSON), `eod-bulk-schedule`; the browser flow is in `test_dashboard_browser` |
| `test_eod_bulk` | one day of every stock (`eod_bulk.py`): the URL from market / token / format / date, trading-day checks, JSON / CSV answers and validation, the stub (every listed symbol, the prices of its single calls, empty days), saving and loading into `Stock_EOD_Stage`, the live client against a mocked `urlopen` (the sample answer), rejected records and strict mode, retries / 401 / bad JSON, the daily limits, promotion by load id, the background job, the dashboard tab, the CLI; the browser flow is in `test_dashboard_browser` |
| `test_load_limits` | the limits of `load_limits.py`: a stock 5 loads a day (refused in the load transaction, skipped by runs with a warning, `eod-fetch` refused before the API call, failed loads and yesterday do not count, resume retries), text files (100 symbols, 5 loads a day), `ApiLoadManager`, the API loading page (warnings, refusals, JSON), the CLI (`REFUSED` / `WARNING`, `--symbols-file`, `eod-limits`); the browser flow is in `test_dashboard_browser` |
| `test_market_load_limit` | the daily limit of whole-market loads (3 per market and day): refused before any API call, what does not count (selected stocks, resume, other markets, yesterday), the atomic re-check at registration, `ApiLoadManager`, the API loading page (count, notice, refusal, JSON), the CLI; the browser flow is in `test_dashboard_browser` |
| `test_eod_api` | EOD API client: URL building from configuration, token redaction, saving json / csv, HTTP errors (mocked `urlopen`), the fake gateway, `eod-fetch` |

## Writing a new test

Start from `TempProjectTestCase`; for web flows create the app with the
test settings (`create_portal(settings)` / `create_dashboard(settings)`),
set `mail_provider = sms_provider = "memory"`, and read codes from
`state.two_factor.email.last`. For payments drive the fake pages
(`/fake-checkout/...`) or call `state.gateway.approve(id)` directly.
