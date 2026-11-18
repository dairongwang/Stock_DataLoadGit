"""
``promote_eod_daily``: Stock_EOD_Stage (EOD API data) -> Stock_Daily with the
adjusted-price conversion

    rate = AdjustedClose / Close;  Open, High, Low = value * rate;  Close = AdjustedClose;  Volume as is

and the matching of symbol, trade date and market.  Sample data: the MRK
response in docs/SampleAPI_JasonData.txt.
"""

from __future__ import annotations

import io
import json
import sqlite3
import unittest
from contextlib import redirect_stderr, redirect_stdout

from stock_dataload import cli
from stock_dataload.config import save_settings
from stock_dataload.db import StageDatabase, StageRow, now_iso
from stock_dataload.eod_api import SAMPLE_RESPONSE
from stock_dataload.eod_stage import EodStageLoader
from stock_dataload.promotion import (EOD_PRICE_DECIMALS, PROC_EOD_DAILY, PromotionError, PromotionService,
                                      render_procedure)
from stock_dataload.quality import DataErrorRepository
from stock_dataload.symbols import SymbolRegistry

from .helpers import DEFAULT_HEADER, PROJECT_ROOT, TempProjectTestCase
from .test_dashboard import OPERATOR, DashboardTestCase

SAMPLE = json.loads(SAMPLE_RESPONSE.read_text(encoding="utf-8"))


def converted(record: dict) -> tuple:
    """The expected daily values of one API record (computed independently of the SQL)."""
    rate = record["adjusted_close"] / record["close"]
    nd = EOD_PRICE_DECIMALS
    return (round(record["open"] * rate, nd), round(record["high"] * rate, nd), round(record["low"] * rate, nd),
            round(record["adjusted_close"], nd), record["volume"])


