-- promote_monthly: Stock_Daily -> Stock_Monthly (one row per stock and month).
-- Parameters: :now. Incremental mode adds "UpdatedAt" >= :since to step 1 and skips step 5.
-- Generated from stock_dataload/promotion.py - run by PromotionService inside ONE transaction (BEGIN IMMEDIATE ... COMMIT).
-- The temporary tables live in the connection only. SQLite has no stored procedures: this is the procedure.

BEGIN IMMEDIATE;

-- the temporary tables live in this connection only (they vanish with it)
DROP TABLE IF EXISTS "_promote_periods";
DROP TABLE IF EXISTS "_promote_agg";
DROP TABLE IF EXISTS "_promote_rows";
DROP TABLE IF EXISTS "_promote_open_days";
DROP TABLE IF EXISTS "_promote_symbols";
DROP TABLE IF EXISTS "_promote_gaps";

-- step 1: the stock/calendar month periods to (re)compute - every period of the daily table (full)
CREATE TEMP TABLE "_promote_periods" AS
SELECT DISTINCT d."Ticker", date(d."TradeDate", 'start of month') AS "PeriodStart"
  FROM "Stock_Daily" d;
SELECT COUNT(*) FROM "_promote_periods";

-- step 2: aggregate the trading days of each period (high = highest high, low = lowest low, volume
-- = sum); the daily rows are found through the UNIQUE (Ticker, TradeDate) index
CREATE TEMP TABLE "_promote_agg" AS
SELECT p."Ticker", p."PeriodStart", date(p."PeriodStart", '+1 month', '-1 day') AS "PeriodEnd",
       MIN(d."TradeDate") AS "FirstTradeDate", MAX(d."TradeDate") AS "LastTradeDate",
       COUNT(*) AS "TradingDays", MAX(d."High") AS "High", MIN(d."Low") AS "Low", SUM(d."Volume") AS "Volume"
  FROM "_promote_periods" p
  JOIN "Stock_Daily" d ON d."Ticker" = p."Ticker" AND d."TradeDate" >= p."PeriodStart" AND d."TradeDate" <= date(p."PeriodStart", '+1 month', '-1 day')
 GROUP BY p."Ticker", p."PeriodStart";

-- step 3: open = open of the first trading day, close = close of the last trading day, market =
-- market of the last day; calendar year and month number
CREATE TEMP TABLE "_promote_rows" AS
SELECT l."Market", a."Ticker", a."PeriodStart", a."PeriodEnd",
       CAST(strftime('%Y', a."PeriodStart") AS INTEGER) AS "Year",
       CAST(strftime('%m', a."PeriodStart") AS INTEGER) AS "Month",
       f."Open", a."High", a."Low", l."Close", a."Volume", a."TradingDays", a."FirstTradeDate", a."LastTradeDate",
       0 AS "ExpectedDays", 0 AS "MissingDays", NULL AS "MissingDates", 1 AS "Complete"
  FROM "_promote_agg" a
  JOIN "Stock_Daily" f ON f."Ticker" = a."Ticker" AND f."TradeDate" = a."FirstTradeDate"
  JOIN "Stock_Daily" l ON l."Ticker" = a."Ticker" AND l."TradeDate" = a."LastTradeDate";
CREATE INDEX "_ix_promote_rows" ON "_promote_rows" ("Ticker", "PeriodStart");

-- step 3b (validation): the days the markets were open over the periods, between the earliest and
-- the latest trade date loaded for the market - filled by the application from the No_Trading_Day
-- table
CREATE TEMP TABLE "_promote_open_days" ("Market" TEXT NOT NULL, "TradeDate" TEXT NOT NULL, PRIMARY KEY ("Market", "TradeDate"));

-- the trading window of every stock with known dates (Stock_Symbol table)
CREATE TEMP TABLE "_promote_symbols" AS
SELECT "Market", "Ticker", "FirstTradeDate", "LastTradeDate"
  FROM "Stock_Symbol"
 WHERE "FirstTradeDate" IS NOT NULL OR "LastTradeDate" IS NOT NULL;
CREATE INDEX "_ix_promote_symbols" ON "_promote_symbols" ("Market", "Ticker");

