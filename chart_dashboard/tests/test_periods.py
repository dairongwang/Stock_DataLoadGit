"""Weekly and monthly candles: the aggregation rules, where the candles come from
(the pipeline's aggregate tables or the daily rows), the JSON / CSV API and the page."""

from __future__ import annotations

import sqlite3
import unittest

from stock_chart.app import create_app
from stock_chart.repository import (PERIOD_COLUMNS, DailyRepository, DataUnavailable, InvalidRequest, aggregate_rows,
                                    check_interval, period_end, period_start)

from .helpers import ChartTestCase, create_period_table, expected_periods

MARKETS = {"AAA": "NASDAQ", "AAB": "NYSE", "ZZZ": None}


class RuleTests(unittest.TestCase):
    def test_period_boundaries(self):
        self.assertEqual(period_start("2025-01-08", "weekly"), "2025-01-06")            # Wednesday -> Monday
        self.assertEqual(period_end("2025-01-08", "weekly"), "2025-01-12")              # ... -> Sunday
        self.assertEqual(period_start("2025-01-01", "weekly"), "2024-12-30")            # ISO week 1 of 2025
        self.assertEqual(period_start("2025-01-06", "weekly"), "2025-01-06")
        self.assertEqual(period_start("2024-02-29", "monthly"), "2024-02-01")
        self.assertEqual(period_end("2024-02-10", "monthly"), "2024-02-29")             # leap year
        self.assertEqual(period_end("2025-12-31", "monthly"), "2025-12-31")
        self.assertEqual(check_interval(" Weekly "), "weekly")
        with self.assertRaises(InvalidRequest):
            check_interval("hourly")

    def test_aggregation(self):
        rows = [("2025-01-02", 10.0, 12.0, 9.0, 11.0, 100), ("2025-01-03", 11.0, 15.0, 10.0, 14.0, 200),
                ("2025-01-06", 14.0, 14.5, 8.0, 9.0, 300), ("2025-01-10", 9.0, 10.0, 8.5, 9.5, 400),
                ("2025-02-03", 9.5, 9.6, 9.4, 9.6, 10)]
        self.assertEqual(aggregate_rows(rows, "weekly"), [
            ("2024-12-30", 10.0, 15.0, 9.0, 14.0, 300, "2025-01-02", "2025-01-03", 2, None),
            ("2025-01-06", 14.0, 14.5, 8.0, 9.5, 700, "2025-01-06", "2025-01-10", 2, None),
            ("2025-02-03", 9.5, 9.6, 9.4, 9.6, 10, "2025-02-03", "2025-02-03", 1, None)])
        self.assertEqual(aggregate_rows(rows, "monthly"), [
            ("2025-01-01", 10.0, 15.0, 8.0, 9.5, 1000, "2025-01-02", "2025-01-10", 4, None),
            ("2025-02-01", 9.5, 9.6, 9.4, 9.6, 10, "2025-02-03", "2025-02-03", 1, None)])
        self.assertEqual(aggregate_rows([], "weekly"), [])


