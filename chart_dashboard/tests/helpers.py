"""Test fixtures: a temporary SQLite database with the daily table of the data contract.

The schema below is the contract the chart dashboard relies on (the pipeline's
Stock_Daily has more columns - they are ignored).  No import of the pipeline:
the dashboard is tested on its own.
"""

from __future__ import annotations

import random
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from stock_chart.config import ChartSettings

DAILY_DDL = '''
CREATE TABLE "Stock_Daily" (
    "DailyId"    INTEGER PRIMARY KEY AUTOINCREMENT,
    "Market"     TEXT,
    "Ticker"     TEXT NOT NULL,
    "TradeDate"  TEXT NOT NULL,
    "Open"       REAL NOT NULL,
    "High"       REAL NOT NULL,
    "Low"        REAL NOT NULL,
    "Close"      REAL NOT NULL,
    "Volume"     INTEGER NOT NULL,
    "SourceFile" TEXT,
    CONSTRAINT "UQ_Stock_Daily_Ticker_TradeDate" UNIQUE ("Ticker", "TradeDate")
)'''


def trading_days(start: date, count: int) -> list[str]:
    out, day = [], start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def make_series(ticker: str, count: int, start: date = date(2025, 1, 2), seed: int = 7, price: float = 100.0):
    """A deterministic random walk: (ticker, date, open, high, low, close, volume) rows."""
    rnd = random.Random(f"{ticker}-{seed}")
    rows = []
    for day in trading_days(start, count):
        o = price
        c = max(1.0, o * (1 + rnd.gauss(0, 0.02)))
        h = max(o, c) * (1 + abs(rnd.gauss(0, 0.01)))
        low = min(o, c) * (1 - abs(rnd.gauss(0, 0.01)))
        rows.append((ticker, day, round(o, 2), round(h, 2), round(low, 2), round(c, 2), rnd.randint(10_000, 5_000_000)))
        price = c
    return rows


def create_daily_db(path: Path, series: dict[str, tuple[int, str | None]]) -> dict[str, list[tuple]]:
    """*series*: ticker -> (days, market).  Returns the rows per ticker."""
    out = {}
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(DAILY_DDL)
    for ticker, (count, market) in series.items():
        rows = make_series(ticker, count)
        out[ticker] = rows
        conn.executemany('INSERT INTO "Stock_Daily" ("Market", "Ticker", "TradeDate", "Open", "High", "Low", "Close", '
                         '"Volume", "SourceFile") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                         [(market, *r, "test.txt") for r in rows])
    conn.commit()
    conn.close()
    return out


PERIOD_DDL = '''
CREATE TABLE "{table}" (
    "Id"             INTEGER PRIMARY KEY AUTOINCREMENT,
    "Market"         TEXT,
    "Ticker"         TEXT NOT NULL,
    "{start}"        TEXT NOT NULL,
    "Open"           REAL NOT NULL,
    "High"           REAL NOT NULL,
    "Low"            REAL NOT NULL,
    "Close"          REAL NOT NULL,
    "Volume"         INTEGER NOT NULL,
    "TradingDays"    INTEGER NOT NULL,
    "FirstTradeDate" TEXT NOT NULL,
    "LastTradeDate"  TEXT NOT NULL,
    "Complete"       INTEGER NOT NULL DEFAULT 1,
    CONSTRAINT "UQ_{table}" UNIQUE ("Ticker", "{start}")
)'''


def expected_periods(rows: list[tuple], interval: str) -> list[tuple]:
    """Independent reference aggregation of daily rows (ticker, date, o, h, l, c, v): per ISO week
    (``date.isocalendar``) or calendar month -> (start, open, high, low, close, volume, first, last, days)."""
    groups: dict[tuple, list[tuple]] = {}
    for r in rows:
        d = date.fromisoformat(r[1])
        key = d.isocalendar()[:2] if interval == "weekly" else (d.year, d.month)
        groups.setdefault(key, []).append(r)
    out = []
    for key in sorted(groups):
        g = groups[key]
        if interval == "weekly":
            start = date.fromisocalendar(key[0], key[1], 1).isoformat()
        else:
            start = date(key[0], key[1], 1).isoformat()
        out.append((start, g[0][2], max(x[3] for x in g), min(x[4] for x in g), g[-1][5], sum(x[6] for x in g),
                    g[0][1], g[-1][1], len(g)))
    return out


def create_period_table(path: Path, interval: str, data: dict[str, list[tuple]], markets: dict[str, str | None],
                        incomplete: tuple[tuple[str, str], ...] = ()) -> None:
    """The pipeline's Stock_Weekly / Stock_Monthly (the columns the dashboard reads) filled with the
    reference aggregates of *data*; *incomplete* = (ticker, period start) pairs flagged Complete = 0."""
    table, start = ("Stock_Weekly", "WeekStart") if interval == "weekly" else ("Stock_Monthly", "MonthStart")
    conn = sqlite3.connect(path)
    conn.execute(PERIOD_DDL.format(table=table, start=start))
    for ticker, rows in data.items():
        for p in expected_periods(rows, interval):
            conn.execute(f'INSERT INTO "{table}" ("Market", "Ticker", "{start}", "Open", "High", "Low", "Close", '
                         f'"Volume", "FirstTradeDate", "LastTradeDate", "TradingDays", "Complete") '
                         f'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                         (markets.get(ticker), ticker, *p, 0 if (ticker, p[0]) in incomplete else 1))
    conn.commit()
    conn.close()


class ChartTestCase(unittest.TestCase):
    """A temporary folder with a database: AAA (300 days, NASDAQ), AAB (40 days, NYSE), ZZZ (5 days, no market)."""

    SERIES = {"AAA": (300, "NASDAQ"), "AAB": (40, "NYSE"), "ZZZ": (5, None)}

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="stock_chart_test_"))
        self.db = self.root / "stock_trade.db"
        self.data = create_daily_db(self.db, self.SERIES)
        self.settings = ChartSettings(sqlite_path=self.db, ticker_cache_seconds=0)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
