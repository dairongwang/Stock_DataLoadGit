-- promote_eod_daily: Stock_EOD_Stage (EOD API data) -> Stock_Daily with the adjusted-price conversion
-- rate = AdjustedClose / Close; Open / High / Low * rate; Close = AdjustedClose; Volume as is.
-- Parameters: :now. A market parameter replaces step 2; [eod_api] default_market adds step 2c;
-- incremental mode adds "UpdatedAt" >= :since OR no daily row yet; filters add WHERE conditions on Symbol / Ticker / LoadId / TradeDate.
-- eod_round(x, n) is an application function (correct rounding) registered on the connection.
-- Generated from stock_dataload/promotion.py - run by PromotionService inside ONE transaction (BEGIN IMMEDIATE ... COMMIT).
-- The temporary tables live in the connection only. SQLite has no stored procedures: this is the procedure.

BEGIN IMMEDIATE;

-- the temporary tables live in this connection only
DROP TABLE IF EXISTS "_promote_eod";
DROP TABLE IF EXISTS "_promote_closed_days";
DROP TABLE IF EXISTS "_promote_symbols";
DROP TABLE IF EXISTS "_promote_rows";

-- step 1: the EOD rows to promote
CREATE TEMP TABLE "_promote_eod" (
    "EodStageId"    INTEGER,
    "Symbol"        TEXT,
    "Ticker"        TEXT,
    "TradeDate"     TEXT,
    "EodOpen"       REAL,
    "EodHigh"       REAL,
    "EodLow"        REAL,
    "EodClose"      REAL,
    "AdjustedClose" REAL,
    "Volume"        INTEGER,
    "Rate"          REAL,
    "Open"          REAL,
    "High"          REAL,
    "Low"           REAL,
    "Close"         REAL,
    "Market"        TEXT,
    "MarketSource"  TEXT,
    "MarketNote"    TEXT,
    "SourceFile"    TEXT,
    "LoadId"        TEXT,
    "UpdatedAt"     TEXT,
    "Reason"        TEXT,
    "Rule"          TEXT,
    "ColumnName"    TEXT,
    "Value"         TEXT,
    "Message"       TEXT
);
INSERT INTO "_promote_eod" ("EodStageId", "Symbol", "Ticker", "TradeDate", "EodOpen", "EodHigh", "EodLow", "EodClose",
                      "AdjustedClose", "Volume", "SourceFile", "LoadId", "UpdatedAt")
SELECT e."EodStageId", e."Symbol", e."Ticker", e."TradeDate", e."Open", e."High", e."Low", e."Close",
       e."AdjustedClose", e."Volume", e."SourceFile", e."LoadId", e."UpdatedAt"
  FROM "Stock_EOD_Stage" e;
CREATE INDEX "_ix_promote_eod" ON "_promote_eod" ("Ticker", "TradeDate", "EodStageId");

-- step 2a: the market - the symbol table, if the ticker is registered in exactly one market
UPDATE "_promote_eod"
   SET "Market" = (SELECT MIN(s."Market") FROM "Stock_Symbol" s
                    WHERE s."Ticker" = "_promote_eod"."Ticker" AND s."Market" <> ''),
       "MarketSource" = 'Stock_Symbol'
 WHERE (SELECT COUNT(DISTINCT s."Market") FROM "Stock_Symbol" s
         WHERE s."Ticker" = "_promote_eod"."Ticker" AND s."Market" <> '') = 1;

-- step 2b: ... else the market the ticker already has in the daily table
UPDATE "_promote_eod"
   SET "Market" = (SELECT MIN(d."Market") FROM "Stock_Daily" d WHERE d."Ticker" = "_promote_eod"."Ticker" AND d."Market" IS NOT NULL),
       "MarketSource" = 'Stock_Daily'
 WHERE "Market" IS NULL
   AND (SELECT COUNT(DISTINCT d."Market") FROM "Stock_Daily" d
         WHERE d."Ticker" = "_promote_eod"."Ticker" AND d."Market" IS NOT NULL) = 1;

