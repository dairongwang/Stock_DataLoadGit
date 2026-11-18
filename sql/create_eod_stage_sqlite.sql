-- Staging table of the EOD API data (created automatically by `python -m stock_dataload init-db`,
-- `eod-fetch` and `eod-load`; generated from stock_dataload/eod_stage.py - kept here for reference).
--
-- One row per symbol and trade date of the API response
--   {"date":"2025-11-14","open":92.2,"high":95.08,"low":91.49,"close":92.92,"adjusted_close":90.2693,"volume":19632000}
-- date -> TradeDate, open -> Open, high -> High, low -> Low, close -> Close,
-- adjusted_close -> AdjustedClose, volume -> Volume; Symbol / Ticker / Exchange come from the request
-- (MRK.US -> Ticker MRK, Exchange US). A later response for the same symbol and date updates the row.

CREATE TABLE IF NOT EXISTS "Stock_EOD_Stage" (
    "EodStageId"    INTEGER PRIMARY KEY AUTOINCREMENT,
    "Symbol"        TEXT NOT NULL,              -- as requested from the API: MRK, MRK.US
    "Ticker"        TEXT NOT NULL,              -- the symbol without the exchange suffix: MRK
    "Exchange"      TEXT,                       -- the exchange suffix (US, LSE, ...) or NULL
    "TradeDate"     TEXT NOT NULL,              -- API field "date" (YYYY-MM-DD)
    "Open"          REAL NOT NULL,              -- "open"
    "High"          REAL NOT NULL,              -- "high"
    "Low"           REAL NOT NULL,              -- "low"
    "Close"         REAL NOT NULL,              -- "close"
    "AdjustedClose" REAL NOT NULL,              -- "adjusted_close" (splits and dividends)
    "Volume"        INTEGER NOT NULL,           -- "volume"
    "SourceFile"    TEXT NOT NULL,              -- the saved API response the values came from
    "LoadId"        TEXT NOT NULL,              -- one id per load
    "LoadedAt"      TEXT NOT NULL,              -- first loaded
    "UpdatedAt"     TEXT NOT NULL,              -- last changed by a later response
    CONSTRAINT "UQ_Stock_EOD_Stage_Symbol_TradeDate" UNIQUE ("Symbol", "TradeDate"),
    CONSTRAINT "CK_Stock_EOD_Stage_TradeDate" CHECK ("TradeDate" GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    CONSTRAINT "CK_Stock_EOD_Stage_Volume" CHECK ("Volume" >= 0)
);
CREATE INDEX IF NOT EXISTS "IX_Stock_EOD_Stage_Ticker_TradeDate" ON "Stock_EOD_Stage" ("Ticker", "TradeDate");
CREATE INDEX IF NOT EXISTS "IX_Stock_EOD_Stage_SourceFile" ON "Stock_EOD_Stage" ("SourceFile");
