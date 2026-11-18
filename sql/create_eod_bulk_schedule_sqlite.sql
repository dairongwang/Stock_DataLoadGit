-- The daily schedules of the one-day bulk load (stock_dataload/bulk_schedule.py creates the table on first use;
-- kept here for reference - generated from the module, see tests/test_bulk_schedule.py).
-- Eod_Bulk_Schedule  one row per exchange: run at RunTime (server local time) on every day the exchange trades,
--                    once per day (LastRunDate), loading the same or the previous trading day with eod-bulk-last-day

CREATE TABLE IF NOT EXISTS "Eod_Bulk_Schedule" (
    "ScheduleId"    INTEGER PRIMARY KEY AUTOINCREMENT,
    "Market"        TEXT NOT NULL UNIQUE,       -- the exchange code of the bulk endpoint: US, LSE ...
    "RunTime"       TEXT NOT NULL,              -- HH:MM, server local time
    "TradeDay"      TEXT NOT NULL DEFAULT 'same',   -- same | previous
    "Fmt"           TEXT NOT NULL DEFAULT 'json',   -- json | csv
    "Symbols"       TEXT,                       -- only these stocks (comma separated) - empty = every stock
    "Promote"       INTEGER NOT NULL DEFAULT 1,
    "Aggregates"    INTEGER NOT NULL DEFAULT 0,
    "Strict"        INTEGER NOT NULL DEFAULT 0,
    "Fake"          INTEGER NOT NULL DEFAULT 0, -- the built-in stub instead of the API
    "AllowFakeData" INTEGER NOT NULL DEFAULT 0,
    "Enabled"       INTEGER NOT NULL DEFAULT 1,
    "CreatedBy"     TEXT,
    "CreatedAt"     TEXT NOT NULL,
    "UpdatedBy"     TEXT,
    "UpdatedAt"     TEXT,
    "LastRunDate"   TEXT,                       -- the day it last ran: once per day
    "LastRunAt"     TEXT,
    "LastTradeDate" TEXT,
    "LastStatus"    TEXT,                       -- running | loaded | loaded with warnings | failed | refused
    "LastMessage"   TEXT,
    "LastRunId"     TEXT                        -- Eod_Market_Run
);