class EodPromotionTestCase(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.loader = EodStageLoader(self.settings)
        self.svc = PromotionService(self.settings)

    def load_sample(self, symbol: str = "MRK", records=None, name: str | None = None):
        if records is None:
            return self.loader.load_file(SAMPLE_RESPONSE, symbol)
        return self.loader.load_file(self.write(name or f"{symbol}_20260101_000000.json", json.dumps(records)), symbol)

    def daily(self, ticker: str = "MRK"):
        with sqlite3.connect(self.settings.sqlite_path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute('SELECT * FROM "Stock_Daily" WHERE "Ticker" = ? ORDER BY "TradeDate"',
                                                  (ticker,))]

    def errors(self, rule: str | None = None, open_only: bool = True):
        found = DataErrorRepository(self.settings).fetch(limit=1000, open_only=open_only)
        return [e for e in found if rule is None or e.rule == rule]


class ConversionTests(EodPromotionTestCase):
    def test_reference_script_matches_the_module(self):
        script = (PROJECT_ROOT / "sql" / f"{PROC_EOD_DAILY}_sqlite.sql").read_text(encoding="utf-8")
        self.assertEqual(script, render_procedure(PROC_EOD_DAILY))

    def test_the_sample_is_converted_to_adjusted_prices(self):
        self.load_sample()
        result = self.svc.promote_eod_daily(market="NYSE")
        self.assertTrue(result.ok)
        self.assertEqual((result.rows_read, result.rows_inserted, result.rows_rejected), (251, 251, 0))
        rows = self.daily()
        self.assertEqual(len(rows), 251)
        for record, row in zip(SAMPLE, rows):
            self.assertEqual(row["TradeDate"], record["date"])
            self.assertEqual((row["Open"], row["High"], row["Low"], row["Close"], row["Volume"]), converted(record),
                             record["date"])
            self.assertEqual((row["Market"], row["Ticker"], row["StageId"], row["SourceFile"]),
                             ("NYSE", "MRK", None, "SampleAPI_JasonData.txt"))
        first = rows[0]                    # 2025-11-14: rate = 90.2693 / 92.92
        self.assertAlmostEqual(first["Open"], 92.2 * 90.2693 / 92.92, places=6)
        self.assertEqual(first["Close"], 90.2693)
        self.assertEqual(first["Volume"], 19632000)
        unadjusted = [r for r in SAMPLE if r["adjusted_close"] == r["close"]][0]    # rate 1: values unchanged
        row = [r for r in rows if r["TradeDate"] == unadjusted["date"]][0]
        self.assertEqual((row["Open"], row["High"], row["Low"], row["Close"]),
                         (unadjusted["open"], unadjusted["high"], unadjusted["low"], unadjusted["close"]))
        # weekly / monthly aggregate the converted daily rows
        self.assertTrue(self.svc.promote_weekly().ok)
        week = self.svc.fetch("weekly", ticker="MRK", newest_first=False, limit=1)[0]
        self.assertEqual(week["Open"], rows[0]["Open"])

    def test_incremental_runs_and_changed_adjustments(self):
        self.load_sample()
        SymbolRegistry(self.settings).set("MRK", "NYSE")
        first = self.svc.promote_eod_daily()                           # the first run is full ...
        self.assertEqual((first.mode, first.rows_inserted), ("full", 251))
        again = self.svc.promote_eod_daily()                           # ... then nothing changed since
        self.assertEqual((again.mode, again.rows_inserted, again.rows_updated), ("incremental", 0, 0))
        changed = [dict(r) for r in SAMPLE[:2]]
        changed[0]["adjusted_close"] = 88.0                             # a new dividend adjustment
        self.load_sample(records=changed, name="MRK_20260102_000000.json")
        result = self.svc.promote_eod_daily()
        self.assertEqual((result.mode, result.rows_inserted, result.rows_updated), ("incremental", 0, 1))
        row = self.daily()[0]
        self.assertEqual((row["Close"], row["Open"], row["Market"]), (88.0, round(92.2 * 88.0 / 92.92, 6), "NYSE"))
        self.assertEqual(row["SourceFile"], "MRK_20260102_000000.json")
        self.assertGreater(row["UpdatedAt"], "")
        full = self.svc.promote_eod_daily(full=True)                    # market from the daily table now
        self.assertEqual((full.rows_read, full.rows_unchanged, full.rows_rejected), (251, 251, 0))

    def test_filters(self):
        self.load_sample()
        result = self.svc.promote_eod_daily(symbol="mrk", market="NYSE", date_from="20251201", date_to="2025-12-31")
        self.assertEqual(result.rows_inserted, len([r for r in SAMPLE if r["date"].startswith("2025-12")]))
        self.assertTrue(result.filtered)
        with self.assertRaises(PromotionError):
            self.svc.promote_eod_daily(date_from="2026-01-02", date_to="2026-01-01")
        self.assertEqual(self.svc.promote_eod_daily(symbol="AAPL", market="NYSE").rows_read, 0)


class MarketTests(EodPromotionTestCase):
    def test_unknown_market_is_rejected_and_resolved_later(self):
        self.load_sample()
        result = self.svc.promote_eod_daily()
        self.assertEqual((result.rows_inserted, result.rows_rejected), (0, 251))
        self.assertEqual(result.reject_reasons, {"market_unknown": 251})
        found = self.errors("market_unknown")
        self.assertEqual(len(found), 251)
        self.assertIn("the market of MRK is unknown", found[0].error_message)
        self.svc.promote_eod_daily()                                     # no duplicate findings
        self.assertEqual(len(self.errors("market_unknown")), 251)
        SymbolRegistry(self.settings).set("MRK", "NYSE")                 # register the symbol ...
        result = self.svc.promote_eod_daily()                            # ... the incremental run retries
        self.assertEqual((result.rows_inserted, result.rows_rejected), (251, 0))
        self.assertEqual({r["Market"] for r in self.daily()}, {"NYSE"})
        self.assertEqual(self.errors("market_unknown"), [])             # resolved automatically
        resolved = self.errors("market_unknown", open_only=False)
        self.assertEqual(resolved[0].resolution_note, f"promoted by {PROC_EOD_DAILY}")

    def test_market_sources_in_order(self):
        self.load_sample(records=SAMPLE[:1])
        SymbolRegistry(self.settings).set("MRK", "NYSE")
        self.svc.promote_eod_daily()
        self.assertEqual(self.daily()[0]["Market"], "NYSE")              # Stock_Symbol
        # the daily table decides for a ticker that is not registered
        self.insert_daily("AAA", "2025-11-13", "NASDAQ")
        self.load_sample("AAA.US", records=SAMPLE[:1])
        self.svc.promote_eod_daily()
        self.assertEqual([r["Market"] for r in self.daily("AAA")], ["NASDAQ", "NASDAQ"])
        # [eod_api] default_market for the rest
        self.settings.eod_default_market = "AMEX"
        self.load_sample("ZZZ", records=SAMPLE[:1])
        self.svc.promote_eod_daily()
        self.assertEqual(self.daily("ZZZ")[0]["Market"], "AMEX")
        # a ticker registered in two markets needs --market
        registry = SymbolRegistry(self.settings)                        # seen in deliveries of two markets
        registry.register_seen("NASDAQ", {"DUAL": ("20250101", "20250102")})
        registry.register_seen("NYSE", {"DUAL": ("20250101", "20250102")})
        self.load_sample("DUAL", records=SAMPLE[:1])
        result = self.svc.promote_eod_daily(symbol="DUAL")
        self.assertEqual(result.reject_reasons, {"market_unknown": 1})
        self.assertIn("registered in several markets (NASDAQ, NYSE)", self.errors("market_unknown")[0].error_message)
        self.assertEqual(self.svc.promote_eod_daily(symbol="DUAL", market="NYSE").rows_inserted, 1)

    def insert_daily(self, ticker: str, day: str, market: str, close: str = "10"):
        db = StageDatabase(self.settings)
        db.ensure_stage_table()
        with db.connection() as conn:
            db.insert_stage_row(conn, StageRow(market, ticker, day.replace("-", ""), close, close, close, close, "100",
                                               f"{market}_{day}.txt", "L1", now_iso()))
        self.settings.reject_market_closed_days = False
        self.assertEqual(self.svc.promote_daily(ticker=ticker).rows_rejected, 0)
        self.settings.reject_market_closed_days = True

    def test_same_market_rows_are_matched_other_markets_are_not_overwritten(self):
        day = SAMPLE[0]["date"]
        self.insert_daily("MRK", day, "NYSE", close="92.92")             # from a delivery file (unadjusted)
        self.load_sample(records=SAMPLE[:2])
        result = self.svc.promote_eod_daily(market="NYSE")
        self.assertEqual((result.rows_inserted, result.rows_updated), (1, 1))
        row = self.daily()[0]
        self.assertEqual((row["Close"], row["StageId"], row["Market"]), (90.2693, None, "NYSE"))   # matched + converted
        # the same ticker / day under another market is left alone
        self.insert_daily("OTHER", day, "NASDAQ", close="5")
        self.load_sample("OTHER", records=SAMPLE[:1])
        result = self.svc.promote_eod_daily(symbol="OTHER", market="NYSE")
        self.assertEqual(result.reject_reasons, {"market_mismatch": 1})
        self.assertEqual((self.daily("OTHER")[0]["Close"], self.daily("OTHER")[0]["Market"]), (5.0, "NASDAQ"))
        self.assertIn("already holds OTHER", self.errors("market_mismatch")[0].error_message)

    def test_one_row_per_ticker_and_day(self):
        self.load_sample("MRK", records=SAMPLE[:3])
        changed = [dict(r, adjusted_close=r["close"]) for r in SAMPLE[:3]]
        self.load_sample("MRK.US", records=changed, name="MRK.US_20260101_000000.json")   # loaded later -> wins
        result = self.svc.promote_eod_daily(market="NYSE")
        self.assertEqual((result.rows_read, result.rows_inserted, result.rows_skipped), (6, 3, 3))
        self.assertEqual([r["Close"] for r in self.daily()], [r["close"] for r in SAMPLE[:3]])


class ValidationTests(EodPromotionTestCase):
    def test_rules_on_the_converted_values(self):
        base = SAMPLE[0]
        records = [dict(base, date="2025-11-17", close=0),                                # no rate
                   dict(base, date="2025-11-18", adjusted_close=0),
                   dict(base, date="2025-11-19", high=80.0),                               # high < low
                   dict(base, date="2025-11-20", open=99.0),                               # open above high
                   dict(base, date="2025-11-22"),                                          # a Saturday
                   dict(base, date="2025-11-27"),                                          # Thanksgiving
                   dict(base, date="2025-11-24")]                                          # fine
        self.load_sample(records=records)
        result = self.svc.promote_eod_daily(market="NYSE")
        self.assertEqual(result.reject_reasons, {"positive_price": 2, "high_above_low": 1, "price_range": 1,
                                                 "market_closed": 2})
        self.assertEqual([r["TradeDate"] for r in self.daily()], ["2025-11-24"])
        messages = {e.trade_date: e.error_message for e in self.errors()}
        self.assertIn("the rate AdjustedClose / Close cannot be computed", messages["2025-11-17"])
        self.assertIn("NYSE was closed on 2025-11-27 (Thanksgiving Day)", messages["2025-11-27"])
        self.assertIn("converted open", messages["2025-11-20"])

    def test_trading_window(self):
        self.load_sample(records=SAMPLE[:2])
        SymbolRegistry(self.settings).set("MRK", "NYSE", first_trade_date=SAMPLE[1]["date"])
        result = self.svc.promote_eod_daily()
        self.assertEqual((result.rows_inserted, result.reject_reasons), (1, {"before_first_trade_date": 1}))


class CliAndDashboardTests(EodPromotionTestCase):
    def setUp(self):
        super().setUp()
        self.settings.config_file = self.root / "config.ini"
        save_settings(self.settings, self.settings.config_file)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["--config", str(self.settings.config_file), "-q", *args])
        return rc, out.getvalue(), err.getvalue()

    def test_commands(self):
        rc, out, err = self.run_cli("eod-load", str(SAMPLE_RESPONSE), "--symbol", "MRK")
        self.assertEqual(rc, 0, err)
        rc, out, _ = self.run_cli("promote", "eod")
        self.assertEqual(rc, 0)
        self.assertIn("251 rejected [market_unknown 251]", out)
        self.assertIn("give --market", out)
        rc, out, _ = self.run_cli("promote", "eod", "--market", "NYSE", "--symbol", "MRK")
        self.assertIn("promote_eod_daily: completed (full) - 251 rows read: 251 inserted", out)
        self.assertEqual(self.run_cli("promote", "eod", "--file", "x")[0], 2)
        self.assertEqual(self.run_cli("promote", "daily", "--market", "NYSE")[0], 2)
        rc, out, _ = self.run_cli("promotions", "--procedure", PROC_EOD_DAILY)
        self.assertIn(PROC_EOD_DAILY, out)
        rc, out, _ = self.run_cli("status")
        self.assertIn("last promote_eod_daily completed", out)
        # fetch (stub) -> load -> promote in one go
        rc, out, err = self.run_cli("eod-fetch", "AAPL.US", "--fake", "--from", "2026-11-01", "--promote",
                                    "--market", "NASDAQ")
        self.assertEqual(rc, 0, err)
        self.assertIn("promote_eod_daily: completed (full) - 10 rows read: 10 inserted", out)
        self.assertEqual({r["Market"] for r in self.daily("AAPL")}, {"NASDAQ"})


class DashboardEodPromotionTests(DashboardTestCase):
    def test_promote_eod_from_the_dashboard(self):
        EodStageLoader(self.state.settings).load_file(SAMPLE_RESPONSE, "MRK")
        page = self.client.get("/promote").get_data(as_text=True)
        self.assertIn('value="eod"', page)
        self.assertIn("rate = AdjustedClose / Close", page)
        r = self.client.post("/promote", data={"what": "eod"})
        self.assertIn("251 rejected [market_unknown 251]", self.flashes(r))
        r = self.client.post("/promote", data={"what": "eod", "market": "NYSE", "symbol": "MRK"})
        self.assertIn("251 inserted", self.flashes(r))
        self.assertEqual(self.client.get("/api/active/daily?ticker=MRK").get_json()["count"], 100)
        self.logout()
        self.login(*OPERATOR)
        self.assertEqual(self.client.post("/promote", data={"what": "eod"}).status_code, 302)


if __name__ == "__main__":
    unittest.main()
