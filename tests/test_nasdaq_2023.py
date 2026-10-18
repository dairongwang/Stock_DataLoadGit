"""
Bulk loading + promotion with the real NASDAQ_2023 deliveries (255 daily files,
848,626 rows, next to the project folder or $STOCK_DATALOAD_TEST_DATA).

By default the first 25 files (January 2023) are used so the suite stays
fast; set STOCK_DATALOAD_FULL_YEAR=1 to run the whole year (about a minute).
Skipped when the folder is not available.
"""

from __future__ import annotations

import collections
import csv
import os
import unittest
from datetime import date, timedelta
from pathlib import Path

from stock_dataload import market_calendar
from stock_dataload.db import StageDatabase
from stock_dataload.loader import StageLoader
from stock_dataload.promotion import PromotionService
from stock_dataload.symbols import SymbolRegistry

from .helpers import TempProjectTestCase

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("STOCK_DATALOAD_TEST_DATA", PROJECT_ROOT.parent / "NASDAQ_2023"))
FULL_YEAR = os.environ.get("STOCK_DATALOAD_FULL_YEAR") == "1"
SUBSET = 25


def read_files(files: list[Path]) -> dict[tuple[str, date], tuple]:
    """{(ticker, day): (open, high, low, close, vol)} of the files, market-open days only."""
    rows = {}
    for f in files:
        with open(f, newline="", encoding="utf-8-sig") as fh:
            reader = csv.reader(fh)
            next(reader)
            for t, d, o, h, l, c, v in reader:
                day = date(int(d[:4]), int(d[4:6]), int(d[6:]))
                if t == "HCMAU":          # first traded on 2025-08-01 - 2023 rows are invalid (see the test)
                    continue
                if market_calendar.is_market_open(day):
                    rows[(t, day)] = (float(o), float(h), float(l), float(c), int(v))
    return rows


def aggregate(rows: list[tuple[date, tuple]]) -> tuple:
    rows.sort()
    return (rows[0][1][0], max(r[1][1] for r in rows), min(r[1][2] for r in rows), rows[-1][1][3],
            sum(r[1][4] for r in rows), len(rows), rows[0][0].isoformat(), rows[-1][0].isoformat())


