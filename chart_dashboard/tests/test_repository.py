"""Read-only access to the daily table."""

from __future__ import annotations

import sqlite3
import unittest

from stock_chart.repository import DailyRepository, DataUnavailable, InvalidRequest, clean_date, clean_ticker

from .helpers import ChartTestCase


class RepositoryTests(ChartTestCase):
    def setUp(self):
        super().setUp()
        self.repo = DailyRepository(self.settings)

    def test_tickers(self):
        found = self.repo.tickers()
        self.assertEqual([t.ticker for t in found], ["AAA", "AAB", "ZZZ"])
        aaa = found[0]
        self.assertEqual((aaa.market, aaa.rows, aaa.first, aaa.last),
                         ("NASDAQ", 300, self.data["AAA"][0][1], self.data["AAA"][-1][1]))
        self.assertEqual([t.ticker for t in self.repo.tickers("aa")], ["AAA", "AAB"])
        self.assertEqual([t.ticker for t in self.repo.tickers("AAB")], ["AAB"])
        self.assertEqual(len(self.repo.tickers(limit=1)), 1)
        self.assertEqual(self.repo.default_ticker(), "AAA")                 # most rows
        self.assertEqual(self.repo.default_ticker("AAB"), "AAB")
        self.assertEqual(self.repo.default_ticker("NOPE"), "AAA")

    def test_daily_rows(self):
        series = self.repo.daily("aaa")
        self.assertEqual((series.ticker, series.market, series.total, series.truncated), ("AAA", "NASDAQ", 300, False))
        self.assertEqual(series.rows, [r[1:] for r in self.data["AAA"]])     # oldest first, exact values
        window = self.repo.daily("AAA", self.data["AAA"][10][1], self.data["AAA"][19][1].replace("-", ""))
        self.assertEqual([r[0] for r in window.rows], [r[1] for r in self.data["AAA"][10:20]])
        recent = self.repo.daily("AAA", limit=50)
        self.assertEqual((len(recent.rows), recent.truncated, recent.total), (50, True, 300))
        self.assertEqual(recent.last, self.data["AAA"][-1][1])              # the most recent days
        self.repo.max_points = 20
        self.assertEqual(len(self.repo.daily("AAA", limit=1000).rows), 20)  # capped by max_points
        self.assertEqual(self.repo.daily("NOPE").rows, [])
        self.assertIsNone(self.repo.daily("ZZZ").market)

    def test_invalid_input(self):
        for bad in ("", "A A", "x" * 30, "AAA;--", "<script>"):
            with self.assertRaises(InvalidRequest, msg=bad):
                clean_ticker(bad)
        with self.assertRaises(InvalidRequest):
            clean_date("2025-02-30", "from")
        self.assertEqual(clean_date("20250131", "from"), "2025-01-31")
        self.assertIsNone(clean_date("", "to"))
        with self.assertRaises(InvalidRequest):
            self.repo.daily("AAA", "2025-03-01", "2025-02-01")

    def test_read_only(self):
        conn = self.repo.connect()
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute('DELETE FROM "Stock_Daily"')
        conn.close()
        self.assertEqual(self.repo.check()["rows"], 345)

    def test_data_unavailable(self):
        self.settings.sqlite_path = self.root / "missing.db"
        with self.assertRaisesRegex(DataUnavailable, "does not exist"):
            DailyRepository(self.settings).tickers()
        self.settings.sqlite_path = self.db
        self.settings.daily_table = "Other_Table"
        repo = DailyRepository(self.settings)
        with self.assertRaisesRegex(DataUnavailable, "promote data"):
            repo.daily("AAA")
        with self.assertRaisesRegex(DataUnavailable, "does not exist"):
            repo.check()
        with sqlite3.connect(self.db) as conn:
            conn.execute('CREATE TABLE "Other_Table" ("Ticker" TEXT, "TradeDate" TEXT)')
        with self.assertRaisesRegex(DataUnavailable, "lacks the column"):
            repo.check()

    def test_ticker_cache(self):
        self.repo.cache_seconds = 3600
        self.assertEqual(len(self.repo.tickers()), 3)
        with sqlite3.connect(self.db) as conn:
            conn.execute('INSERT INTO "Stock_Daily" ("Ticker", "TradeDate", "Open", "High", "Low", "Close", "Volume") '
                         "VALUES ('NEW', '2025-01-02', 1, 1, 1, 1, 1)")
        self.assertEqual(len(self.repo.tickers()), 3)                       # cached
        self.assertEqual(len(self.repo.all_tickers(refresh=True)), 4)


if __name__ == "__main__":
    unittest.main()
