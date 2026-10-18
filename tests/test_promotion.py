"""
Active tables + promotion procedures (stock_dataload/promotion.py):

  * Stock_Daily / Stock_Weekly / Stock_Monthly DDL: unique and check constraints
  * promote_daily: validation rules, Data_Error findings, de-duplication, upsert,
    incremental / full / filtered runs, Promotion_History, rollback on failure
  * promote_weekly / promote_monthly: aggregation (ISO weeks, months), incremental
    recomputation, orphan removal in full mode
  * the reference SQL scripts in sql/ are generated from the module (no drift)
  * CLI: promote / promotions / status / init-db
"""

from __future__ import annotations

import contextlib
import io
import sqlite3
import time
import unittest
import unittest.mock as mock
from datetime import date, timedelta
from pathlib import Path

from stock_dataload import cli
from stock_dataload.audit import AuditLogger
from stock_dataload.config import Settings, load_settings, save_settings
from stock_dataload.correction import CorrectionService
from stock_dataload.db import StageDatabase, StageRow, now_iso
from stock_dataload.loader import StageLoader
from stock_dataload.promotion import (PROC_DAILY, PROC_MONTHLY, PROC_WEEKLY, PromotionError, PromotionService,
                                      normalise_date, render_procedure)
from stock_dataload.quality import DataErrorRepository