-- markets of an ambiguous ticker (for the message of the rejected rows)
UPDATE "_promote_eod"
   SET "MarketNote" = (SELECT group_concat("Market", ', ') FROM (SELECT DISTINCT s."Market" FROM "Stock_Symbol" s
                        WHERE s."Ticker" = "_promote_eod"."Ticker" AND s."Market" <> '' ORDER BY 1))
 WHERE "Market" IS NULL;

-- step 3: days the markets were closed - filled by the application from the No_Trading_Day table
CREATE TEMP TABLE "_promote_closed_days" ("Market" TEXT NOT NULL, "TradeDate" TEXT NOT NULL, "Reason" TEXT NOT NULL, PRIMARY KEY ("Market", "TradeDate"));

-- step 3b: the trading window of every stock with known dates (Stock_Symbol table)
CREATE TEMP TABLE "_promote_symbols" AS
SELECT "Market", "Ticker", "FirstTradeDate", "LastTradeDate"
  FROM "Stock_Symbol"
 WHERE "FirstTradeDate" IS NOT NULL OR "LastTradeDate" IS NOT NULL;
CREATE INDEX "_ix_promote_symbols" ON "_promote_symbols" ("Market", "Ticker");

-- step 4: the conversion - rate = AdjustedClose / Close; Open, High, Low = the EOD value * rate;
-- Close = AdjustedClose; Volume unchanged. Rounded to 6 decimals (floating-point noise) with
-- eod_round(x, n), a correctly rounding function the application registers on the connection
-- (SQLite's round() may be one unit in the last place off).
UPDATE "_promote_eod"
   SET "Rate"  = "AdjustedClose" / "EodClose",
       "Open"  = eod_round("EodOpen" * ("AdjustedClose" / "EodClose"), 6),
       "High"  = eod_round("EodHigh" * ("AdjustedClose" / "EodClose"), 6),
       "Low"   = eod_round("EodLow" * ("AdjustedClose" / "EodClose"), 6),
       "Close" = eod_round("AdjustedClose", 6)
 WHERE "EodClose" > 0 AND "AdjustedClose" > 0;

-- step 5: validation of the converted values - the first failing rule decides (rule:column)
UPDATE "_promote_eod" SET "Reason" = CASE
        WHEN "EodClose" <= 0 THEN 'positive_price:Close'
        WHEN "AdjustedClose" <= 0 THEN 'positive_price:AdjustedClose'
        WHEN "Market" IS NULL THEN 'market_unknown:Market'
        WHEN "Open" <= 0 THEN 'positive_price:Open'
        WHEN "High" <= 0 THEN 'positive_price:High'
        WHEN "Low" <= 0 THEN 'positive_price:Low'
        WHEN "High" < "Low" THEN 'high_above_low:High'
        WHEN "Open" < "Low" OR "Open" > "High" THEN 'price_range:Open'
        WHEN "Close" < "Low" OR "Close" > "High" THEN 'price_range:Close'
        WHEN EXISTS (SELECT 1 FROM "_promote_closed_days" x WHERE x."Market" = "_promote_eod"."Market" AND x."TradeDate" = "_promote_eod"."TradeDate") THEN 'market_closed:TradeDate'
        WHEN EXISTS (SELECT 1 FROM "_promote_symbols" s WHERE s."Market" = "_promote_eod"."Market" AND s."Ticker" = "_promote_eod"."Ticker" AND s."FirstTradeDate" > "_promote_eod"."TradeDate") THEN 'before_first_trade_date:TradeDate'
        WHEN EXISTS (SELECT 1 FROM "_promote_symbols" s WHERE s."Market" = "_promote_eod"."Market" AND s."Ticker" = "_promote_eod"."Ticker" AND s."LastTradeDate" < "_promote_eod"."TradeDate") THEN 'after_last_trade_date:TradeDate'
        WHEN EXISTS (SELECT 1 FROM "Stock_Daily" d WHERE d."Ticker" = "_promote_eod"."Ticker" AND d."TradeDate" = "_promote_eod"."TradeDate" AND d."Market" IS NOT "_promote_eod"."Market") THEN 'market_mismatch:Market'
    END;

-- rule, column and offending value of the rejected rows
UPDATE "_promote_eod"
   SET "Rule" = substr("Reason", 1, instr("Reason", ':') - 1),
       "ColumnName" = substr("Reason", instr("Reason", ':') + 1),
       "Value" = CASE substr("Reason", instr("Reason", ':') + 1)
                     WHEN 'Open' THEN CASE WHEN typeof("Open") = 'real' THEN rtrim(rtrim(CAST("Open" AS TEXT), '0'), '.') ELSE CAST("Open" AS TEXT) END
                     WHEN 'High' THEN CASE WHEN typeof("High") = 'real' THEN rtrim(rtrim(CAST("High" AS TEXT), '0'), '.') ELSE CAST("High" AS TEXT) END
                     WHEN 'Low' THEN CASE WHEN typeof("Low") = 'real' THEN rtrim(rtrim(CAST("Low" AS TEXT), '0'), '.') ELSE CAST("Low" AS TEXT) END
                     WHEN 'Close' THEN CASE WHEN "Rule" = 'positive_price' THEN CASE WHEN typeof("EodClose") = 'real' THEN rtrim(rtrim(CAST("EodClose" AS TEXT), '0'), '.') ELSE CAST("EodClose" AS TEXT) END ELSE CASE WHEN typeof("Close") = 'real' THEN rtrim(rtrim(CAST("Close" AS TEXT), '0'), '.') ELSE CAST("Close" AS TEXT) END END
                     WHEN 'AdjustedClose' THEN CASE WHEN typeof("AdjustedClose") = 'real' THEN rtrim(rtrim(CAST("AdjustedClose" AS TEXT), '0'), '.') ELSE CAST("AdjustedClose" AS TEXT) END
                     WHEN 'Market' THEN "Market"
                     WHEN 'TradeDate' THEN "TradeDate"
                 END
 WHERE "Reason" IS NOT NULL;

-- messages for the rejected rows
UPDATE "_promote_eod" SET "Message" = CASE "Rule"
        WHEN 'positive_price' THEN CASE "ColumnName" WHEN 'Close' THEN 'close (' || CASE WHEN typeof("EodClose") = 'real' THEN rtrim(rtrim(CAST("EodClose" AS TEXT), '0'), '.') ELSE CAST("EodClose" AS TEXT) END || ') must be greater than 0 - the rate AdjustedClose / Close cannot be computed'
                                    WHEN 'AdjustedClose' THEN 'adjusted_close (' || CASE WHEN typeof("AdjustedClose") = 'real' THEN rtrim(rtrim(CAST("AdjustedClose" AS TEXT), '0'), '.') ELSE CAST("AdjustedClose" AS TEXT) END || ') must be greater than 0'
                                    ELSE 'converted ' || lower("ColumnName") || ' (' || "Value" || ') must be greater than 0' END
        WHEN 'market_unknown' THEN CASE WHEN "MarketNote" IS NOT NULL
                                    THEN "Ticker" || ' is registered in several markets (' || "MarketNote" || ') - promote with --market'
                                    ELSE 'the market of ' || "Symbol" || ' is unknown - register it (symbols set ' || "Ticker"
                                         || ' --market NYSE), promote with --market or set [eod_api] default_market' END
        WHEN 'high_above_low' THEN 'converted high (' || "Value" || ') is smaller than low (' || CASE WHEN typeof("Low") = 'real' THEN rtrim(rtrim(CAST("Low" AS TEXT), '0'), '.') ELSE CAST("Low" AS TEXT) END || ')'
        WHEN 'price_range' THEN 'converted ' || lower("ColumnName") || ' (' || "Value" || ') is outside the low..high range ('
                                 || CASE WHEN typeof("Low") = 'real' THEN rtrim(rtrim(CAST("Low" AS TEXT), '0'), '.') ELSE CAST("Low" AS TEXT) END || '..' || CASE WHEN typeof("High") = 'real' THEN rtrim(rtrim(CAST("High" AS TEXT), '0'), '.') ELSE CAST("High" AS TEXT) END || ')'
        WHEN 'market_closed' THEN "Market" || ' was closed on ' || "TradeDate" || ' ('
                                  || COALESCE((SELECT x."Reason" FROM "_promote_closed_days" x WHERE x."Market" = "_promote_eod"."Market"
                                                 AND x."TradeDate" = "_promote_eod"."TradeDate"), 'no trading day') || ')'
        WHEN 'before_first_trade_date' THEN "Ticker" || ' started to trade on '
                                  || (SELECT s."FirstTradeDate" FROM "_promote_symbols" s WHERE s."Market" = "_promote_eod"."Market"
                                       AND s."Ticker" = "_promote_eod"."Ticker") || ' - data for ' || "TradeDate" || ' is before its first trade date'
        WHEN 'after_last_trade_date' THEN "Ticker" || ' stopped trading on '
                                  || (SELECT s."LastTradeDate" FROM "_promote_symbols" s WHERE s."Market" = "_promote_eod"."Market"
                                       AND s."Ticker" = "_promote_eod"."Ticker") || ' - data for ' || "TradeDate" || ' is after its last trade date'
        WHEN 'market_mismatch' THEN 'Stock_Daily already holds ' || "Ticker" || ' ' || "TradeDate" || ' in market '
                                  || COALESCE((SELECT d."Market" FROM "Stock_Daily" d WHERE d."Ticker" = "_promote_eod"."Ticker"
                                                 AND d."TradeDate" = "_promote_eod"."TradeDate"), '(none)')
                                  || ', not ' || "Market" || ' - it is not overwritten'
    END
 WHERE "Rule" IS NOT NULL;
SELECT "Rule", COUNT(*) FROM "_promote_eod" WHERE "Rule" IS NOT NULL GROUP BY "Rule";

-- step 6: rejected rows -> Data_Error (once per open finding)
INSERT INTO "Data_Error" ("DetectedAt", "SourceFile", "LoadId", "Market", "Ticker", "TradeDate",
                                  "ColumnName", "Value", "Rule", "Severity", "Alert", "ErrorMessage", "LineNo")
SELECT :now, c."SourceFile", c."LoadId", c."Market", c."Ticker", c."TradeDate", c."ColumnName", c."Value",
       c."Rule", 'error', 0, 'promotion of ' || c."Symbol" || ' to Stock_Daily rejected the row: ' || c."Message", NULL
  FROM "_promote_eod" c
 WHERE c."Rule" IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM "Data_Error" e
                    WHERE e."Resolved" = 0 AND e."Rule" = c."Rule" AND e."SourceFile" IS c."SourceFile"
                      AND e."Ticker" IS c."Ticker" AND e."TradeDate" IS c."TradeDate"
                      AND e."ColumnName" IS c."ColumnName");

