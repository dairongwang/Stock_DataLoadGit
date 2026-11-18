# Documentation

Welcome to the documentation of **stock_dataload** - the stock data pipeline
(convert → load → promote), the internal **dashboard**, and the public
**portal** with its token shop and data API. The documentation is a tree:
start with the section that fits your role and drill down to the details.

| I am ... | Start here |
|----------|-----------|
| a **business user** working with deliveries, formats, promotions, the calendar or symbols on the dashboard | [User manual](user-manual/index.md) |
| a **customer** (or support) of the data API - buying tokens, calling the API, managing an account | [Portal guide](portal/index.md) |
| an **administrator** installing, configuring, publishing and maintaining the system | [Operations](operations/index.md) |
| a **developer** who wants to understand or change the code | [Technical design](tech-design/index.md) |
| looking up a command, an endpoint, a table or a configuration key | [Reference](reference/index.md) |

## The system in one picture

```
 files                          conversion            loading               database                  web
 csv / text / xlsx        ┌─► convert ──────► <ticker>,<date>,<o>,<h>, ─► load ─► Stock_Stage ─► promote ─► Stock_Daily
 json / xml (any layout) ─┘  (formats.json)      <l>,<c>,<vol>  .txt                          ┌──────────► Stock_Weekly ──► dashboard :8050 (staff)
 NASDAQ_20250123.txt  ────────────────────────── default format ────────────────────────────┘            Stock_Monthly ──► portal :8060 (customers, data API)
```

* **Convert** - any supported file type / column order becomes a default-format text file in the loading folder.
* **Load** - default-format files are loaded *as delivered* into the staging table `Stock_Stage` (one copy per file; deliveries dated on no-trading days are skipped).
* **Promote** - staged rows are validated and typed into `Stock_Daily`, then aggregated into `Stock_Weekly` / `Stock_Monthly`; problems become `Data_Error` findings.
* **Dashboard** - business users run and watch all of this, correct data, manage users.
* **Portal** - customers buy tokens (Stripe / PayPal) and fetch daily / weekly / monthly data per ticker and year through the API.

## Conventions used in these pages

* Commands are shown for the activated virtual environment: `python -m stock_dataload …`. On Windows the same commands work in Command Prompt or PowerShell.
* `[section] key` refers to `config.ini`; `$NAME` to an environment variable.
* **Stub** / **fake** means a built-in stand-in for an external service (payments, identity providers, e-mail, SMS) so that everything can run and be tested without credentials. The pages of the dashboard and the portal say when a stub is active.

## Quick links

* [Getting started](user-manual/10-getting-started.md) - install, first sign-in, your account
* [Troubleshooting](operations/50-troubleshooting.md) - "No module named coverage", "cannot sign in", codes that never arrive …
* [CLI reference](reference/10-cli.md) · [Data API reference](reference/20-api.md) · [Configuration keys](reference/40-configuration.md) · [Tables](reference/30-tables.md)
