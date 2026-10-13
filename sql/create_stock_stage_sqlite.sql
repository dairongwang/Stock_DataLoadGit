-- Staging table for SQLite (created automatically by the loader).
CREATE TABLE IF NOT EXISTS "Stock_Stage" (
    "StageId"    INTEGER PRIMARY KEY AUTOINCREMENT,
    "Market"     TEXT,
    "Ticker"     TEXT NOT NULL,
    "TradeDate"  TEXT NOT NULL,
    "Open"       TEXT,
    "High"       TEXT,
    "Low"        TEXT,
    "Close"      TEXT,
    "Vol"        TEXT,
    "SourceFile" TEXT NOT NULL,
    "LoadId"     TEXT NOT NULL,
    "LoadedAt"   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS "IX_Stock_Stage_SourceFile" ON "Stock_Stage" ("SourceFile");
