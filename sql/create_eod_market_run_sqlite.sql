-- The run registry of eod-fetch-market (stock_dataload/eod_market.py creates the tables on first use;
-- kept here for reference - generated from the module, see tests/test_eod_market.py).
-- Eod_Market_Run       one row per run over a market: status, counts, API calls
-- Eod_Market_Run_Item  one row per stock of a run: loaded / no_data / failed / skipped / pending,
--                      attempts, HTTP status of the last failure, rows, error - resume continues the pending / failed

CREATE TABLE IF NOT EXISTS "Eod_Market_Run" (
    "RunId"        TEXT PRIMARY KEY,
    "Market"       TEXT NOT NULL,
    "Gateway"      TEXT NOT NULL,              -- live | fake
    "Status"       TEXT NOT NULL,              -- running | completed | completed_with_errors | aborted | interrupted
    "Parameters"   TEXT,                       -- JSON: types, from / to, retries ...
    "Symbols"      INTEGER NOT NULL DEFAULT 0,
    "Loaded"       INTEGER NOT NULL DEFAULT 0,
    "NoData"       INTEGER NOT NULL DEFAULT 0,
    "Failed"       INTEGER NOT NULL DEFAULT 0,
    "Skipped"      INTEGER NOT NULL DEFAULT 0,
    "Rows"         INTEGER NOT NULL DEFAULT 0, -- records loaded into the staging table
    "ApiCalls"     INTEGER NOT NULL DEFAULT 0,
    "Message"      TEXT,
    "StartedAt"    TEXT NOT NULL,
    "FinishedAt"   TEXT
);
CREATE TABLE IF NOT EXISTS "Eod_Market_Run_Item" (
    "RunId"        TEXT NOT NULL REFERENCES "Eod_Market_Run" ("RunId"),
    "Position"     INTEGER NOT NULL,
    "Symbol"       TEXT NOT NULL,
    "Status"       TEXT NOT NULL,              -- pending | loaded | no_data | failed | skipped
    "Attempts"     INTEGER NOT NULL DEFAULT 0, -- API calls made for the stock
    "HttpStatus"   INTEGER,                    -- of the last failed call
    "Received"     INTEGER NOT NULL DEFAULT 0,
    "Inserted"     INTEGER NOT NULL DEFAULT 0,
    "Updated"      INTEGER NOT NULL DEFAULT 0,
    "Unchanged"    INTEGER NOT NULL DEFAULT 0,
    "Rejected"     INTEGER NOT NULL DEFAULT 0,
    "FirstDate"    TEXT,
    "LastDate"     TEXT,
    "LoadId"       TEXT,
    "Error"        TEXT,
    "FinishedAt"   TEXT,
    CONSTRAINT "PK_Eod_Market_Run_Item" PRIMARY KEY ("RunId", "Symbol")
);
CREATE INDEX IF NOT EXISTS "IX_Eod_Market_Run_Market" ON "Eod_Market_Run" ("Market", "StartedAt");
CREATE INDEX IF NOT EXISTS "IX_Eod_Market_Run_Item_Status" ON "Eod_Market_Run_Item" ("RunId", "Status");
