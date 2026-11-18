# Data API internals

## Queries

`dataapi.DataApiService.query(kind, tickers, year, market)` selects from
the active table of *kind* with `Ticker IN (...)` and a year condition -
`TradeDate` between `YYYY-01-01` and `YYYY-12-31` (uses the index) for
daily, `ISOYear = ?` for weekly, `Year = ?` for monthly - and returns rows
grouped per ticker with **public snake_case names** only
(`PUBLIC_COLUMNS`; no load ids, stage ids or timestamps).

## Metering

The route `/api/v1/<kind>`:

1. authenticates the key (`CustomerStore.authenticate_key`: hash lookup,
   key active, customer active);
2. parses tickers (≤ `max_tickers_per_request`, upper-cased, de-duplicated)
   and the year;
3. refuses immediately (402) if the balance is below `tokens_per_query` -
   the refused request is logged in `Api_Usage`;
4. runs the query, computes the cost = tickers **with data** ×
   `tokens_per_query`;
5. charges atomically: `UPDATE Api_Key SET Balance = Balance - ?, TokensUsed = … WHERE ApiKeyId = ? AND Active = 1 AND Balance >= ?`
   inside `BEGIN IMMEDIATE`; `rowcount == 0` means "not enough" → 402 and
   nothing delivered; the usage row records tickers, year, tokens and rows;
6. answers JSON (or CSV) with `tokens_charged` / `tokens_remaining`.

A `CHECK (Balance >= 0)` constraint guards the ledger even against bugs.

## Catalogue

`/api/v1/catalog` scans the three tables for row / ticker counts, years and
markets - cached for five minutes (`CATALOG_TTL`) because the data only
changes when a promotion runs.

## Keys

Keys are `sdl_` + 32 URL-safe random characters; only the SHA-256 hash and
a 12-character display prefix are stored. Rotation replaces hash and prefix
and keeps balance, label and history; revocation sets `Active = 0`.