@unittest.skipUnless(DATA_DIR.is_dir(), f"test data folder {DATA_DIR} not available")
class Nasdaq2023Tests(TempProjectTestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = sorted(DATA_DIR.glob("NASDAQ_2023*.txt"))
        if not FULL_YEAR:
            cls.files = cls.files[:SUBSET]
        cls.expected = read_files(cls.files)

    def setUp(self):
        super().setUp()
        self.settings.batch_size = 5000
        self.db = StageDatabase(self.settings)
        self.loader = StageLoader(self.settings, self.db)
        self.svc = PromotionService(self.settings, self.db)

    def test_bulk_load_then_promote_daily_weekly_monthly(self):
        folder = self.root / "NASDAQ_2023"
        folder.mkdir()
        for f in self.files:
            try:
                (folder / f.name).symlink_to(f)
            except OSError:                       # no symlink privilege (Windows): copy instead
                (folder / f.name).write_bytes(f.read_bytes())

        # -- bulk load: one load per file, files untouched ------------------------
        # the deliveries dated on market holidays (2 Jan, 16 Jan, 7 Apr, 29 May, 19 Jun - placeholder
        # files with zero volume) are skipped with a warning, everything else is loaded
        holidays = {"NASDAQ_20230102.txt", "NASDAQ_20230116.txt", "NASDAQ_20230407.txt", "NASDAQ_20230529.txt",
                    "NASDAQ_20230619.txt"}
        expected_skips = [f.name for f in self.files if f.name in holidays]
        # HCMAU (HCM III Acquisition Corp units) first traded on 2025-08-01 - the 2023 rows carrying
        # that symbol are invalid, and its absence on other days must not be an alert
        SymbolRegistry(self.settings, self.db).set("HCMAU", "NASDAQ", first_trade_date="2025-08-01")
        hcmau_files = [f.name for f in self.files if f.name not in holidays
                       and any(line.startswith("HCMAU,") for line in f.read_text(encoding="utf-8-sig").splitlines())]
        summary = self.loader.load_folder(folder)
        self.assertTrue(summary.ok, str(summary))
        self.assertEqual([r.source_file.name for r in summary.results if r.skipped], expected_skips)
        self.assertEqual(summary.skipped, len(expected_skips))
        self.assertEqual(summary.loaded, len(self.files) - len(expected_skips))
        self.assertIn(f"{summary.skipped} skipped (no trading day)", str(summary))
        total_rows = 0
        for f in self.files:
            if f.name in holidays:
                continue
            with open(f, encoding="utf-8-sig") as fh:
                total_rows += sum(1 for _ in fh) - 1
        self.assertEqual(summary.rows_loaded, total_rows)
        self.assertEqual(self.db.count_rows(), total_rows)
        history = self.db.load_history(limit=1000)
        self.assertEqual(len(history), len(self.files))
        self.assertEqual(sorted(h.file_name for h in history if h.status == "skipped"), expected_skips)
        self.assertEqual(len(list(folder.iterdir())), len(self.files))
        skipped = [r for r in summary.results if r.skipped][0]
        self.assertIn("NASDAQ was closed on 2023-01-02 (New Year's Day (observed))", skipped.message)
        self.assertTrue(all(r.alerts == 0 for r in summary.results if r.skipped))
        # symbol table: every ticker registered; no stock has a first trade date except HCMAU -> no
        # missing-day alerts at all, but every HCMAU row is flagged as before its first trade date
        registry = SymbolRegistry(self.settings, self.db)
        self.assertEqual(registry.counts()["symbols"], len({t for t, _ in self.expected} | {"HCMAU"}))
        self.assertEqual(summary.alerts, 0)
        flagged = [r.source_file.name for r in summary.results
                   if any(i.rule == "before_first_trade_date" and i.ticker == "HCMAU" for i in r.quality_issues)]
        self.assertEqual(flagged, hcmau_files)
        self.assertTrue(len(hcmau_files) >= 1)
        self.assertTrue(registry.get("HCMAU").data_before_listing)

        # -- promote daily: only the HCMAU rows (before its first trade date) are rejected ----
        r = self.svc.promote_daily()
        self.assertTrue(r.ok, str(r))
        self.assertEqual(r.rows_read, total_rows)
        self.assertEqual(r.rows_inserted, len(self.expected))
        self.assertEqual(r.reject_reasons, {"before_first_trade_date": len(hcmau_files)})
        self.assertEqual(self.svc.count_rows("daily"), len(self.expected))
        for row in self.svc.fetch("daily", ticker="AAPL", limit=1000):
            self.assertEqual(self.expected[("AAPL", date.fromisoformat(row["TradeDate"]))],
                             (row["Open"], row["High"], row["Low"], row["Close"], row["Volume"]))
        self.assertEqual(self._findings(), [])
        self.assertEqual(self.svc.fetch("daily", ticker="HCMAU"), [])

        # -- weekly / monthly match an independent computation ---------------------------
        weeks, months = collections.defaultdict(list), collections.defaultdict(list)
        for (t, day), values in self.expected.items():
            weeks[(t, day - timedelta(days=day.weekday()))].append((day, values))
            months[(t, day.replace(day=1))].append((day, values))
        rw = self.svc.promote_weekly()
        rm = self.svc.promote_monthly()
        self.assertEqual((rw.rows_inserted, rm.rows_inserted), (len(weeks), len(months)))
        # completeness: a period is incomplete when one of the market-open days between the first and
        # the last loaded day has no row (no symbol has a first trade date - every open day counts)
        open_days = sorted({d for _, d in self.expected})
        lo, hi = open_days[0], open_days[-1]
        all_open = {d for d in (lo + timedelta(n) for n in range((hi - lo).days + 1)) if market_calendar.is_market_open(d)}
        def incomplete(periods, end_of):
            out = {}
            for (t, start), rows in periods.items():
                present = {d for d, _ in rows}
                missing = sorted(d for d in all_open if start <= d <= end_of(start) and d not in present)
                if missing:
                    out[(t, start.isoformat())] = ",".join(d.isoformat() for d in missing)
            return out
        exp_weeks = incomplete(weeks, lambda s: s + timedelta(days=6))
        exp_months = incomplete(months, lambda s: (s.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1))
        self.assertEqual((rw.rows_incomplete, rm.rows_incomplete), (len(exp_weeks), len(exp_months)))
        db_weeks_incomplete = {(w["Ticker"], w["WeekStart"]): w["MissingDates"]
                               for w in self.svc.fetch("weekly", incomplete_only=True, limit=10 ** 7)}
        self.assertEqual(db_weeks_incomplete, exp_weeks)
        self.assertEqual({(m["Ticker"], m["MonthStart"]): m["MissingDates"]
                          for m in self.svc.fetch("monthly", incomplete_only=True, limit=10 ** 7)}, exp_months)
        self.assertTrue(len(exp_weeks) > 0)
        findings = self._rule_findings("incomplete_week")
        self.assertEqual(len(findings), len(exp_weeks))
        self.assertTrue(all(w["Complete"] == 1 for w in self.svc.fetch("weekly", ticker="AAPL", limit=100)))
        for w in self.svc.fetch("weekly", ticker="AAPL", limit=100):
            monday = date.fromisoformat(w["WeekStart"])
            self.assertEqual(monday.weekday(), 0)
            self.assertEqual(w["WeekEnd"], (monday + timedelta(days=6)).isoformat())
            self.assertEqual((w["ISOYear"], w["ISOWeek"]), monday.isocalendar()[:2])
            self.assertEqual(aggregate(weeks[("AAPL", monday)]),
                             (w["Open"], w["High"], w["Low"], w["Close"], w["Volume"], w["TradingDays"],
                              w["FirstTradeDate"], w["LastTradeDate"]))
        for m in self.svc.fetch("monthly", ticker="MSFT", limit=100):
            first = date.fromisoformat(m["MonthStart"])
            self.assertEqual((m["Year"], m["Month"]), (first.year, first.month))
            self.assertEqual(aggregate(months[("MSFT", first)]),
                             (m["Open"], m["High"], m["Low"], m["Close"], m["Volume"], m["TradingDays"],
                              m["FirstTradeDate"], m["LastTradeDate"]))
        # every weekly / monthly row, not just the two stocks
        db_weeks = {(w["Ticker"], w["WeekStart"]): (w["Volume"], w["TradingDays"])
                    for w in self.svc.fetch("weekly", limit=10 ** 7)}
        self.assertEqual(db_weeks, {(t, d.isoformat()): (sum(v[4] for _, v in rows), len(rows))
                                    for (t, d), rows in weeks.items()})

        # -- promoting again changes nothing ------------------------------------------
        r2 = self.svc.promote_daily(full=True)
        self.assertEqual((r2.rows_inserted, r2.rows_updated, r2.rows_unchanged), (0, 0, len(self.expected)))
        rw2 = self.svc.promote_weekly(full=True)
        self.assertEqual((rw2.rows_inserted, rw2.rows_updated, rw2.rows_deleted), (0, 0, 0))

    def _findings(self) -> list[dict]:
        return self._rule_findings("market_closed")

    def _rule_findings(self, rule: str) -> list[dict]:
        import sqlite3
        with sqlite3.connect(self.settings.sqlite_path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute('SELECT * FROM "Data_Error" WHERE "Rule" = ?', (rule,))]


if __name__ == "__main__":
    unittest.main()
