-- Support tables of stock_dataload (created automatically by the app /
-- `python -m stock_dataload init-db`; kept here for reference).
-- The active tables (Stock_Daily / Stock_Weekly / Stock_Monthly) and
-- Promotion_History are in create_stock_active_tables_sqlite.sql.

-- Registry of every load attempt. The content hash (sha-256) makes
-- re-loads idempotent: a file whose exact content was already loaded
-- successfully is skipped, so only ONE copy ends up in the staging table.
CREATE TABLE IF NOT EXISTS "Load_History" (
    "LoadHistoryId" INTEGER PRIMARY KEY AUTOINCREMENT,
    "FileName"      TEXT NOT NULL,
    "FileHash"      TEXT NOT NULL,              -- sha-256 of the file content
    "FileSize"      INTEGER,
    "LoadId"        TEXT NOT NULL,
    "Market"        TEXT,
    "Status"        TEXT NOT NULL,              -- loaded | failed | skipped_duplicate | replaced
    "RowsLoaded"    INTEGER NOT NULL DEFAULT 0,
    "Message"       TEXT,
    "LoadedAt"      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS "IX_Load_History_FileHash" ON "Load_History" ("FileHash");

-- App activity log: every noteworthy step of the app.
CREATE TABLE IF NOT EXISTS "App_Log" (
    "LogId"      INTEGER PRIMARY KEY AUTOINCREMENT,
    "LoggedAt"   TEXT NOT NULL,
    "Level"      TEXT NOT NULL,                 -- DEBUG | INFO | WARNING | ERROR | ALERT
    "Component"  TEXT NOT NULL,                 -- loader | converter | cli | ...
    "Event"      TEXT NOT NULL,                 -- load_started | file_loaded | data_alert | ...
    "SourceFile" TEXT,
    "LoadId"     TEXT,
    "Message"    TEXT,
    "Details"    TEXT                           -- JSON
);
CREATE INDEX IF NOT EXISTS "IX_App_Log_LoggedAt" ON "App_Log" ("LoggedAt");

-- Operational errors: which step of which component failed, and how.
CREATE TABLE IF NOT EXISTS "App_Error" (
    "ErrorId"      INTEGER PRIMARY KEY AUTOINCREMENT,
    "OccurredAt"   TEXT NOT NULL,
    "Component"    TEXT NOT NULL,
    "Operation"    TEXT NOT NULL,               -- the step that failed
    "SourceFile"   TEXT,
    "LoadId"       TEXT,
    "ErrorType"    TEXT,                        -- exception class
    "ErrorMessage" TEXT NOT NULL,
    "Stacktrace"   TEXT,
    "Details"      TEXT                         -- JSON
);
CREATE INDEX IF NOT EXISTS "IX_App_Error_OccurredAt" ON "App_Error" ("OccurredAt");

-- Data quality findings of the strategy-based checks (quality.py) and the
-- rows rejected by the promotion to the daily table (promotion.py).
-- Alert = 1 marks alert-level findings, e.g. a known stock with no valid
-- data for a day the market was open.
CREATE TABLE IF NOT EXISTS "Data_Error" (
    "DataErrorId" INTEGER PRIMARY KEY AUTOINCREMENT,
    "DetectedAt"  TEXT NOT NULL,
    "SourceFile"  TEXT,
    "LoadId"      TEXT,
    "Market"      TEXT,
    "Ticker"      TEXT,
    "TradeDate"   TEXT,
    "ColumnName"  TEXT,
    "Value"       TEXT,
    "Rule"        TEXT NOT NULL,                -- validation strategy name
    "Severity"    TEXT NOT NULL,                -- error | alert
    "Alert"       INTEGER NOT NULL DEFAULT 0,
    "ErrorMessage" TEXT NOT NULL,
    "LineNo"      INTEGER,
    "Resolved"    INTEGER NOT NULL DEFAULT 0,   -- set by the data correction page
    "ResolvedAt"  TEXT,
    "ResolutionNote" TEXT
);
CREATE INDEX IF NOT EXISTS "IX_Data_Error_SourceFile" ON "Data_Error" ("SourceFile");
CREATE INDEX IF NOT EXISTS "IX_Data_Error_Ticker_TradeDate" ON "Data_Error" ("Ticker", "TradeDate");

-- Corrections made on the dashboard's data correction page: one row per
-- changed column (old / new value), linked to the staging row and, when
-- the correction came from a finding, to the Data_Error entry.
CREATE TABLE IF NOT EXISTS "Data_Correction" (
    "CorrectionId" INTEGER PRIMARY KEY AUTOINCREMENT,
    "CorrectedAt"  TEXT NOT NULL,
    "Action"       TEXT NOT NULL,               -- updated | inserted
    "StageId"      INTEGER,
    "SourceFile"   TEXT,
    "Ticker"       TEXT,
    "TradeDate"    TEXT,
    "ColumnName"   TEXT NOT NULL,
    "OldValue"     TEXT,
    "NewValue"     TEXT,
    "DataErrorId"  INTEGER,
    "Note"         TEXT
);
CREATE INDEX IF NOT EXISTS "IX_Data_Correction_StageId" ON "Data_Correction" ("StageId");

-- Dashboard user accounts. Only a salted PBKDF2-HMAC-SHA256 hash of the
-- password is stored (auth.py); roles: viewer < operator < admin.
CREATE TABLE IF NOT EXISTS "App_User" (
    "UserId"             INTEGER PRIMARY KEY AUTOINCREMENT,
    "Username"           TEXT NOT NULL UNIQUE COLLATE NOCASE,
    "DisplayName"        TEXT,
    "PasswordHash"       TEXT NOT NULL,
    "Role"               TEXT NOT NULL,
    "Active"             INTEGER NOT NULL DEFAULT 1,
    "MustChangePassword" INTEGER NOT NULL DEFAULT 0,
    "CreatedAt"          TEXT NOT NULL,
    "CreatedBy"          TEXT,
    "LastLoginAt"        TEXT
);
