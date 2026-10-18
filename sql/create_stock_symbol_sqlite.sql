-- Stock symbol table of stock_dataload (created automatically by the app / `python -m stock_dataload init-db`;
-- kept here for reference, generated from stock_dataload/symbols.py).
--
-- One row per symbol and market: the first day it traded (FirstTradeDate) and, if delisted, the last.
-- The loader registers every symbol it loads (Source = loader) and keeps FirstSeen / LastSeen;
-- FirstTradeDate / LastTradeDate are maintained with `symbols set` / `symbols import` / the dashboard.
-- Data outside the trading window is invalid; the missing-market-day alert is only raised inside it,
-- and not at all for symbols whose FirstTradeDate is unknown (NULL).

CREATE TABLE IF NOT EXISTS "Stock_Symbol" (
    "StockSymbolId"  INTEGER PRIMARY KEY AUTOINCREMENT,
    "Ticker"         TEXT NOT NULL COLLATE NOCASE,
    "Market"         TEXT NOT NULL DEFAULT '' COLLATE NOCASE,  -- '' = market unknown
    "Name"           TEXT,
    "FirstTradeDate" TEXT,                                    -- YYYY-MM-DD; NULL = unknown (no missing-day check)
    "LastTradeDate"  TEXT,                                    -- YYYY-MM-DD; NULL = still trading
    "FirstSeen"      TEXT,                                    -- earliest trade date loaded for the symbol
    "LastSeen"       TEXT,                                    -- latest trade date loaded
    "Source"         TEXT NOT NULL,                           -- loader | manual | import
    "Note"           TEXT,
    "CreatedAt"      TEXT NOT NULL,
    "UpdatedAt"      TEXT NOT NULL,
    CONSTRAINT "UQ_Stock_Symbol_Market_Ticker" UNIQUE ("Market", "Ticker"),
    CONSTRAINT "CK_Stock_Symbol_Dates" CHECK ("FirstTradeDate" IS NULL OR "LastTradeDate" IS NULL
                                                      OR "FirstTradeDate" <= "LastTradeDate")
);
CREATE INDEX IF NOT EXISTS "IX_Stock_Symbol_Ticker" ON "Stock_Symbol" ("Ticker");
