"""
The stock symbol table (Stock_Symbol, stock_dataload/symbols.py):

  * the loader registers every symbol it loads (market, first / last date seen)
  * first / last trade dates: set, import (CSV), export
  * data before the first trade date (or after the last) is flagged at load time
    and rejected by the promotion
  * the missing-market-day alert respects the trading window and is skipped for
    symbols without a first trade date  (the HCMAU case)
  * CLI 'symbols ...' and the dashboard's Symbols page
"""

from __future__ import annotations

import contextlib
import io
import re
import sqlite3
import unittest
from datetime import date
from io import BytesIO
from pathlib import Path

from stock_dataload import cli
from stock_dataload.audit import AuditLogger
from stock_dataload.config import save_settings
from stock_dataload.correction import CorrectionError, CorrectionService
from stock_dataload.db import StageDatabase, now_iso
from stock_dataload.loader import StageLoader
from stock_dataload.promotion import PromotionService
from stock_dataload.quality import DataErrorRepository, DataQualityChecker, DataRow, QualityContext
from stock_dataload.symbols import SymbolError, SymbolRegistry, render_ddl

from .helpers import DEFAULT_HEADER, TempProjectTestCase, register_listed
from .test_dashboard import OPERATOR, VIEWER, DashboardTestCase

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class RegistryTests(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.reg = SymbolRegistry(self.settings)

    def test_table_and_constraints(self):
        self.reg.ensure_table()
        ddl = {r[0]: r[1] for r in sqlite3.connect(self.settings.sqlite_path).execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table'")}
        self.assertIn('UNIQUE ("Market", "Ticker")', ddl["Stock_Symbol"])
        with sqlite3.connect(self.settings.sqlite_path) as conn:
            conn.execute('INSERT INTO "Stock_Symbol" ("Ticker","Market","Source","CreatedAt","UpdatedAt") VALUES (?,?,?,?,?)',
                         ("AAA", "NASDAQ", "manual", now_iso(), now_iso()))
            with self.assertRaises(sqlite3.IntegrityError):     # same symbol, case-insensitive
                conn.execute('INSERT INTO "Stock_Symbol" ("Ticker","Market","Source","CreatedAt","UpdatedAt") VALUES (?,?,?,?,?)',
                             ("aaa", "nasdaq", "manual", now_iso(), now_iso()))
            conn.execute('INSERT INTO "Stock_Symbol" ("Ticker","Market","Source","CreatedAt","UpdatedAt") VALUES (?,?,?,?,?)',
                         ("AAA", "NYSE", "manual", now_iso(), now_iso()))        # another market is fine
            with self.assertRaises(sqlite3.IntegrityError):     # first > last
                conn.execute('INSERT INTO "Stock_Symbol" ("Ticker","Market","FirstTradeDate","LastTradeDate","Source",'
                             '"CreatedAt","UpdatedAt") VALUES (?,?,?,?,?,?,?)',
                             ("BBB", "NYSE", "2024-01-01", "2023-01-01", "manual", now_iso(), now_iso()))
        script = (PROJECT_ROOT / "sql" / "create_stock_symbol_sqlite.sql").read_text(encoding="utf-8")
        self.assertTrue(script.endswith(render_ddl()))

    def test_register_seen_widens_the_seen_range(self):
        self.assertEqual(self.reg.register_seen("NASDAQ", {"AAA": ("20230105", "20230105"), "BBB": ("20230105", "20230105")}), 2)
        self.assertEqual(self.reg.register_seen("NASDAQ", {"AAA": ("20230103", "20230104"), "CCC": ("20230110", "20230110")}), 1)
        self.assertEqual(self.reg.register_seen("NASDAQ", {"AAA": ("20230201", "20230201")}), 0)
        self.assertEqual(self.reg.register_seen("NASDAQ", {}), 0)
        aaa = self.reg.get("AAA", "NASDAQ")
        self.assertEqual((aaa.first_seen, aaa.last_seen, aaa.source, aaa.first_trade_date), ("2023-01-03", "2023-02-01", "loader", None))
        self.assertEqual(self.reg.counts(), {"symbols": 3, "with_first_trade_date": 0, "without_first_trade_date": 3,
                                             "data_before_listing": 0})
        self.assertEqual(self.reg.trading_windows("NASDAQ"), {"AAA": (None, None), "BBB": (None, None), "CCC": (None, None)})
        self.assertEqual(self.reg.trading_windows("NASDAQ", ["AAA"]), {"AAA": (None, None)})
        self.assertEqual(self.reg.trading_windows("NYSE"), {})
        self.assertEqual(self.reg.markets(), ["NASDAQ"])

    def test_set_get_delete(self):
        s = self.reg.set("hcmau", "NASDAQ", first_trade_date="2025-08-01", name="HCM III Acquisition Corp units")
        self.assertEqual((s.ticker, s.market, s.first_trade_date, s.last_trade_date, s.source), ("HCMAU", "NASDAQ", "2025-08-01", None, "manual"))
        self.assertEqual(s.window, (date(2025, 8, 1), None))
        self.assertEqual(self.reg.get("HCMAU").name, "HCM III Acquisition Corp units")
        s = self.reg.set("HCMAU", last_trade_date="20261231")         # market found by ticker
        self.assertEqual((s.first_trade_date, s.last_trade_date), ("2025-08-01", "2026-12-31"))
        s = self.reg.set("HCMAU", first_trade_date="")                # clear
        self.assertIsNone(s.first_trade_date)
        self.assertEqual(s.last_trade_date, "2026-12-31")
        with self.assertRaisesRegex(SymbolError, "after the last trade date"):
            self.reg.set("HCMAU", first_trade_date="2027-01-01")
        with self.assertRaisesRegex(SymbolError, "not a date"):
            self.reg.set("HCMAU", first_trade_date="Aug 1 2025")
        with self.assertRaisesRegex(SymbolError, "must not be empty"):
            self.reg.set("  ")
        self.reg.set("HCMAU", "NYSE", first_trade_date="2025-08-01")
        with self.assertRaisesRegex(SymbolError, "exists on 2 markets"):
            self.reg.get("HCMAU")
        self.assertEqual(len(self.reg.find(ticker="hc")), 2)
        self.assertEqual(len(self.reg.find(market="NYSE")), 1)
        self.assertEqual(len(self.reg.find(missing_dates=True)), 1)
        removed = self.reg.delete("HCMAU", "NYSE")
        self.assertEqual(removed.market, "NYSE")
        self.assertIsNotNone(self.reg.get("HCMAU"))
        with self.assertRaises(SymbolError):
            self.reg.delete("ZZZ")
        events = [r.event for r in AuditLogger(self.settings).fetch_logs(limit=20, component="symbols")]
        self.assertIn("symbol_updated", events)
        self.assertIn("symbol_deleted", events)

    def test_data_before_listing_is_flagged_as_suspicious(self):
        self.reg.register_seen("NASDAQ", {"HCMAU": ("20230123", "20231204")})
        with self.assertLogs("stock_dataload.symbols", level="WARNING") as logs:
            s = self.reg.set("HCMAU", "NASDAQ", first_trade_date="2025-08-01")
        self.assertTrue(s.data_before_listing)
        self.assertIn("before its first trade date", logs.output[0])
        self.assertEqual([x.ticker for x in self.reg.find(suspicious=True)], ["HCMAU"])
        self.assertEqual(self.reg.counts()["data_before_listing"], 1)

    def test_import_and_export_csv(self):
        self.reg.register_seen("NASDAQ", {"AAPL": ("20230103", "20231229")})
        path = self.write("symbols.csv", "Symbol;Exchange;IPO Date;Delisted;Company\n"
                                         "AAPL;NASDAQ;1980-12-12;;Apple\n"
                                         "hcmau;NASDAQ;20250801;;HCM III\n"
                                         "OLD;NASDAQ;2010-01-04;2022-06-30;\n"
                                         "BAD;NASDAQ;soon;;\n"
                                         ";NASDAQ;2020-01-01;;\n"
                                         "\n")
        result = self.reg.import_csv(path)
        self.assertEqual((result.rows, result.added, result.updated, len(result.errors)), (5, 2, 1, 2))
        self.assertIn("line 5: first trade date 'soon' is not a date", result.errors[0])
        self.assertIn("line 6: ticker is empty", result.errors[1])
        self.assertIn("symbols.csv: 5 row(s) - 2 symbol(s) added, 1 updated, 2 error(s)", str(result))
        aapl = self.reg.get("AAPL", "NASDAQ")
        self.assertEqual((aapl.first_trade_date, aapl.name, aapl.source, aapl.first_seen), ("1980-12-12", "Apple", "import", "2023-01-03"))
        self.assertEqual(self.reg.get("OLD").window, (date(2010, 1, 4), date(2022, 6, 30)))
        # market from the argument when the file has none; dates may be cleared by an empty cell
        path2 = self.write("more.csv", "ticker,first_trade_date\nAAPL,\nMSFT,1986-03-13\n")
        result = self.reg.import_csv(path2, market="NASDAQ")
        self.assertEqual((result.added, result.updated, result.errors), (1, 1, []))
        self.assertIsNone(self.reg.get("AAPL").first_trade_date)
        with self.assertRaisesRegex(SymbolError, "no ticker column"):
            self.reg.import_csv(self.write("bad.csv", "a,b\n1,2\n"))
        with self.assertRaisesRegex(SymbolError, "no first_trade_date"):
            self.reg.import_csv(self.write("bad2.csv", "ticker,name\nA,B\n"))
        with self.assertRaisesRegex(SymbolError, "file not found"):
            self.reg.import_csv(self.root / "nope.csv")
        with self.assertRaisesRegex(SymbolError, "empty"):
            self.reg.import_csv(self.write("empty.csv", ""))
        text = self.reg.export_csv(self.root / "out.csv")
        self.assertTrue(text.startswith("ticker,market,first_trade_date,last_trade_date,name,first_seen,last_seen,source,note\n"))
        self.assertIn("OLD,NASDAQ,2010-01-04,2022-06-30,,,,import,", text)
        self.assertEqual((self.root / "out.csv").read_text(encoding="utf-8"), text)
        # round trip: export, import again -> nothing changes
        result = self.reg.import_csv(self.root / "out.csv")
        self.assertEqual((result.added, result.errors), (0, []))


class QualityRuleTests(unittest.TestCase):
    def check(self, rows, windows):
        data = [DataRow.from_sequence(i + 2, r.split(",")) for i, r in enumerate(rows)]
        context = QualityContext(market="NASDAQ", expected_tickers=frozenset({"AAA", "BBB", "HCMAU"}),
                                 trading_windows=windows, market_open=lambda d: d.weekday() < 5)
        return DataQualityChecker().check(data, context)

    def test_rows_outside_the_trading_window_are_errors(self):
        windows = {"HCMAU": (date(2025, 8, 1), None), "OLD": (date(2010, 1, 4), date(2022, 6, 30)), "AAA": (None, None)}
        issues = self.check(["HCMAU,20231221,1,2,0.5,1.5,100", "OLD,20231221,1,2,0.5,1.5,100",
                             "AAA,20231221,1,2,0.5,1.5,100", "HCMAU,20250801,1,2,0.5,1.5,100",
                             "OLD,20220630,1,2,0.5,1.5,100"], windows)
        by_rule = {(i.rule, i.ticker): i for i in issues if not i.alert}
        self.assertEqual(set(by_rule), {("before_first_trade_date", "HCMAU"), ("after_last_trade_date", "OLD")})
        hcmau = by_rule[("before_first_trade_date", "HCMAU")]
        self.assertEqual(hcmau.message, "HCMAU started to trade on 2025-08-01 - data for 2023-12-21 is before its first trade date")
        self.assertEqual((hcmau.column, hcmau.value, hcmau.line_no, hcmau.severity), ("date", "20231221", 2, "error"))
        self.assertIn("OLD stopped trading on 2022-06-30 - data for 2023-12-21", by_rule[("after_last_trade_date", "OLD")].message)

    def test_missing_day_alert_respects_the_window_and_skips_unknown_first_dates(self):
        windows = {"AAA": (date(2020, 1, 2), None), "BBB": (None, None), "HCMAU": (date(2025, 8, 1), None)}
        # 21 Dec 2023 (Thursday): only AAA delivered
        alerts = [i for i in self.check(["AAA,20231221,1,2,0.5,1.5,100"], windows) if i.alert]
        self.assertEqual(alerts, [], "BBB has no first trade date (not checked), HCMAU is not listed yet")
        # now BBB is missing and checked, HCMAU still before its first trade date
        windows["BBB"] = (date(2020, 1, 2), None)
        alerts = [i for i in self.check(["AAA,20231221,1,2,0.5,1.5,100"], windows) if i.alert]
        self.assertEqual([a.ticker for a in alerts], ["BBB"])
        # after the listing HCMAU is expected too; a delisted stock is not
        windows["HCMAU"] = (date(2025, 8, 1), None)
        windows["AAA"] = (date(2020, 1, 2), date(2025, 7, 31))
        alerts = [i for i in self.check(["BBB,20250804,1,2,0.5,1.5,100"], windows) if i.alert]
        self.assertEqual([a.ticker for a in alerts], ["HCMAU"])
        # a stock that is not in the symbol table at all is not checked either
        alerts = [i for i in self.check(["BBB,20250804,1,2,0.5,1.5,100"], {"BBB": (date(2020, 1, 2), None)}) if i.alert]
        self.assertEqual(alerts, [])
        # without a symbol table (None) the old behaviour: every expected ticker is checked
        alerts = [i for i in self.check(["BBB,20250804,1,2,0.5,1.5,100"], None) if i.alert]
        self.assertEqual([a.ticker for a in alerts], ["AAA", "HCMAU"])


class LoaderIntegrationTests(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.db = StageDatabase(self.settings)
        self.loader = StageLoader(self.settings, self.db)
        self.reg = self.loader.symbols

    def deliver(self, name, rows):
        return self.write(f"loading/{name}", DEFAULT_HEADER + rows)

    def test_loader_registers_symbols(self):
        self.deliver("NASDAQ_20231220.txt", "AAA,20231220,1,2,0.5,1.5,100\nHCMAU,20231220,10,10,10,10,0\n")
        r = self.loader.run()[0]
        self.assertEqual(r.new_symbols, 2)
        self.assertIn("[2 new symbol(s)]", str(r))
        self.deliver("NASDAQ_20231221.txt", "AAA,20231221,1,2,0.5,1.5,100\nBBB,20231221,1,2,0.5,1.5,100\n")
        r = self.loader.run()[0]
        self.assertEqual(r.new_symbols, 1)
        self.assertEqual(r.alerts, 0, "HCMAU has no first trade date - its absence is not an alert")
        aaa = self.reg.get("AAA", "NASDAQ")
        self.assertEqual((aaa.first_seen, aaa.last_seen, aaa.first_trade_date), ("2023-12-20", "2023-12-21", None))
        self.assertEqual(self.reg.counts()["symbols"], 3)

    def test_hcmau_case(self):
        """HCMAU (HCM III Acquisition Corp units) did not trade before 2025-08-01: rows dated 2023 are
        invalid, and its absence on other 2023 days is not a missing-market-day alert."""
        self.reg.set("HCMAU", "NASDAQ", first_trade_date="2025-08-01", name="HCM III Acquisition Corp units")
        register_listed(self.settings, "NASDAQ", ["AAA", "BBB"])
        self.deliver("NASDAQ_20231220.txt", "AAA,20231220,1,2,0.5,1.5,100\nBBB,20231220,1,2,0.5,1.5,100\n"
                                            "HCMAU,20231220,10,10,10,10,0\n")
        r = self.loader.run()[0]
        self.assertTrue(r.ok)
        self.assertEqual([(i.rule, i.ticker) for i in r.quality_issues], [("before_first_trade_date", "HCMAU")])
        self.assertEqual(r.alerts, 0)
        self.deliver("NASDAQ_20231221.txt", "AAA,20231221,1,2,0.5,1.5,100\n")
        r = self.loader.run()[0]
        self.assertEqual([(i.rule, i.ticker, i.alert) for i in r.quality_issues], [("missing_market_day", "BBB", True)],
                         "BBB is missing (listed, inside its window); HCMAU is not - it did not exist yet")
        findings = DataErrorRepository(self.settings).fetch(limit=10)
        self.assertEqual({(f.rule, f.ticker) for f in findings}, {("before_first_trade_date", "HCMAU"), ("missing_market_day", "BBB")})
        hcmau = [f for f in findings if f.ticker == "HCMAU"][0]
        self.assertEqual(hcmau.error_message, "HCMAU started to trade on 2025-08-01 - data for 2023-12-20 is before its first trade date")
        self.assertEqual((hcmau.severity, hcmau.alert, hcmau.line_no), ("error", 0, 4))
        # the symbol table shows the problem too
        self.assertTrue(self.reg.get("HCMAU").data_before_listing)
        # from its first trade date on, HCMAU is expected like any other stock
        self.deliver("NASDAQ_20250801.txt", "AAA,20250801,1,2,0.5,1.5,100\nBBB,20250801,1,2,0.5,1.5,100\n")
        r = self.loader.run()[0]
        self.assertEqual([i.ticker for i in r.quality_issues if i.alert], ["HCMAU"])

    def test_delisted_stock(self):
        register_listed(self.settings, "NASDAQ", ["AAA", "OLD"])
        self.reg.set("OLD", "NASDAQ", last_trade_date="2023-12-20")
        self.deliver("NASDAQ_20231220.txt", "AAA,20231220,1,2,0.5,1.5,100\nOLD,20231220,1,2,0.5,1.5,100\n")
        self.loader.run()
        self.deliver("NASDAQ_20231221.txt", "AAA,20231221,1,2,0.5,1.5,100\nOLD,20231221,1,2,0.5,1.5,100\n")
        r = self.loader.run()[0]
        self.assertEqual([(i.rule, i.ticker) for i in r.quality_issues], [("after_last_trade_date", "OLD")])
        self.deliver("NASDAQ_20231222.txt", "AAA,20231222,1,2,0.5,1.5,100\n")
        r = self.loader.run()[0]
        self.assertEqual(r.alerts, 0, "a delisted stock is not missing")

    def test_promotion_rejects_rows_outside_the_window(self):
        self.reg.set("HCMAU", "NASDAQ", first_trade_date="2025-08-01")
        self.deliver("NASDAQ_20231220.txt", "AAA,20231220,1,2,0.5,1.5,100\nHCMAU,20231220,10,10,10,10,0\n")
        self.loader.run()
        svc = PromotionService(self.settings, self.db)
        r = svc.promote_daily()
        self.assertEqual((r.rows_inserted, r.rows_rejected, r.reject_reasons), (1, 1, {"before_first_trade_date": 1}))
        self.assertEqual([x["Ticker"] for x in svc.fetch("daily")], ["AAA"])
        self.settings.check_trading_window = False
        r = PromotionService(self.settings, self.db).promote_daily(full=True)
        self.assertEqual(r.rows_rejected, 0)
        self.assertEqual(r.rows_inserted, 1)

    def test_correction_must_lie_inside_the_window(self):
        self.reg.set("HCMAU", "NASDAQ", first_trade_date="2025-08-01")
        service = CorrectionService(self.settings, self.db)
        issues = service.validate({"Market": "NASDAQ", "Ticker": "HCMAU", "TradeDate": "20231220", "Open": "1",
                                   "High": "2", "Low": "0.5", "Close": "1.5", "Vol": "1"})
        self.assertEqual([i.rule for i in issues], ["before_first_trade_date"])
        with self.assertRaisesRegex(CorrectionError, "before its first trade date"):
            service.add_row({"Market": "NASDAQ", "Ticker": "HCMAU", "TradeDate": "20231220", "Open": "1", "High": "2",
                             "Low": "0.5", "Close": "1.5", "Vol": "1"})
        self.assertEqual(service.validate({"Market": "NASDAQ", "Ticker": "HCMAU", "TradeDate": "20250801", "Open": "1",
                                           "High": "2", "Low": "0.5", "Close": "1.5", "Vol": "1"}), [])


class SymbolsCliTests(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.settings.config_file = self.root / "config.ini"
        save_settings(self.settings, self.settings.config_file)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["--config", str(self.settings.config_file), "-q", *args])
        return rc, out.getvalue(), err.getvalue()

    def test_symbols_commands(self):
        SymbolRegistry(self.settings).register_seen("NASDAQ", {"HCMAU": ("20230123", "20231204"), "AAA": ("20230103", "20231229")})
        rc, out, _ = self.run_cli("symbols", "set", "HCMAU", "--first-trade-date", "2025-08-01", "--name", "HCM III")
        self.assertEqual(rc, 0)
        self.assertIn("HCMAU (NASDAQ): first trade 2025-08-01, last trade -, HCM III", out)
        self.assertIn("! data loaded from 2023-01-23 is before the first trade date", out)
        rc, out, _ = self.run_cli("symbols", "show", "hcmau")
        self.assertIn("first trade date : 2025-08-01", out)
        self.assertIn("! loaded data starts before the first trade date", out)
        rc, out, err = self.run_cli("symbols", "show", "ZZZ")
        self.assertEqual(rc, 1)
        rc, out, _ = self.run_cli("symbols", "list")
        self.assertIn("HCMAU      NASDAQ   2025-08-01", out)
        self.assertIn("HCM III !", out)
        self.assertIn("2 symbol(s), 1 with a first trade date (checked for missing days), 1 without", out)
        rc, out, _ = self.run_cli("symbols", "list", "--missing-dates")
        self.assertIn("AAA", out)
        self.assertNotIn("HCMAU", out.split("\n")[1])
        rc, out, _ = self.run_cli("symbols", "list", "--suspicious", "--ticker", "HC")
        self.assertIn("HCMAU", out)
        rc, out, _ = self.run_cli("symbols", "list", "--ticker", "QQQ")
        self.assertIn("no symbols match", out)
        rc, out, err = self.run_cli("symbols", "set", "HCMAU", "--last-trade-date", "2024-01-01")
        self.assertEqual(rc, 1)
        self.assertIn("after the last trade date", err)
        csv_path = self.write("sym.csv", "ticker,ipo\nMSFT,1986-03-13\nBAD,x\n")
        rc, out, err = self.run_cli("symbols", "import", str(csv_path), "--market", "NASDAQ")
        self.assertEqual(rc, 1)
        self.assertIn("1 symbol(s) added, 0 updated, 1 error(s)", out)
        self.assertIn("line 3", err)
        rc, out, _ = self.run_cli("symbols", "export")
        self.assertIn("MSFT,NASDAQ,1986-03-13", out)
        rc, out, _ = self.run_cli("symbols", "export", str(self.root / "out.csv"), "--market", "NASDAQ")
        self.assertIn("3 symbol(s) written", out)
        rc, out, _ = self.run_cli("symbols", "delete", "MSFT")
        self.assertIn("MSFT (NASDAQ) removed", out)
        rc, out, _ = self.run_cli("status")
        self.assertIn("stock symbols: 2 (1 with a first trade date, 1 without", out)
        rc, out, _ = self.run_cli("init-db")
        self.assertIn("Stock_Symbol", out)


class SymbolsDashboardTests(DashboardTestCase):
    def flashes_of(self, response):
        page = self.client.get(response.headers.get("Location", "/")).get_data(as_text=True)
        return [re.sub(r"<[^>]+>", "", m).strip() for m in re.findall(r'class="flash[^"]*">(.*?)</div>', page, re.S)]

    def test_symbols_page_and_actions(self):
        self.state.symbols.register_seen("NASDAQ", {"HCMAU": ("20230123", "20231204"), "AAA": ("20230103", "20231229")})
        page = self.client.get("/symbols").get_data(as_text=True)
        self.assertIn("HCMAU", page)
        self.assertIn("unknown - not checked", page)
        r = self.client.post("/symbols/save", data={"ticker": "hcmau", "market": "NASDAQ", "first_trade_date": "2025-08-01",
                                                   "name": "HCM III Acquisition Corp units"})
        msgs = self.flashes_of(r)
        self.assertTrue(any("HCMAU (NASDAQ): first trade 2025-08-01" in m for m in msgs), msgs)
        self.assertTrue(any("before the first trade date" in m for m in msgs), msgs)
        page = self.client.get("/symbols?suspicious=1").get_data(as_text=True)
        self.assertIn("HCMAU", page)
        self.assertNotIn(">AAA<", page)
        page = self.client.get("/symbols?edit=HCMAU&edit_market=NASDAQ").get_data(as_text=True)
        self.assertIn('value="2025-08-01"', page)
        self.assertIn("Edit HCMAU", page)
        r = self.client.post("/symbols/save", data={"ticker": "HCMAU", "market": "NASDAQ", "first_trade_date": "bad"})
        self.assertTrue(any("Could not save" in m for m in self.flashes_of(r)))
        r = self.client.post("/symbols/import", data={"file": (BytesIO(b"symbol,ipo\nMSFT,1986-03-13\nX,?\n"), "s.csv"),
                                                     "market": "NASDAQ"}, content_type="multipart/form-data")
        msgs = self.flashes_of(r)
        self.assertTrue(any("Imported s.csv: 2 row(s) - 2 symbol(s) added" in m for m in msgs), msgs)    # '?' clears = valid
        r = self.client.post("/symbols/import", data={"market": "NASDAQ"}, content_type="multipart/form-data")
        self.assertTrue(any("Choose a CSV file" in m for m in self.flashes_of(r)))
        r = self.client.post("/symbols/import", data={"file": (BytesIO(b"a,b\n1,2\n"), "s.csv")}, content_type="multipart/form-data")
        self.assertTrue(any("Import failed" in m for m in self.flashes_of(r)))
        data = self.client.get("/api/symbols?ticker=HC").get_json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["records"][0]["first_trade_date"], "2025-08-01")
        self.assertEqual(data["counts"]["symbols"], 4)
        r = self.client.post("/symbols/delete", data={"ticker": "MSFT", "market": "NASDAQ"})
        self.assertTrue(any("MSFT (NASDAQ) removed" in m for m in self.flashes_of(r)))
        r = self.client.post("/symbols/delete", data={"ticker": "MSFT", "market": "NASDAQ"})
        self.assertTrue(any("unknown symbol" in m for m in self.flashes_of(r)))
        self.assertEqual(self.client.get("/symbols?edit=NOPE").status_code, 200)

    def test_roles(self):
        self.logout()
        self.login(*VIEWER)
        self.assertEqual(self.client.get("/symbols").status_code, 200)
        self.assertNotIn("Set a symbol", self.client.get("/symbols").get_data(as_text=True))
        self.assertEqual(self.client.post("/symbols/save", data={"ticker": "A"}).status_code, 403)
        self.logout()
        self.login(*OPERATOR)
        self.assertEqual(self.client.post("/symbols/save", data={"ticker": "A", "market": "NASDAQ"}).status_code, 302)


if __name__ == "__main__":
    unittest.main()