from .helpers import DEFAULT_HEADER, TempProjectTestCase

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class PromotionTestCase(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.db = StageDatabase(self.settings)
        self.svc = PromotionService(self.settings, self.db)
        self.db.ensure_stage_table()
        self._stage_id = 0

    # -- helpers ---------------------------------------------------------------
    def stage(self, ticker: str, day: str, o="10", h="12", l="9", c="11", v="1000", source_file="NASDAQ_x.txt",
              load_id="load1", market="NASDAQ", loaded_at: str | None = None) -> int:
        """Insert one staging row directly (as text, like the loader does)."""
        row = StageRow(market, ticker, day, o, h, l, c, v, source_file, load_id, loaded_at or now_iso())
        with self.db.connection() as conn:
            return self.db.insert_stage_row(conn, row)

    def load_file(self, name: str, rows: list[str]) -> None:
        path = self.settings.loading_dir / name
        path.write_text(DEFAULT_HEADER + "".join(r + "\n" for r in rows), encoding="utf-8")
        result = StageLoader(self.settings, self.db).run(files=[path], move_files=False)[0]
        self.assertTrue(result.ok, result.message)

    def query(self, sql: str, params=()) -> list[tuple]:
        with sqlite3.connect(self.settings.sqlite_path) as conn:
            return conn.execute(sql, params).fetchall()

    def daily(self, ticker: str | None = None) -> list[dict]:
        return self.svc.fetch("daily", ticker=ticker, newest_first=False, limit=1000)

    def findings(self) -> list[tuple]:
        return self.query('SELECT "Ticker", "TradeDate", "Rule", "ColumnName", "Value", "ErrorMessage", "Resolved" '
                          'FROM "Data_Error" WHERE "ErrorMessage" LIKE \'promotion%\' ORDER BY "DataErrorId"')


# ===========================================================================
class ActiveTableDdlTests(PromotionTestCase):
    def test_tables_and_constraints_exist(self):
        self.svc.ensure_tables()
        names = {r[0] for r in self.query("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"Stock_Daily", "Stock_Weekly", "Stock_Monthly", "Promotion_History"} <= names)
        ddl = {r[0]: r[1] for r in self.query("SELECT name, sql FROM sqlite_master WHERE type = 'table'")}
        self.assertIn('UNIQUE ("Ticker", "TradeDate")', ddl["Stock_Daily"])
        self.assertIn('UNIQUE ("Ticker", "WeekStart")', ddl["Stock_Weekly"])
        self.assertIn('UNIQUE ("Ticker", "MonthStart")', ddl["Stock_Monthly"])
        for table in ("Stock_Daily", "Stock_Weekly", "Stock_Monthly"):
            for col in ('"Open"       REAL NOT NULL', '"High"', '"Low"', '"Close"', '"Volume"     INTEGER NOT NULL'):
                self.assertIn(col.split()[0], ddl[table])
            self.assertIn("INTEGER NOT NULL", ddl[table])

    def test_unique_constraints_are_enforced(self):
        self.svc.ensure_tables()
        now = now_iso()
        with sqlite3.connect(self.settings.sqlite_path) as conn:
            conn.execute('INSERT INTO "Stock_Daily" ("Ticker","TradeDate","Open","High","Low","Close","Volume",'
                         '"PromotedAt","UpdatedAt") VALUES (?,?,?,?,?,?,?,?,?)',
                         ("AAA", "2025-01-06", 1, 2, 0.5, 1.5, 100, now, now))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute('INSERT INTO "Stock_Daily" ("Ticker","TradeDate","Open","High","Low","Close","Volume",'
                             '"PromotedAt","UpdatedAt") VALUES (?,?,?,?,?,?,?,?,?)',
                             ("AAA", "2025-01-06", 9, 9, 9, 9, 9, now, now))
            weekly = ('INSERT INTO "Stock_Weekly" ("Ticker","WeekStart","WeekEnd","ISOYear","ISOWeek","Open","High",'
                      '"Low","Close","Volume","TradingDays","FirstTradeDate","LastTradeDate","PromotedAt","UpdatedAt") '
                      'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)')
            conn.execute(weekly, ("AAA", "2025-01-06", "2025-01-12", 2025, 2, 1, 2, 0.5, 1.5, 100, 5,
                                  "2025-01-06", "2025-01-10", now, now))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(weekly, ("AAA", "2025-01-06", "2025-01-12", 2025, 2, 1, 2, 0.5, 1.5, 100, 5,
                                      "2025-01-06", "2025-01-10", now, now))
            monthly = ('INSERT INTO "Stock_Monthly" ("Ticker","MonthStart","MonthEnd","Year","Month","Open","High",'
                       '"Low","Close","Volume","TradingDays","FirstTradeDate","LastTradeDate","PromotedAt","UpdatedAt") '
                       'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)')
            conn.execute(monthly, ("AAA", "2025-01-01", "2025-01-31", 2025, 1, 1, 2, 0.5, 1.5, 100, 21,
                                   "2025-01-02", "2025-01-31", now, now))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(monthly, ("AAA", "2025-01-01", "2025-01-31", 2025, 1, 1, 2, 0.5, 1.5, 100, 21,
                                       "2025-01-02", "2025-01-31", now, now))

    def test_check_constraints_are_enforced(self):
        self.svc.ensure_tables()
        now = now_iso()
        sql = ('INSERT INTO "Stock_Daily" ("Ticker","TradeDate","Open","High","Low","Close","Volume","PromotedAt",'
               '"UpdatedAt") VALUES (?,?,?,?,?,?,?,?,?)')
        bad = [("AAA", "2025-01-06", 1, 0.4, 0.5, 1.5, 100),    # high < low
               ("AAA", "2025-01-06", 0, 2, 0.5, 1.5, 100),      # price 0
               ("AAA", "2025-01-06", 1, 2, 0.5, 1.5, -1),       # negative volume
               ("AAA", "20250106", 1, 2, 0.5, 1.5, 100)]        # date not ISO
        with sqlite3.connect(self.settings.sqlite_path) as conn:
            for values in bad:
                with self.assertRaises(sqlite3.IntegrityError, msg=str(values)):
                    conn.execute(sql, values + (now, now))

    def test_configurable_table_names(self):
        self.settings.daily_table, self.settings.weekly_table, self.settings.monthly_table = "D", "W", "M"
        svc = PromotionService(self.settings, self.db)
        svc.ensure_tables()
        names = {r[0] for r in self.query("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"D", "W", "M"} <= names)
        self.stage("AAA", "20250106")
        svc.promote_all()
        self.assertEqual(self.query('SELECT COUNT(*) FROM "D"'), [(1,)])
        self.assertEqual(self.query('SELECT COUNT(*) FROM "W"'), [(1,)])
        self.assertEqual(self.query('SELECT COUNT(*) FROM "M"'), [(1,)])
        self.assertEqual(svc.summary("weekly").table, "W")

    def test_reference_sql_scripts_are_in_sync_with_the_module(self):
        """sql/*.sql are generated from promotion.py - regenerate them if this fails."""
        svc = PromotionService(Settings())
        ddl_file = (PROJECT_ROOT / "sql" / "create_stock_active_tables_sqlite.sql").read_text(encoding="utf-8")
        self.assertTrue(ddl_file.endswith(svc.render_ddl()))
        for name in (PROC_DAILY, PROC_WEEKLY, PROC_MONTHLY):
            script = (PROJECT_ROOT / "sql" / f"{name}_sqlite.sql").read_text(encoding="utf-8")
            self.assertEqual(script, render_procedure(name), name)
        with self.assertRaises(ValueError):
            render_procedure("promote_yearly")


# ===========================================================================
class PromoteDailyTests(PromotionTestCase):
    def test_valid_rows_are_promoted_typed_and_deduplicated(self):
        self.stage("AAA", "20250106", "1", "2", "0.5", "1.5", "100")
        self.stage(" BBB ", "20250106", "5", "6", "4", "5.5", "2800.0")       # ticker trimmed, vol 2800.0 -> 2800
        self.stage("CCC", "2025-01-06", "1e2", "+110", "99.5", "105", "1e3")   # ISO date and exotic numbers
        r = self.svc.promote_daily()
        self.assertTrue(r.ok)
        self.assertEqual(r.mode, "full")                # first run
        self.assertEqual((r.rows_read, r.rows_inserted, r.rows_updated, r.rows_unchanged, r.rows_rejected),
                         (3, 3, 0, 0, 0))
        rows = self.daily()
        self.assertEqual([x["Ticker"] for x in rows], ["AAA", "BBB", "CCC"])
        aaa = rows[0]
        self.assertEqual(aaa["TradeDate"], "2025-01-06")
        self.assertEqual((aaa["Open"], aaa["High"], aaa["Low"], aaa["Close"], aaa["Volume"]), (1.0, 2.0, 0.5, 1.5, 100))
        self.assertIsInstance(aaa["Volume"], int)
        self.assertIsInstance(aaa["Close"], float)
        self.assertEqual((aaa["Market"], aaa["SourceFile"], aaa["LoadId"], aaa["StageId"]),
                         ("NASDAQ", "NASDAQ_x.txt", "load1", 1))
        self.assertEqual(aaa["PromotedAt"], aaa["UpdatedAt"])
        self.assertEqual(rows[1]["Volume"], 2800)
        self.assertEqual((rows[2]["Open"], rows[2]["High"], rows[2]["Volume"]), (100.0, 110.0, 1000))
        self.assertIn("3 rows read: 3 inserted", str(r))
        self.assertIsNotNone(r.history_id)

    def test_every_validation_rule(self):
        cases = {
            "ticker_required": ("", "20250106", "1", "2", "0.5", "1.5", "100"),
            "date_is_valid": ("D1", "20250230", "1", "2", "0.5", "1.5", "100"),
            "numeric_columns": ("N1", "20250106", "abc", "2", "0.5", "1.5", "100"),
            "numeric_missing": ("N2", "20250106", "1", "2", "", "1.5", "100"),
            "positive_price": ("P1", "20250106", "1", "2", "0", "1.5", "100"),
            "volume_invalid": ("V1", "20250106", "1", "2", "0.5", "1.5", "-5"),
            "volume_fraction": ("V2", "20250106", "1", "2", "0.5", "1.5", "10.5"),
            "high_above_low": ("H1", "20250106", "1", "0.4", "0.5", "0.45", "100"),
            "price_range_open": ("R1", "20250106", "3", "2", "0.5", "1.5", "100"),
            "price_range_close": ("R2", "20250106", "1", "2", "0.5", "0.1", "100"),
            "market_closed": ("M1", "20250105", "1", "2", "0.5", "1.5", "100"),      # a Sunday
            "market_holiday": ("M2", "20250101", "1", "2", "0.5", "1.5", "100"),     # New Year's Day
        }
        for values in cases.values():
            self.stage(*values)
        self.stage("OK", "20250106", "1", "2", "0.5", "1.5", "100")
        r = self.svc.promote_daily()
        self.assertEqual((r.rows_read, r.rows_inserted, r.rows_rejected), (13, 1, 12))
        self.assertEqual(r.reject_reasons, {"ticker_required": 1, "date_is_valid": 1, "numeric_columns": 2,
                                            "positive_price": 1, "volume_invalid": 2, "high_above_low": 1,
                                            "price_range": 2, "market_closed": 2})
        self.assertEqual([x["Ticker"] for x in self.daily()], ["OK"])
        found = {(f[0], f[2], f[3], f[4]): f[5] for f in self.findings()}
        self.assertEqual(found[(None, "ticker_required", "Ticker", "")], "promotion to Stock_Daily rejected the row: ticker is empty")
        self.assertIn("date '20250230' is not a valid", found[("D1", "date_is_valid", "TradeDate", "20250230")])
        self.assertIn("open 'abc' is not a number", found[("N1", "numeric_columns", "Open", "abc")])
        self.assertIn("low is missing", found[("N2", "numeric_columns", "Low", "")])
        self.assertIn("low (0) must be greater than 0", found[("P1", "positive_price", "Low", "0")])
        self.assertIn("vol (-5) must be a whole number >= 0", found[("V1", "volume_invalid", "Vol", "-5")])
        self.assertIn("vol (10.5) must be", found[("V2", "volume_invalid", "Vol", "10.5")])
        self.assertIn("high (0.4) is smaller than low (0.5)", found[("H1", "high_above_low", "High", "0.4")])
        self.assertIn("open (3) is outside the low..high range (0.5..2)", found[("R1", "price_range", "Open", "3")])
        self.assertIn("close (0.1) is outside", found[("R2", "price_range", "Close", "0.1")])
        self.assertIn("NASDAQ was closed on 2025-01-05 (weekend (Sunday))", found[("M1", "market_closed", "TradeDate", "20250105")])
        self.assertIn("NASDAQ was closed on 2025-01-01 (New Year's Day)", found[("M2", "market_closed", "TradeDate", "20250101")])
        # rejected rows carry the staging provenance so the correction page finds them
        src = self.query('SELECT DISTINCT "SourceFile", "LoadId", "Market", "Severity", "Alert" FROM "Data_Error"')
        self.assertEqual(src, [("NASDAQ_x.txt", "load1", "NASDAQ", "error", 0)])
        self.assertIn("12 rejected [", str(r))
        self.assertIn("market_closed 2", str(r))

    def test_findings_are_recorded_once_while_open(self):
        self.stage("H1", "20250106", "1", "0.4", "0.5", "0.45", "100")
        self.svc.promote_daily(full=True)
        self.svc.promote_daily(full=True)
        self.assertEqual(len(self.findings()), 1)
        # resolved on the correction page -> still invalid -> a new finding
        DataErrorRepository(self.settings).resolve(ticker="H1", trade_date="20250106", note="looked at it")
        self.svc.promote_daily(full=True)
        self.assertEqual([f[6] for f in self.findings()], [1, 0])

    def test_configurable_rules_can_be_switched_off(self):
        self.settings.reject_market_closed_days = False
        self.settings.check_price_range = False
        self.stage("M1", "20250105", "1", "2", "0.5", "1.5", "100")     # Sunday
        self.stage("R1", "20250106", "3", "2", "0.5", "1.5", "100")     # open above high
        r = PromotionService(self.settings, self.db).promote_daily()
        self.assertEqual((r.rows_inserted, r.rows_rejected), (2, 0))
        self.assertNotIn("_promote_closed_days", "")   # (script still valid)

    def test_extra_holidays_from_config_count_as_closed(self):
        self.settings.extra_market_holidays = ("2025-01-07",)
        self.stage("AAA", "20250106")
        self.stage("AAA", "20250107")
        r = PromotionService(self.settings, self.db).promote_daily()
        self.assertEqual((r.rows_inserted, r.rows_rejected), (1, 1))
        self.assertEqual(r.reject_reasons, {"market_closed": 1})

    def test_latest_loaded_copy_of_a_stock_day_wins(self):
        self.stage("AAA", "20250106", "1", "2", "0.5", "1.5", "100", source_file="a.txt")
        self.stage("AAA", "20250106", "1", "3", "0.5", "2.5", "200", source_file="b.txt")
        self.stage("AAA", "20250106", "1", "0.4", "0.5", "1.5", "100", source_file="c.txt")    # invalid: ignored
        r = self.svc.promote_daily()
        self.assertEqual((r.rows_read, r.rows_inserted, r.rows_rejected, r.rows_skipped), (3, 1, 1, 1))
        row = self.daily("AAA")[0]
        self.assertEqual((row["High"], row["Volume"], row["SourceFile"], row["StageId"]), (3.0, 200, "b.txt", 2))
        self.assertIn("1 duplicate(s) skipped", str(r))

    def test_upsert_updates_only_changed_rows(self):
        self.stage("AAA", "20250106", "1", "2", "0.5", "1.5", "100", source_file="a.txt")
        self.stage("BBB", "20250106", "5", "6", "4", "5.5", "200", source_file="a.txt")
        self.svc.promote_daily()
        before = {r["Ticker"]: r for r in self.daily()}
        time.sleep(1.1)
        # a re-delivery of a.txt: AAA changed, BBB identical
        with self.db.connection() as conn:
            self.db.delete_source_file(conn, "a.txt")
        self.stage("AAA", "20250106", "1", "2", "0.5", "1.6", "100", source_file="a.txt")
        self.stage("BBB", "20250106", "5", "6", "4", "5.5", "200", source_file="a.txt")
        self.stage("CCC", "20250106", "5", "6", "4", "5.5", "200", source_file="c.txt",
                   loaded_at="2024-01-01 00:00:00")          # an old row: not part of the incremental run
        r = self.svc.promote_daily()
        self.assertEqual(r.mode, "incremental")
        # BBB: same values but a new staging row -> provenance updated, counts as updated
        self.assertEqual((r.rows_read, r.rows_inserted, r.rows_updated, r.rows_unchanged), (2, 0, 2, 0))
        after = {r["Ticker"]: r for r in self.daily()}
        self.assertEqual(after["AAA"]["Close"], 1.6)
        self.assertNotEqual(after["AAA"]["UpdatedAt"], before["AAA"]["UpdatedAt"])
        self.assertEqual(after["AAA"]["PromotedAt"], before["AAA"]["PromotedAt"])
        self.assertEqual(after["AAA"]["StageId"], 3)
        self.assertEqual(after["BBB"]["StageId"], 4)
        self.assertEqual(self.query('SELECT COUNT(*) FROM "Stock_Daily"'), [(2,)])
        # a full run promotes everything and leaves the rest alone
        r = self.svc.promote_daily(full=True)
        self.assertEqual((r.mode, r.rows_read, r.rows_inserted, r.rows_updated, r.rows_unchanged), ("full", 3, 1, 0, 2))

    def test_incremental_mode_picks_up_new_loads_and_corrections(self):
        self.load_file("NASDAQ_20250106.txt", ["AAA,20250106,1,2,0.5,1.5,100", "BBB,20250106,5,6,4,5.5,200"])
        time.sleep(1.1)          # (rows loaded in the same second as a run starts are re-read by the next run)
        r1 = self.svc.promote_daily()
        self.assertEqual((r1.mode, r1.rows_inserted), ("full", 2))
        time.sleep(1.1)
        r2 = self.svc.promote_daily()
        self.assertEqual((r2.mode, r2.rows_read), ("incremental", 0))
        # a new file
        self.load_file("NASDAQ_20250107.txt", ["AAA,20250107,1,2,0.5,1.5,100", "BBB,20250107,5,6,4,5.5,200"])
        # and a correction of an already promoted row (same LoadedAt as before)
        row = self.db.find_stage_rows(ticker="AAA", trade_date="20250106")[0]
        CorrectionService(self.settings, self.db).correct(row["StageId"], {**row, "Close": "1.9"}, note="fix")
        r3 = self.svc.promote_daily()
        self.assertEqual(r3.mode, "incremental")
        self.assertEqual((r3.rows_read, r3.rows_inserted, r3.rows_updated, r3.rows_unchanged), (3, 2, 1, 0))
        self.assertEqual(self.daily("AAA")[0]["Close"], 1.9)
        self.assertEqual(len(self.daily()), 4)

    def test_incremental_mode_considers_all_copies_of_a_touched_day(self):
        """Older duplicates must not be overridden by a later correction of an older copy."""
        self.stage("AAA", "20250106", "1", "2", "0.5", "1.5", "100", source_file="old.txt",
                   loaded_at="2024-01-01 00:00:00")
        self.stage("AAA", "20250106", "1", "2", "0.5", "1.7", "100", source_file="new.txt",
                   loaded_at="2024-06-01 00:00:00")
        self.svc.promote_daily()
        self.assertEqual(self.daily("AAA")[0]["Close"], 1.7)
        time.sleep(1.1)
        old = self.db.get_stage_row(1)
        CorrectionService(self.settings, self.db).correct(1, {**old, "Close": "1.2"}, note="correct the OLD copy")
        r = self.svc.promote_daily()
        self.assertEqual(r.mode, "incremental")
        self.assertEqual((r.rows_read, r.rows_skipped, r.rows_unchanged), (2, 1, 1))
        self.assertEqual(self.daily("AAA")[0]["Close"], 1.7, "the newer copy still wins")

    def test_filters(self):
        self.stage("AAA", "20250106", source_file="a.txt", load_id="L1")
        self.stage("AAA", "20250107", source_file="a.txt", load_id="L1")
        self.stage("BBB", "20250106", source_file="b.txt", load_id="L2")
        self.stage("BBB", "20250108", source_file="b.txt", load_id="L2")
        r = self.svc.promote_daily(source_file="b.txt")
        self.assertEqual((r.mode, r.filtered, r.rows_inserted), ("full", True, 2))
        r = self.svc.promote_daily(load_id="L1", ticker="AAA", date_from="20250107")
        self.assertEqual(r.rows_inserted, 1)
        self.assertEqual([x["TradeDate"] for x in self.daily()], ["2025-01-06", "2025-01-07", "2025-01-08"])
        r = self.svc.promote_daily(date_from="2025-01-06", date_to="2025-01-06")
        self.assertEqual((r.rows_read, r.rows_inserted, r.rows_unchanged), (2, 1, 1))
        self.assertEqual(r.parameters, {"date_from": "2025-01-06", "date_to": "2025-01-06"})
        with self.assertRaisesRegex(PromotionError, "not a date"):
            self.svc.promote_daily(date_from="yesterday")
        with self.assertRaisesRegex(PromotionError, "date_from must not be after"):
            self.svc.promote_daily(date_from="20250201", date_to="20250101")
        # filtered runs do not move the incremental watermark
        self.assertIsNone(self.svc.last_completed(PROC_DAILY))
        self.assertIsNotNone(self.svc.last_completed(PROC_DAILY, unfiltered_only=False))

    def test_normalise_date(self):
        self.assertEqual(normalise_date("20250106"), "2025-01-06")
        self.assertEqual(normalise_date("2025-01-06"), "2025-01-06")
        self.assertEqual(normalise_date(date(2025, 1, 6)), "2025-01-06")
        self.assertIsNone(normalise_date(""))
        self.assertIsNone(normalise_date(None))
        with self.assertRaises(PromotionError):
            normalise_date("06/01/2025")

    def test_empty_staging_table(self):
        r = self.svc.promote_daily()
        self.assertTrue(r.ok)
        self.assertEqual(r.rows_read, 0)

    def test_history_records_every_run(self):
        self.stage("AAA", "20250106")
        self.svc.promote_daily()
        self.svc.promote_daily(ticker="AAA")
        records = self.svc.history(limit=10)
        self.assertEqual([r.procedure for r in records], [PROC_DAILY, PROC_DAILY])
        self.assertEqual([r.filtered for r in records], [1, 0])
        self.assertEqual(records[0].parameters, '{"ticker": "AAA"}')
        self.assertEqual((records[1].status, records[1].rows_read, records[1].rows_inserted), ("completed", 1, 1))
        self.assertEqual(records[1].target_table, "Stock_Daily")
        self.assertEqual(self.svc.count_history(), 2)
        self.assertEqual(self.svc.count_history(status="failed"), 0)
        self.assertEqual(len(self.svc.history(procedure=PROC_WEEKLY)), 0)

    def test_failure_rolls_back_and_is_recorded(self):
        self.stage("AAA", "20250106")
        self.svc.promote_daily()
        self.stage("BBB", "20250106")
        import stock_dataload.promotion as promotion_module
        real_procedure = promotion_module.daily_procedure

        def broken_procedure(*args, **kwargs):
            steps = real_procedure(*args, **kwargs)
            for step in steps:
                if 'ON CONFLICT ("Ticker", "TradeDate")' in step.sql:      # the upsert hits a broken table
                    step.sql = 'INSERT INTO "Stock_Daily" SELECT * FROM "table_that_vanished"'
            return steps

        with mock.patch.object(promotion_module, "daily_procedure", broken_procedure):
            with self.assertRaisesRegex(PromotionError, "promote_daily failed .*no such table") as ctx:
                self.svc.promote_daily(full=True)
        result = ctx.exception.result
        self.assertEqual(result.status, "failed")
        self.assertIn("insert", result.step)
        self.assertEqual(self.query('SELECT COUNT(*) FROM "Stock_Daily"'), [(1,)], "rolled back")
        history = self.svc.history(limit=1)[0]
        self.assertEqual((history.status, history.procedure), ("failed", PROC_DAILY))
        self.assertIn("no such table", history.message)
        errors = AuditLogger(self.settings).fetch_errors(limit=5, component="promotion")
        self.assertEqual(errors[0].error_type, "OperationalError")
        self.assertTrue(errors[0].operation.startswith("promote_daily/"))
        events = [r.event for r in AuditLogger(self.settings).fetch_logs(limit=10, component="promotion")]
        self.assertIn("promote_daily_failed", events)
        self.assertIn("failed", str(result))
        # the failed run is not a watermark
        r = self.svc.promote_daily()
        self.assertEqual((r.mode, r.rows_inserted), ("incremental", 1))

    def test_null_market_and_unknown_values(self):
        self.stage("AAA", "20250106", market="")
        self.stage("BBB", "20250106", market="  ")
        r = self.svc.promote_daily()
        self.assertEqual(r.rows_inserted, 2)
        self.assertEqual([x["Market"] for x in self.daily()], [None, None])


# ===========================================================================
class PromotePeriodTests(PromotionTestCase):
    def seed(self, days: dict[str, tuple], ticker: str = "AAA") -> None:
        for day, (o, h, l, c, v) in days.items():
            self.stage(ticker, day, str(o), str(h), str(l), str(c), str(v))

    def test_weekly_aggregation(self):
        # ISO week 2 of 2025: Mon 6 .. Fri 10 (Wednesday missing), plus Monday 13
        self.seed({"20250106": (10, 12, 9, 11, 100), "20250107": (11, 15, 10, 14, 200),
                   "20250109": (14, 14.5, 8, 9, 300), "20250110": (9, 10, 8.5, 9.5, 400),
                   "20250113": (9.5, 9.6, 9.4, 9.5, 10)})
        self.svc.promote_daily()
        r = self.svc.promote_weekly()
        self.assertTrue(r.ok)
        self.assertEqual((r.mode, r.rows_read, r.rows_inserted), ("full", 2, 2))
        weeks = self.svc.fetch("weekly", ticker="AAA", newest_first=False)
        w = weeks[0]
        self.assertEqual((w["WeekStart"], w["WeekEnd"], w["ISOYear"], w["ISOWeek"]), ("2025-01-06", "2025-01-12", 2025, 2))
        self.assertEqual((w["Open"], w["High"], w["Low"], w["Close"], w["Volume"], w["TradingDays"]),
                         (10.0, 15.0, 8.0, 9.5, 1000, 4))
        self.assertEqual((w["FirstTradeDate"], w["LastTradeDate"], w["Market"]), ("2025-01-06", "2025-01-10", "NASDAQ"))
        self.assertIsInstance(w["Volume"], int)
        w2 = weeks[1]
        self.assertEqual((w2["WeekStart"], w2["ISOWeek"], w2["TradingDays"], w2["Open"], w2["Close"]),
                         ("2025-01-13", 3, 1, 9.5, 9.5))

    def test_iso_week_boundaries(self):
        self.settings.reject_market_closed_days = False        # keep the calendar out of this
        days = {"20221230": (1, 1, 1, 1, 1),     # Fri -> 2022-W52 (Mon 26 Dec)
                "20230102": (1, 1, 1, 1, 1),     # Mon -> 2023-W01
                "20241230": (1, 1, 1, 1, 1),     # Mon -> 2025-W01 (week of 30 Dec 2024 belongs to 2025)
                "20210101": (1, 1, 1, 1, 1),     # Fri -> 2020-W53
                "20260101": (1, 1, 1, 1, 1)}     # Thu -> 2026-W01 (Mon 29 Dec 2025)
        self.seed(days)
        svc = PromotionService(self.settings, self.db)
        svc.promote_daily()
        svc.promote_weekly()
        rows = {w["FirstTradeDate"]: (w["WeekStart"], w["WeekEnd"], w["ISOYear"], w["ISOWeek"])
                for w in svc.fetch("weekly")}
        for day, expected in rows.items():
            d = date.fromisoformat(day)
            iso = d.isocalendar()
            monday = d - timedelta(days=d.weekday())
            self.assertEqual(expected, (monday.isoformat(), (monday + timedelta(days=6)).isoformat(), iso[0], iso[1]), day)
        self.assertEqual(rows["2022-12-30"][2:], (2022, 52))
        self.assertEqual(rows["2024-12-30"][2:], (2025, 1))
        self.assertEqual(rows["2021-01-01"][2:], (2020, 53))

    def test_monthly_aggregation(self):
        self.seed({"20240227": (10, 12, 9, 11, 100), "20240228": (11, 15, 10, 14, 200),
                   "20240229": (14, 14.5, 8, 9, 300),                       # leap day
                   "20240301": (9, 10, 8.5, 9.5, 400)})
        self.svc.promote_daily()
        r = self.svc.promote_monthly()
        self.assertEqual((r.mode, r.rows_read, r.rows_inserted), ("full", 2, 2))
        months = self.svc.fetch("monthly", ticker="AAA", newest_first=False)
        feb, mar = months
        self.assertEqual((feb["MonthStart"], feb["MonthEnd"], feb["Year"], feb["Month"]), ("2024-02-01", "2024-02-29", 2024, 2))
        self.assertEqual((feb["Open"], feb["High"], feb["Low"], feb["Close"], feb["Volume"], feb["TradingDays"]),
                         (10.0, 15.0, 8.0, 9.0, 600, 3))
        self.assertEqual((feb["FirstTradeDate"], feb["LastTradeDate"]), ("2024-02-27", "2024-02-29"))
        self.assertEqual((mar["MonthStart"], mar["MonthEnd"], mar["TradingDays"], mar["Volume"]),
                         ("2024-03-01", "2024-03-31", 1, 400))

    def test_incremental_recomputes_only_touched_periods(self):
        self.seed({"20250106": (10, 12, 9, 11, 100), "20250113": (11, 15, 10, 14, 200)})
        self.seed({"20250106": (1, 2, 0.5, 1.5, 10)}, ticker="BBB")
        self.svc.promote_daily()
        time.sleep(1.1)          # daily rows updated in the same second as a run starts are re-read by the next run
        self.svc.promote_weekly()
        self.svc.promote_monthly()
        time.sleep(1.1)
        r = self.svc.promote_weekly()
        self.assertEqual((r.mode, r.rows_read, r.rows_unchanged), ("incremental", 0, 0))
        # Tuesday of week 2 arrives for AAA only
        self.seed({"20250107": (11, 20, 10, 19, 1000)})
        self.svc.promote_daily()
        time.sleep(1.1)
        r = self.svc.promote_weekly()
        self.assertEqual((r.mode, r.rows_read, r.rows_inserted, r.rows_updated, r.rows_unchanged),
                         ("incremental", 1, 0, 1, 0))
        w = self.svc.fetch("weekly", ticker="AAA", date_to="2025-01-06")[0]
        self.assertEqual((w["High"], w["Close"], w["Volume"], w["TradingDays"]), (20.0, 19.0, 1100, 2))
        r = self.svc.promote_monthly()
        self.assertEqual((r.mode, r.rows_read, r.rows_updated), ("incremental", 1, 1))
        m = self.svc.fetch("monthly", ticker="AAA")[0]
        self.assertEqual((m["High"], m["Volume"], m["TradingDays"]), (20.0, 1300, 3))
        # BBB untouched
        self.assertEqual(self.svc.fetch("weekly", ticker="BBB")[0]["TradingDays"], 1)

    def test_full_mode_removes_orphaned_aggregates(self):
        self.seed({"20250106": (10, 12, 9, 11, 100), "20250113": (11, 15, 10, 14, 200)})
        self.svc.promote_all()
        self.assertEqual(len(self.svc.fetch("weekly")), 2)
        with sqlite3.connect(self.settings.sqlite_path) as conn:
            conn.execute('DELETE FROM "Stock_Daily" WHERE "TradeDate" = ?', ("2025-01-13",))
        r = self.svc.promote_weekly()                  # incremental: cannot see deletions
        self.assertEqual((r.rows_deleted, len(self.svc.fetch("weekly"))), (0, 2))
        r = self.svc.promote_weekly(full=True)
        self.assertEqual((r.mode, r.rows_read, r.rows_unchanged, r.rows_deleted), ("full", 1, 1, 1))
        self.assertEqual([w["WeekStart"] for w in self.svc.fetch("weekly")], ["2025-01-06"])
        self.assertIn("1 orphaned aggregate(s) deleted", str(r))
        r = self.svc.promote_monthly(full=True)
        self.assertEqual((r.rows_deleted, self.svc.fetch("monthly")[0]["TradingDays"]), (0, 1))

    def test_ticker_filter_is_not_a_watermark(self):
        self.seed({"20250106": (10, 12, 9, 11, 100)})
        self.seed({"20250106": (1, 2, 0.5, 1.5, 10)}, ticker="BBB")
        self.svc.promote_daily()
        r = self.svc.promote_weekly(ticker="AAA")
        self.assertEqual((r.mode, r.filtered, r.rows_inserted), ("full", True, 1))
        self.assertEqual([w["Ticker"] for w in self.svc.fetch("weekly")], ["AAA"])
        r = self.svc.promote_weekly()
        self.assertEqual((r.mode, r.rows_inserted, r.rows_unchanged), ("full", 1, 1))
        self.assertEqual(sorted(w["Ticker"] for w in self.svc.fetch("weekly")), ["AAA", "BBB"])
        # full + ticker deletes orphans of that ticker only
        with sqlite3.connect(self.settings.sqlite_path) as conn:
            conn.execute('DELETE FROM "Stock_Daily"')
        r = self.svc.promote_weekly(full=True, ticker="AAA")
        self.assertEqual(r.rows_deleted, 1)
        self.assertEqual([w["Ticker"] for w in self.svc.fetch("weekly")], ["BBB"])

    def test_promote_all_order_and_failure(self):
        self.seed({"20250106": (10, 12, 9, 11, 100)})
        results = self.svc.promote_all()
        self.assertEqual([r.procedure for r in results], [PROC_DAILY, PROC_WEEKLY, PROC_MONTHLY])
        self.assertTrue(all(r.ok for r in results))
        with mock.patch.object(self.svc, "promote_weekly", side_effect=PromotionError("boom")):
            with self.assertRaises(PromotionError):
                self.svc.promote_all(full=True)
        self.assertEqual(self.svc.history(limit=1)[0].procedure, PROC_DAILY)

    def test_period_failure_is_recorded(self):
        self.seed({"20250106": (10, 12, 9, 11, 100)})
        self.svc.promote_daily()
        with sqlite3.connect(self.settings.sqlite_path) as conn:
            conn.execute('DROP TABLE "Stock_Daily"')
            conn.execute('CREATE TABLE "Stock_Daily" ("x" INTEGER)')      # wrong shape
        with self.assertRaisesRegex(PromotionError, "promote_weekly failed"):
            self.svc.promote_weekly()
        self.assertEqual(self.svc.history(limit=1)[0].status, "failed")

    # -- reading ------------------------------------------------------------------
    def test_fetch_and_summary(self):
        self.seed({"20250106": (10, 12, 9, 11, 100), "20250107": (11, 15, 10, 14, 200)})
        self.seed({"20250106": (1, 2, 0.5, 1.5, 10)}, ticker="BBB")
        self.svc.promote_all()
        rows = self.svc.fetch("daily")
        self.assertEqual([(r["Ticker"], r["TradeDate"]) for r in rows],
                         [("AAA", "2025-01-07"), ("AAA", "2025-01-06"), ("BBB", "2025-01-06")])
        self.assertEqual(len(self.svc.fetch("daily", limit=1)), 1)
        self.assertEqual([r["TradeDate"] for r in self.svc.fetch("daily", ticker="AAA", newest_first=False)],
                         ["2025-01-06", "2025-01-07"])
        self.assertEqual(len(self.svc.fetch("daily", date_from="20250107")), 1)
        self.assertEqual(len(self.svc.fetch("daily", date_to="2025-01-06")), 2)
        with self.assertRaises(PromotionError):
            self.svc.fetch("yearly")
        summaries = {s.kind: s for s in self.svc.summaries()}
        d = summaries["daily"]
        self.assertEqual((d.table, d.rows, d.tickers, d.first_period, d.last_period),
                         ("Stock_Daily", 3, 2, "2025-01-06", "2025-01-07"))
        self.assertEqual(d.last_run.procedure, PROC_DAILY)
        self.assertEqual((summaries["weekly"].rows, summaries["weekly"].first_period), (2, "2025-01-06"))
        self.assertEqual((summaries["monthly"].rows, summaries["monthly"].first_period), (2, "2025-01-01"))
        self.assertEqual(self.svc.count_rows("weekly"), 2)
        self.settings.daily_table = "Other_Daily"
        s = PromotionService(self.settings).summary("daily")
        self.assertEqual((s.rows, s.first_period, s.last_run), (0, None, None))


# ===========================================================================
class PromotionCliTests(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.settings.config_file = self.root / "config.ini"
        save_settings(self.settings, self.settings.config_file)
        self.db = StageDatabase(self.settings)
        self.db.ensure_stage_table()
        with self.db.connection() as conn:
            for day in ("20250106", "20250107", "20250105"):       # Sunday 5 Jan is rejected
                self.db.insert_stage_row(conn, StageRow("NASDAQ", "AAA", day, "1", "2", "0.5", "1.5", "100",
                                                        "NASDAQ_x.txt", "L1", now_iso()))

    def run_cli(self, *args) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["--config", str(self.settings.config_file), "-q", *args])
        return rc, out.getvalue(), err.getvalue()

    def test_promote_commands(self):
        rc, out, _ = self.run_cli("promote", "daily")
        self.assertEqual(rc, 0)
        self.assertIn("promote_daily: completed (full) - 3 rows read: 2 inserted", out)
        self.assertIn("1 rejected [market_closed 1]", out)
        self.assertIn("1 staging row(s) were rejected", out)
        rc, out, _ = self.run_cli("promote", "weekly")
        self.assertIn("promote_weekly: completed (full) - 1 rows read: 1 inserted", out)
        rc, out, _ = self.run_cli("promote", "monthly", "--full")
        self.assertIn("promote_monthly: completed (full)", out)
        rc, out, _ = self.run_cli("promote", "all")
        self.assertEqual(rc, 0)
        self.assertEqual(out.count("completed (incremental)"), 3)
        rc, out, _ = self.run_cli("promote", "daily", "--ticker", "AAA", "--from", "20250107", "--to", "2025-01-07")
        self.assertIn("1 rows read", out)
        rc, out, err = self.run_cli("promote", "weekly", "--ticker", "AAA")
        self.assertEqual(rc, 0)
        self.assertIn("promote_weekly: completed (full)", out)
        rc, out, err = self.run_cli("promote", "weekly", "--file", "x.txt")
        self.assertEqual(rc, 2)
        self.assertIn("only apply to 'promote daily'", err)
        rc, out, err = self.run_cli("promote", "daily", "--from", "bad")
        self.assertEqual(rc, 1)
        self.assertIn("not a date", err)

    def test_promotions_status_and_init_db(self):
        rc, out, _ = self.run_cli("promotions")
        self.assertIn("no promotion runs yet", out)
        self.run_cli("promote", "all")
        self.run_cli("promote", "daily", "--ticker", "AAA")
        rc, out, _ = self.run_cli("promotions", "--limit", "5")
        self.assertEqual(rc, 0)
        self.assertIn("promote_daily", out)
        self.assertIn("full*", out)             # filtered run marked
        rc, out, _ = self.run_cli("promotions", "--procedure", "promote_weekly")
        self.assertNotIn("promote_daily", out)
        rc, out, _ = self.run_cli("status")
        self.assertIn("active tables:", out)
        self.assertIn("Stock_Daily", out)
        self.assertIn("2 rows", out)
        self.assertIn("last promote_daily completed", out)
        rc, out, _ = self.run_cli("init-db")
        self.assertIn("active tables ready: Stock_Daily, Stock_Weekly, Stock_Monthly", out)
        self.assertIn("Promotion_History", out)

    def test_promote_failure_exit_code(self):
        with mock.patch.object(PromotionService, "promote_daily", side_effect=PromotionError("boom")):
            rc, out, err = self.run_cli("promote", "daily")
        self.assertEqual(rc, 1)
        self.assertIn("boom", err)


class PromotionConfigTests(TempProjectTestCase):
    def test_settings_parse_and_round_trip(self):
        cfg = self.write("cfg/config.ini", """
[database]
daily_table = Active_Daily
weekly_table = Active_Weekly
monthly_table = Active_Monthly
[promotion]
reject_market_closed_days = false
check_price_range = false
""")
        s = load_settings(cfg)
        self.assertEqual((s.daily_table, s.weekly_table, s.monthly_table), ("Active_Daily", "Active_Weekly", "Active_Monthly"))
        self.assertFalse(s.reject_market_closed_days)
        self.assertFalse(s.check_price_range)
        target = self.root / "cfg" / "saved.ini"
        save_settings(s, target)
        s2 = load_settings(target)
        self.assertEqual((s2.daily_table, s2.reject_market_closed_days, s2.check_price_range),
                         ("Active_Daily", False, False))
        text = target.read_text()
        self.assertIn("[promotion]", text)
        self.assertIn("monthly_table = Active_Monthly", text)

    def test_defaults_and_shipped_config(self):
        s = Settings()
        self.assertEqual((s.daily_table, s.weekly_table, s.monthly_table), ("Stock_Daily", "Stock_Weekly", "Stock_Monthly"))
        self.assertTrue(s.reject_market_closed_days and s.check_price_range)
        shipped = load_settings(PROJECT_ROOT / "config.ini")
        self.assertEqual(shipped.daily_table, "Stock_Daily")
        self.assertTrue(shipped.reject_market_closed_days)

    def test_table_names_must_differ(self):
        from stock_dataload.config import ConfigError
        cfg = self.write("cfg/config.ini", "[database]\nstage_table = Same\ndaily_table = same\n")
        with self.assertRaisesRegex(ConfigError, "must differ"):
            load_settings(cfg)


if __name__ == "__main__":
    unittest.main()


class CompletenessTests(PromotionTestCase):
    """promote_weekly / promote_monthly validation: every trading day of the period must be there."""

    def seed_days(self, ticker: str, days: list[str]) -> None:
        for day in days:
            self.stage(ticker, day)

    def findings(self, rule: str) -> list[tuple]:
        return self.query('SELECT "Ticker", "TradeDate", "Value", "ErrorMessage", "Resolved", "SourceFile", "ColumnName" '
                          'FROM "Data_Error" WHERE "Rule" = ? ORDER BY "Ticker", "TradeDate"', (rule,))

    def test_week_with_a_trading_day_missing_is_a_data_error(self):
        self.seed_days("AAA", ["20250106", "20250107", "20250109", "20250110"])   # Wednesday 8 Jan missing
        self.seed_days("BBB", ["20250106", "20250107", "20250108", "20250109", "20250110"])
        self.svc.promote_daily()
        r = self.svc.promote_weekly()
        self.assertEqual((r.rows_inserted, r.rows_incomplete), (2, 1))
        self.assertIn("1 incomplete (a trading day missing - see data errors)", str(r))
        weeks = {w["Ticker"]: w for w in self.svc.fetch("weekly")}
        self.assertEqual((weeks["AAA"]["TradingDays"], weeks["AAA"]["ExpectedDays"], weeks["AAA"]["MissingDays"],
                          weeks["AAA"]["MissingDates"], weeks["AAA"]["Complete"]), (4, 5, 1, "2025-01-08", 0))
        self.assertEqual((weeks["BBB"]["ExpectedDays"], weeks["BBB"]["MissingDays"], weeks["BBB"]["MissingDates"],
                          weeks["BBB"]["Complete"]), (5, 0, None, 1))
        # the aggregate is still computed from the days there are
        self.assertEqual(weeks["AAA"]["Volume"], 4000)
        found = self.findings("incomplete_week")
        self.assertEqual(len(found), 1)
        ticker, period, value, message, resolved, source, column = found[0]
        self.assertEqual((ticker, period, value, resolved, source, column), ("AAA", "2025-01-06", "4/5", 0, "Stock_Weekly", "TradingDays"))
        self.assertEqual(message, "AAA week 2025-W02 (2025-01-06..2025-01-12) is incomplete: 4 of 5 trading days loaded - missing 2025-01-08")
        self.assertEqual(self.svc.history(limit=1)[0].rows_incomplete, 1)
        self.assertEqual([w["Ticker"] for w in self.svc.fetch("weekly", incomplete_only=True)], ["AAA"])
        # promoting again does not duplicate the finding
        self.svc.promote_weekly(full=True)
        self.assertEqual(len(self.findings("incomplete_week")), 1)

    def test_month_with_a_trading_day_missing_is_a_data_error(self):
        january = ["20250102", "20250103", "20250106", "20250107", "20250108", "20250110", "20250113", "20250114",
                   "20250115", "20250116", "20250117", "20250121", "20250122", "20250123", "20250124", "20250127",
                   "20250128", "20250129", "20250130", "20250131"]              # 9 Jan (closure) + 20 Jan (MLK) closed
        self.seed_days("AAA", january)                                            # complete
        self.seed_days("BBB", [d for d in january if d != "20250115"])           # one day missing
        self.settings.extra_market_holidays = ("2025-01-09",)
        svc = PromotionService(self.settings, self.db)
        svc.promote_daily()
        r = svc.promote_monthly()
        self.assertEqual((r.rows_inserted, r.rows_incomplete), (2, 1))
        months = {m["Ticker"]: m for m in svc.fetch("monthly")}
        self.assertEqual((months["AAA"]["ExpectedDays"], months["AAA"]["Complete"]), (20, 1))
        self.assertEqual((months["BBB"]["TradingDays"], months["BBB"]["ExpectedDays"], months["BBB"]["MissingDates"],
                          months["BBB"]["Complete"]), (19, 20, "2025-01-15", 0))
        found = self.findings("incomplete_month")
        self.assertEqual(found[0][3], "BBB month 2025-01 is incomplete: 19 of 20 trading days loaded - missing 2025-01-15")
        self.assertEqual(found[0][5], "Stock_Monthly")

    def test_finding_is_resolved_once_the_gap_is_filled(self):
        self.seed_days("AAA", ["20250106", "20250107", "20250109"])
        self.svc.promote_all()
        self.assertEqual([f[4] for f in self.findings("incomplete_week")], [0])
        self.assertEqual([f[4] for f in self.findings("incomplete_month")], [0])
        time.sleep(1.1)
        self.stage("AAA", "20250108", source_file="late.txt")
        self.svc.promote_daily()
        time.sleep(1.1)
        r = self.svc.promote_weekly()
        self.assertEqual((r.mode, r.rows_updated, r.rows_incomplete), ("incremental", 1, 0))
        week = self.svc.fetch("weekly")[0]
        self.assertEqual((week["TradingDays"], week["MissingDays"], week["MissingDates"], week["Complete"]), (4, 0, None, 1))
        ticker, period, value, message, resolved, source, column = self.findings("incomplete_week")[0]
        self.assertEqual(resolved, 1)
        note = self.query('SELECT "ResolutionNote" FROM "Data_Error" WHERE "Rule" = ?', ("incomplete_week",))[0][0]
        self.assertTrue(note.startswith("complete after the promotion of"))
        self.svc.promote_monthly()
        self.assertEqual([f[4] for f in self.findings("incomplete_month")], [1])
        # a new gap later raises a new finding (the resolved one stays resolved)
        time.sleep(1.1)
        self.stage("AAA", "20250114")
        self.svc.promote_daily()
        self.svc.promote_weekly()
        self.assertEqual([(f[1], f[4]) for f in self.findings("incomplete_week")], [("2025-01-06", 1), ("2025-01-13", 0)])

    def test_expected_days_follow_the_calendar_the_trading_window_and_the_data_history(self):
        from stock_dataload.symbols import SymbolRegistry
        from stock_dataload.trading_calendar import TradingCalendar
        TradingCalendar(self.settings, self.db).add_day("NASDAQ", "2025-01-08", "ad-hoc closure")
        reg = SymbolRegistry(self.settings, self.db)
        reg.set("IPO", "NASDAQ", first_trade_date="2025-01-09")          # lists on Thursday
        reg.set("BYE", "NASDAQ", last_trade_date="2025-01-07")           # delisted after Tuesday
        self.seed_days("AAA", ["20250106", "20250107", "20250109", "20250110"])      # Wednesday closed: complete
        self.seed_days("IPO", ["20250109", "20250110"])                               # complete from its listing
        self.seed_days("BYE", ["20250106", "20250107"])                               # complete until delisting
        self.seed_days("NEW", ["20250109", "20250110"])                               # no first trade date: expected all week
        self.seed_days("LAST", ["20250110"])                                          # (data history ends Friday)
        self.svc.promote_daily()
        r = self.svc.promote_weekly()
        weeks = {w["Ticker"]: (w["ExpectedDays"], w["MissingDays"], w["MissingDates"], w["Complete"]) for w in self.svc.fetch("weekly")}
        self.assertEqual(weeks["AAA"], (4, 0, None, 1))
        self.assertEqual(weeks["IPO"], (2, 0, None, 1))
        self.assertEqual(weeks["BYE"], (2, 0, None, 1))
        self.assertEqual(weeks["NEW"], (4, 2, "2025-01-06,2025-01-07", 0))
        self.assertEqual(weeks["LAST"], (4, 3, "2025-01-06,2025-01-07,2025-01-09", 0))
        self.assertEqual(r.rows_incomplete, 2)
        # recording NEW's listing date and promoting again clears its finding
        reg.set("NEW", "NASDAQ", first_trade_date="2025-01-09")
        self.svc.promote_weekly(full=True)
        self.assertEqual({w["Ticker"] for w in self.svc.fetch("weekly", incomplete_only=True)}, {"LAST"})
        self.assertEqual([f[0] for f in self.findings("incomplete_week") if f[4] == 0], ["LAST"])

    def test_periods_in_progress_and_before_the_history_are_not_incomplete(self):
        # the data history starts on a Wednesday and ends on the next Tuesday
        self.seed_days("AAA", ["20250108", "20250109", "20250110", "20250113", "20250114"])
        self.svc.promote_daily()
        r = self.svc.promote_weekly()
        self.assertEqual(r.rows_incomplete, 0)
        weeks = [(w["WeekStart"], w["ExpectedDays"], w["Complete"]) for w in self.svc.fetch("weekly", newest_first=False)]
        self.assertEqual(weeks, [("2025-01-06", 3, 1), ("2025-01-13", 2, 1)])
        r = self.svc.promote_monthly()
        self.assertEqual(r.rows_incomplete, 0)
        self.assertEqual(self.svc.fetch("monthly")[0]["ExpectedDays"], 5)

    def test_completeness_check_can_be_switched_off(self):
        self.seed_days("AAA", ["20250106", "20250110"])
        self.settings.check_completeness = False
        svc = PromotionService(self.settings, self.db)
        svc.promote_daily()
        r = svc.promote_weekly()
        self.assertEqual(r.rows_incomplete, 0)
        week = svc.fetch("weekly")[0]
        self.assertEqual((week["ExpectedDays"], week["MissingDays"], week["Complete"]), (0, 0, 1))
        self.assertEqual(self.findings("incomplete_week"), [])

    def test_existing_tables_get_the_new_columns(self):
        """An active table created by an earlier version is upgraded in place."""
        with sqlite3.connect(self.settings.sqlite_path) as conn:
            conn.execute('CREATE TABLE "Stock_Weekly" ("WeeklyId" INTEGER PRIMARY KEY, "Ticker" TEXT, "WeekStart" TEXT)')
            conn.execute('CREATE TABLE "Promotion_History" ("PromotionId" INTEGER PRIMARY KEY, "Procedure" TEXT)')
        self.svc.ensure_tables()
        with sqlite3.connect(self.settings.sqlite_path) as conn:
            weekly = {r[1] for r in conn.execute('PRAGMA table_info("Stock_Weekly")')}
            history = {r[1] for r in conn.execute('PRAGMA table_info("Promotion_History")')}
        self.assertTrue({"ExpectedDays", "MissingDays", "MissingDates", "Complete"} <= weekly)
        self.assertIn("RowsIncomplete", history)
