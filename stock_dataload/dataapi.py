"""
The data behind the public **data API**: daily / weekly / monthly stock data
per ticker and year, read from the active tables (``Stock_Daily``,
``Stock_Weekly``, ``Stock_Monthly`` - see :mod:`promotion`).

Only the business columns are published (no load ids, stage ids or
timestamps), with snake_case names::

    daily    market, ticker, date, open, high, low, close, volume
    weekly   market, ticker, week_start, week_end, iso_year, iso_week, open, high, low, close,
             volume, trading_days, complete
    monthly  market, ticker, month_start, month_end, year, month, open, high, low, close,
             volume, trading_days, complete

"Per year" means the calendar year of the trading day (daily), the ISO year
of the week (weekly) and the calendar year of the month (monthly).

The service does not know about tokens - :class:`DataApiService.query`
returns the rows per ticker and the caller (the portal's ``/api/v1/...``
routes) charges the key for every ticker that returned data.
"""

from __future__ import annotations

import csv
import io
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import date

from .config import Settings
from .db import DatabaseError, StageDatabase, quote_identifier as _q

KINDS = ("daily", "weekly", "monthly")

#: (table column, published name) per kind
PUBLIC_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "daily": (("Market", "market"), ("Ticker", "ticker"), ("TradeDate", "date"), ("Open", "open"),
              ("High", "high"), ("Low", "low"), ("Close", "close"), ("Volume", "volume")),
    "weekly": (("Market", "market"), ("Ticker", "ticker"), ("WeekStart", "week_start"), ("WeekEnd", "week_end"),
               ("ISOYear", "iso_year"), ("ISOWeek", "iso_week"), ("Open", "open"), ("High", "high"), ("Low", "low"),
               ("Close", "close"), ("Volume", "volume"), ("TradingDays", "trading_days"), ("Complete", "complete")),
    "monthly": (("Market", "market"), ("Ticker", "ticker"), ("MonthStart", "month_start"), ("MonthEnd", "month_end"),
                ("Year", "year"), ("Month", "month"), ("Open", "open"), ("High", "high"), ("Low", "low"),
                ("Close", "close"), ("Volume", "volume"), ("TradingDays", "trading_days"), ("Complete", "complete")),
}

#: the column that orders the rows / carries the period
PERIOD_COLUMN = {"daily": "TradeDate", "weekly": "WeekStart", "monthly": "MonthStart"}

_TICKER_RE = re.compile(r"^[A-Za-z0-9.\-^=]{1,15}$")
MIN_YEAR, MAX_YEAR = 1900, 2100


class DataApiError(Exception):
    """Invalid request parameters (HTTP 400 material)."""


@dataclass
class QueryResult:
    kind: str
    year: int
    market: str | None
    #: ticker -> rows (only tickers with data)
    rows: dict[str, list[dict]]
    requested: list[str]

    @property
    def count(self) -> int:
        return sum(len(r) for r in self.rows.values())

    @property
    def tickers_with_data(self) -> list[str]:
        return [t for t in self.requested if t in self.rows]

    @property
    def tickers_without_data(self) -> list[str]:
        return [t for t in self.requested if t not in self.rows]

    def records(self) -> list[dict]:
        out: list[dict] = []
        for t in self.requested:
            out.extend(self.rows.get(t, []))
        return out


def parse_tickers(text: str | list[str] | None, limit: int) -> list[str]:
    """``"aapl, MSFT"`` -> ``["AAPL", "MSFT"]`` (unique, upper case, at most *limit*)."""
    if text is None:
        raise DataApiError("the 'ticker' parameter is required (one or more symbols, comma separated)")
    parts = text if isinstance(text, list) else [text]
    raw: list[str] = []
    for p in parts:
        raw.extend(p.replace(";", ",").split(","))
    out: list[str] = []
    for t in raw:
        t = t.strip().upper()
        if not t:
            continue
        if not _TICKER_RE.match(t):
            raise DataApiError(f"'{t}' is not a valid ticker symbol")
        if t not in out:
            out.append(t)
    if not out:
        raise DataApiError("the 'ticker' parameter is required (one or more symbols, comma separated)")
    if len(out) > limit:
        raise DataApiError(f"at most {limit} tickers per request (got {len(out)})")
    return out


