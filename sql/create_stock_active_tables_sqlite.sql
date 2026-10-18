-- Active tables of stock_dataload (created automatically by the app / `python -m stock_dataload init-db`;
-- kept here for reference, generated from stock_dataload/promotion.py).
--
--   Stock_Stage --promote_daily--> Stock_Daily --promote_weekly--> Stock_Weekly
--                                             --promote_monthly-> Stock_Monthly
--
-- Stock_Daily   one validated, typed row per stock and trading day   UNIQUE (Ticker, TradeDate)
-- Stock_Weekly  one row per stock and ISO week (Monday..Sunday)       UNIQUE (Ticker, WeekStart)
-- Stock_Monthly one row per stock and calendar month                 UNIQUE (Ticker, MonthStart)
-- Promotion_History  one row per procedure run (counts, status, parameters)
-- Table names are configurable in config.ini [database]; the procedures are in sql/promote_*_sqlite.sql.

CREATE TABLE IF NOT EXISTS "Stock_Daily" (
    "DailyId"    INTEGER PRIMARY KEY AUTOINCREMENT,
    "Market"     TEXT,
    "Ticker"     TEXT NOT NULL,
    "TradeDate"  TEXT NOT NULL,                 -- YYYY-MM-DD
    "Open"       REAL NOT NULL,
    "High"       REAL NOT NULL,
    "Low"        REAL NOT NULL,
    "Close"      REAL NOT NULL,
    "Volume"     INTEGER NOT NULL,              -- shares traded on the day
    "SourceFile" TEXT,                          -- staging row the values came from
    "LoadId"     TEXT,
    "StageId"    INTEGER,
    "PromotedAt" TEXT NOT NULL,                 -- first promotion
    "UpdatedAt"  TEXT NOT NULL,                 -- last change of the row
    CONSTRAINT "UQ_Stock_Daily_Ticker_TradeDate" UNIQUE ("Ticker", "TradeDate"),
    CONSTRAINT "CK_Stock_Daily_TradeDate" CHECK ("TradeDate" GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    CONSTRAINT "CK_Stock_Daily_Prices" CHECK ("Open" > 0 AND "High" > 0 AND "Low" > 0 AND "Close" > 0),
    CONSTRAINT "CK_Stock_Daily_HighLow" CHECK ("High" >= "Low"),
    CONSTRAINT "CK_Stock_Daily_Volume" CHECK ("Volume" >= 0)
);
CREATE INDEX IF NOT EXISTS "IX_Stock_Daily_TradeDate" ON "Stock_Daily" ("TradeDate");
CREATE INDEX IF NOT EXISTS "IX_Stock_Daily_UpdatedAt" ON "Stock_Daily" ("UpdatedAt");
CREATE TABLE IF NOT EXISTS "Stock_Weekly" (
    "WeeklyId"       INTEGER PRIMARY KEY AUTOINCREMENT,
    "Market"         TEXT,
    "Ticker"         TEXT NOT NULL,
    "WeekStart"      TEXT NOT NULL,             -- Monday of the ISO 8601 week, YYYY-MM-DD
    "WeekEnd"        TEXT NOT NULL,             -- Sunday
    "ISOYear"        INTEGER NOT NULL,
    "ISOWeek"        INTEGER NOT NULL,          -- 1..53
    "Open"           REAL NOT NULL,             -- open of the first trading day of the week
    "High"           REAL NOT NULL,             -- highest high
    "Low"            REAL NOT NULL,             -- lowest low
    "Close"          REAL NOT NULL,             -- close of the last trading day of the week
    "Volume"         INTEGER NOT NULL,          -- shares traded during the week
    "TradingDays"    INTEGER NOT NULL,          -- daily rows aggregated
    "FirstTradeDate" TEXT NOT NULL,
    "LastTradeDate"  TEXT NOT NULL,
    "ExpectedDays"   INTEGER NOT NULL DEFAULT 0, -- trading days of the week (market open, inside the stock's window)
    "MissingDays"    INTEGER NOT NULL DEFAULT 0, -- expected days without a daily row
    "MissingDates"   TEXT,                      -- the missing days, comma separated
    "Complete"       INTEGER NOT NULL DEFAULT 1, -- 0 = a trading day is missing (data error)
    "PromotedAt"     TEXT NOT NULL,
    "UpdatedAt"      TEXT NOT NULL,
    CONSTRAINT "UQ_Stock_Weekly_Ticker_WeekStart" UNIQUE ("Ticker", "WeekStart"),
    CONSTRAINT "CK_Stock_Weekly_HighLow" CHECK ("High" >= "Low"),
    CONSTRAINT "CK_Stock_Weekly_Volume" CHECK ("Volume" >= 0),
    CONSTRAINT "CK_Stock_Weekly_Days" CHECK ("TradingDays" BETWEEN 1 AND 7)
);
CREATE INDEX IF NOT EXISTS "IX_Stock_Weekly_WeekStart" ON "Stock_Weekly" ("WeekStart");
CREATE TABLE IF NOT EXISTS "Stock_Monthly" (
    "MonthlyId"      INTEGER PRIMARY KEY AUTOINCREMENT,
    "Market"         TEXT,
    "Ticker"         TEXT NOT NULL,
    "MonthStart"     TEXT NOT NULL,             -- first day of the month, YYYY-MM-DD
    "MonthEnd"       TEXT NOT NULL,             -- last day of the month
    "Year"           INTEGER NOT NULL,
    "Month"          INTEGER NOT NULL,          -- 1..12
    "Open"           REAL NOT NULL,             -- open of the first trading day of the month
    "High"           REAL NOT NULL,
    "Low"            REAL NOT NULL,
    "Close"          REAL NOT NULL,             -- close of the last trading day of the month
    "Volume"         INTEGER NOT NULL,          -- shares traded during the month
    "TradingDays"    INTEGER NOT NULL,
    "FirstTradeDate" TEXT NOT NULL,
    "LastTradeDate"  TEXT NOT NULL,
    "ExpectedDays"   INTEGER NOT NULL DEFAULT 0, -- trading days of the month (market open, inside the stock's window)
    "MissingDays"    INTEGER NOT NULL DEFAULT 0, -- expected days without a daily row
    "MissingDates"   TEXT,                      -- the missing days, comma separated
    "Complete"       INTEGER NOT NULL DEFAULT 1, -- 0 = a trading day is missing (data error)
    "PromotedAt"     TEXT NOT NULL,
    "UpdatedAt"      TEXT NOT NULL,
    CONSTRAINT "UQ_Stock_Monthly_Ticker_MonthStart" UNIQUE ("Ticker", "MonthStart"),
    CONSTRAINT "CK_Stock_Monthly_HighLow" CHECK ("High" >= "Low"),
    CONSTRAINT "CK_Stock_Monthly_Volume" CHECK ("Volume" >= 0),
    CONSTRAINT "CK_Stock_Monthly_Days" CHECK ("TradingDays" BETWEEN 1 AND 31)
);
CREATE INDEX IF NOT EXISTS "IX_Stock_Monthly_MonthStart" ON "Stock_Monthly" ("MonthStart");
CREATE TABLE IF NOT EXISTS "Promotion_History" (
    "PromotionId"   INTEGER PRIMARY KEY AUTOINCREMENT,
    "Procedure"     TEXT NOT NULL,              -- promote_daily | promote_weekly | promote_monthly
    "TargetTable"   TEXT NOT NULL,
    "Mode"          TEXT NOT NULL,              -- full | incremental
    "Filtered"      INTEGER NOT NULL DEFAULT 0, -- 1 = run limited to a file / ticker / date range
    "Parameters"    TEXT,                       -- JSON
    "StartedAt"     TEXT NOT NULL,
    "FinishedAt"    TEXT NOT NULL,
    "Status"        TEXT NOT NULL,              -- completed | failed
    "RowsRead"      INTEGER NOT NULL DEFAULT 0,
    "RowsInserted"  INTEGER NOT NULL DEFAULT 0,
    "RowsUpdated"   INTEGER NOT NULL DEFAULT 0,
    "RowsUnchanged" INTEGER NOT NULL DEFAULT 0,
    "RowsRejected"  INTEGER NOT NULL DEFAULT 0,
    "RowsSkipped"   INTEGER NOT NULL DEFAULT 0, -- duplicates (an older copy of the same stock/day)
    "RowsDeleted"   INTEGER NOT NULL DEFAULT 0, -- aggregates without daily rows (full mode)
    "RowsIncomplete" INTEGER NOT NULL DEFAULT 0, -- weekly / monthly: aggregates with a trading day missing
    "Message"       TEXT
);
CREATE INDEX IF NOT EXISTS "IX_Promotion_History_Procedure" ON "Promotion_History" ("Procedure", "StartedAt");