-- step 7: one row per stock/day - when several symbols of the same ticker (MRK, MRK.US) have the
-- day, the most recently updated EOD row wins
CREATE TEMP TABLE "_promote_rows" AS
SELECT c."Market", c."Ticker", c."TradeDate", c."Open", c."High", c."Low", c."Close", c."Volume",
       c."SourceFile", c."LoadId"
  FROM "_promote_eod" c
 WHERE c."Rule" IS NULL
   AND NOT EXISTS (SELECT 1 FROM "_promote_eod" c2
                    WHERE c2."Ticker" = c."Ticker" AND c2."TradeDate" = c."TradeDate" AND c2."Rule" IS NULL
                      AND (c2."UpdatedAt" > c."UpdatedAt"
                           OR (c2."UpdatedAt" = c."UpdatedAt" AND c2."EodStageId" > c."EodStageId")))
 ORDER BY c."Ticker", c."TradeDate";
SELECT (SELECT COUNT(*) FROM "_promote_eod" WHERE "Rule" IS NULL) - (SELECT COUNT(*) FROM "_promote_rows");

-- step 8: how many rows already exist ...
SELECT COUNT(*) FROM "_promote_rows" r JOIN "Stock_Daily" d ON d."Ticker" = r."Ticker" AND d."TradeDate" = r."TradeDate";

