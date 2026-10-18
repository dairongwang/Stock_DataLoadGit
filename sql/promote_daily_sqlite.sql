-- promote_daily: staging table -> Stock_Daily (validated, typed, one row per stock and day).
-- Parameters: :now (timestamp). Optional filters add WHERE conditions on SourceFile / LoadId / Ticker / TradeDate;
-- incremental mode adds "LoadedAt" >= :since OR StageId corrected since :since.
-- Generated from stock_dataload/promotion.py - run by PromotionService inside ONE transaction (BEGIN IMMEDIATE ... COMMIT).
-- The temporary tables live in the connection only. SQLite has no stored procedures: this is the procedure.

BEGIN IMMEDIATE;

-- the temporary tables live in this connection only (they vanish with it)
DROP TABLE IF EXISTS "_promote_candidates";
DROP TABLE IF EXISTS "_promote_closed_days";
DROP TABLE IF EXISTS "_promote_symbols";
DROP TABLE IF EXISTS "_promote_rows";

-- step 1: candidate rows. The typed columns (REAL / NUMERIC affinity) keep text that is not a
-- number as TEXT - typeof() tells valid numbers from garbage; NUMERIC turns '2800.0' into the
-- integer 2800 but leaves 28.5 a REAL (fractional volume).
CREATE TEMP TABLE "_promote_candidates" (
    "StageId"    INTEGER,
    "Market"     TEXT,
    "Ticker"     TEXT,
    "RawDate"    TEXT,
    "TradeDate"  TEXT,
    "Open"       REAL,
    "High"       REAL,
    "Low"        REAL,
    "Close"      REAL,
    "Vol"        NUMERIC,
    "SourceFile" TEXT,
    "LoadId"     TEXT,
    "Reason"     TEXT,
    "Rule"       TEXT,
    "ColumnName" TEXT,
    "Value"      TEXT,
    "Message"    TEXT
);
INSERT INTO "_promote_candidates" ("StageId", "Market", "Ticker", "RawDate", "TradeDate", "Open", "High", "Low",
                             "Close", "Vol", "SourceFile", "LoadId")