class AggregatedFromDailyTests(ChartTestCase):
    """No aggregate tables in the database: the candles are computed from the daily rows."""

    def setUp(self):
        super().setUp()
        self.repo = DailyRepository(self.settings)

    def test_weekly_and_monthly_match_the_reference(self):
        for interval in ("weekly", "monthly"):
            series = self.repo.periods(interval, "aaa")
            self.assertEqual((series.ticker, series.interval, series.market, series.source),
                             ("AAA", interval, "NASDAQ", "Stock_Daily (aggregated)"))
            self.assertEqual([r[:9] for r in series.rows], expected_periods(self.data["AAA"], interval))
            self.assertTrue(all(r[9] is None for r in series.rows))             # completeness is unknown here
            self.assertEqual((series.truncated, series.total), (False, len(series.rows)))
        self.assertEqual(len(self.repo.periods("monthly", "AAA").rows), 14)      # 300 weekdays: Jan 2025 .. Feb 2026

    def test_ranges_limits_and_unknown_tickers(self):
        weeks = expected_periods(self.data["AAA"], "weekly")
        # a range selects every week it overlaps: a Wednesday .. a Tuesday
        series = self.repo.periods("weekly", "AAA", "2025-01-15", "2025-02-04")
        self.assertEqual([r[0] for r in series.rows], ["2025-01-13", "2025-01-20", "2025-01-27", "2025-02-03"])
        self.assertEqual(series.rows[-1][:9], next(w for w in weeks if w[0] == "2025-02-03"))   # the whole week
        recent = self.repo.periods("weekly", "AAA", limit=10)
        self.assertEqual((len(recent.rows), recent.truncated, recent.total), (10, True, len(weeks)))
        self.assertEqual(recent.last, weeks[-1][0])                                # the most recent weeks
        self.repo.max_points = 12
        self.assertEqual(len(self.repo.periods("weekly", "AAA", limit=1000).rows), 12)
        self.assertEqual(self.repo.periods("monthly", "NOPE").rows, [])
        self.assertIsNone(self.repo.periods("monthly", "ZZZ").market)
        with self.assertRaises(InvalidRequest):
            self.repo.periods("weekly", "AAA", "2025-03-01", "2025-02-01")
        with self.assertRaises(InvalidRequest):
            self.repo.periods("daily", "AAA")
        with self.assertRaises(InvalidRequest):
            self.repo.periods("weekly", "A A")


