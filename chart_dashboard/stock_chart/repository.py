"""
Read-only access to the stock tables (the data contract with the pipeline).
(The stock lists - the only thing the dashboard writes - are in ``lists.py``.)

Every query opens the SQLite file with ``mode=ro`` - the chart dashboard can
never change the data, and it works while the pipeline writes (WAL mode:
readers see the last committed state).  Expected columns::

    daily    Ticker, TradeDate ('YYYY-MM-DD'), Market, Open, High, Low, Close, Volume
    weekly   Ticker, WeekStart, Market, Open, High, Low, Close, Volume, TradingDays,
             FirstTradeDate, LastTradeDate, Complete                   (optional - see below)
    monthly  Ticker, MonthStart, ... the same columns as weekly           (optional - see below)

Queries use the tables' UNIQUE (Ticker, TradeDate / WeekStart / MonthStart)
index.  The ticker list (a GROUP BY over the daily table) is cached for
``ticker_cache_seconds``.

Weekly / monthly candles
------------------------
One candle per ISO week (Monday..Sunday) or calendar month: open of the first
trading day, highest high, lowest low, close of the last trading day, summed
volume - the rules of the pipeline's ``promote weekly`` / ``promote monthly``.
``[chart] period_source`` decides where they come from:

``auto`` (default)
    the aggregate table (``Stock_Weekly`` / ``Stock_Monthly``) when it holds the
    ticker and is as recent as its daily rows - otherwise the daily rows are
    aggregated on the fly, so the chart never lags behind the daily data;
``tables``
    the aggregate tables only (a ticker without aggregates has no candles);
``daily``
    always aggregated from the daily rows (the aggregate tables are not read).

Only the aggregate tables know whether a period is complete (no trading day
missing); candles aggregated on the fly report ``complete = null``.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from .config import INTERVALS, ChartSettings

log = logging.getLogger(__name__)

TICKER_RE = re.compile(r"^[A-Za-z0-9.\-^=_]{1,20}$")
REQUIRED_COLUMNS = ("Ticker", "TradeDate", "Market", "Open", "High", "Low", "Close", "Volume")
#: the columns of a weekly / monthly row (JSON "columns"); the first six are those of a daily row
PERIOD_COLUMNS = ("date", "open", "high", "low", "close", "volume", "first_trade", "last_trade", "trading_days",
                  "complete")
#: the period-start column of the aggregate tables
PERIOD_START_COLUMN = {"weekly": "WeekStart", "monthly": "MonthStart"}


class DataUnavailable(Exception):
    """The database / table cannot be read (missing file, missing table, wrong columns)."""


class InvalidRequest(Exception):
    """Bad parameters (HTTP 400)."""


@dataclass
class TickerInfo:
    ticker: str
    market: str | None
    rows: int
    first: str
    last: str

    def as_dict(self) -> dict:
        return {"ticker": self.ticker, "market": self.market, "rows": self.rows, "first": self.first, "last": self.last}


@dataclass
class DailySeries:
    ticker: str
    market: str | None
    rows: list[tuple[str, float, float, float, float, int]]    # (date, open, high, low, close, volume), oldest first
    truncated: bool                                              # older rows were left out (max_points)
    total: int                                                   # rows of the ticker in the requested range

    @property
    def first(self) -> str | None:
        return self.rows[0][0] if self.rows else None

    @property
    def last(self) -> str | None:
        return self.rows[-1][0] if self.rows else None


@dataclass
class PeriodSeries:
    """Weekly or monthly candles of one ticker, oldest first."""
    ticker: str
    interval: str                                                # weekly | monthly
    market: str | None
    #: (period start, open, high, low, close, volume, first trade date, last trade date, trading days, complete)
    rows: list[tuple]
    truncated: bool                                              # older periods were left out (max_points)
    total: int                                                   # periods of the ticker in the requested range
    source: str                                                  # the aggregate table, or "<daily table> (aggregated)"

    @property
    def first(self) -> str | None:
        return self.rows[0][0] if self.rows else None

    @property
    def last(self) -> str | None:
        return self.rows[-1][0] if self.rows else None


def period_start(day: str | date, interval: str) -> str:
    """The first day of the ISO week (Monday) / calendar month that contains *day*."""
    d = date.fromisoformat(day) if isinstance(day, str) else day
    if interval == "weekly":
        return (d - timedelta(days=d.weekday())).isoformat()
    return d.replace(day=1).isoformat()


def period_end(day: str | date, interval: str) -> str:
    """The last day of the ISO week (Sunday) / calendar month that contains *day*."""
    start = date.fromisoformat(period_start(day, interval))
    if interval == "weekly":
        return (start + timedelta(days=6)).isoformat()
    following = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return (following - timedelta(days=1)).isoformat()


def aggregate_rows(rows, interval: str) -> list[tuple]:
    """Daily rows ``(date, open, high, low, close, volume)`` - oldest first - into one row per ISO week /
    calendar month: open of the first day, highest high, lowest low, close of the last day, summed volume."""
    out: list[tuple] = []
    current: list | None = None
    for day, o, h, lo, c, v in rows:
        start = period_start(day, interval)
        if current is None or current[0] != start:
            if current is not None:
                out.append(tuple(current))
            current = [start, o, h, lo, c, v, day, day, 1, None]
        else:
            current[2] = max(current[2], h)
            current[3] = min(current[3], lo)
            current[4] = c
            current[5] += v
            current[7] = day
            current[8] += 1
    if current is not None:
        out.append(tuple(current))
    return out


def check_interval(interval: str | None) -> str:
    value = (interval or "").strip().lower()
    if value not in INTERVALS:
        raise InvalidRequest(f"'{interval}' is not an interval - use one of {', '.join(INTERVALS)}")
    return value


def clean_ticker(text: str | None) -> str:
    ticker = (text or "").strip().upper()
    if not TICKER_RE.match(ticker):
        raise InvalidRequest(f"'{text}' is not a valid ticker")
    return ticker


def clean_date(text: str | None, what: str) -> str | None:
    text = (text or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{8}", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise InvalidRequest(f"{what} '{text}' is not a date (YYYY-MM-DD)") from None


class DailyRepository:
    def __init__(self, settings: ChartSettings):
        self.path = Path(settings.sqlite_path)
        self.table = settings.daily_table
        self._q = '"' + self.table + '"'                       # validated identifier (config)
        self.max_points = settings.max_points
        self.period_tables = {"weekly": settings.weekly_table, "monthly": settings.monthly_table}
        self.period_source = settings.period_source
        self.cache_seconds = settings.ticker_cache_seconds
        self._cache: tuple[float, list[TickerInfo]] | None = None
        self._lock = threading.Lock()

    # -- connections ------------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        if not self.path.is_file():
            raise DataUnavailable(f"the database {self.path} does not exist")
        uri = "file:" + urllib.parse.quote(str(self.path.resolve()).replace("\\", "/")) + "?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=10.0)
            conn.execute("PRAGMA query_only = ON")
            return conn
        except sqlite3.Error as exc:
            raise DataUnavailable(f"cannot open {self.path}: {exc}") from exc

    def _query(self, sql: str, params: tuple = (), table: str | None = None) -> list[tuple]:
        conn = self.connect()
        try:
            return conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc) or "no such column" in str(exc):
                name = table or self.table
                kind = next((k for k, t in self.period_tables.items() if t == name), "daily")
                raise DataUnavailable(f"{name} is not available in {self.path.name}: {exc} - promote data "
                                      f"into the {kind} table first") from exc
            raise DataUnavailable(f"query failed: {exc}") from exc
        finally:
            conn.close()

    # -- queries ------------------------------------------------------------------------
    def check(self) -> dict:
        """For /health: the table is readable and has the expected columns."""
        conn = self.connect()
        try:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({self._q})")}
            if not cols:
                raise DataUnavailable(f"{self.table} does not exist in {self.path.name}")
            missing = [c for c in REQUIRED_COLUMNS if c not in cols]
            if missing:
                raise DataUnavailable(f"{self.table} lacks the column(s) {', '.join(missing)}")
            rows, last = conn.execute(f'SELECT COUNT(*), MAX("TradeDate") FROM {self._q}').fetchone()
        except sqlite3.Error as exc:
            raise DataUnavailable(f"query failed: {exc}") from exc
        finally:
            conn.close()
        return {"table": self.table, "rows": int(rows), "last_trade_date": last}

    def all_tickers(self, refresh: bool = False) -> list[TickerInfo]:
        with self._lock:
            cached = self._cache
            if cached and not refresh and time.monotonic() - cached[0] < self.cache_seconds:
                return cached[1]
        rows = self._query(f'SELECT "Ticker", MAX("Market"), COUNT(*), MIN("TradeDate"), MAX("TradeDate") '
                           f'FROM {self._q} GROUP BY "Ticker" ORDER BY "Ticker"')
        tickers = [TickerInfo(r[0], r[1], int(r[2]), r[3], r[4]) for r in rows]
        with self._lock:
            self._cache = (time.monotonic(), tickers)
        return tickers

    def tickers(self, q: str | None = None, limit: int = 50) -> list[TickerInfo]:
        """Tickers starting with *q* (case-insensitive) - exact match first."""
        q = (q or "").strip().upper()
        everything = self.all_tickers()
        found = [t for t in everything if t.ticker.startswith(q)] if q else everything
        found.sort(key=lambda t: (t.ticker != q, t.ticker))
        return found[:max(1, min(int(limit), 10_000))]

    def default_ticker(self, preferred: str = "") -> str | None:
        """*preferred* if it has data, else the ticker with the most daily rows."""
        everything = self.all_tickers()
        if preferred and any(t.ticker == preferred for t in everything):
            return preferred
        return max(everything, key=lambda t: (t.rows, t.last), default=None).ticker if everything else None

    def daily(self, ticker: str, date_from: str | None = None, date_to: str | None = None,
              limit: int | None = None) -> DailySeries:
        """The daily rows of *ticker* (oldest first) - at most *limit* (default max_points), the most recent."""
        ticker = clean_ticker(ticker)
        date_from, date_to = clean_date(date_from, "from"), clean_date(date_to, "to")
        if date_from and date_to and date_from > date_to:
            raise InvalidRequest("'from' must not be after 'to'")
        limit = max(1, min(int(limit or self.max_points), self.max_points))
        where, params = ['"Ticker" = ?'], [ticker]
        if date_from:
            where.append('"TradeDate" >= ?')
            params.append(date_from)
        if date_to:
            where.append('"TradeDate" <= ?')
            params.append(date_to)
        cond = " AND ".join(where)
        rows = self._query(f'SELECT "TradeDate", "Open", "High", "Low", "Close", "Volume", "Market" FROM {self._q} '
                           f'WHERE {cond} ORDER BY "TradeDate" DESC LIMIT ?', (*params, limit + 1))
        truncated = len(rows) > limit
        rows = rows[:limit]
        total = len(rows)
        if truncated:
            total = int(self._query(f'SELECT COUNT(*) FROM {self._q} WHERE {cond}', tuple(params))[0][0])
        market = rows[0][6] if rows else None
        series = [(r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), int(r[5])) for r in reversed(rows)]
        return DailySeries(ticker, market, series, truncated, total)

    # -- weekly / monthly candles ---------------------------------------------------------
    def _period_range(self, ticker: str, date_from: str | None, date_to: str | None, interval: str):
        ticker = clean_ticker(ticker)
        date_from, date_to = clean_date(date_from, "from"), clean_date(date_to, "to")
        if date_from and date_to and date_from > date_to:
            raise InvalidRequest("'from' must not be after 'to'")
        # a period is selected when it overlaps [from, to]
        return ticker, (period_start(date_from, interval) if date_from else None), date_to

    def _table_source(self, interval: str, ticker: str) -> str | None:
        """The aggregate table to read, or None = aggregate the daily rows (see the module docstring)."""
        if self.period_source == "daily":
            return None
        table = self.period_tables[interval]
        try:
            newest = self._query(f'SELECT MAX("LastTradeDate") FROM "{table}" WHERE "Ticker" = ?', (ticker,),
                                 table)[0][0]
        except DataUnavailable:
            if self.period_source == "tables":
                raise
            return None                                      # no aggregate table (yet): use the daily rows
        if self.period_source == "tables":
            return table
        if newest is None:
            return None
        daily_newest = self._query(f'SELECT MAX("TradeDate") FROM {self._q} WHERE "Ticker" = ?', (ticker,))[0][0]
        return table if not daily_newest or newest >= daily_newest else None   # stale aggregates: recompute

    def periods(self, interval: str, ticker: str, date_from: str | None = None, date_to: str | None = None,
                limit: int | None = None) -> PeriodSeries:
        """Weekly / monthly candles of *ticker* (oldest first) - at most *limit* (default max_points),
        the most recent.  *date_from* / *date_to* select the periods that overlap the range."""
        interval = check_interval(interval)
        if interval == "daily":
            raise InvalidRequest("use daily() for daily rows")
        ticker, start_from, date_to = self._period_range(ticker, date_from, date_to, interval)
        limit = max(1, min(int(limit or self.max_points), self.max_points))
        table = self._table_source(interval, ticker)
        if table is not None:
            col = PERIOD_START_COLUMN[interval]
            where, params = ['"Ticker" = ?'], [ticker]
            if start_from:
                where.append(f'"{col}" >= ?')
                params.append(start_from)
            if date_to:
                where.append(f'"{col}" <= ?')
                params.append(date_to)
            cond = " AND ".join(where)
            found = self._query(f'SELECT "{col}", "Open", "High", "Low", "Close", "Volume", "FirstTradeDate", '
                                f'"LastTradeDate", "TradingDays", "Complete", "Market" FROM "{table}" '
                                f'WHERE {cond} ORDER BY "{col}" DESC LIMIT ?', (*params, limit + 1), table)
            truncated = len(found) > limit
            found = found[:limit]
            total = len(found)
            if truncated:
                total = int(self._query(f'SELECT COUNT(*) FROM "{table}" WHERE {cond}', tuple(params), table)[0][0])
            market = found[0][10] if found else None
            rows = [(r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), int(r[5]), r[6], r[7], int(r[8]),
                     None if r[9] is None else bool(r[9])) for r in reversed(found)]
            return PeriodSeries(ticker, interval, market, rows, truncated, total, table)
        # aggregated on the fly from the daily rows of the whole periods
        where, params = ['"Ticker" = ?'], [ticker]
        if start_from:
            where.append('"TradeDate" >= ?')
            params.append(start_from)
        if date_to:
            where.append('"TradeDate" <= ?')
            params.append(period_end(date_to, interval))
        found = self._query(f'SELECT "TradeDate", "Open", "High", "Low", "Close", "Volume", "Market" FROM {self._q} '
                            f'WHERE {" AND ".join(where)} ORDER BY "TradeDate"', tuple(params))
        market = found[-1][6] if found else None
        rows = aggregate_rows(((r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), int(r[5])) for r in found),
                              interval)
        total = len(rows)
        return PeriodSeries(ticker, interval, market, rows[-limit:], total > limit, total,
                            f"{self.table} (aggregated)")

    def period_counts(self) -> dict:
        """For /health: the rows of the aggregate tables (None = the table is not there)."""
        out = {}
        for interval, table in self.period_tables.items():
            try:
                out[f"{interval}_rows"] = int(self._query(f'SELECT COUNT(*) FROM "{table}"', (), table)[0][0])
            except DataUnavailable:
                out[f"{interval}_rows"] = None
        return out

    # -- the figures of the lists -----------------------------------------------------------
    def quotes(self, tickers) -> tuple[list[dict], list[str]]:
        """The latest close of every ticker and its change against the previous trading day:
        ``([{"ticker", "market", "date", "close", "previous_close", "change", "change_pct"}, ...], [unknown tickers])``
        in the order asked for."""
        wanted = list(dict.fromkeys(clean_ticker(t) for t in tickers))
        if not wanted:
            return [], []
        found: dict[str, list[tuple]] = {}
        for i in range(0, len(wanted), 500):                       # SQLite: at most 999 parameters
            chunk = wanted[i:i + 500]
            marks = ", ".join("?" * len(chunk))
            rows = self._query(
                f'SELECT "Ticker", "Market", "TradeDate", "Close" FROM ('
                f'SELECT "Ticker", "Market", "TradeDate", "Close", ROW_NUMBER() OVER '
                f'(PARTITION BY "Ticker" ORDER BY "TradeDate" DESC) AS rn FROM {self._q} WHERE "Ticker" IN ({marks})) '
                f'WHERE rn <= 2 ORDER BY "Ticker", "TradeDate" DESC', tuple(chunk))
            for r in rows:
                found.setdefault(r[0], []).append(r)
        quotes, unknown = [], []
        for ticker in wanted:
            rows = found.get(ticker)
            if not rows:
                unknown.append(ticker)
                continue
            close = float(rows[0][3])
            previous = float(rows[1][3]) if len(rows) > 1 else None
            change = round(close - previous, 6) if previous is not None else None
            quotes.append({"ticker": ticker, "market": rows[0][1], "date": rows[0][2], "close": close,
                           "previous_close": previous, "change": change,
                           "change_pct": round(change / previous * 100, 4) if previous else None})
        return quotes, unknown