def parse_year(text: str | int | None) -> int:
    if text is None or str(text).strip() == "":
        raise DataApiError("the 'year' parameter is required (e.g. year=2023)")
    try:
        year = int(str(text).strip())
    except ValueError as exc:
        raise DataApiError(f"'{text}' is not a valid year") from exc
    if not MIN_YEAR <= year <= MAX_YEAR:
        raise DataApiError(f"year must be between {MIN_YEAR} and {MAX_YEAR}")
    return year


def parse_kind(text: str | None) -> str:
    kind = (text or "").strip().lower()
    if kind not in KINDS:
        raise DataApiError(f"unknown data set '{text}' - use one of {', '.join(KINDS)}")
    return kind


class DataApiService:
    """Read access to the active tables for the data API."""

    def __init__(self, settings: Settings, db: StageDatabase | None = None):
        self.settings = settings
        self.db = db or StageDatabase(settings)
        self._ready = False
        self._catalog_cache: tuple[float, dict] | None = None
        self._lock = threading.Lock()

    def _table(self, kind: str) -> str:
        return {"daily": self.settings.daily_table, "weekly": self.settings.weekly_table,
                "monthly": self.settings.monthly_table}[kind]

    def _ensure(self, conn: sqlite3.Connection) -> None:
        if not self._ready:
            from .promotion import PromotionService
            PromotionService(self.settings, self.db).ensure_tables(conn)
            self._ready = True

    # -- catalogue (free) --------------------------------------------------------
    def years(self, kind: str, conn: sqlite3.Connection | None = None) -> list[int]:
        """The years with data, newest first."""
        kind = parse_kind(kind)
        if conn is None:
            with self.db.connection() as c:
                return self.years(kind, c)
        self._ensure(conn)
        table, period = _q(self._table(kind)), _q(PERIOD_COLUMN[kind])
        if kind == "weekly":
            sql = f'SELECT DISTINCT "ISOYear" FROM {table} ORDER BY 1 DESC'
        elif kind == "monthly":
            sql = f'SELECT DISTINCT "Year" FROM {table} ORDER BY 1 DESC'
        else:
            sql = f'SELECT DISTINCT substr({period}, 1, 4) FROM {table} ORDER BY 1 DESC'
        return [int(r[0]) for r in conn.execute(sql).fetchall() if r[0] is not None]

    #: the catalogue scans the active tables - it is cached for this long (seconds)
    CATALOG_TTL = 300.0

    def catalog(self, refresh: bool = False) -> dict:
        """What is on offer: per data set the years, row / ticker counts and the markets
        (cached for :attr:`CATALOG_TTL` seconds - the tables only change when data is promoted)."""
        with self._lock:
            cached = self._catalog_cache
            if cached is not None and not refresh and time.monotonic() - cached[0] < self.CATALOG_TTL:
                return dict(cached[1])
        out = self._build_catalog()
        with self._lock:
            self._catalog_cache = (time.monotonic(), out)
        return dict(out)

    def invalidate(self) -> None:
        with self._lock:
            self._catalog_cache = None

    def _build_catalog(self) -> dict:
        out: dict = {"kinds": {}, "markets": [], "tokens_per_ticker_year": self.settings.tokens_per_query,
                     "max_tickers_per_request": self.settings.max_tickers_per_request}
        with self.db.connection() as conn:
            self._ensure(conn)
            markets: set[str] = set()
            for kind in KINDS:
                table, period = _q(self._table(kind)), _q(PERIOD_COLUMN[kind])
                row = conn.execute(f'SELECT COUNT(*), COUNT(DISTINCT "Ticker"), MIN({period}), MAX({period}) '
                                   f'FROM {table}').fetchone()
                out["kinds"][kind] = {"rows": int(row[0]), "tickers": int(row[1]), "first": row[2], "last": row[3],
                                      "years": self.years(kind, conn)}
                markets.update(r[0] for r in conn.execute(f'SELECT DISTINCT "Market" FROM {table}').fetchall()
                               if r[0])
            out["markets"] = sorted(markets)
        return out

    def tickers(self, kind: str = "daily", year: int | None = None, market: str | None = None,
                q: str | None = None, limit: int = 500) -> list[dict]:
        """The tickers that have data (optionally of a year / market / prefix) with their row counts."""
        kind = parse_kind(kind)
        table, period = _q(self._table(kind)), _q(PERIOD_COLUMN[kind])
        where, params = [], []
        if year is not None:
            where.append(self._year_condition(kind))
            params.extend(self._year_params(kind, int(year)))
        if market and market.strip():
            where.append('"Market" = ?')
            params.append(market.strip().upper())
        if q and q.strip():
            where.append('"Ticker" LIKE ?')
            params.append(q.strip().upper() + "%")
        sql = f'SELECT "Ticker", "Market", COUNT(*), MIN({period}), MAX({period}) FROM {table}'
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += ' GROUP BY "Ticker", "Market" ORDER BY "Ticker" LIMIT ?'
        params.append(int(limit))
        with self.db.connection() as conn:
            self._ensure(conn)
            return [{"ticker": r[0], "market": r[1], "rows": int(r[2]), "first": r[3], "last": r[4]}
                    for r in conn.execute(sql, params).fetchall()]

    # -- the data (metered by the caller) -------------------------------------------
    @staticmethod
    def _year_condition(kind: str) -> str:
        if kind == "weekly":
            return '"ISOYear" = ?'
        if kind == "monthly":
            return '"Year" = ?'
        return '"TradeDate" >= ? AND "TradeDate" <= ?'

    @staticmethod
    def _year_params(kind: str, year: int) -> list:
        if kind in ("weekly", "monthly"):
            return [year]
        return [f"{year:04d}-01-01", f"{year:04d}-12-31"]

    def query(self, kind: str, tickers: list[str], year: int, market: str | None = None) -> QueryResult:
        """Rows of *kind* for each ticker in *year* (tickers without data are absent from ``rows``)."""
        kind = parse_kind(kind)
        year = parse_year(year)
        if not tickers:
            raise DataApiError("no ticker given")
        cols = PUBLIC_COLUMNS[kind]
        table, period = _q(self._table(kind)), _q(PERIOD_COLUMN[kind])
        where = ['"Ticker" IN (%s)' % ",".join("?" for _ in tickers), self._year_condition(kind)]
        params: list = list(tickers) + self._year_params(kind, year)
        market = (market or "").strip().upper() or None
        if market:
            where.append('"Market" = ?')
            params.append(market)
        sql = (f'SELECT {", ".join(_q(c) for c, _ in cols)} FROM {table} WHERE {" AND ".join(where)} '
               f'ORDER BY "Ticker", {period}')
        names = [n for _, n in cols]
        rows: dict[str, list[dict]] = {}
        try:
            with self.db.connection() as conn:
                self._ensure(conn)
                for r in conn.execute(sql, params):
                    rec = dict(zip(names, r))
                    if "complete" in rec:
                        rec["complete"] = bool(rec["complete"])
                    rows.setdefault(rec["ticker"], []).append(rec)
        except sqlite3.Error as exc:
            raise DatabaseError(f"query failed: {exc}") from exc
        return QueryResult(kind, year, market, rows, list(tickers))

    @staticmethod
    def to_csv(kind: str, records: list[dict]) -> str:
        names = [n for _, n in PUBLIC_COLUMNS[parse_kind(kind)]]
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=names, lineterminator="\n")
        writer.writeheader()
        for rec in records:
            writer.writerow({k: (int(v) if isinstance(v, bool) else v) for k, v in rec.items()})
        return buf.getvalue()


def current_year() -> int:
    return date.today().year
