# Data API

Base URL `<portal>/api/v1`. JSON unless `format=csv`.

## Authentication

`Authorization: Bearer <key>` · `X-API-Key: <key>` · `?api_key=<key>`.
Keys start with `sdl_`. 401 for a missing, unknown or revoked key or a
disabled customer.

## Metered endpoints

| Endpoint | Parameters | Result |
|----------|------------|--------|
| `GET /daily` | `ticker` (1-N symbols, comma separated or repeated; `max_tickers_per_request`), `year` (1900-2100), optional `market`, `format=json|csv` | rows per trading day of the calendar year |
| `GET /weekly` | same | rows per ISO week of the ISO year |
| `GET /monthly` | same | rows per calendar month |

Cost: `tokens_per_query` × tickers **with data**; all-or-nothing.

Response:

```json
{"kind": "daily", "year": 2023, "market": null, "tickers": ["AAPL", "X"],
 "tickers_with_data": ["AAPL"], "tickers_without_data": ["X"], "count": 250,
 "tokens_charged": 1, "tokens_remaining": 99, "records": [ ... ]}
```
Headers `X-Tokens-Charged`, `X-Tokens-Remaining`. CSV: `text/csv`,
`Content-Disposition: attachment; filename="daily_2023_AAPL.csv"`.

### Fields

| Data set | Fields |
|----------|--------|
| daily | `market, ticker, date, open, high, low, close, volume` |
| weekly | `market, ticker, week_start, week_end, iso_year, iso_week, open, high, low, close, volume, trading_days, complete` |
| monthly | `market, ticker, month_start, month_end, year, month, open, high, low, close, volume, trading_days, complete` |

Prices are floats, `volume` an integer, dates `YYYY-MM-DD`, `complete` a
boolean (false when a trading day of the period is missing in the source).

## Free endpoints

| Endpoint | Result |
|----------|--------|
| `GET /catalog` | `kinds.{daily,weekly,monthly}.{rows,tickers,first,last,years}`, `markets`, `packs`, `currency`, `tokens_per_ticker_year`, `max_tickers_per_request`, `docs` |
| `GET /tickers?kind=daily&year=2023&market=NASDAQ&q=AA&limit=500` | tickers with data and their row counts |
| `GET /balance` (key) | `tokens_remaining`, `tokens_purchased`, `tokens_used`, `requests`, `rows_delivered`, `last_used_at` |
| `GET /usage?limit=50` (key) | the key's recent requests |

## Errors

`{"error": "...", "status": 4xx, ...}` - 400 bad parameter · 401 key ·
402 not enough tokens (`tokens_required`, `tokens_remaining`,
`tickers_with_data`, `buy_tokens`) · 404 unknown data set · 405 method ·
503 data unavailable.
