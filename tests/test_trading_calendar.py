"""
The trading calendar (No_Trading_Day / Market tables, stock_dataload/trading_calendar.py):

  * generation per market and year: weekends + holidays, manual rows kept, extra markets
  * the loader skips deliveries dated on a no-trading day (warning, Load_History 'skipped',
    file moved to the skipped folder) - unless asked to load them
  * the missing-market-day check and the promotion's market_closed rule use the table
  * CLI 'no-trading-days ...' and the dashboard's Trading calendar page
"""

from __future__ import annotations

import contextlib
import io
import re
import sqlite3
import unittest
from datetime import date
from pathlib import Path

from stock_dataload import cli
from stock_dataload.audit import AuditLogger
from stock_dataload.config import Settings, load_settings, save_settings
from stock_dataload.db import StageDatabase, StageRow, now_iso
from stock_dataload.loader import StageLoader
from stock_dataload.promotion import PromotionService
from stock_dataload.trading_calendar import (DEFAULT_MARKETS, CalendarError, TradingCalendar, default_update_years,
                                             generate_no_trading_days, render_ddl)

from .helpers import DEFAULT_HEADER, TempProjectTestCase, register_listed
from .test_dashboard import OPERATOR, VIEWER, DashboardTestCase

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class GenerationTests(unittest.TestCase):
    def test_us_rules_cover_weekends_and_holidays_with_reasons(self):
        days = generate_no_trading_days("us", 2023)
        self.assertEqual(len(days), 115)                     # 105 weekend days + 10 holidays
        self.assertEqual(days[date(2023, 1, 1)], ("weekend (Sunday)", "generated"))
        self.assertEqual(days[date(2023, 1, 2)], ("New Year's Day (observed)", "generated"))
        self.assertEqual(days[date(2023, 4, 7)], ("Good Friday", "generated"))
        self.assertEqual(days[date(2023, 6, 19)], ("Juneteenth National Independence Day", "generated"))
        self.assertEqual(days[date(2023, 11, 23)], ("Thanksgiving Day", "generated"))
        self.assertNotIn(date(2023, 1, 3), days)
        self.assertTrue(all(d.year == 2023 for d in days))
        self.assertEqual(sum(1 for d in days if d.weekday() < 5), 10)

    def test_weekends_rules_and_extra_holidays(self):
        days = generate_no_trading_days("weekends", 2024, extra_holidays=[date(2024, 12, 25), date(2025, 1, 1)])
        self.assertEqual(sum(1 for d in days if d.weekday() < 5), 1)
        self.assertEqual(days[date(2024, 12, 25)][1], "config")
        self.assertNotIn(date(2025, 1, 1), days)
        with self.assertRaises(CalendarError):
            generate_no_trading_days("lunar", 2024)

    def test_default_update_years(self):
        self.assertEqual(default_update_years(date(2025, 11, 30)), [2025, 2026])