-- every trading day of the period (market open, inside the stock's trading window if its first /
-- last trade date is known) must have a daily row - count the expected and the missing days per
-- stock and period
CREATE TEMP TABLE "_promote_gaps" AS
SELECT "Ticker", "PeriodStart", COUNT(*) AS "ExpectedDays",
       SUM("Missing") AS "MissingDays",
       GROUP_CONCAT(CASE WHEN "Missing" = 1 THEN "TradeDate" END, ',') AS "MissingDates"
  FROM (SELECT r."Ticker", r."PeriodStart", o."TradeDate",
               CASE WHEN d."TradeDate" IS NULL THEN 1 ELSE 0 END AS "Missing"
          FROM "_promote_rows" r
          JOIN "_promote_open_days" o ON o."Market" = COALESCE(r."Market", '')
                                AND o."TradeDate" BETWEEN r."PeriodStart" AND r."PeriodEnd"
          LEFT JOIN "_promote_symbols" s ON s."Market" = COALESCE(r."Market", '') AND s."Ticker" = r."Ticker"
          LEFT JOIN "Stock_Daily" d ON d."Ticker" = r."Ticker" AND d."TradeDate" = o."TradeDate"
         WHERE (s."FirstTradeDate" IS NULL OR o."TradeDate" >= s."FirstTradeDate")
           AND (s."LastTradeDate" IS NULL OR o."TradeDate" <= s."LastTradeDate")
         ORDER BY r."Ticker", r."PeriodStart", o."TradeDate")
 GROUP BY "Ticker", "PeriodStart";
CREATE INDEX "_ix_promote_gaps" ON "_promote_gaps" ("Ticker", "PeriodStart");

-- mark the incomplete periods
UPDATE "_promote_rows" SET
       "ExpectedDays" = COALESCE((SELECT g."ExpectedDays" FROM "_promote_gaps" g
                                   WHERE g."Ticker" = "_promote_rows"."Ticker" AND g."PeriodStart" = "_promote_rows"."PeriodStart"), 0),
       "MissingDays" = COALESCE((SELECT g."MissingDays" FROM "_promote_gaps" g
                                  WHERE g."Ticker" = "_promote_rows"."Ticker" AND g."PeriodStart" = "_promote_rows"."PeriodStart"), 0),
       "MissingDates" = (SELECT g."MissingDates" FROM "_promote_gaps" g
                          WHERE g."Ticker" = "_promote_rows"."Ticker" AND g."PeriodStart" = "_promote_rows"."PeriodStart");
UPDATE "_promote_rows" SET "Complete" = CASE WHEN "MissingDays" > 0 THEN 0 ELSE 1 END;
SELECT COUNT(*) FROM "_promote_rows" WHERE "Complete" = 0;

-- incomplete periods -> Data_Error (once per open finding)
INSERT INTO "Data_Error" ("DetectedAt", "SourceFile", "LoadId", "Market", "Ticker", "TradeDate",
                                  "ColumnName", "Value", "Rule", "Severity", "Alert", "ErrorMessage", "LineNo")
SELECT :now, 'Stock_Monthly', NULL, r."Market", r."Ticker", r."PeriodStart", 'TradingDays',
       r."TradingDays" || '/' || r."ExpectedDays", 'incomplete_month', 'error', 0,
       r."Ticker" || ' ' || 'month ' || substr(r."PeriodStart", 1, 7) || ' is incomplete: ' || (r."ExpectedDays" - r."MissingDays") || ' of '
       || r."ExpectedDays" || ' trading days loaded - missing ' || r."MissingDates", NULL
  FROM "_promote_rows" r
 WHERE r."Complete" = 0
   AND NOT EXISTS (SELECT 1 FROM "Data_Error" e
                    WHERE e."Resolved" = 0 AND e."Rule" = 'incomplete_month' AND e."SourceFile" = 'Stock_Monthly'
                      AND e."Ticker" = r."Ticker" AND e."TradeDate" = r."PeriodStart");

-- periods that are complete again resolve their open finding
UPDATE "Data_Error"
   SET "Resolved" = 1, "ResolvedAt" = :now,
       "ResolutionNote" = 'complete after the promotion of ' || :now
 WHERE "Resolved" = 0 AND "Rule" = 'incomplete_month' AND "SourceFile" = 'Stock_Monthly'
   AND EXISTS (SELECT 1 FROM "_promote_rows" r
                WHERE r."Ticker" = "Data_Error"."Ticker" AND r."PeriodStart" = "Data_Error"."TradeDate"
                  AND r."Complete" = 1);

-- step 4: how many periods already exist ...
SELECT COUNT(*) FROM "_promote_rows" r JOIN "Stock_Monthly" t ON t."Ticker" = r."Ticker" AND t."MonthStart" = r."PeriodStart";