SELECT s."StageId", NULLIF(TRIM(COALESCE(s."Market", '')), ''), TRIM(COALESCE(s."Ticker", '')), s."TradeDate",
       COALESCE(CASE WHEN TRIM(s."TradeDate") GLOB '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]' THEN substr(TRIM(s."TradeDate"), 1, 4) || '-' || substr(TRIM(s."TradeDate"), 5, 2) || '-' || substr(TRIM(s."TradeDate"), 7, 2) WHEN TRIM(s."TradeDate") GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' THEN TRIM(s."TradeDate") END, ''),
       TRIM(COALESCE(s."Open", '')), TRIM(COALESCE(s."High", '')), TRIM(COALESCE(s."Low", '')),
       TRIM(COALESCE(s."Close", '')), TRIM(COALESCE(s."Vol", '')), s."SourceFile", s."LoadId"
FROM "Stock_Stage" s;
CREATE INDEX "_ix_promote_candidates" ON "_promote_candidates" ("Ticker", "TradeDate", "StageId");

-- step 2: days the markets were closed - filled by the application from the No_Trading_Day table
-- (weekends, holidays, ad-hoc closures) for the distinct market / trade date pairs found
CREATE TEMP TABLE "_promote_closed_days" ("Market" TEXT NOT NULL, "TradeDate" TEXT NOT NULL, "Reason" TEXT NOT NULL, PRIMARY KEY ("Market", "TradeDate"));

-- step 2b: the trading window of every stock with known dates (Stock_Symbol table)
CREATE TEMP TABLE "_promote_symbols" AS
SELECT "Market", "Ticker", "FirstTradeDate", "LastTradeDate"
  FROM "Stock_Symbol"
 WHERE "FirstTradeDate" IS NOT NULL OR "LastTradeDate" IS NOT NULL;
CREATE INDEX "_ix_promote_symbols" ON "_promote_symbols" ("Market", "Ticker");

-- step 3: validation - the first failing rule decides (rule:column)
UPDATE "_promote_candidates" SET "Reason" = CASE
        WHEN "Ticker" = '' THEN 'ticker_required:Ticker'
        WHEN "TradeDate" = '' OR date("TradeDate") IS NULL OR date("TradeDate") <> "TradeDate" THEN 'date_is_valid:TradeDate'
        WHEN NOT typeof("Open") IN ('integer', 'real') THEN 'numeric_columns:Open'
        WHEN NOT typeof("High") IN ('integer', 'real') THEN 'numeric_columns:High'
        WHEN NOT typeof("Low") IN ('integer', 'real') THEN 'numeric_columns:Low'
        WHEN NOT typeof("Close") IN ('integer', 'real') THEN 'numeric_columns:Close'
        WHEN NOT typeof("Vol") IN ('integer', 'real') THEN 'numeric_columns:Vol'
        WHEN "Open" <= 0 THEN 'positive_price:Open'
        WHEN "High" <= 0 THEN 'positive_price:High'
        WHEN "Low" <= 0 THEN 'positive_price:Low'
        WHEN "Close" <= 0 THEN 'positive_price:Close'
        WHEN typeof("Vol") <> 'integer' OR "Vol" < 0 THEN 'volume_invalid:Vol'
        WHEN "High" < "Low" THEN 'high_above_low:High'
        WHEN "Open" < "Low" OR "Open" > "High" THEN 'price_range:Open'
        WHEN "Close" < "Low" OR "Close" > "High" THEN 'price_range:Close'
        WHEN EXISTS (SELECT 1 FROM "_promote_closed_days" x WHERE x."Market" = COALESCE("_promote_candidates"."Market", '') AND x."TradeDate" = "_promote_candidates"."TradeDate") THEN 'market_closed:TradeDate'
        WHEN EXISTS (SELECT 1 FROM "_promote_symbols" s WHERE s."Market" = COALESCE("_promote_candidates"."Market", '') AND s."Ticker" = "_promote_candidates"."Ticker" AND s."FirstTradeDate" > "_promote_candidates"."TradeDate") THEN 'before_first_trade_date:TradeDate'
        WHEN EXISTS (SELECT 1 FROM "_promote_symbols" s WHERE s."Market" = COALESCE("_promote_candidates"."Market", '') AND s."Ticker" = "_promote_candidates"."Ticker" AND s."LastTradeDate" < "_promote_candidates"."TradeDate") THEN 'after_last_trade_date:TradeDate'
    END;

-- rule, column and offending value of the rejected rows
UPDATE "_promote_candidates"
   SET "Rule" = substr("Reason", 1, instr("Reason", ':') - 1),
       "ColumnName" = substr("Reason", instr("Reason", ':') + 1),
       "Value" = CASE substr("Reason", instr("Reason", ':') + 1)
                     WHEN 'Open' THEN CASE WHEN typeof("Open") = 'real' THEN rtrim(rtrim(CAST("Open" AS TEXT), '0'), '.') ELSE CAST("Open" AS TEXT) END
                     WHEN 'High' THEN CASE WHEN typeof("High") = 'real' THEN rtrim(rtrim(CAST("High" AS TEXT), '0'), '.') ELSE CAST("High" AS TEXT) END
                     WHEN 'Low' THEN CASE WHEN typeof("Low") = 'real' THEN rtrim(rtrim(CAST("Low" AS TEXT), '0'), '.') ELSE CAST("Low" AS TEXT) END
                     WHEN 'Close' THEN CASE WHEN typeof("Close") = 'real' THEN rtrim(rtrim(CAST("Close" AS TEXT), '0'), '.') ELSE CAST("Close" AS TEXT) END
                     WHEN 'Vol' THEN CAST("Vol" AS TEXT)
                     WHEN 'TradeDate' THEN "RawDate"
                     WHEN 'Ticker' THEN "Ticker"
                 END
 WHERE "Reason" IS NOT NULL;

-- messages for the rejected rows
UPDATE "_promote_candidates" SET "Message" = CASE "Rule"
        WHEN 'ticker_required' THEN 'ticker is empty'
        WHEN 'date_is_valid' THEN 'date ''' || COALESCE("RawDate", '') || ''' is not a valid YYYYMMDD calendar date'
        WHEN 'numeric_columns' THEN CASE WHEN COALESCE("Value", '') = '' THEN lower("ColumnName") || ' is missing'
                                  ELSE lower("ColumnName") || ' ''' || "Value" || ''' is not a number' END
        WHEN 'positive_price' THEN lower("ColumnName") || ' (' || "Value" || ') must be greater than 0'
        WHEN 'volume_invalid' THEN 'vol (' || "Value" || ') must be a whole number >= 0'
        WHEN 'high_above_low' THEN 'high (' || "Value" || ') is smaller than low (' || CASE WHEN typeof("Low") = 'real' THEN rtrim(rtrim(CAST("Low" AS TEXT), '0'), '.') ELSE CAST("Low" AS TEXT) END || ')'
        WHEN 'price_range' THEN lower("ColumnName") || ' (' || "Value" || ') is outside the low..high range ('
                                 || CASE WHEN typeof("Low") = 'real' THEN rtrim(rtrim(CAST("Low" AS TEXT), '0'), '.') ELSE CAST("Low" AS TEXT) END || '..' || CASE WHEN typeof("High") = 'real' THEN rtrim(rtrim(CAST("High" AS TEXT), '0'), '.') ELSE CAST("High" AS TEXT) END || ')'
        WHEN 'before_first_trade_date' THEN "Ticker" || ' started to trade on '
                                  || (SELECT s."FirstTradeDate" FROM "_promote_symbols" s
                                       WHERE s."Market" = COALESCE("_promote_candidates"."Market", '') AND s."Ticker" = "_promote_candidates"."Ticker")
                                  || ' - data for ' || "TradeDate" || ' is before its first trade date'
        WHEN 'after_last_trade_date' THEN "Ticker" || ' stopped trading on '
                                  || (SELECT s."LastTradeDate" FROM "_promote_symbols" s
                                       WHERE s."Market" = COALESCE("_promote_candidates"."Market", '') AND s."Ticker" = "_promote_candidates"."Ticker")
                                  || ' - data for ' || "TradeDate" || ' is after its last trade date'
        WHEN 'market_closed' THEN COALESCE("Market", 'the market') || ' was closed on ' || "TradeDate" || ' ('
                                  || COALESCE((SELECT x."Reason" FROM "_promote_closed_days" x
                                                WHERE x."Market" = COALESCE("_promote_candidates"."Market", '')
                                                  AND x."TradeDate" = "_promote_candidates"."TradeDate"), 'no trading day')
                                  || ')'
    END
 WHERE "Rule" IS NOT NULL;
SELECT "Rule", COUNT(*) FROM "_promote_candidates" WHERE "Rule" IS NOT NULL GROUP BY "Rule";

-- step 4: rejected rows -> Data_Error (once per open finding; fix them on the data correction page
-- and promote again)
INSERT INTO "Data_Error" ("DetectedAt", "SourceFile", "LoadId", "Market", "Ticker", "TradeDate",
                                  "ColumnName", "Value", "Rule", "Severity", "Alert", "ErrorMessage", "LineNo")
SELECT :now, c."SourceFile", c."LoadId", c."Market", NULLIF(c."Ticker", ''), c."RawDate", c."ColumnName",
       c."Value", c."Rule", 'error', 0,
       'promotion to Stock_Daily rejected the row: ' || c."Message", NULL
  FROM "_promote_candidates" c
 WHERE c."Rule" IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM "Data_Error" e
                    WHERE e."Resolved" = 0 AND e."Rule" = c."Rule" AND e."SourceFile" IS c."SourceFile"
                      AND e."Ticker" IS NULLIF(c."Ticker", '') AND e."TradeDate" IS c."RawDate"
                      AND e."ColumnName" IS c."ColumnName");

-- step 5: one row per stock/day - the most recently loaded valid staging row (highest StageId)
-- wins; ordered like the target's unique index, which keeps the following look-ups sequential
CREATE TEMP TABLE "_promote_rows" AS
SELECT c."StageId", c."Market", c."Ticker", c."TradeDate", c."Open", c."High", c."Low", c."Close",
       c."Vol" AS "Volume", c."SourceFile", c."LoadId"
  FROM "_promote_candidates" c
 WHERE c."Rule" IS NULL
   AND NOT EXISTS (SELECT 1 FROM "_promote_candidates" c2
                    WHERE c2."Ticker" = c."Ticker" AND c2."TradeDate" = c."TradeDate"
                      AND c2."StageId" > c."StageId" AND c2."Rule" IS NULL)
 ORDER BY c."Ticker", c."TradeDate";
SELECT (SELECT COUNT(*) FROM "_promote_candidates" WHERE "Rule" IS NULL) - (SELECT COUNT(*) FROM "_promote_rows");

-- step 6: how many rows already exist ...
SELECT COUNT(*) FROM "_promote_rows" r JOIN "Stock_Daily" d ON d."Ticker" = r."Ticker" AND d."TradeDate" = r."TradeDate";

-- ... and how many of them changed
SELECT COUNT(*) FROM "_promote_rows" r JOIN "Stock_Daily" d ON d."Ticker" = r."Ticker" AND d."TradeDate" = r."TradeDate" WHERE (d."Open" IS NOT r."Open" OR d."High" IS NOT r."High" OR d."Low" IS NOT r."Low" OR d."Close" IS NOT r."Close" OR d."Volume" IS NOT r."Volume" OR d."Market" IS NOT r."Market" OR d."SourceFile" IS NOT r."SourceFile" OR d."LoadId" IS NOT r."LoadId" OR d."StageId" IS NOT r."StageId");

-- upsert: new stock/days are inserted, existing ones updated only if something changed (UpdatedAt
-- drives the incremental weekly / monthly promotion)
INSERT INTO "Stock_Daily" ("Market", "Ticker", "TradeDate", "Open", "High", "Low", "Close", "Volume",
                     "SourceFile", "LoadId", "StageId", "PromotedAt", "UpdatedAt")
SELECT r."Market", r."Ticker", r."TradeDate", r."Open", r."High", r."Low", r."Close", r."Volume",
       r."SourceFile", r."LoadId", r."StageId", :now, :now
  FROM "_promote_rows" r
 WHERE true
ON CONFLICT ("Ticker", "TradeDate") DO UPDATE SET
       "Market" = excluded."Market", "Open" = excluded."Open", "High" = excluded."High",
       "Low" = excluded."Low", "Close" = excluded."Close", "Volume" = excluded."Volume",
       "SourceFile" = excluded."SourceFile", "LoadId" = excluded."LoadId", "StageId" = excluded."StageId",
       "UpdatedAt" = excluded."UpdatedAt"
 WHERE "Stock_Daily"."Open" IS NOT excluded."Open" OR "Stock_Daily"."High" IS NOT excluded."High"
    OR "Stock_Daily"."Low" IS NOT excluded."Low" OR "Stock_Daily"."Close" IS NOT excluded."Close"
    OR "Stock_Daily"."Volume" IS NOT excluded."Volume" OR "Stock_Daily"."Market" IS NOT excluded."Market"
    OR "Stock_Daily"."SourceFile" IS NOT excluded."SourceFile" OR "Stock_Daily"."LoadId" IS NOT excluded."LoadId"
    OR "Stock_Daily"."StageId" IS NOT excluded."StageId";

COMMIT;