class CalendarTableTests(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.cal = TradingCalendar(self.settings)

    def query(self, sql, params=()):
        with sqlite3.connect(self.settings.sqlite_path) as conn:
            return conn.execute(sql, params).fetchall()

    def test_tables_and_default_markets(self):
        self.assertEqual([m.market for m in self.cal.markets()], ["AMEX", "NASDAQ", "NYSE"])
        self.assertEqual([m.rules for m in self.cal.markets()], ["us", "us", "us"])
        self.assertTrue(all(m.is_active for m in self.cal.markets()))
        ddl = {r[0]: r[1] for r in self.query("SELECT name, sql FROM sqlite_master WHERE type = 'table'")}
        self.assertIn('UNIQUE ("Market", "TradeDate")', ddl["No_Trading_Day"])
        self.assertIn("Market", ddl)
        self.assertEqual(len(DEFAULT_MARKETS), 3)
        self.assertIsNotNone(self.cal.get_market("nasdaq"))      # case-insensitive
        self.assertIsNone(self.cal.get_market("LSE"))
        with sqlite3.connect(self.settings.sqlite_path) as conn:
            conn.execute('INSERT INTO "No_Trading_Day" ("Market","TradeDate","Reason","Source","CreatedAt") '
                         'VALUES (?,?,?,?,?)', ("NASDAQ", "2030-01-01", "x", "manual", now_iso()))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute('INSERT INTO "No_Trading_Day" ("Market","TradeDate","Reason","Source","CreatedAt") '
                             'VALUES (?,?,?,?,?)', ("NASDAQ", "2030-01-01", "y", "manual", now_iso()))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute('INSERT INTO "No_Trading_Day" ("Market","TradeDate","Reason","Source","CreatedAt") '
                             'VALUES (?,?,?,?,?)', ("NASDAQ", "20300102", "y", "manual", now_iso()))

    def test_update_year_generates_and_keeps_manual_rows(self):
        results = self.cal.update_year(2025)
        self.assertEqual([(r.market, r.generated, r.added, r.holidays) for r in results],
                         [("AMEX", 114, 114, 10), ("NASDAQ", 114, 114, 10), ("NYSE", 114, 114, 10)])
        self.assertEqual(self.cal.count_days(), 3 * 114)
        self.cal.add_day("NASDAQ", "2025-01-09", "National Day of Mourning")
        again = self.cal.update_year(2025, ["nasdaq"])
        self.assertEqual((again[0].added, again[0].existing), (0, 114))
        rows = self.cal.days("NASDAQ", 2025, holidays_only=True)
        self.assertEqual(len(rows), 11)
        mourning = [r for r in rows if r.trade_date == "2025-01-09"][0]
        self.assertEqual((mourning.reason, mourning.source, mourning.weekday), ("National Day of Mourning", "manual", "Thursday"))
        self.assertEqual(self.cal.years_available("NASDAQ"), [2025])
        self.assertEqual(len(self.cal.days("NASDAQ", date_from="2025-01-01", date_to="2025-01-07")), 3)
        self.assertEqual(self.cal.days("NASDAQ", 2024), [])
        with self.assertRaisesRegex(CalendarError, "unknown market"):
            self.cal.update_year(2025, ["LSE"])
        with self.assertRaisesRegex(CalendarError, "out of range"):
            self.cal.update_year(1800)
        self.assertEqual(len(self.cal.update_years([2026, 2027], ["NYSE"])), 2)
        self.assertEqual(self.cal.years_available("NYSE"), [2025, 2026, 2027])
        events = [r.event for r in AuditLogger(self.settings).fetch_logs(limit=50, component="calendar")]
        self.assertIn("calendar_updated", events)
        self.assertIn("no_trading_day_added", events)

    def test_add_and_remove_days(self):
        self.cal.update_year(2025, ["NASDAQ"])
        with self.assertRaisesRegex(CalendarError, "already closed"):
            self.cal.add_day("NASDAQ", "20250101")
        with self.assertRaisesRegex(CalendarError, "unknown market"):
            self.cal.add_day("LSE", "2025-01-09")
        with self.assertRaises(CalendarError):
            self.cal.add_day("NASDAQ", "9/1/2025")
        row = self.cal.add_day("NASDAQ", "20250109")
        self.assertEqual(row.reason, "closure")
        self.assertFalse(self.cal.is_open("NASDAQ", date(2025, 1, 9)))
        removed = self.cal.remove_day("NASDAQ", date(2025, 1, 9))
        self.assertEqual(removed.trade_date, "2025-01-09")
        self.assertTrue(self.cal.is_open("NASDAQ", date(2025, 1, 9)))
        with self.assertRaisesRegex(CalendarError, "not marked closed"):
            self.cal.remove_day("NASDAQ", "2025-01-09")
        # the market trades on Good Friday after all? remove the generated row
        self.cal.remove_day("NASDAQ", "2025-04-18")
        self.assertTrue(self.cal.is_open("NASDAQ", date(2025, 4, 18)))
        self.assertFalse(self.cal.is_open("NASDAQ", date(2025, 4, 19)))     # Saturday

    def test_additional_markets(self):
        lse = self.cal.add_market("lse", "London Stock Exchange", "weekends")
        self.assertEqual((lse.market, lse.name, lse.rules), ("LSE", "London Stock Exchange", "weekends"))
        with self.assertRaisesRegex(CalendarError, "already exists"):
            self.cal.add_market("LSE")
        with self.assertRaises(CalendarError):
            self.cal.add_market("L S E")
        with self.assertRaises(CalendarError):
            self.cal.add_market("TSX", rules="canada")
        r = self.cal.update_year(2025, ["LSE"])[0]
        self.assertEqual((r.generated, r.holidays), (104, 0))
        self.cal.add_day("LSE", "2025-12-25", "Christmas Day")
        self.cal.add_day("LSE", "2025-12-26", "Boxing Day")
        self.assertEqual(self.cal.closure_reason("LSE", date(2025, 12, 26)), "Boxing Day")
        self.assertTrue(self.cal.is_open("LSE", date(2025, 7, 4)))          # no US holiday here
        self.assertFalse(self.cal.is_open("NYSE", date(2025, 7, 4)))
        self.cal.set_market_active("LSE", False)
        self.assertEqual([m.market for m in self.cal.markets(active_only=True)], ["AMEX", "NASDAQ", "NYSE"])
        self.assertEqual([r.market for r in self.cal.update_year(2026)], ["AMEX", "NASDAQ", "NYSE"])
        self.cal.set_market_active("LSE", True)
        self.assertEqual(len(self.cal.markets(active_only=True)), 4)
        with self.assertRaises(CalendarError):
            self.cal.set_market_active("XXX", True)

    def test_lookups_generate_missing_years_on_demand(self):
        self.assertEqual(self.cal.years_available("NASDAQ"), [])
        self.assertEqual(self.cal.closure_reason("NASDAQ", date(2023, 1, 2)), "New Year's Day (observed)")
        self.assertEqual(self.cal.years_available("NASDAQ"), [2023])
        self.assertIsNone(self.cal.closure_reason("NASDAQ", date(2023, 1, 3)))
        events = [r.event for r in AuditLogger(self.settings).fetch_logs(limit=50, component="calendar")]
        self.assertIn("calendar_generated_on_demand", events)
        # unknown market / no market: US rules, table untouched
        self.assertEqual(self.cal.closure_reason("XETRA", date(2023, 1, 2)), "New Year's Day (observed)")
        self.assertEqual(self.cal.closure_reason(None, date(2023, 1, 7)), "weekend (Saturday)")
        self.assertEqual(self.cal.years_available("XETRA"), [])
        self.assertTrue(self.cal.opener("NASDAQ")(date(2023, 1, 3)))
        self.assertFalse(self.cal.opener("NASDAQ")(date(2023, 1, 2)))

    def test_config_extra_holidays_apply_after_generation_too(self):
        self.cal.update_year(2025, ["NASDAQ"])
        self.settings.extra_market_holidays = ("2025-01-09",)
        cal = TradingCalendar(self.settings)
        self.assertEqual(cal.closure_reason("NASDAQ", date(2025, 1, 9)), "configured closure ([quality] extra_holidays)")
        self.assertEqual(cal.days("NASDAQ", 2025, holidays_only=True)[1].trade_date, "2025-01-20")  # not persisted
        r = cal.update_year(2025, ["NASDAQ"])[0]
        self.assertEqual(r.added, 1)
        self.assertEqual([d.source for d in cal.days("NASDAQ", 2025) if d.trade_date == "2025-01-09"], ["config"])

    def test_reference_sql_in_sync(self):
        script = (PROJECT_ROOT / "sql" / "create_no_trading_day_sqlite.sql").read_text(encoding="utf-8")
        self.assertTrue(script.endswith(render_ddl()))


class LoaderSkipTests(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.db = StageDatabase(self.settings)
        self.loader = StageLoader(self.settings, self.db)

    def deliver(self, name: str, rows: str = "AAA,20250106,1,2,0.5,1.5,100\n") -> Path:
        return self.write(f"loading/{name}", DEFAULT_HEADER + rows)

    def test_holiday_and_weekend_deliveries_are_skipped_with_a_warning(self):
        self.deliver("NASDAQ_20250101.txt", "AAA,20250101,1,1,1,1,0\n")      # New Year's Day
        self.deliver("NASDAQ_20250104.txt", "AAA,20250104,1,1,1,1,0\n")      # Saturday
        self.deliver("NASDAQ_20250106.txt")                                   # Monday
        with self.assertLogs("stock_dataload.loader", level="WARNING") as logs:
            results = self.loader.run()
        self.assertEqual([r.status for r in results], ["skipped", "skipped", "loaded"])
        skipped = results[0]
        self.assertTrue(skipped.skipped and not skipped.ok and not skipped.failed)
        self.assertEqual(skipped.trade_date, date(2025, 1, 1))
        self.assertEqual(skipped.message, "NASDAQ was closed on 2025-01-01 (New Year's Day) - no trading day, file not loaded")
        self.assertIn("NASDAQ_20250101.txt: skipped - NASDAQ was closed on 2025-01-01 (New Year's Day)", str(skipped))
        self.assertTrue(any("NASDAQ_20250101.txt skipped: NASDAQ was closed" in m for m in logs.output), logs.output)
        self.assertTrue(any("weekend (Saturday)" in m for m in logs.output))
        # nothing of the skipped files reached the staging table
        self.assertEqual(self.db.count_rows(), 1)
        # they went to the skipped folder, the loaded one to the archive
        self.assertEqual(sorted(p.name for p in self.settings.skipped_dir.iterdir()),
                         ["NASDAQ_20250101.txt", "NASDAQ_20250104.txt"])
        self.assertEqual([p.name for p in self.settings.archive_dir.iterdir()], ["NASDAQ_20250106.txt"])
        self.assertEqual(skipped.final_path, self.settings.skipped_dir / "NASDAQ_20250101.txt")
        # registered in Load_History and App_Log
        history = {h.file_name: h for h in self.db.load_history(limit=10)}
        self.assertEqual(history["NASDAQ_20250101.txt"].status, "skipped")
        self.assertIn("New Year's Day", history["NASDAQ_20250101.txt"].message)
        self.assertEqual(history["NASDAQ_20250106.txt"].status, "loaded")
        logs_db = AuditLogger(self.settings).fetch_logs(limit=50, component="loader")
        skipped_events = [r for r in logs_db if r.event == "file_skipped"]
        self.assertEqual({r.source_file for r in skipped_events}, {"NASDAQ_20250101.txt", "NASDAQ_20250104.txt"})
        self.assertTrue(all(r.level == "WARNING" for r in skipped_events))
        self.assertEqual(AuditLogger(self.settings).count_errors(), 0, "a skip is not an error")
        run_complete = [r for r in logs_db if r.event == "run_complete"][0]
        self.assertIn("1 file(s) loaded, 0 failed, 2 skipped", run_complete.message)

    def test_skipping_can_be_switched_off_per_run_or_in_config(self):
        self.deliver("NASDAQ_20250101.txt", "AAA,20250101,1,1,1,1,0\n")
        r = self.loader.run(include_no_trading_days=True)[0]
        self.assertTrue(r.ok)
        self.settings.skip_no_trading_days = False
        self.deliver("NASDAQ_20250104.txt", "AAA,20250104,1,1,1,1,0\n")
        r = StageLoader(self.settings, self.db).run()[0]
        self.assertTrue(r.ok)
        self.assertEqual(self.db.count_rows(), 2)

    def test_files_without_a_date_or_market_in_the_name(self):
        self.deliver("prices.txt")
        r = self.loader.run()[0]
        self.assertTrue(r.ok)
        self.assertIsNone(r.trade_date)
        # no market in the name: the US rules decide (a Sunday is skipped)
        self.deliver("20250105.txt", "AAA,20250105,1,1,1,1,0\n")
        r = self.loader.run()[0]
        self.assertTrue(r.skipped)
        self.assertIn("the market was closed on 2025-01-05 (weekend (Sunday))", r.message)
        # an explicit market that has its own calendar
        TradingCalendar(self.settings, self.db).add_market("LSE", rules="weekends")
        self.deliver("LSE_20250704.txt", "AAA,20250704,1,2,0.5,1.5,100\n")   # US holiday, London trades
        r = self.loader.run()[0]
        self.assertTrue(r.ok, r.message)

    def test_derive_trade_date(self):
        self.assertEqual(self.loader.derive_trade_date(Path("NASDAQ_20250123.txt")), date(2025, 1, 23))
        self.assertIsNone(self.loader.derive_trade_date(Path("NASDAQ_20251345.txt")))
        self.assertIsNone(self.loader.derive_trade_date(Path("prices.txt")))
        self.settings.date_from_filename_regex = r"(\d{4}-\d{2}-\d{2})"
        loader = StageLoader(self.settings, self.db)
        self.assertEqual(loader.derive_trade_date(Path("nyse-2025-01-23.txt")), date(2025, 1, 23))
        self.settings.date_from_filename_regex = ""
        self.assertIsNone(StageLoader(self.settings, self.db).derive_trade_date(Path("NASDAQ_20250123.txt")))

    def test_bulk_load_reports_skipped_files(self):
        folder = self.root / "deliveries"
        folder.mkdir()
        for day in ("20250103", "20250104", "20250105", "20250106"):
            (folder / f"NASDAQ_{day}.txt").write_text(DEFAULT_HEADER + f"AAA,{day},1,2,0.5,1.5,100\n")
        summary = self.loader.load_folder(folder)
        self.assertTrue(summary.ok)
        self.assertEqual((summary.loaded, summary.skipped, summary.failed), (2, 2, 0))
        self.assertIn("2 of 4 file(s) loaded (2 rows), 2 skipped (no trading day)", str(summary))
        self.assertEqual(summary.as_dict()["skipped"], 2)
        self.assertEqual(len(list(folder.iterdir())), 4)
        summary = self.loader.load_folder(folder, include_no_trading_days=True)
        self.assertEqual((summary.loaded, summary.skipped), (4, 0))

    def test_missing_market_day_check_uses_the_calendar(self):
        cal = TradingCalendar(self.settings, self.db)
        cal.add_day("NASDAQ", "2025-01-08", "ad-hoc closure")
        register_listed(self.settings, "NASDAQ", ["AAA", "BBB"])
        self.deliver("NASDAQ_20250106.txt", "AAA,20250106,1,2,0.5,1.5,100\nBBB,20250106,1,2,0.5,1.5,100\n")
        self.deliver("NASDAQ_20250107.txt", "AAA,20250107,1,2,0.5,1.5,100\n")
        results = self.loader.run()
        self.assertEqual(results[1].alerts, 1)                       # BBB missing on an open day
        self.deliver("NASDAQ_20250108.txt", "AAA,20250108,1,2,0.5,1.5,100\n")
        r = self.loader.run()[0]
        self.assertTrue(r.skipped, "the ad-hoc closure is a no-trading day")
        r = self.loader.run(files=[self.settings.skipped_dir / "NASDAQ_20250108.txt"], include_no_trading_days=True,
                            move_files=False)[0]
        self.assertTrue(r.ok)
        self.assertEqual(r.alerts, 0, "nobody is expected to deliver on a closed day")


class PromotionCalendarTests(TempProjectTestCase):
    def test_market_closed_rule_uses_the_market_calendar(self):
        db = StageDatabase(self.settings)
        db.ensure_stage_table()
        cal = TradingCalendar(self.settings, db)
        cal.add_market("LSE", rules="weekends")
        with db.connection() as conn:
            for market, day in (("NASDAQ", "20250704"), ("LSE", "20250704"), ("NASDAQ", "20250707"), ("", "20250705")):
                db.insert_stage_row(conn, StageRow(market, "AAA", day, "1", "2", "0.5", "1.5", "100", "f.txt", "L", now_iso()))
        r = PromotionService(self.settings, db, calendar=cal).promote_daily()
        self.assertEqual((r.rows_inserted, r.rows_rejected), (2, 2))
        with sqlite3.connect(self.settings.sqlite_path) as conn:
            msgs = sorted(m for (m,) in conn.execute('SELECT "ErrorMessage" FROM "Data_Error"'))
        self.assertIn("NASDAQ was closed on 2025-07-04 (Independence Day)", msgs[0])
        self.assertIn("the market was closed on 2025-07-05 (weekend (Saturday))", msgs[1])


class CalendarCliTests(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.settings.config_file = self.root / "config.ini"
        save_settings(self.settings, self.settings.config_file)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["--config", str(self.settings.config_file), "-q", *args])
        return rc, out.getvalue(), err.getvalue()

    def test_no_trading_days_commands(self):
        rc, out, _ = self.run_cli("no-trading-days", "update", "--year", "2026")
        self.assertEqual(rc, 0)
        self.assertIn("NASDAQ 2026: 114 no-trading days (10 holidays) - 114 added", out)
        self.assertIn("updated for 2026 (342 day(s) added)", out)
        rc, out, _ = self.run_cli("no-trading-days", "update", "--from", "2027", "--to", "2028", "--market", "NYSE")
        self.assertIn("updated for 2027, 2028", out)
        rc, out, _ = self.run_cli("no-trading-days", "list", "NASDAQ", "--year", "2026")
        self.assertEqual(rc, 0)
        self.assertIn("NASDAQ 2026: 10 no-trading day(s)", out)
        self.assertIn("2026-07-03  Friday    Independence Day (observed)", out)
        rc, out, _ = self.run_cli("no-trading-days", "list", "NASDAQ", "--year", "2026", "--all")
        self.assertIn("114 no-trading day(s)", out)
        rc, out, _ = self.run_cli("no-trading-days", "list", "NASDAQ", "--year", "2031")
        self.assertIn("no no-trading days recorded", out)
        rc, out, err = self.run_cli("no-trading-days", "list", "XXX")
        self.assertEqual(rc, 1)
        self.assertIn("unknown market", err)
        rc, out, _ = self.run_cli("calendar", "check", "nasdaq", "20260101")
        self.assertIn("NASDAQ is CLOSED on 2026-01-01 (New Year's Day)", out)
        rc, out, _ = self.run_cli("calendar", "check", "NASDAQ", "2026-01-02")
        self.assertIn("NASDAQ is open on 2026-01-02", out)
        rc, out, _ = self.run_cli("no-trading-days", "add", "NASDAQ", "2026-01-02", "--reason", "snow")
        self.assertIn("NASDAQ closed on 2026-01-02: snow", out)
        rc, out, err = self.run_cli("no-trading-days", "add", "NASDAQ", "2026-01-02")
        self.assertEqual(rc, 1)
        self.assertIn("already closed", err)
        rc, out, _ = self.run_cli("no-trading-days", "remove", "NASDAQ", "2026-01-02")
        self.assertIn("NASDAQ trades on 2026-01-02 (snow removed)", out)
        rc, out, _ = self.run_cli("no-trading-days", "add-market", "lse", "--name", "London", "--rules", "weekends")
        self.assertIn("market LSE added", out)
        rc, out, _ = self.run_cli("no-trading-days", "disable", "LSE")
        self.assertIn("LSE inactive", out)
        rc, out, _ = self.run_cli("no-trading-days", "enable", "LSE")
        self.assertIn("LSE active", out)
        rc, out, _ = self.run_cli("no-trading-days", "markets")
        self.assertIn("LSE        weekends   yes", out)
        self.assertIn("NYSE       us         yes      2026-2028", out)
        rc, out, _ = self.run_cli("no-trading-days", "update")          # default: this year and next
        self.assertIn(str(date.today().year + 1), out)

    def test_init_db_prepares_the_calendar_and_load_skips(self):
        rc, out, _ = self.run_cli("init-db")
        self.assertEqual(rc, 0)
        years = default_update_years()
        self.assertIn(f"no-trading days ready for AMEX, NASDAQ, NYSE ({years[0]}-{years[1]}", out)
        self.assertEqual(TradingCalendar(self.settings).years_available("NYSE"), years)
        self.write("loading/NASDAQ_20250101.txt", DEFAULT_HEADER + "AAA,20250101,1,1,1,1,0\n")
        self.write("loading/NASDAQ_20250102.txt", DEFAULT_HEADER + "AAA,20250102,1,2,0.5,1.5,100\n")
        rc, out, _ = self.run_cli("load")
        self.assertEqual(rc, 0, "a skipped file is not a failure")
        self.assertIn("NASDAQ_20250101.txt: skipped - NASDAQ was closed on 2025-01-01 (New Year's Day)", out)
        self.assertIn("1 file(s) skipped - dated on a no-trading day", out)
        self.assertIn("NASDAQ_20250102.txt: loaded", out)
        self.write("loading/NASDAQ_20250104.txt", DEFAULT_HEADER + "AAA,20250104,1,1,1,1,0\n")
        rc, out, _ = self.run_cli("load", "--include-no-trading-days")
        self.assertIn("NASDAQ_20250104.txt: loaded", out)
        rc, out, _ = self.run_cli("history")
        self.assertIn("skipped", out)

    def test_settings_round_trip(self):
        cfg = self.write("cfg/config.ini", "[paths]\nskipped_dir = aside\n[loader]\nskip_no_trading_days = false\n"
                                           "date_from_filename_regex = (?P<date>\\d{8})x\n")
        s = load_settings(cfg)
        self.assertEqual(s.skipped_dir, cfg.parent / "aside")
        self.assertFalse(s.skip_no_trading_days)
        self.assertEqual(s.date_from_filename_regex, "(?P<date>\\d{8})x")
        save_settings(s, cfg.parent / "out.ini")
        s2 = load_settings(cfg.parent / "out.ini")
        self.assertEqual((s2.skipped_dir, s2.skip_no_trading_days, s2.date_from_filename_regex),
                         (s.skipped_dir, False, "(?P<date>\\d{8})x"))
        self.assertIn("skipped_dir", Settings().folders)
        shipped = load_settings(PROJECT_ROOT / "config.ini")
        self.assertTrue(shipped.skip_no_trading_days)
        self.assertEqual(shipped.skipped_dir, PROJECT_ROOT / "data" / "skipped")


class CalendarDashboardTests(DashboardTestCase):
    def flashes_of(self, response) -> list[str]:
        page = self.client.get(response.headers.get("Location", "/")).get_data(as_text=True)
        return [re.sub(r"<[^>]+>", "", m).strip() for m in re.findall(r'class="flash[^"]*">(.*?)</div>', page, re.S)]

    def test_calendar_page_and_actions(self):
        r = self.client.get("/calendar")
        self.assertEqual(r.status_code, 200)
        page = r.get_data(as_text=True)
        self.assertIn("NASDAQ", page)
        self.assertIn("nothing generated for this market yet", page)
        r = self.client.post("/calendar/update", data={"year": "2026"})
        msgs = self.flashes_of(r)
        self.assertTrue(any("NASDAQ 2026: 114 no-trading days" in m for m in msgs), msgs)
        page = self.client.get("/calendar?market=NASDAQ&year=2026").get_data(as_text=True)
        self.assertIn("Thanksgiving Day", page)
        self.assertNotIn("weekend (Saturday)", page)
        page = self.client.get("/calendar?market=NASDAQ&year=2026&all=1").get_data(as_text=True)
        self.assertIn("weekend (Saturday)", page)
        r = self.client.post("/calendar/days/add", data={"market": "NASDAQ", "date": "2026-03-03", "reason": "storm"})
        self.assertTrue(any("closed on 2026-03-03: storm" in m for m in self.flashes_of(r)))
        self.assertIn("market=NASDAQ", r.headers["Location"])
        r = self.client.post("/calendar/days/add", data={"market": "NASDAQ", "date": "bad"})
        self.assertTrue(any("Could not add" in m for m in self.flashes_of(r)))
        r = self.client.post("/calendar/days/remove", data={"market": "NASDAQ", "date": "2026-03-03"})
        self.assertTrue(any("trades on 2026-03-03" in m for m in self.flashes_of(r)))
        r = self.client.post("/calendar/days/remove", data={"market": "NASDAQ", "date": "2026-03-03"})
        self.assertTrue(any("Could not remove" in m for m in self.flashes_of(r)))
        r = self.client.post("/calendar/markets/add", data={"market": "LSE", "name": "London", "rules": "weekends"})
        self.assertTrue(any("Market LSE added" in m for m in self.flashes_of(r)))
        r = self.client.post("/calendar/markets/add", data={"market": "LSE"})
        self.assertTrue(any("already exists" in m for m in self.flashes_of(r)))
        r = self.client.post("/calendar/markets/LSE/toggle")
        self.assertTrue(any("deactivated" in m for m in self.flashes_of(r)))
        self.assertEqual(self.client.post("/calendar/markets/XXX/toggle").status_code, 404)
        r = self.client.post("/calendar/update", data={"year": "2026", "markets": "LSE"})
        self.assertTrue(any("LSE 2026: 104" in m for m in self.flashes_of(r)))
        r = self.client.post("/calendar/update", data={"year": "abc"})
        self.assertTrue(any("Could not update" in m for m in self.flashes_of(r)))
        data = self.client.get("/api/no-trading-days?market=NASDAQ&year=2026").get_json()
        self.assertEqual((data["market"], data["year"], data["count"]), ("NASDAQ", 2026, 10))
        self.assertEqual(data["records"][0]["reason"], "New Year's Day")
        self.assertEqual(data["records"][0]["weekday"], "Thursday")
        self.assertEqual(self.client.get("/api/no-trading-days?market=NASDAQ&year=2026&all=1").get_json()["count"], 114)
        self.assertEqual(self.client.get("/calendar?year=abc").status_code, 200)
        self.assertEqual(self.client.get("/browse/skipped_dir").status_code, 200)

    def test_roles(self):
        self.logout()
        self.login(*VIEWER)
        self.assertEqual(self.client.get("/calendar").status_code, 200)
        self.assertNotIn("Add closure", self.client.get("/calendar").get_data(as_text=True))
        self.assertEqual(self.client.post("/calendar/update", data={"year": "2026"}).status_code, 403)
        self.logout()
        self.login(*OPERATOR)
        self.assertEqual(self.client.post("/calendar/update", data={"year": "2026"}).status_code, 302)
        self.assertEqual(self.client.post("/calendar/markets/add", data={"market": "LSE"}).status_code, 403)

    def test_load_skips_on_the_dashboard(self):
        self.write("loading/NASDAQ_20250101.txt", DEFAULT_HEADER + "AAA,20250101,1,1,1,1,0\n")
        r = self.client.post("/load", data={})
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn('class="flash info">NASDAQ_20250101.txt: skipped - NASDAQ was closed on 2025-01-01', page)
        self.assertIn("Skipped (no trading day): 1", page)
        self.write("loading/NASDAQ_20250101.txt", DEFAULT_HEADER + "AAA,20250101,1,1,1,1,0\n")
        r = self.client.post("/load", data={"include_no_trading_days": "on"})
        self.assertIn("NASDAQ_20250101.txt: loaded", self.flashes(r))
        folder = self.root / "deliveries"
        folder.mkdir()
        (folder / "NASDAQ_20250104.txt").write_text(DEFAULT_HEADER + "AAA,20250104,1,1,1,1,0\n")
        r = self.client.post("/load-folder", data={"folder": str(folder)})
        msgs = self.flashes(r)
        self.assertIn("1 skipped (no trading day)", msgs)
        self.assertIn("weekend (Saturday)", msgs)
        page = self.client.get("/monitor/history").get_data(as_text=True)
        self.assertIn('class="badge warn">skipped</span>', page)

    def test_settings_fields(self):
        page = self.client.get("/settings").get_data(as_text=True)
        for field in ("skipped_dir", "skip_no_trading_days", "date_from_filename_regex"):
            self.assertIn(f'name="{field}"', page)
        from .test_dashboard_promotion import PromotionDashboardTests
        form = PromotionDashboardTests.settings_form(self, skipped_dir="aside", date_from_filename_regex=r"(?P<date>\d{8})")
        r = self.client.post("/settings", data=form)
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True))
        self.assertEqual(self.state.settings.skipped_dir, self.root / "aside")
        self.assertFalse(self.state.settings.skip_no_trading_days)
        r = self.client.post("/settings", data={**form, "date_from_filename_regex": "("})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