class AggregateTableTests(ChartTestCase):
    """The pipeline's Stock_Weekly / Stock_Monthly are used when they are up to date."""

    def setUp(self):
        super().setUp()
        self.first_week = expected_periods(self.data["AAA"], "weekly")[0][0]
        create_period_table(self.db, "weekly", self.data, MARKETS, incomplete=(("AAA", self.first_week),))
        create_period_table(self.db, "monthly", self.data, MARKETS)

    def repo(self, source: str = "auto") -> DailyRepository:
        self.settings.period_source = source
        return DailyRepository(self.settings)

    def test_auto_reads_the_tables_with_their_completeness(self):
        series = self.repo().periods("weekly", "AAA")
        self.assertEqual(series.source, "Stock_Weekly")
        self.assertEqual([r[:9] for r in series.rows], expected_periods(self.data["AAA"], "weekly"))
        self.assertIs(series.rows[0][9], False)                                    # flagged incomplete
        self.assertTrue(all(r[9] is True for r in series.rows[1:]))
        monthly = self.repo().periods("monthly", "AAB", limit=1)
        self.assertEqual((monthly.source, monthly.market, monthly.truncated, monthly.total),
                         ("Stock_Monthly", "NYSE", True, 2))

    def test_stale_tables_are_bypassed_in_auto_mode(self):
        with sqlite3.connect(self.db) as conn:                                      # a newer day, not aggregated yet
            conn.execute('INSERT INTO "Stock_Daily" ("Market", "Ticker", "TradeDate", "Open", "High", "Low", "Close", '
                         '"Volume") VALUES (\'NASDAQ\', \'AAA\', \'2026-12-01\', 1, 2, 0.5, 1.5, 10)')
        auto = self.repo().periods("monthly", "AAA")
        self.assertEqual((auto.source, auto.last), ("Stock_Daily (aggregated)", "2026-12-01"))
        self.assertEqual(self.repo("tables").periods("monthly", "AAA").source, "Stock_Monthly")   # as it is
        self.assertEqual(self.repo("daily").periods("weekly", "AAA").source, "Stock_Daily (aggregated)")

    def test_tables_mode_without_tables(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute('DROP TABLE "Stock_Weekly"')
        self.assertEqual(self.repo().periods("weekly", "AAA").source, "Stock_Daily (aggregated)")
        with self.assertRaisesRegex(DataUnavailable, "promote data into the weekly table"):
            self.repo("tables").periods("weekly", "AAA")
        self.assertEqual(self.repo().period_counts(), {"weekly_rows": None,
                                                       "monthly_rows": sum(len(expected_periods(r, "monthly"))
                                                                           for r in self.data.values())})


class PeriodApiTests(ChartTestCase):
    def setUp(self):
        super().setUp()
        self.app = create_app(self.settings)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_json(self):
        data = self.client.get("/api/weekly/AAA").get_json()
        weeks = expected_periods(self.data["AAA"], "weekly")
        self.assertEqual(data["columns"], list(PERIOD_COLUMNS))
        self.assertEqual((data["interval"], data["source"], data["market"], data["count"], data["truncated"]),
                         ("weekly", "Stock_Daily (aggregated)", "NASDAQ", len(weeks), False))
        self.assertEqual(data["rows"][0], [*weeks[0], None])
        data = self.client.get("/api/monthly/AAA?from=2025-03-15&to=2025-04-02").get_json()
        self.assertEqual([r[0] for r in data["rows"]], ["2025-03-01", "2025-04-01"])
        data = self.client.get("/api/monthly/AAA?limit=2").get_json()
        self.assertEqual((data["count"], data["truncated"], data["total"]),
                         (2, True, len(expected_periods(self.data["AAA"], "monthly"))))
        self.assertEqual(self.client.get("/api/daily/AAB").get_json()["interval"], "daily")

    def test_csv(self):
        r = self.client.get("/api/weekly/AAB?format=csv")
        self.assertEqual(r.mimetype, "text/csv")
        lines = r.get_data(as_text=True).splitlines()
        self.assertEqual(lines[0], "week_start,open,high,low,close,volume,first_trade,last_trade,trading_days,complete")
        self.assertEqual(len(lines) - 1, len(expected_periods(self.data["AAB"], "weekly")))
        self.assertTrue(lines[1].endswith(","))                                    # completeness unknown
        self.assertIn('filename="AAB_weekly_', r.headers["Content-Disposition"])
        create_period_table(self.db, "monthly", self.data, MARKETS)
        lines = self.client.get("/api/monthly/AAB?format=csv").get_data(as_text=True).splitlines()
        self.assertTrue(lines[0].startswith("month_start,") and lines[1].endswith(",true"))

    def test_errors(self):
        for url, status in (("/api/weekly/NOPE", 404), ("/api/monthly/A%20A", 400),
                            ("/api/weekly/AAA?from=yesterday", 400), ("/api/monthly/AAA?limit=many", 400),
                            ("/api/yearly/AAA", 404)):
            r = self.client.get(url)
            self.assertEqual(r.status_code, status, url)
            self.assertIn("error", r.get_json())
        self.assertIn("no weekly data for NOPE", self.client.get("/api/weekly/NOPE").get_json()["error"])
        self.settings.daily_table = "Missing_Table"
        client = create_app(self.settings).test_client()
        self.assertEqual(client.get("/api/weekly/AAA").status_code, 503)

    def test_page_and_health(self):
        page = self.client.get("/?ticker=AAA&interval=Weekly").get_data(as_text=True)
        self.assertIn('data-interval="weekly"', page)
        for interval in ("daily", "weekly", "monthly"):
            self.assertIn(f'data-interval-button="{interval}"', page)
        self.assertIn('data-interval="daily"', self.client.get("/?interval=hourly").get_data(as_text=True))
        r = self.client.get("/chart/AAB?interval=monthly")
        self.assertIn("interval=monthly", r.headers["Location"])
        body = self.client.get("/health").get_json()
        self.assertEqual((body["status"], body["weekly_rows"], body["monthly_rows"]), ("ok", None, None))
        self.settings.default_interval = "monthly"
        page = create_app(self.settings).test_client().get("/").get_data(as_text=True)
        self.assertIn('data-interval="monthly"', page)


if __name__ == "__main__":
    unittest.main()
