"""
The trading calendar: the ``No_Trading_Day`` and ``Market`` tables.

``No_Trading_Day`` lists every day a market is closed - weekends as well as
holidays and ad-hoc closures - one row per market and date::

    Market   TradeDate    Reason                         Source
    NASDAQ   2023-01-01   weekend (Sunday)               generated
    NASDAQ   2023-01-02   New Year's Day (observed)      generated
    NASDAQ   2025-01-09   National Day of Mourning       manual
    NASDAQ   2025-06-05   configured closure             config        ([quality] extra_holidays)

``Market`` lists the markets the calendar is kept for, each with the rule set
used to *generate* its days: ``us`` (weekends + the NYSE / NASDAQ holidays of
:mod:`market_calendar`) or ``weekends`` (weekends only - holidays are added by
hand).  The US markets NASDAQ, NYSE and AMEX are created automatically; more
markets can be added at any time (``no-trading-days add-market``).

The table is filled **per year** by :meth:`TradingCalendar.update_year` -
meant to be run before a year begins (``python -m stock_dataload
no-trading-days update``); generated rows never overwrite manual ones.  As a
safety net a year that is looked up but was never generated is generated on
the spot (and logged as a warning).

Everything that needs to know whether a market was open asks this calendar:

* the loader skips a delivery dated on a no-trading day (file name date);
* the missing-market-day data quality check;
* the promotion's ``market_closed`` rule.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, Iterable, Sequence

from . import market_calendar
from .audit import AuditLogger
from .config import Settings
from .db import StageDatabase, now_iso

log = logging.getLogger(__name__)

COMPONENT = "calendar"

NO_TRADING_DAY_TABLE = "No_Trading_Day"
MARKET_TABLE = "Market"

#: how the no-trading days of a market are generated
RULES_US = "us"
RULES_WEEKENDS = "weekends"
RULE_SETS = {
    RULES_US: "weekends + US exchange holidays (NYSE / NASDAQ)",
    RULES_WEEKENDS: "weekends only - holidays are added by hand",
}

#: markets created automatically
DEFAULT_MARKETS = (
    ("NASDAQ", "NASDAQ Stock Market", RULES_US),
    ("NYSE", "New York Stock Exchange", RULES_US),
    ("AMEX", "NYSE American", RULES_US),
)

SOURCE_GENERATED = "generated"     # from the market's rule set
SOURCE_CONFIG = "config"           # [quality] extra_holidays of config.ini
SOURCE_MANUAL = "manual"           # added on the command line / dashboard

WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

MARKET_DDL = f"""
CREATE TABLE IF NOT EXISTS "{MARKET_TABLE}" (
    "MarketId"  INTEGER PRIMARY KEY AUTOINCREMENT,
    "Market"    TEXT NOT NULL UNIQUE COLLATE NOCASE,   -- NASDAQ, NYSE, ...
    "Name"      TEXT,
    "Rules"     TEXT NOT NULL,                         -- us | weekends
    "Active"    INTEGER NOT NULL DEFAULT 1,
    "CreatedAt" TEXT NOT NULL
)
"""

NO_TRADING_DAY_DDL = f"""
CREATE TABLE IF NOT EXISTS "{NO_TRADING_DAY_TABLE}" (
    "NoTradingDayId" INTEGER PRIMARY KEY AUTOINCREMENT,
    "Market"    TEXT NOT NULL COLLATE NOCASE,
    "TradeDate" TEXT NOT NULL,                         -- YYYY-MM-DD
    "Reason"    TEXT NOT NULL,                         -- weekend (Saturday) | Good Friday | ...
    "Source"    TEXT NOT NULL,                         -- generated | config | manual
    "CreatedAt" TEXT NOT NULL,
    CONSTRAINT "UQ_{NO_TRADING_DAY_TABLE}_Market_TradeDate" UNIQUE ("Market", "TradeDate"),
    CONSTRAINT "CK_{NO_TRADING_DAY_TABLE}_TradeDate" CHECK ("TradeDate" GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')
)
"""

NO_TRADING_DAY_INDEX = (f'CREATE INDEX IF NOT EXISTS "IX_{NO_TRADING_DAY_TABLE}_TradeDate" '
                        f'ON "{NO_TRADING_DAY_TABLE}" ("TradeDate")')

MARKET_COLUMNS = ("MarketId", "Market", "Name", "Rules", "Active", "CreatedAt")
NO_TRADING_DAY_COLUMNS = ("NoTradingDayId", "Market", "TradeDate", "Reason", "Source", "CreatedAt")


class CalendarError(Exception):
    """Invalid market / date / rule set."""


@dataclass
class MarketRecord:
    market_id: int
    market: str
    name: str | None
    rules: str
    active: int
    created_at: str

    @property
    def is_active(self) -> bool:
        return bool(self.active)


@dataclass
class NoTradingDay:
    no_trading_day_id: int
    market: str
    trade_date: str
    reason: str
    source: str
    created_at: str

    @property
    def weekday(self) -> str:
        return WEEKDAY_NAMES[date.fromisoformat(self.trade_date).weekday()]

    @property
    def is_weekend(self) -> bool:
        return date.fromisoformat(self.trade_date).weekday() >= 5


@dataclass
class CalendarUpdateResult:
    """Outcome of generating one market's no-trading days for one year."""
    market: str
    year: int
    rules: str
    generated: int = 0        # rows the rule set produced
    added: int = 0            # rows that were new
    existing: int = 0         # rows that were already there (generated earlier or by hand)
    holidays: int = 0         # non-weekend days among the generated rows

    def __str__(self) -> str:
        return (f"{self.market} {self.year}: {self.generated} no-trading days ({self.holidays} holidays) - "
                f"{self.added} added, {self.existing} already present")


def render_ddl() -> str:
    return "\n".join(s.strip() + ";" for s in (MARKET_DDL, NO_TRADING_DAY_DDL, NO_TRADING_DAY_INDEX)) + "\n"


def generate_no_trading_days(rules: str, year: int, extra_holidays: Iterable[date] = ()) -> dict[date, tuple[str, str]]:
    """``{date: (reason, source)}`` of every day a market with *rules* is closed in *year*.

    Weekends and the rule set's holidays (source ``generated``) plus the
    configured ad-hoc closures that fall into the year (source ``config``).
    """
    if rules not in RULE_SETS:
        raise CalendarError(f"unknown rule set '{rules}' (use {', '.join(RULE_SETS)})")
    days: dict[date, tuple[str, str]] = {}
    day = date(year, 1, 1)
    while day.year == year:
        if day.weekday() >= 5:
            days[day] = (f"weekend ({WEEKDAY_NAMES[day.weekday()]})", SOURCE_GENERATED)
        day += timedelta(days=1)
    if rules == RULES_US:
        for holiday, name in market_calendar.us_market_holiday_names(year).items():
            days[holiday] = (name, SOURCE_GENERATED)
    for extra in extra_holidays:
        if extra.year == year and extra not in days:
            days[extra] = ("configured closure ([quality] extra_holidays)", SOURCE_CONFIG)
    return days


def parse_day(value: str | date) -> date:
    try:
        return market_calendar.parse_holiday(value)
    except market_calendar.CalendarError as exc:
        raise CalendarError(str(exc)) from exc


class TradingCalendar:
    """Keeps and answers the trading calendar (``No_Trading_Day`` / ``Market``)."""

    def __init__(self, settings: Settings, db: StageDatabase | None = None, audit: AuditLogger | None = None):
        self.settings = settings
        self.db = db or StageDatabase(settings)
        self.audit = audit or AuditLogger(settings)
        self._tables_ready = False
        # (market, year) -> {date: reason}; filled on demand, cleared when the tables change
        self._cache: dict[tuple[str, int], dict[date, str]] = {}
        self._warned: set[str] = set()
        self._pending_audit: list[tuple[str, int, CalendarUpdateResult]] = []

    # -- connections / DDL ---------------------------------------------------
    def connection(self):
        return self.db.connection()

    def ensure_tables(self, conn: sqlite3.Connection | None = None) -> None:
        """Create the tables and the default (US) markets if missing."""
        if conn is None:
            with self.connection() as c:
                self.ensure_tables(c)
            return
        conn.execute(MARKET_DDL)
        conn.execute(NO_TRADING_DAY_DDL)
        conn.execute(NO_TRADING_DAY_INDEX)
        now = now_iso()
        conn.executemany(f'INSERT OR IGNORE INTO "{MARKET_TABLE}" ("Market", "Name", "Rules", "Active", "CreatedAt") '
                         'VALUES (?, ?, ?, 1, ?)', [(m, n, r, now) for m, n, r in DEFAULT_MARKETS])
        self._tables_ready = True

    @property
    def extra_holidays(self) -> frozenset[date]:
        return frozenset(market_calendar.parse_holiday(h) for h in self.settings.extra_market_holidays)

    # -- markets ---------------------------------------------------------------
    def markets(self, active_only: bool = False, conn: sqlite3.Connection | None = None) -> list[MarketRecord]:
        if conn is None:
            with self.connection() as c:
                return self.markets(active_only, c)
        self.ensure_tables(conn)
        cols = ", ".join(f'"{c}"' for c in MARKET_COLUMNS)
        sql = f'SELECT {cols} FROM "{MARKET_TABLE}"'
        if active_only:
            sql += ' WHERE "Active" = 1'
        sql += ' ORDER BY "Market"'
        return [MarketRecord(*r) for r in conn.execute(sql).fetchall()]

    def get_market(self, market: str, conn: sqlite3.Connection | None = None) -> MarketRecord | None:
        if conn is None:
            with self.connection() as c:
                return self.get_market(market, c)
        self.ensure_tables(conn)
        cols = ", ".join(f'"{c}"' for c in MARKET_COLUMNS)
        row = conn.execute(f'SELECT {cols} FROM "{MARKET_TABLE}" WHERE "Market" = ?',
                           ((market or "").strip(),)).fetchone()
        return MarketRecord(*row) if row else None

    def add_market(self, market: str, name: str = "", rules: str = RULES_US) -> MarketRecord:
        """Add a market to keep a calendar for (its days are generated by ``update_year``)."""
        market = (market or "").strip().upper()
        if not market or not market.replace("_", "").replace("-", "").isalnum():
            raise CalendarError("the market code must be letters / digits, e.g. LSE")
        if rules not in RULE_SETS:
            raise CalendarError(f"unknown rule set '{rules}' (use {', '.join(RULE_SETS)})")
        with self.connection() as conn:
            self.ensure_tables(conn)
            if self.get_market(market, conn) is not None:
                raise CalendarError(f"market {market} already exists")
            conn.execute(f'INSERT INTO "{MARKET_TABLE}" ("Market", "Name", "Rules", "Active", "CreatedAt") '
                         'VALUES (?, ?, ?, 1, ?)', (market, (name or "").strip() or None, rules, now_iso()))
            record = self.get_market(market, conn)
        self._cache.clear()
        self.audit.log(COMPONENT, "market_added", f"market {market} added ({rules} rules)",
                       details={"market": market, "name": name, "rules": rules})
        return record

    def set_market_active(self, market: str, active: bool) -> MarketRecord:
        with self.connection() as conn:
            record = self.get_market(market, conn)
            if record is None:
                raise CalendarError(f"unknown market '{market}'")
            conn.execute(f'UPDATE "{MARKET_TABLE}" SET "Active" = ? WHERE "MarketId" = ?',
                         (1 if active else 0, record.market_id))
            record = self.get_market(market, conn)
        self.audit.log(COMPONENT, "market_updated", f"market {record.market} {'activated' if active else 'deactivated'}",
                       details={"market": record.market, "active": active})
        return record

    # -- yearly update -----------------------------------------------------------
    def update_year(self, year: int, markets: Sequence[str] | None = None) -> list[CalendarUpdateResult]:
        """Generate the no-trading days of *year* for the given (default: all active) markets.

        Meant to be run before a year begins.  Existing rows - generated earlier
        or added by hand - are kept; only missing days are added.
        """
        year = int(year)
        if not 1900 <= year <= 2200:
            raise CalendarError(f"year {year} is out of range")
        records = self.markets(active_only=markets is None)
        if markets:
            wanted = {m.strip().upper() for m in markets}
            known = {r.market.upper() for r in records}
            missing = sorted(wanted - known)
            if missing:
                raise CalendarError(f"unknown market(s): {', '.join(missing)} - add them with 'add-market' first")
            records = [r for r in records if r.market.upper() in wanted]
        results = []
        with self.connection() as conn:
            self.ensure_tables(conn)
            for record in records:
                results.append(self._generate_into(conn, record, year))
        self._cache.clear()
        for r in results:
            log.info("no-trading days updated: %s", r)
            self.audit.log(COMPONENT, "calendar_updated", str(r),
                           details={"market": r.market, "year": r.year, "added": r.added, "existing": r.existing,
                                    "generated": r.generated})
        return results

    def update_years(self, years: Iterable[int], markets: Sequence[str] | None = None) -> list[CalendarUpdateResult]:
        results = []
        for year in years:
            results.extend(self.update_year(year, markets))
        return results

    def _generate_into(self, conn: sqlite3.Connection, record: MarketRecord, year: int) -> CalendarUpdateResult:
        days = generate_no_trading_days(record.rules, year, self.extra_holidays)
        result = CalendarUpdateResult(record.market, year, record.rules, generated=len(days),
                                      holidays=sum(1 for d in days if d.weekday() < 5))
        now = now_iso()
        before = self._count(conn, record.market, year)
        conn.executemany(
            f'INSERT OR IGNORE INTO "{NO_TRADING_DAY_TABLE}" ("Market", "TradeDate", "Reason", "Source", "CreatedAt") '
            'VALUES (?, ?, ?, ?, ?)',
            [(record.market, d.isoformat(), reason, source, now) for d, (reason, source) in sorted(days.items())])
        result.added = self._count(conn, record.market, year) - before
        result.existing = result.generated - result.added
        return result

    def _count(self, conn: sqlite3.Connection, market: str, year: int) -> int:
        return int(conn.execute(
            f'SELECT COUNT(*) FROM "{NO_TRADING_DAY_TABLE}" WHERE "Market" = ? AND "TradeDate" BETWEEN ? AND ?',
            (market, f"{year:04d}-01-01", f"{year:04d}-12-31")).fetchone()[0])

    def years_available(self, market: str) -> list[int]:
        """Years for which the market's calendar has been generated."""
        with self.connection() as conn:
            self.ensure_tables(conn)
            rows = conn.execute(f'SELECT DISTINCT substr("TradeDate", 1, 4) FROM "{NO_TRADING_DAY_TABLE}" '
                                f'WHERE "Market" = ? ORDER BY 1', ((market or "").strip(),)).fetchall()
        return [int(r[0]) for r in rows]

    # -- single days -----------------------------------------------------------
    def add_day(self, market: str, day: str | date, reason: str = "") -> NoTradingDay:
        """Add an ad-hoc closure (a day of mourning, a weather closure, ...)."""
        day = parse_day(day)
        with self.connection() as conn:
            record = self.get_market(market, conn)
            if record is None:
                raise CalendarError(f"unknown market '{market}' - add it with 'add-market' first")
            cur = conn.execute(
                f'INSERT OR IGNORE INTO "{NO_TRADING_DAY_TABLE}" ("Market", "TradeDate", "Reason", "Source", "CreatedAt") '
                'VALUES (?, ?, ?, ?, ?)',
                (record.market, day.isoformat(), (reason or "").strip() or "closure", SOURCE_MANUAL, now_iso()))
            if cur.rowcount == 0:
                raise CalendarError(f"{record.market} is already closed on {day.isoformat()}")
            row = self._get_day(conn, record.market, day)
        self._cache.clear()
        self.audit.log(COMPONENT, "no_trading_day_added", f"{record.market} closed on {day.isoformat()}: {row.reason}",
                       level="WARNING", details={"market": record.market, "date": day.isoformat(), "reason": row.reason})
        return row

    def remove_day(self, market: str, day: str | date) -> NoTradingDay:
        """Remove a closure (the market trades on that day after all)."""
        day = parse_day(day)
        with self.connection() as conn:
            self.ensure_tables(conn)
            row = self._get_day(conn, market, day)
            if row is None:
                raise CalendarError(f"{market} is not marked closed on {day.isoformat()}")
            conn.execute(f'DELETE FROM "{NO_TRADING_DAY_TABLE}" WHERE "NoTradingDayId" = ?', (row.no_trading_day_id,))
        self._cache.clear()
        self.audit.log(COMPONENT, "no_trading_day_removed", f"{row.market} trades on {day.isoformat()} ({row.reason} removed)",
                       level="WARNING", details={"market": row.market, "date": day.isoformat()})
        return row

    def _get_day(self, conn: sqlite3.Connection, market: str, day: date) -> NoTradingDay | None:
        cols = ", ".join(f'"{c}"' for c in NO_TRADING_DAY_COLUMNS)
        row = conn.execute(f'SELECT {cols} FROM "{NO_TRADING_DAY_TABLE}" WHERE "Market" = ? AND "TradeDate" = ?',
                           ((market or "").strip(), day.isoformat())).fetchone()
        return NoTradingDay(*row) if row else None

    def days(self, market: str, year: int | None = None, holidays_only: bool = False,
             date_from: str | date | None = None, date_to: str | date | None = None) -> list[NoTradingDay]:
        """The no-trading days of a market (a year, a range, optionally without weekends)."""
        cols = ", ".join(f'"{c}"' for c in NO_TRADING_DAY_COLUMNS)
        where, params = ['"Market" = ?'], [(market or "").strip()]
        if year is not None:
            where.append('"TradeDate" BETWEEN ? AND ?')
            params += [f"{int(year):04d}-01-01", f"{int(year):04d}-12-31"]
        if date_from:
            where.append('"TradeDate" >= ?')
            params.append(parse_day(date_from).isoformat())
        if date_to:
            where.append('"TradeDate" <= ?')
            params.append(parse_day(date_to).isoformat())
        with self.connection() as conn:
            self.ensure_tables(conn)
            rows = [NoTradingDay(*r) for r in conn.execute(
                f'SELECT {cols} FROM "{NO_TRADING_DAY_TABLE}" WHERE {" AND ".join(where)} ORDER BY "TradeDate"',
                params).fetchall()]
        if holidays_only:
            rows = [r for r in rows if not r.is_weekend]
        return rows

    def count_days(self, market: str | None = None) -> int:
        with self.connection() as conn:
            self.ensure_tables(conn)
            if market:
                return int(conn.execute(f'SELECT COUNT(*) FROM "{NO_TRADING_DAY_TABLE}" WHERE "Market" = ?',
                                        (market,)).fetchone()[0])
            return int(conn.execute(f'SELECT COUNT(*) FROM "{NO_TRADING_DAY_TABLE}"').fetchone()[0])

    # -- look-ups --------------------------------------------------------------
    def closed_days(self, market: str | None, year: int, conn: sqlite3.Connection | None = None) -> dict[date, str]:
        """``{date: reason}`` of the market's closures in *year* (cached).

        A known market whose year was never generated gets it generated now
        (logged as a warning - run the yearly update before the year begins).
        An unknown / empty market falls back to the US rules without touching
        the table (warned once).  Pass *conn* when called inside a transaction
        of the same database (e.g. by a promotion procedure).
        """
        key = ((market or "").strip().upper(), int(year))
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        name = key[0]
        if conn is not None:
            days = self._lookup(name, year, conn)
        else:
            with self.connection() as c:
                days = self._lookup(name, year, c)
        self._cache[key] = days
        return days

    def _lookup(self, name: str, year: int, conn: sqlite3.Connection) -> dict[date, str]:
        record = self.get_market(name, conn) if name else None
        if record is None:
            if name not in self._warned:
                self._warned.add(name)
                what = f"market '{name}'" if name else "rows without a market"
                log.warning("%s has no trading calendar - using the US rules; add the market with "
                            "'no-trading-days add-market'", what)
            days = {d: reason for d, (reason, _) in
                    generate_no_trading_days(RULES_US, year, self.extra_holidays).items()}
        else:
            if self._count(conn, record.market, year) == 0:
                generated = self._generate_into(conn, record, year)
                log.warning("the %s no-trading days of %d had not been generated yet - generated now (%d days); "
                            "run 'no-trading-days update --year %d' before a year begins", record.market, year,
                            generated.generated, year)
                self._pending_audit.append((record.market, year, generated))
            rows = conn.execute(
                f'SELECT "TradeDate", "Reason" FROM "{NO_TRADING_DAY_TABLE}" WHERE "Market" = ? '
                f'AND "TradeDate" BETWEEN ? AND ?',
                (record.market, f"{year:04d}-01-01", f"{year:04d}-12-31")).fetchall()
            days = {date.fromisoformat(d): reason for d, reason in rows}
            for extra in self.extra_holidays:       # config.ini closures apply even if added after generation
                if extra.year == year:
                    days.setdefault(extra, "configured closure ([quality] extra_holidays)")
        return days

    def flush_audit(self) -> None:
        """Write the App_Log entries of on-demand generations (kept back while a
        transaction of the caller holds the database lock)."""
        pending, self._pending_audit = self._pending_audit, []
        for market, year, generated in pending:
            self.audit.log(COMPONENT, "calendar_generated_on_demand", str(generated), level="WARNING",
                           details={"market": market, "year": year, "generated": generated.generated})

    def closure_reason(self, market: str | None, day: date, conn: sqlite3.Connection | None = None) -> str | None:
        """Why the market is closed on *day* - or None if it trades."""
        reason = self.closed_days(market, day.year, conn).get(day)
        if conn is None:
            self.flush_audit()
        return reason

    def is_open(self, market: str | None, day: date, conn: sqlite3.Connection | None = None) -> bool:
        return self.closure_reason(market, day, conn) is None

    def opener(self, market: str | None) -> Callable[[date], bool]:
        """``is_open`` bound to one market (for the data quality context)."""
        return lambda day: self.is_open(market, day)

    def invalidate(self) -> None:
        self._cache.clear()


def default_update_years(today: date | None = None) -> list[int]:
    """The years the yearly update covers by default: this year and the next."""
    today = today or date.today()
    return [today.year, today.year + 1]