-- ... and how many of them changed
SELECT COUNT(*) FROM "_promote_rows" r JOIN "Stock_Daily" d ON d."Ticker" = r."Ticker" AND d."TradeDate" = r."TradeDate" WHERE (d."Open" IS NOT r."Open" OR d."High" IS NOT r."High" OR d."Low" IS NOT r."Low" OR d."Close" IS NOT r."Close" OR d."Volume" IS NOT r."Volume" OR d."Market" IS NOT r."Market" OR d."SourceFile" IS NOT r."SourceFile" OR d."LoadId" IS NOT r."LoadId" OR d."StageId" IS NOT NULL);

-- upsert on (Ticker, TradeDate): new stock/days are inserted, existing ones (same market - step 5)
-- updated only if something changed; UpdatedAt drives the incremental weekly / monthly promotion
INSERT INTO "Stock_Daily" ("Market", "Ticker", "TradeDate", "Open", "High", "Low", "Close", "Volume",
                     "SourceFile", "LoadId", "StageId", "PromotedAt", "UpdatedAt")
SELECT r."Market", r."Ticker", r."TradeDate", r."Open", r."High", r."Low", r."Close", r."Volume",
       r."SourceFile", r."LoadId", NULL, :now, :now
  FROM "_promote_rows" r
 WHERE true
