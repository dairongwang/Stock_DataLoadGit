-- The ledger of the daily limit per stock (load_limits.py; created automatically by the loads /
-- `python -m stock_dataload init-db`; kept here for reference).
--
-- Eod_Stock_Load  one row per SUCCESSFUL load of a stock into Stock_EOD_Stage (eod-fetch, eod-load,
--                 eod-fetch-market, the dashboard's API loading page), written in the load's transaction.
--                 A stock (Ticker: MRK and MRK.US are the same stock) is loaded at most 5 times a day
--                 (local date of LoadedAt); a 6th load is refused, a run skips the stock.

CREATE TABLE IF NOT EXISTS "Eod_Stock_Load" (
    "StockLoadId"  INTEGER PRIMARY KEY AUTOINCREMENT,
    "Ticker"       TEXT NOT NULL,              -- the stock: MRK (also for MRK.US)
    "Symbol"       TEXT NOT NULL,              -- as loaded: MRK, MRK.US
    "LoadId"       TEXT NOT NULL,              -- the load (Stock_EOD_Stage.LoadId)
    "SourceFile"   TEXT,                       -- api/NYSE/MRK, MRK_20251114_101500.json ...
    "Records"      INTEGER NOT NULL DEFAULT 0, -- rows loaded (inserted + updated + unchanged)
    "LoadedAt"     TEXT NOT NULL               -- local time; the day counts against the limit
);
CREATE INDEX IF NOT EXISTS "IX_Eod_Stock_Load_Ticker_LoadedAt" ON "Eod_Stock_Load" ("Ticker", "LoadedAt");