-- ... and how many of them changed
SELECT COUNT(*) FROM "_promote_rows" r JOIN "Stock_Monthly" t ON t."Ticker" = r."Ticker" AND t."MonthStart" = r."PeriodStart" WHERE t."Open" IS NOT r."Open" OR t."High" IS NOT r."High" OR t."Low" IS NOT r."Low" OR t."Close" IS NOT r."Close" OR t."Volume" IS NOT r."Volume" OR t."TradingDays" IS NOT r."TradingDays" OR t."FirstTradeDate" IS NOT r."FirstTradeDate" OR t."LastTradeDate" IS NOT r."LastTradeDate" OR t."Market" IS NOT r."Market" OR t."MonthEnd" IS NOT r."PeriodEnd" OR t."Year" IS NOT r."Year" OR t."Month" IS NOT r."Month" OR t."ExpectedDays" IS NOT r."ExpectedDays" OR t."MissingDays" IS NOT r."MissingDays" OR t."MissingDates" IS NOT r."MissingDates" OR t."Complete" IS NOT r."Complete";

-- upsert: new periods are inserted, existing ones updated only if something changed
INSERT INTO "Stock_Monthly" ("Market", "Ticker", "MonthStart", "MonthEnd", "Year", "Month",
                      "Open", "High", "Low", "Close", "Volume", "TradingDays", "FirstTradeDate", "LastTradeDate",
                      "ExpectedDays", "MissingDays", "MissingDates", "Complete", "PromotedAt", "UpdatedAt")
SELECT r."Market", r."Ticker", r."PeriodStart", r."PeriodEnd", r."Year", r."Month",
       r."Open", r."High", r."Low", r."Close", r."Volume", r."TradingDays", r."FirstTradeDate", r."LastTradeDate",
       r."ExpectedDays", r."MissingDays", r."MissingDates", r."Complete", :now, :now
  FROM "_promote_rows" r
 WHERE true
ON CONFLICT ("Ticker", "MonthStart") DO UPDATE SET
       "Market" = excluded."Market", "MonthEnd" = excluded."MonthEnd",
       "Year" = excluded."Year", "Month" = excluded."Month",
       "Open" = excluded."Open", "High" = excluded."High", "Low" = excluded."Low", "Close" = excluded."Close",
       "Volume" = excluded."Volume", "TradingDays" = excluded."TradingDays",
       "FirstTradeDate" = excluded."FirstTradeDate", "LastTradeDate" = excluded."LastTradeDate",
       "ExpectedDays" = excluded."ExpectedDays", "MissingDays" = excluded."MissingDays",
       "MissingDates" = excluded."MissingDates", "Complete" = excluded."Complete",
       "UpdatedAt" = excluded."UpdatedAt"
 WHERE "Stock_Monthly"."Open" IS NOT excluded."Open" OR "Stock_Monthly"."High" IS NOT excluded."High" OR "Stock_Monthly"."Low" IS NOT excluded."Low" OR "Stock_Monthly"."Close" IS NOT excluded."Close" OR "Stock_Monthly"."Volume" IS NOT excluded."Volume" OR "Stock_Monthly"."TradingDays" IS NOT excluded."TradingDays" OR "Stock_Monthly"."FirstTradeDate" IS NOT excluded."FirstTradeDate" OR "Stock_Monthly"."LastTradeDate" IS NOT excluded."LastTradeDate" OR "Stock_Monthly"."Market" IS NOT excluded."Market" OR "Stock_Monthly"."MonthEnd" IS NOT excluded."MonthEnd" OR "Stock_Monthly"."Year" IS NOT excluded."Year" OR "Stock_Monthly"."Month" IS NOT excluded."Month" OR "Stock_Monthly"."ExpectedDays" IS NOT excluded."ExpectedDays" OR "Stock_Monthly"."MissingDays" IS NOT excluded."MissingDays" OR "Stock_Monthly"."MissingDates" IS NOT excluded."MissingDates" OR "Stock_Monthly"."Complete" IS NOT excluded."Complete";

-- step 5 (full mode only): remove aggregates whose daily rows are gone
DELETE FROM "Stock_Monthly" WHERE NOT EXISTS (SELECT 1 FROM "Stock_Daily" d WHERE d."Ticker" = "Stock_Monthly"."Ticker" AND d."TradeDate" >= "Stock_Monthly"."MonthStart" AND d."TradeDate" <= "Stock_Monthly"."MonthEnd");

COMMIT;