ON CONFLICT ("Ticker", "TradeDate") DO UPDATE SET
       "Market" = excluded."Market", "Open" = excluded."Open", "High" = excluded."High",
       "Low" = excluded."Low", "Close" = excluded."Close", "Volume" = excluded."Volume",
       "SourceFile" = excluded."SourceFile", "LoadId" = excluded."LoadId", "StageId" = NULL,
       "UpdatedAt" = excluded."UpdatedAt"
 WHERE "Stock_Daily"."Open" IS NOT excluded."Open" OR "Stock_Daily"."High" IS NOT excluded."High"
    OR "Stock_Daily"."Low" IS NOT excluded."Low" OR "Stock_Daily"."Close" IS NOT excluded."Close"
    OR "Stock_Daily"."Volume" IS NOT excluded."Volume" OR "Stock_Daily"."Market" IS NOT excluded."Market"
    OR "Stock_Daily"."SourceFile" IS NOT excluded."SourceFile" OR "Stock_Daily"."LoadId" IS NOT excluded."LoadId"
    OR "Stock_Daily"."StageId" IS NOT NULL;

-- step 9: earlier findings of this procedure for the stock/days promoted now are resolved (e.g.
-- market_unknown after the symbol was registered)
UPDATE "Data_Error"
   SET "Resolved" = 1, "ResolvedAt" = :now, "ResolutionNote" = 'promoted by promote_eod_daily'
 WHERE "Resolved" = 0
   AND "ErrorMessage" LIKE 'promotion of % to Stock_Daily rejected the row: %'
   AND EXISTS (SELECT 1 FROM "_promote_rows" r WHERE r."Ticker" = "Data_Error"."Ticker"
                                          AND r."TradeDate" = "Data_Error"."TradeDate");

COMMIT;
