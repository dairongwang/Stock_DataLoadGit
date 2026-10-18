"""
Promotion of staged stock data into the ACTIVE tables.

::

    Stock_Stage  (text, as delivered)
        │  promote_daily   - validate, type, de-duplicate, upsert
        ▼
    Stock_Daily  (one row per stock and trading day; UNIQUE (Ticker, TradeDate))
        │  promote_weekly  /  promote_monthly   - aggregate
        ▼
    Stock_Weekly  (one row per stock and ISO week; UNIQUE (Ticker, WeekStart))
    Stock_Monthly (one row per stock and month;    UNIQUE (Ticker, MonthStart))

SQLite has no stored procedures.  Each promotion is therefore implemented as
a *procedure* of this module: a fixed, set-based SQL script that
:class:`PromotionService` runs inside ONE transaction (``BEGIN IMMEDIATE`` …
``COMMIT``) - validation, de-duplication, upsert and the row counts all
happen in SQL, Python only supplies the market calendar and parameters.
Every run is recorded in ``Promotion_History``; rejected staging rows are
recorded in ``Data_Error`` (rule, column, value, message) so they can be
fixed on the data correction page and promoted again.  The SQL of the three
procedures is exported to ``sql/promote_*_sqlite.sql`` for reference
(:func:`render_procedure`).

Validation rules of ``promote_daily`` (the first failing rule is reported)::

    ticker_required   ticker is not empty
    date_is_valid     trade date is a real YYYYMMDD (or YYYY-MM-DD) calendar date
    numeric_columns   open, high, low, close and vol are present and numeric
    positive_price    open, high, low and close are > 0
    volume_invalid    vol is a whole number >= 0
    high_above_low    high >= low
    price_range       low <= open <= high and low <= close <= high       ([promotion] check_price_range)
    market_closed     the market was open on the trade date (No_Trading_Day table)  ([promotion] reject_market_closed_days)
    before_first_trade_date / after_last_trade_date
                      the trade date lies inside the stock's trading window (Stock_Symbol table)  ([promotion] check_trading_window)

Validation of ``promote_weekly`` / ``promote_monthly``: every trading day of
the period - a day the market was open (``No_Trading_Day``) inside the
stock's trading window (``Stock_Symbol``) - must have a daily row.  A week /
month with a day missing is still written (from the days there are) but
marked ``Complete = 0`` with ``MissingDays`` / ``MissingDates``, and recorded
in ``Data_Error`` (``incomplete_week`` / ``incomplete_month``); the finding is
resolved automatically once the gap is filled and the period promoted again.

Rows that pass are promoted; when the same stock/day is staged more than
once (two files, a manual row) the most recently loaded row wins.  Promoting
again is safe: existing active rows are updated only when something changed
(``UpdatedAt``), so the weekly / monthly procedures can work incrementally on
the days changed since their last run (``--full`` recomputes everything and
removes aggregates whose daily rows are gone).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Mapping, Sequence

from .audit import AuditLogger
from .config import Settings
from .correction import CorrectionService
from .db import DatabaseError, StageDatabase, now_iso, quote_identifier
from .quality import DATA_ERROR_TABLE, DataErrorRepository
from .symbols import STOCK_SYMBOL_TABLE, SymbolRegistry
from .trading_calendar import TradingCalendar

log = logging.getLogger(__name__)

COMPONENT = "promotion"

PROMOTION_HISTORY_TABLE = "Promotion_History"

PROC_DAILY = "promote_daily"
PROC_WEEKLY = "promote_weekly"
PROC_MONTHLY = "promote_monthly"
PROCEDURES = (PROC_DAILY, PROC_WEEKLY, PROC_MONTHLY)

MODE_FULL = "full"
MODE_INCREMENTAL = "incremental"

STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

#: rule names of the daily validation (also used in Data_Error.Rule)
RULE_TICKER = "ticker_required"
RULE_DATE = "date_is_valid"
RULE_NUMERIC = "numeric_columns"
RULE_POSITIVE = "positive_price"
RULE_VOLUME = "volume_invalid"
RULE_HIGH_LOW = "high_above_low"
RULE_RANGE = "price_range"
RULE_CLOSED = "market_closed"
RULE_BEFORE_FIRST = "before_first_trade_date"
RULE_AFTER_LAST = "after_last_trade_date"
RULE_INCOMPLETE_WEEK = "incomplete_week"
RULE_INCOMPLETE_MONTH = "incomplete_month"

#: names of the temporary tables the procedures work with
_T_CANDIDATES = '"_promote_candidates"'
_T_CLOSED = '"_promote_closed_days"'
_T_SYMBOLS = '"_promote_symbols"'
_T_OPEN_DAYS = '"_promote_open_days"'
_T_GAPS = '"_promote_gaps"'
_T_ROWS = '"_promote_rows"'
_T_PERIODS = '"_promote_periods"'
_T_AGG = '"_promote_agg"'

_ISO_DATE_GLOB = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]"
_YMD_GLOB = "[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]"


class PromotionError(Exception):
    """A promotion procedure failed (the transaction was rolled back)."""

    def __init__(self, message: str, result: "PromotionResult | None" = None):
        super().__init__(message)
        self.result = result


# ---------------------------------------------------------------------------
# DDL of the active tables
# ---------------------------------------------------------------------------
DAILY_DDL = """
CREATE TABLE IF NOT EXISTS {daily} (
    "DailyId"    INTEGER PRIMARY KEY AUTOINCREMENT,
    "Market"     TEXT,
    "Ticker"     TEXT NOT NULL,
    "TradeDate"  TEXT NOT NULL,                 -- YYYY-MM-DD
    "Open"       REAL NOT NULL,
    "High"       REAL NOT NULL,
    "Low"        REAL NOT NULL,
    "Close"      REAL NOT NULL,
    "Volume"     INTEGER NOT NULL,              -- shares traded on the day
    "SourceFile" TEXT,                          -- staging row the values came from
    "LoadId"     TEXT,
    "StageId"    INTEGER,
    "PromotedAt" TEXT NOT NULL,                 -- first promotion
    "UpdatedAt"  TEXT NOT NULL,                 -- last change of the row
    CONSTRAINT "UQ_{daily_name}_Ticker_TradeDate" UNIQUE ("Ticker", "TradeDate"),
    CONSTRAINT "CK_{daily_name}_TradeDate" CHECK ("TradeDate" GLOB '%(iso)s'),
    CONSTRAINT "CK_{daily_name}_Prices" CHECK ("Open" > 0 AND "High" > 0 AND "Low" > 0 AND "Close" > 0),
    CONSTRAINT "CK_{daily_name}_HighLow" CHECK ("High" >= "Low"),
    CONSTRAINT "CK_{daily_name}_Volume" CHECK ("Volume" >= 0)
)
""" % {"iso": _ISO_DATE_GLOB}

DAILY_INDEXES = (
    'CREATE INDEX IF NOT EXISTS "IX_{daily_name}_TradeDate" ON {daily} ("TradeDate")',
    'CREATE INDEX IF NOT EXISTS "IX_{daily_name}_UpdatedAt" ON {daily} ("UpdatedAt")',
)

WEEKLY_DDL = """
CREATE TABLE IF NOT EXISTS {weekly} (
    "WeeklyId"       INTEGER PRIMARY KEY AUTOINCREMENT,
    "Market"         TEXT,
    "Ticker"         TEXT NOT NULL,
    "WeekStart"      TEXT NOT NULL,             -- Monday of the ISO 8601 week, YYYY-MM-DD
    "WeekEnd"        TEXT NOT NULL,             -- Sunday
    "ISOYear"        INTEGER NOT NULL,
    "ISOWeek"        INTEGER NOT NULL,          -- 1..53
    "Open"           REAL NOT NULL,             -- open of the first trading day of the week
    "High"           REAL NOT NULL,             -- highest high
    "Low"            REAL NOT NULL,             -- lowest low
    "Close"          REAL NOT NULL,             -- close of the last trading day of the week
    "Volume"         INTEGER NOT NULL,          -- shares traded during the week
    "TradingDays"    INTEGER NOT NULL,          -- daily rows aggregated
    "FirstTradeDate" TEXT NOT NULL,
    "LastTradeDate"  TEXT NOT NULL,
    "ExpectedDays"   INTEGER NOT NULL DEFAULT 0, -- trading days of the week (market open, inside the stock's window)
    "MissingDays"    INTEGER NOT NULL DEFAULT 0, -- expected days without a daily row
    "MissingDates"   TEXT,                      -- the missing days, comma separated
    "Complete"       INTEGER NOT NULL DEFAULT 1, -- 0 = a trading day is missing (data error)
    "PromotedAt"     TEXT NOT NULL,
    "UpdatedAt"      TEXT NOT NULL,
    CONSTRAINT "UQ_{weekly_name}_Ticker_WeekStart" UNIQUE ("Ticker", "WeekStart"),
    CONSTRAINT "CK_{weekly_name}_HighLow" CHECK ("High" >= "Low"),
    CONSTRAINT "CK_{weekly_name}_Volume" CHECK ("Volume" >= 0),
    CONSTRAINT "CK_{weekly_name}_Days" CHECK ("TradingDays" BETWEEN 1 AND 7)
)
"""

WEEKLY_INDEXES = (
    'CREATE INDEX IF NOT EXISTS "IX_{weekly_name}_WeekStart" ON {weekly} ("WeekStart")',
)

MONTHLY_DDL = """
CREATE TABLE IF NOT EXISTS {monthly} (
    "MonthlyId"      INTEGER PRIMARY KEY AUTOINCREMENT,
    "Market"         TEXT,
    "Ticker"         TEXT NOT NULL,
    "MonthStart"     TEXT NOT NULL,             -- first day of the month, YYYY-MM-DD
    "MonthEnd"       TEXT NOT NULL,             -- last day of the month
    "Year"           INTEGER NOT NULL,
    "Month"          INTEGER NOT NULL,          -- 1..12
    "Open"           REAL NOT NULL,             -- open of the first trading day of the month
    "High"           REAL NOT NULL,
    "Low"            REAL NOT NULL,
    "Close"          REAL NOT NULL,             -- close of the last trading day of the month
    "Volume"         INTEGER NOT NULL,          -- shares traded during the month
    "TradingDays"    INTEGER NOT NULL,
    "FirstTradeDate" TEXT NOT NULL,
    "LastTradeDate"  TEXT NOT NULL,
    "ExpectedDays"   INTEGER NOT NULL DEFAULT 0, -- trading days of the month (market open, inside the stock's window)
    "MissingDays"    INTEGER NOT NULL DEFAULT 0, -- expected days without a daily row
    "MissingDates"   TEXT,                      -- the missing days, comma separated
    "Complete"       INTEGER NOT NULL DEFAULT 1, -- 0 = a trading day is missing (data error)
    "PromotedAt"     TEXT NOT NULL,
    "UpdatedAt"      TEXT NOT NULL,
    CONSTRAINT "UQ_{monthly_name}_Ticker_MonthStart" UNIQUE ("Ticker", "MonthStart"),
    CONSTRAINT "CK_{monthly_name}_HighLow" CHECK ("High" >= "Low"),
    CONSTRAINT "CK_{monthly_name}_Volume" CHECK ("Volume" >= 0),
    CONSTRAINT "CK_{monthly_name}_Days" CHECK ("TradingDays" BETWEEN 1 AND 31)
)
"""

MONTHLY_INDEXES = (
    'CREATE INDEX IF NOT EXISTS "IX_{monthly_name}_MonthStart" ON {monthly} ("MonthStart")',
)

HISTORY_DDL = f"""
CREATE TABLE IF NOT EXISTS "{PROMOTION_HISTORY_TABLE}" (
    "PromotionId"   INTEGER PRIMARY KEY AUTOINCREMENT,
    "Procedure"     TEXT NOT NULL,              -- promote_daily | promote_weekly | promote_monthly
    "TargetTable"   TEXT NOT NULL,
    "Mode"          TEXT NOT NULL,              -- full | incremental
    "Filtered"      INTEGER NOT NULL DEFAULT 0, -- 1 = run limited to a file / ticker / date range
    "Parameters"    TEXT,                       -- JSON
    "StartedAt"     TEXT NOT NULL,
    "FinishedAt"    TEXT NOT NULL,
    "Status"        TEXT NOT NULL,              -- completed | failed
    "RowsRead"      INTEGER NOT NULL DEFAULT 0,
    "RowsInserted"  INTEGER NOT NULL DEFAULT 0,
    "RowsUpdated"   INTEGER NOT NULL DEFAULT 0,
    "RowsUnchanged" INTEGER NOT NULL DEFAULT 0,
    "RowsRejected"  INTEGER NOT NULL DEFAULT 0,
    "RowsSkipped"   INTEGER NOT NULL DEFAULT 0, -- duplicates (an older copy of the same stock/day)
    "RowsDeleted"   INTEGER NOT NULL DEFAULT 0, -- aggregates without daily rows (full mode)
    "RowsIncomplete" INTEGER NOT NULL DEFAULT 0, -- weekly / monthly: aggregates with a trading day missing
    "Message"       TEXT
)
"""

#: columns added after the first release - added to existing databases on the fly
#: ((table key, column, DDL), ...); table key = daily | weekly | monthly | history
ACTIVE_MIGRATIONS = (
    ("weekly", "ExpectedDays", '"ExpectedDays" INTEGER NOT NULL DEFAULT 0'),
    ("weekly", "MissingDays", '"MissingDays" INTEGER NOT NULL DEFAULT 0'),
    ("weekly", "MissingDates", '"MissingDates" TEXT'),
    ("weekly", "Complete", '"Complete" INTEGER NOT NULL DEFAULT 1'),
    ("monthly", "ExpectedDays", '"ExpectedDays" INTEGER NOT NULL DEFAULT 0'),
    ("monthly", "MissingDays", '"MissingDays" INTEGER NOT NULL DEFAULT 0'),
    ("monthly", "MissingDates", '"MissingDates" TEXT'),
    ("monthly", "Complete", '"Complete" INTEGER NOT NULL DEFAULT 1'),
    ("history", "RowsIncomplete", '"RowsIncomplete" INTEGER NOT NULL DEFAULT 0'),
)

HISTORY_INDEX = (f'CREATE INDEX IF NOT EXISTS "IX_{PROMOTION_HISTORY_TABLE}_Procedure" '
                 f'ON "{PROMOTION_HISTORY_TABLE}" ("Procedure", "StartedAt")')

DAILY_COLUMNS = ("DailyId", "Market", "Ticker", "TradeDate", "Open", "High", "Low", "Close", "Volume",
                 "SourceFile", "LoadId", "StageId", "PromotedAt", "UpdatedAt")
WEEKLY_COLUMNS = ("WeeklyId", "Market", "Ticker", "WeekStart", "WeekEnd", "ISOYear", "ISOWeek", "Open", "High",
                  "Low", "Close", "Volume", "TradingDays", "ExpectedDays", "MissingDays", "MissingDates", "Complete",
                  "FirstTradeDate", "LastTradeDate", "PromotedAt", "UpdatedAt")
MONTHLY_COLUMNS = ("MonthlyId", "Market", "Ticker", "MonthStart", "MonthEnd", "Year", "Month", "Open", "High",
                   "Low", "Close", "Volume", "TradingDays", "ExpectedDays", "MissingDays", "MissingDates", "Complete",
                   "FirstTradeDate", "LastTradeDate", "PromotedAt", "UpdatedAt")
HISTORY_COLUMNS = ("PromotionId", "Procedure", "TargetTable", "Mode", "Filtered", "Parameters", "StartedAt",
                   "FinishedAt", "Status", "RowsRead", "RowsInserted", "RowsUpdated", "RowsUnchanged",
                   "RowsRejected", "RowsSkipped", "RowsDeleted", "RowsIncomplete", "Message")


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------
@dataclass
class PromotionResult:
    """Outcome of one procedure run (one row of Promotion_History)."""
    procedure: str
    target_table: str
    mode: str = MODE_FULL
    status: str = "pending"                  # completed | failed
    parameters: dict = field(default_factory=dict)
    rows_read: int = 0                       # source rows considered
    rows_inserted: int = 0
    rows_updated: int = 0
    rows_unchanged: int = 0
    rows_rejected: int = 0                   # daily: rows that failed validation
    rows_skipped: int = 0                    # daily: older duplicates of a stock/day
    rows_deleted: int = 0                    # weekly/monthly full mode: orphaned aggregates removed
    rows_incomplete: int = 0                 # weekly/monthly: aggregates with a trading day missing
    reject_reasons: dict[str, int] = field(default_factory=dict)
    message: str = ""
    step: str = ""                           # the step that failed
    started: datetime = field(default_factory=datetime.now)
    finished: datetime | None = None
    history_id: int | None = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_COMPLETED

    @property
    def filtered(self) -> bool:
        return any(v not in (None, "", False) for k, v in self.parameters.items() if k != "full")

    @property
    def duration(self) -> float:
        return ((self.finished or datetime.now()) - self.started).total_seconds()

    def as_dict(self) -> dict:
        return {"procedure": self.procedure, "target_table": self.target_table, "mode": self.mode,
                "status": self.status, "parameters": self.parameters, "rows_read": self.rows_read,
                "rows_inserted": self.rows_inserted, "rows_updated": self.rows_updated,
                "rows_unchanged": self.rows_unchanged, "rows_rejected": self.rows_rejected,
                "rows_skipped": self.rows_skipped, "rows_deleted": self.rows_deleted,
                "rows_incomplete": self.rows_incomplete,
                "reject_reasons": self.reject_reasons, "message": self.message, "step": self.step,
                "seconds": round(self.duration, 1), "history_id": self.history_id}

    def __str__(self) -> str:
        text = f"{self.procedure}: {self.status} ({self.mode})"
        if self.ok:
            text += (f" - {self.rows_read:,} rows read: {self.rows_inserted:,} inserted, "
                     f"{self.rows_updated:,} updated, {self.rows_unchanged:,} unchanged")
            if self.rows_rejected:
                reasons = ", ".join(f"{k} {v:,}" for k, v in sorted(self.reject_reasons.items()))
                text += f", {self.rows_rejected:,} rejected [{reasons}]"
            if self.rows_skipped:
                text += f", {self.rows_skipped:,} duplicate(s) skipped"
            if self.rows_deleted:
                text += f", {self.rows_deleted:,} orphaned aggregate(s) deleted"
            if self.rows_incomplete:
                text += f", {self.rows_incomplete:,} incomplete (a trading day missing - see data errors)"
            text += f" into {self.target_table} in {self.duration:.1f}s"
        elif self.message:
            text += f" - step {self.step}: {self.message}" if self.step else f" - {self.message}"
        return text


@dataclass
class PromotionRecord:
    """One row of the Promotion_History table."""
    promotion_id: int
    procedure: str
    target_table: str
    mode: str
    filtered: int
    parameters: str | None
    started_at: str
    finished_at: str
    status: str
    rows_read: int
    rows_inserted: int
    rows_updated: int
    rows_unchanged: int
    rows_rejected: int
    rows_skipped: int
    rows_deleted: int
    rows_incomplete: int
    message: str | None


@dataclass
class ActiveTableSummary:
    kind: str                 # daily | weekly | monthly
    table: str
    rows: int = 0
    tickers: int = 0
    first_period: str | None = None
    last_period: str | None = None
    last_run: PromotionRecord | None = None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def normalise_date(value: str | date | None) -> str | None:
    """``YYYYMMDD`` / ``YYYY-MM-DD`` / date -> ``YYYY-MM-DD`` (None if empty).
    Raises :class:`PromotionError` for anything else."""
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    for pattern in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    raise PromotionError(f"'{value}' is not a date (use YYYY-MM-DD or YYYYMMDD)")


def _sql_date_ymd(expr: str) -> str:
    """SQL: staging date text (YYYYMMDD or YYYY-MM-DD) -> YYYY-MM-DD or NULL."""
    return (f"CASE WHEN TRIM({expr}) GLOB '{_YMD_GLOB}' THEN "
            f"substr(TRIM({expr}), 1, 4) || '-' || substr(TRIM({expr}), 5, 2) || '-' || substr(TRIM({expr}), 7, 2) "
            f"WHEN TRIM({expr}) GLOB '{_ISO_DATE_GLOB}' THEN TRIM({expr}) END")


def _q(name: str) -> str:
    return quote_identifier(name)


def _fmt_number(expr: str) -> str:
    """SQL: a number as text without trailing zeros (4.0 -> 4, 0.9229 stays); non-numbers as is."""
    return (f"CASE WHEN typeof({expr}) = 'real' THEN rtrim(rtrim(CAST({expr} AS TEXT), '0'), '.') "
            f"ELSE CAST({expr} AS TEXT) END")


# ---------------------------------------------------------------------------
# the procedures (SQL)
# ---------------------------------------------------------------------------
class _Statement:
    """One SQL statement of a procedure, with a description for the reference script."""

    def __init__(self, sql: str, comment: str = "", params: Mapping[str, Any] | None = None,
                 counts: str | None = None, hook: str | None = None):
        self.sql = sql.strip()
        self.comment = comment
        self.params = dict(params or {})
        self.counts = counts          # name of the result attribute the row count / value goes to
        self.hook = hook              # application hook to run after this statement (market calendar)


def daily_procedure(settings: Settings, filters: Mapping[str, Any] | None = None,
                    since: str | None = None, now: str = ":now") -> list[_Statement]:
    """The ``promote_daily`` procedure: statements in execution order.

    *filters*: ``source_file``, ``load_id``, ``ticker``, ``date_from``,
    ``date_to`` (YYYY-MM-DD).  *since*: incremental mode - only staging rows
    loaded or corrected at/after this timestamp.
    """
    stage, daily = _q(settings.stage_table), _q(settings.daily_table)
    f = filters or {}
    where, params = [], {}
    if f.get("source_file"):
        where.append('s."SourceFile" = :source_file')
        params["source_file"] = f["source_file"]
    if f.get("load_id"):
        where.append('s."LoadId" = :load_id')
        params["load_id"] = f["load_id"]
    if f.get("ticker"):
        where.append('TRIM(s."Ticker") = :ticker')
        params["ticker"] = f["ticker"]
    iso_date = _sql_date_ymd('s."TradeDate"')
    if f.get("date_from"):
        where.append(f"{iso_date} >= :date_from")
        params["date_from"] = f["date_from"]
    if f.get("date_to"):
        where.append(f"{iso_date} <= :date_to")
        params["date_to"] = f["date_to"]
    if since:
        # incremental: every staging row of a stock/day that was loaded or corrected
        # since the watermark - ALL copies of such a day take part, so duplicates are
        # resolved exactly as in a full run
        where.append(f'(s."Ticker", s."TradeDate") IN (SELECT x."Ticker", x."TradeDate" FROM {stage} x '
                     f'WHERE x."LoadedAt" >= :since OR x."StageId" IN '
                     f'(SELECT "StageId" FROM "Data_Correction" WHERE "CorrectedAt" >= :since))')
        params["since"] = since
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    steps = [
        _Statement(f"DROP TABLE IF EXISTS {_T_CANDIDATES}",
                   "the temporary tables live in this connection only (they vanish with it)"),
        _Statement(f"DROP TABLE IF EXISTS {_T_CLOSED}"),
        _Statement(f"DROP TABLE IF EXISTS {_T_SYMBOLS}"),
        _Statement(f"DROP TABLE IF EXISTS {_T_ROWS}"),
        _Statement(f"""
CREATE TEMP TABLE {_T_CANDIDATES} (
    "StageId"    INTEGER,
    "Market"     TEXT,
    "Ticker"     TEXT,
    "RawDate"    TEXT,
    "TradeDate"  TEXT,
    "Open"       REAL,
    "High"       REAL,
    "Low"        REAL,
    "Close"      REAL,
    "Vol"        NUMERIC,
    "SourceFile" TEXT,
    "LoadId"     TEXT,
    "Reason"     TEXT,
    "Rule"       TEXT,
    "ColumnName" TEXT,
    "Value"      TEXT,
    "Message"    TEXT
)""", "step 1: candidate rows. The typed columns (REAL / NUMERIC affinity) keep text that is "
      "not a number as TEXT - typeof() tells valid numbers from garbage; NUMERIC turns '2800.0' "
      "into the integer 2800 but leaves 28.5 a REAL (fractional volume)."),
        _Statement(f"""
INSERT INTO {_T_CANDIDATES} ("StageId", "Market", "Ticker", "RawDate", "TradeDate", "Open", "High", "Low",
                             "Close", "Vol", "SourceFile", "LoadId")
SELECT s."StageId", NULLIF(TRIM(COALESCE(s."Market", '')), ''), TRIM(COALESCE(s."Ticker", '')), s."TradeDate",
       COALESCE({iso_date}, ''),
       TRIM(COALESCE(s."Open", '')), TRIM(COALESCE(s."High", '')), TRIM(COALESCE(s."Low", '')),
       TRIM(COALESCE(s."Close", '')), TRIM(COALESCE(s."Vol", '')), s."SourceFile", s."LoadId"
FROM {stage} s{where_sql}""", params=params, counts="rows_read"),
        _Statement(f'CREATE INDEX "_ix_promote_candidates" ON {_T_CANDIDATES} ("Ticker", "TradeDate", "StageId")'),
        _Statement(f'CREATE TEMP TABLE {_T_CLOSED} ("Market" TEXT NOT NULL, "TradeDate" TEXT NOT NULL, '
                   f'"Reason" TEXT NOT NULL, PRIMARY KEY ("Market", "TradeDate"))',
                   "step 2: days the markets were closed - filled by the application from the No_Trading_Day "
                   "table (weekends, holidays, ad-hoc closures) for the distinct market / trade date pairs found"
                   if settings.reject_market_closed_days else
                   "step 2: market-closed days are not checked ([promotion] reject_market_closed_days = false)",
                   hook="closed_days"),
        _Statement(f"""
CREATE TEMP TABLE {_T_SYMBOLS} AS
SELECT "Market", "Ticker", "FirstTradeDate", "LastTradeDate"
  FROM "{STOCK_SYMBOL_TABLE}"
 WHERE "FirstTradeDate" IS NOT NULL OR "LastTradeDate" IS NOT NULL""",
                   "step 2b: the trading window of every stock with known dates (Stock_Symbol table)"
                   if settings.check_trading_window else
                   "step 2b: trading windows are not checked ([promotion] check_trading_window = false)"),
        _Statement(f'CREATE INDEX "_ix_promote_symbols" ON {_T_SYMBOLS} ("Market", "Ticker")'),
    ]

    # -- step 3: validation ------------------------------------------------
    numeric_ok = "typeof({c}) IN ('integer', 'real')"
    checks = [
        f"WHEN \"Ticker\" = '' THEN '{RULE_TICKER}:Ticker'",
        f"WHEN \"TradeDate\" = '' OR date(\"TradeDate\") IS NULL OR date(\"TradeDate\") <> \"TradeDate\" "
        f"THEN '{RULE_DATE}:TradeDate'",
    ]
    for col in ("Open", "High", "Low", "Close", "Vol"):
        checks.append(f"WHEN NOT {numeric_ok.format(c=_q(col))} THEN '{RULE_NUMERIC}:{col}'")
    for col in ("Open", "High", "Low", "Close"):
        checks.append(f"WHEN {_q(col)} <= 0 THEN '{RULE_POSITIVE}:{col}'")
    checks.append(f"WHEN typeof(\"Vol\") <> 'integer' OR \"Vol\" < 0 THEN '{RULE_VOLUME}:Vol'")
    checks.append(f"WHEN \"High\" < \"Low\" THEN '{RULE_HIGH_LOW}:High'")
    if settings.check_price_range:
        checks.append(f"WHEN \"Open\" < \"Low\" OR \"Open\" > \"High\" THEN '{RULE_RANGE}:Open'")
        checks.append(f"WHEN \"Close\" < \"Low\" OR \"Close\" > \"High\" THEN '{RULE_RANGE}:Close'")
    if settings.reject_market_closed_days:
        checks.append(f"WHEN EXISTS (SELECT 1 FROM {_T_CLOSED} x WHERE x.\"Market\" = COALESCE({_T_CANDIDATES}.\"Market\", '') "
                      f"AND x.\"TradeDate\" = {_T_CANDIDATES}.\"TradeDate\") THEN '{RULE_CLOSED}:TradeDate'")
    if settings.check_trading_window:
        symbol_match = (f"s.\"Market\" = COALESCE({_T_CANDIDATES}.\"Market\", '') AND s.\"Ticker\" = {_T_CANDIDATES}.\"Ticker\"")
        checks.append(f"WHEN EXISTS (SELECT 1 FROM {_T_SYMBOLS} s WHERE {symbol_match} "
                      f"AND s.\"FirstTradeDate\" > {_T_CANDIDATES}.\"TradeDate\") THEN '{RULE_BEFORE_FIRST}:TradeDate'")
        checks.append(f"WHEN EXISTS (SELECT 1 FROM {_T_SYMBOLS} s WHERE {symbol_match} "
                      f"AND s.\"LastTradeDate\" < {_T_CANDIDATES}.\"TradeDate\") THEN '{RULE_AFTER_LAST}:TradeDate'")
    case = "\n        ".join(checks)
    steps.append(_Statement(f"""
UPDATE {_T_CANDIDATES} SET "Reason" = CASE
        {case}
    END""", "step 3: validation - the first failing rule decides (rule:column)"))
    # the offending value as text: prices (REAL) without trailing zeros, everything else as is
    high, low = _fmt_number('"High"'), _fmt_number('"Low"')
    steps.append(_Statement(f"""
UPDATE {_T_CANDIDATES}
   SET "Rule" = substr("Reason", 1, instr("Reason", ':') - 1),
       "ColumnName" = substr("Reason", instr("Reason", ':') + 1),
       "Value" = CASE substr("Reason", instr("Reason", ':') + 1)
                     WHEN 'Open' THEN {_fmt_number('"Open"')}
                     WHEN 'High' THEN {high}
                     WHEN 'Low' THEN {low}
                     WHEN 'Close' THEN {_fmt_number('"Close"')}
                     WHEN 'Vol' THEN CAST("Vol" AS TEXT)
                     WHEN 'TradeDate' THEN "RawDate"
                     WHEN 'Ticker' THEN "Ticker"
                 END
 WHERE "Reason" IS NOT NULL""", "rule, column and offending value of the rejected rows", counts="rows_rejected"))
    steps.append(_Statement(f"""
UPDATE {_T_CANDIDATES} SET "Message" = CASE "Rule"
        WHEN '{RULE_TICKER}' THEN 'ticker is empty'
        WHEN '{RULE_DATE}' THEN 'date \'\'\' || COALESCE("RawDate", '') || \'\'\' is not a valid YYYYMMDD calendar date'
        WHEN '{RULE_NUMERIC}' THEN CASE WHEN COALESCE("Value", '') = '' THEN lower("ColumnName") || ' is missing'
                                  ELSE lower("ColumnName") || ' \'\'\' || "Value" || \'\'\' is not a number' END
        WHEN '{RULE_POSITIVE}' THEN lower("ColumnName") || ' (' || "Value" || ') must be greater than 0'
        WHEN '{RULE_VOLUME}' THEN 'vol (' || "Value" || ') must be a whole number >= 0'
        WHEN '{RULE_HIGH_LOW}' THEN 'high (' || "Value" || ') is smaller than low (' || {low} || ')'
        WHEN '{RULE_RANGE}' THEN lower("ColumnName") || ' (' || "Value" || ') is outside the low..high range ('
                                 || {low} || '..' || {high} || ')'
        WHEN '{RULE_BEFORE_FIRST}' THEN "Ticker" || ' started to trade on '
                                  || (SELECT s."FirstTradeDate" FROM {_T_SYMBOLS} s
                                       WHERE s."Market" = COALESCE({_T_CANDIDATES}."Market", '') AND s."Ticker" = {_T_CANDIDATES}."Ticker")
                                  || ' - data for ' || "TradeDate" || ' is before its first trade date'
        WHEN '{RULE_AFTER_LAST}' THEN "Ticker" || ' stopped trading on '
                                  || (SELECT s."LastTradeDate" FROM {_T_SYMBOLS} s
                                       WHERE s."Market" = COALESCE({_T_CANDIDATES}."Market", '') AND s."Ticker" = {_T_CANDIDATES}."Ticker")
                                  || ' - data for ' || "TradeDate" || ' is after its last trade date'
        WHEN '{RULE_CLOSED}' THEN COALESCE("Market", 'the market') || ' was closed on ' || "TradeDate" || ' ('
                                  || COALESCE((SELECT x."Reason" FROM {_T_CLOSED} x
                                                WHERE x."Market" = COALESCE({_T_CANDIDATES}."Market", '')
                                                  AND x."TradeDate" = {_T_CANDIDATES}."TradeDate"), 'no trading day')
                                  || ')'
    END
 WHERE "Rule" IS NOT NULL""", "messages for the rejected rows"))
    steps.append(_Statement(f'SELECT "Rule", COUNT(*) FROM {_T_CANDIDATES} WHERE "Rule" IS NOT NULL GROUP BY "Rule"',
                            counts="reject_reasons"))

    # -- step 4: record rejections ------------------------------------------
    steps.append(_Statement(f"""
INSERT INTO "{DATA_ERROR_TABLE}" ("DetectedAt", "SourceFile", "LoadId", "Market", "Ticker", "TradeDate",
                                  "ColumnName", "Value", "Rule", "Severity", "Alert", "ErrorMessage", "LineNo")
SELECT {now}, c."SourceFile", c."LoadId", c."Market", NULLIF(c."Ticker", ''), c."RawDate", c."ColumnName",
       c."Value", c."Rule", 'error', 0,
       'promotion to {settings.daily_table} rejected the row: ' || c."Message", NULL
  FROM {_T_CANDIDATES} c
 WHERE c."Rule" IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM "{DATA_ERROR_TABLE}" e
                    WHERE e."Resolved" = 0 AND e."Rule" = c."Rule" AND e."SourceFile" IS c."SourceFile"
                      AND e."Ticker" IS NULLIF(c."Ticker", '') AND e."TradeDate" IS c."RawDate"
                      AND e."ColumnName" IS c."ColumnName")""",
                            "step 4: rejected rows -> Data_Error (once per open finding; fix them on the data "
                            "correction page and promote again)", params={"now": None}))

    # -- step 5: de-duplicate (latest staged row per stock/day wins) --------
    steps.append(_Statement(f"""
CREATE TEMP TABLE {_T_ROWS} AS
SELECT c."StageId", c."Market", c."Ticker", c."TradeDate", c."Open", c."High", c."Low", c."Close",
       c."Vol" AS "Volume", c."SourceFile", c."LoadId"
  FROM {_T_CANDIDATES} c
 WHERE c."Rule" IS NULL
   AND NOT EXISTS (SELECT 1 FROM {_T_CANDIDATES} c2
                    WHERE c2."Ticker" = c."Ticker" AND c2."TradeDate" = c."TradeDate"
                      AND c2."StageId" > c."StageId" AND c2."Rule" IS NULL)
 ORDER BY c."Ticker", c."TradeDate"
""",
                            "step 5: one row per stock/day - the most recently loaded valid staging row "
                            "(highest StageId) wins; ordered like the target's unique index, which keeps "
                            "the following look-ups sequential"))
    steps.append(_Statement(f"SELECT (SELECT COUNT(*) FROM {_T_CANDIDATES} WHERE \"Rule\" IS NULL) - "
                            f"(SELECT COUNT(*) FROM {_T_ROWS})", counts="rows_skipped"))

    # -- step 6: counts + upsert --------------------------------------------
    join = f'FROM {_T_ROWS} r JOIN {daily} d ON d."Ticker" = r."Ticker" AND d."TradeDate" = r."TradeDate"'
    differs = ('(d."Open" IS NOT r."Open" OR d."High" IS NOT r."High" OR d."Low" IS NOT r."Low" '
               'OR d."Close" IS NOT r."Close" OR d."Volume" IS NOT r."Volume" OR d."Market" IS NOT r."Market" '
               'OR d."SourceFile" IS NOT r."SourceFile" OR d."LoadId" IS NOT r."LoadId" '
               'OR d."StageId" IS NOT r."StageId")')
    steps.append(_Statement(f"SELECT COUNT(*) {join}", "step 6: how many rows already exist ...",
                            counts="_matched"))
    steps.append(_Statement(f"SELECT COUNT(*) {join} WHERE {differs}", "... and how many of them changed",
                            counts="rows_updated"))
    steps.append(_Statement(f"""
INSERT INTO {daily} ("Market", "Ticker", "TradeDate", "Open", "High", "Low", "Close", "Volume",
                     "SourceFile", "LoadId", "StageId", "PromotedAt", "UpdatedAt")
SELECT r."Market", r."Ticker", r."TradeDate", r."Open", r."High", r."Low", r."Close", r."Volume",
       r."SourceFile", r."LoadId", r."StageId", {now}, {now}
  FROM {_T_ROWS} r
 WHERE true
ON CONFLICT ("Ticker", "TradeDate") DO UPDATE SET
       "Market" = excluded."Market", "Open" = excluded."Open", "High" = excluded."High",
       "Low" = excluded."Low", "Close" = excluded."Close", "Volume" = excluded."Volume",
       "SourceFile" = excluded."SourceFile", "LoadId" = excluded."LoadId", "StageId" = excluded."StageId",
       "UpdatedAt" = excluded."UpdatedAt"
 WHERE {daily}."Open" IS NOT excluded."Open" OR {daily}."High" IS NOT excluded."High"
    OR {daily}."Low" IS NOT excluded."Low" OR {daily}."Close" IS NOT excluded."Close"
    OR {daily}."Volume" IS NOT excluded."Volume" OR {daily}."Market" IS NOT excluded."Market"
    OR {daily}."SourceFile" IS NOT excluded."SourceFile" OR {daily}."LoadId" IS NOT excluded."LoadId"
    OR {daily}."StageId" IS NOT excluded."StageId"
""",
                            "upsert: new stock/days are inserted, existing ones updated only if something "
                            "changed (UpdatedAt drives the incremental weekly / monthly promotion)",
                            params={"now": None}))
    return steps


def _period_procedure(settings: Settings, kind: str, filters: Mapping[str, Any] | None = None,
                      since: str | None = None, full: bool = True, now: str = ":now") -> list[_Statement]:
    """The ``promote_weekly`` / ``promote_monthly`` procedure (they only differ
    in how a day maps to its period)."""
    daily = _q(settings.daily_table)
    if kind == "weekly":
        target = _q(settings.weekly_table)
        start_col, end_col = "WeekStart", "WeekEnd"
        period_start = "date({d}, 'weekday 0', '-6 days')"            # Monday of the ISO week
        period_end = "date({p}, '+6 days')"                            # Sunday
        extra_cols = ("ISOYear", "ISOWeek")
        extra_exprs = ("CAST(strftime('%Y', {p}, '+3 days') AS INTEGER)",
                       "(CAST(strftime('%j', {p}, '+3 days') AS INTEGER) - 1) / 7 + 1")
        extra_comment = ("ISO year / week number: the week belongs to the year of its Thursday "
                         "(Monday + 3 days); week = (day of year of that Thursday - 1) / 7 + 1")
        period_name = "ISO week (Monday..Sunday)"
    elif kind == "monthly":
        target = _q(settings.monthly_table)
        start_col, end_col = "MonthStart", "MonthEnd"
        period_start = "date({d}, 'start of month')"
        period_end = "date({p}, '+1 month', '-1 day')"
        extra_cols = ("Year", "Month")
        extra_exprs = ("CAST(strftime('%Y', {p}) AS INTEGER)", "CAST(strftime('%m', {p}) AS INTEGER)")
        extra_comment = "calendar year and month number"
        period_name = "calendar month"
    else:  # pragma: no cover - programming error
        raise ValueError(kind)
    rule = RULE_INCOMPLETE_WEEK if kind == "weekly" else RULE_INCOMPLETE_MONTH
    target_name = settings.weekly_table if kind == "weekly" else settings.monthly_table
    label = ("'week ' || r.\"ISOYear\" || '-W' || printf('%02d', r.\"ISOWeek\") || ' (' || r.\"PeriodStart\" || '..' || r.\"PeriodEnd\" || ')'"
             if kind == "weekly" else "'month ' || substr(r.\"PeriodStart\", 1, 7)")

    f = filters or {}
    where, params = [], {}
    if f.get("ticker"):
        where.append('d."Ticker" = :ticker')
        params["ticker"] = f["ticker"]
    if since:
        where.append('d."UpdatedAt" >= :since')
        params["since"] = since
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    ps = period_start.format(d='d."TradeDate"')
    pe = period_end.format(p='p."PeriodStart"')

    steps = [
        _Statement(f"DROP TABLE IF EXISTS {_T_PERIODS}",
                   "the temporary tables live in this connection only (they vanish with it)"),
        _Statement(f"DROP TABLE IF EXISTS {_T_AGG}"),
        _Statement(f"DROP TABLE IF EXISTS {_T_ROWS}"),
        _Statement(f"DROP TABLE IF EXISTS {_T_OPEN_DAYS}"),
        _Statement(f"DROP TABLE IF EXISTS {_T_SYMBOLS}"),
        _Statement(f"DROP TABLE IF EXISTS {_T_GAPS}"),
        _Statement(f"""
CREATE TEMP TABLE {_T_PERIODS} AS
SELECT DISTINCT d."Ticker", {ps} AS "PeriodStart"
  FROM {daily} d{where_sql}""",
                   f"step 1: the stock/{period_name} periods to (re)compute" +
                   (" - only periods with daily rows changed since the last run (incremental)" if since else
                    " - every period of the daily table (full)"), params=params),
        _Statement(f"SELECT COUNT(*) FROM {_T_PERIODS}", counts="rows_read"),
        _Statement(f"""
CREATE TEMP TABLE {_T_AGG} AS
SELECT p."Ticker", p."PeriodStart", {pe} AS "PeriodEnd",
       MIN(d."TradeDate") AS "FirstTradeDate", MAX(d."TradeDate") AS "LastTradeDate",
       COUNT(*) AS "TradingDays", MAX(d."High") AS "High", MIN(d."Low") AS "Low", SUM(d."Volume") AS "Volume"
  FROM {_T_PERIODS} p
  JOIN {daily} d ON d."Ticker" = p."Ticker" AND d."TradeDate" >= p."PeriodStart" AND d."TradeDate" <= {pe}
 GROUP BY p."Ticker", p."PeriodStart"
""",
                   "step 2: aggregate the trading days of each period (high = highest high, low = lowest low, "
                   "volume = sum); the daily rows are found through the UNIQUE (Ticker, TradeDate) index"),
        _Statement(f"""
CREATE TEMP TABLE {_T_ROWS} AS
SELECT l."Market", a."Ticker", a."PeriodStart", a."PeriodEnd",
       {extra_exprs[0].format(p='a."PeriodStart"')} AS "{extra_cols[0]}",
       {extra_exprs[1].format(p='a."PeriodStart"')} AS "{extra_cols[1]}",
       f."Open", a."High", a."Low", l."Close", a."Volume", a."TradingDays", a."FirstTradeDate", a."LastTradeDate",
       0 AS "ExpectedDays", 0 AS "MissingDays", NULL AS "MissingDates", 1 AS "Complete"
  FROM {_T_AGG} a
  JOIN {daily} f ON f."Ticker" = a."Ticker" AND f."TradeDate" = a."FirstTradeDate"
  JOIN {daily} l ON l."Ticker" = a."Ticker" AND l."TradeDate" = a."LastTradeDate"
""",
                   "step 3: open = open of the first trading day, close = close of the last trading day, "
                   "market = market of the last day; " + extra_comment),
        _Statement(f'CREATE INDEX "_ix_promote_rows" ON {_T_ROWS} ("Ticker", "PeriodStart")'),
    ]
    if settings.check_completeness:
        steps += [
            _Statement(f'CREATE TEMP TABLE {_T_OPEN_DAYS} ("Market" TEXT NOT NULL, "TradeDate" TEXT NOT NULL, '
                       f'PRIMARY KEY ("Market", "TradeDate"))',
                       "step 3b (validation): the days the markets were open over the periods, between the earliest "
                       "and the latest trade date loaded for the market - filled by the application from the "
                       "No_Trading_Day table", hook="open_days"),
            _Statement(f"""
CREATE TEMP TABLE {_T_SYMBOLS} AS
SELECT "Market", "Ticker", "FirstTradeDate", "LastTradeDate"
  FROM "{STOCK_SYMBOL_TABLE}"
 WHERE "FirstTradeDate" IS NOT NULL OR "LastTradeDate" IS NOT NULL""",
                       "the trading window of every stock with known dates (Stock_Symbol table)"),
            _Statement(f'CREATE INDEX "_ix_promote_symbols" ON {_T_SYMBOLS} ("Market", "Ticker")'),
            _Statement(f"""
CREATE TEMP TABLE {_T_GAPS} AS
SELECT "Ticker", "PeriodStart", COUNT(*) AS "ExpectedDays",
       SUM("Missing") AS "MissingDays",
       GROUP_CONCAT(CASE WHEN "Missing" = 1 THEN "TradeDate" END, ',') AS "MissingDates"
  FROM (SELECT r."Ticker", r."PeriodStart", o."TradeDate",
               CASE WHEN d."TradeDate" IS NULL THEN 1 ELSE 0 END AS "Missing"
          FROM {_T_ROWS} r
          JOIN {_T_OPEN_DAYS} o ON o."Market" = COALESCE(r."Market", '')
                                AND o."TradeDate" BETWEEN r."PeriodStart" AND r."PeriodEnd"
          LEFT JOIN {_T_SYMBOLS} s ON s."Market" = COALESCE(r."Market", '') AND s."Ticker" = r."Ticker"
          LEFT JOIN {daily} d ON d."Ticker" = r."Ticker" AND d."TradeDate" = o."TradeDate"
         WHERE (s."FirstTradeDate" IS NULL OR o."TradeDate" >= s."FirstTradeDate")
           AND (s."LastTradeDate" IS NULL OR o."TradeDate" <= s."LastTradeDate")
         ORDER BY r."Ticker", r."PeriodStart", o."TradeDate")
 GROUP BY "Ticker", "PeriodStart"
""",
                       "every trading day of the period (market open, inside the stock's trading window if its "
                       "first / last trade date is known) must have a daily row - count the expected and the "
                       "missing days per stock and period"),
            _Statement(f'CREATE INDEX "_ix_promote_gaps" ON {_T_GAPS} ("Ticker", "PeriodStart")'),
            _Statement(f"""
UPDATE {_T_ROWS} SET
       "ExpectedDays" = COALESCE((SELECT g."ExpectedDays" FROM {_T_GAPS} g
                                   WHERE g."Ticker" = {_T_ROWS}."Ticker" AND g."PeriodStart" = {_T_ROWS}."PeriodStart"), 0),
       "MissingDays" = COALESCE((SELECT g."MissingDays" FROM {_T_GAPS} g
                                  WHERE g."Ticker" = {_T_ROWS}."Ticker" AND g."PeriodStart" = {_T_ROWS}."PeriodStart"), 0),
       "MissingDates" = (SELECT g."MissingDates" FROM {_T_GAPS} g
                          WHERE g."Ticker" = {_T_ROWS}."Ticker" AND g."PeriodStart" = {_T_ROWS}."PeriodStart")""",
                       "mark the incomplete periods"),
            _Statement(f'UPDATE {_T_ROWS} SET "Complete" = CASE WHEN "MissingDays" > 0 THEN 0 ELSE 1 END'),
            _Statement(f'SELECT COUNT(*) FROM {_T_ROWS} WHERE "Complete" = 0', counts="rows_incomplete"),
            _Statement(f"""
INSERT INTO "{DATA_ERROR_TABLE}" ("DetectedAt", "SourceFile", "LoadId", "Market", "Ticker", "TradeDate",
                                  "ColumnName", "Value", "Rule", "Severity", "Alert", "ErrorMessage", "LineNo")
SELECT {now}, '{target_name}', NULL, r."Market", r."Ticker", r."PeriodStart", 'TradingDays',
       r."TradingDays" || '/' || r."ExpectedDays", '{rule}', 'error', 0,
       r."Ticker" || ' ' || {label} || ' is incomplete: ' || (r."ExpectedDays" - r."MissingDays") || ' of '
       || r."ExpectedDays" || ' trading days loaded - missing ' || r."MissingDates", NULL
  FROM {_T_ROWS} r
 WHERE r."Complete" = 0
   AND NOT EXISTS (SELECT 1 FROM "{DATA_ERROR_TABLE}" e
                    WHERE e."Resolved" = 0 AND e."Rule" = '{rule}' AND e."SourceFile" = '{target_name}'
                      AND e."Ticker" = r."Ticker" AND e."TradeDate" = r."PeriodStart")""",
                       "incomplete periods -> Data_Error (once per open finding)", params={"now": None}),
            _Statement(f"""
UPDATE "{DATA_ERROR_TABLE}"
   SET "Resolved" = 1, "ResolvedAt" = {now},
       "ResolutionNote" = 'complete after the promotion of ' || {now}
 WHERE "Resolved" = 0 AND "Rule" = '{rule}' AND "SourceFile" = '{target_name}'
   AND EXISTS (SELECT 1 FROM {_T_ROWS} r
                WHERE r."Ticker" = "{DATA_ERROR_TABLE}"."Ticker" AND r."PeriodStart" = "{DATA_ERROR_TABLE}"."TradeDate"
                  AND r."Complete" = 1)""",
                       "periods that are complete again resolve their open finding", params={"now": None}),
        ]
    join = (f'FROM {_T_ROWS} r JOIN {target} t ON t."Ticker" = r."Ticker" '
            f'AND t."{start_col}" = r."PeriodStart"')
    cols_differ = ['"Open"', '"High"', '"Low"', '"Close"', '"Volume"', '"TradingDays"', '"FirstTradeDate"',
                   '"LastTradeDate"', '"Market"', f'"{end_col}"', f'"{extra_cols[0]}"', f'"{extra_cols[1]}"',
                   '"ExpectedDays"', '"MissingDays"', '"MissingDates"', '"Complete"']
    r_name = {f'"{end_col}"': '"PeriodEnd"'}
    differs = " OR ".join(f't.{c} IS NOT r.{r_name.get(c, c)}' for c in cols_differ)
    steps.append(_Statement(f"SELECT COUNT(*) {join}", "step 4: how many periods already exist ...",
                            counts="_matched"))
    steps.append(_Statement(f"SELECT COUNT(*) {join} WHERE {differs}", "... and how many of them changed",
                            counts="rows_updated"))
    upsert_where = " OR ".join(f'{target}.{c} IS NOT excluded.{c}' for c in cols_differ)
    steps.append(_Statement(f"""
INSERT INTO {target} ("Market", "Ticker", "{start_col}", "{end_col}", "{extra_cols[0]}", "{extra_cols[1]}",
                      "Open", "High", "Low", "Close", "Volume", "TradingDays", "FirstTradeDate", "LastTradeDate",
                      "ExpectedDays", "MissingDays", "MissingDates", "Complete", "PromotedAt", "UpdatedAt")
SELECT r."Market", r."Ticker", r."PeriodStart", r."PeriodEnd", r."{extra_cols[0]}", r."{extra_cols[1]}",
       r."Open", r."High", r."Low", r."Close", r."Volume", r."TradingDays", r."FirstTradeDate", r."LastTradeDate",
       r."ExpectedDays", r."MissingDays", r."MissingDates", r."Complete", {now}, {now}
  FROM {_T_ROWS} r
 WHERE true
ON CONFLICT ("Ticker", "{start_col}") DO UPDATE SET
       "Market" = excluded."Market", "{end_col}" = excluded."{end_col}",
       "{extra_cols[0]}" = excluded."{extra_cols[0]}", "{extra_cols[1]}" = excluded."{extra_cols[1]}",
       "Open" = excluded."Open", "High" = excluded."High", "Low" = excluded."Low", "Close" = excluded."Close",
       "Volume" = excluded."Volume", "TradingDays" = excluded."TradingDays",
       "FirstTradeDate" = excluded."FirstTradeDate", "LastTradeDate" = excluded."LastTradeDate",
       "ExpectedDays" = excluded."ExpectedDays", "MissingDays" = excluded."MissingDays",
       "MissingDates" = excluded."MissingDates", "Complete" = excluded."Complete",
       "UpdatedAt" = excluded."UpdatedAt"
 WHERE {upsert_where}""", "upsert: new periods are inserted, existing ones updated only if something changed",
                            params={"now": None}))
    if full:
        orphan_where = (f'NOT EXISTS (SELECT 1 FROM {daily} d WHERE d."Ticker" = {target}."Ticker" '
                        f'AND d."TradeDate" >= {target}."{start_col}" AND d."TradeDate" <= {target}."{end_col}")')
        if f.get("ticker"):
            orphan_where += f' AND {target}."Ticker" = :ticker'
        steps.append(_Statement(f"DELETE FROM {target} WHERE {orphan_where}",
                                "step 5 (full mode only): remove aggregates whose daily rows are gone",
                                params={"ticker": f.get("ticker")} if f.get("ticker") else None,
                                counts="rows_deleted"))
    return steps


def weekly_procedure(settings: Settings, filters: Mapping[str, Any] | None = None, since: str | None = None,
                     full: bool = True, now: str = ":now") -> list[_Statement]:
    return _period_procedure(settings, "weekly", filters, since, full, now)


def monthly_procedure(settings: Settings, filters: Mapping[str, Any] | None = None, since: str | None = None,
                      full: bool = True, now: str = ":now") -> list[_Statement]:
    return _period_procedure(settings, "monthly", filters, since, full, now)


def render_procedure(name: str, settings: Settings | None = None) -> str:
    """The SQL of a procedure as a readable reference script (sql/promote_*_sqlite.sql)."""
    settings = settings or Settings()
    if name == PROC_DAILY:
        steps = daily_procedure(settings, {}, since=None)
        head = (f"-- {PROC_DAILY}: staging table -> {settings.daily_table} (validated, typed, one row per "
                f"stock and day).\n-- Parameters: :now (timestamp). Optional filters add WHERE conditions on "
                f"SourceFile / LoadId / Ticker / TradeDate;\n-- incremental mode adds "
                f"\"LoadedAt\" >= :since OR StageId corrected since :since.")
    elif name == PROC_WEEKLY:
        steps = weekly_procedure(settings, {}, since=None, full=True)
        head = (f"-- {PROC_WEEKLY}: {settings.daily_table} -> {settings.weekly_table} (one row per stock and "
                f"ISO week).\n-- Parameters: :now. Incremental mode adds \"UpdatedAt\" >= :since to step 1 "
                f"and skips step 5.")
    elif name == PROC_MONTHLY:
        steps = monthly_procedure(settings, {}, since=None, full=True)
        head = (f"-- {PROC_MONTHLY}: {settings.daily_table} -> {settings.monthly_table} (one row per stock and "
                f"month).\n-- Parameters: :now. Incremental mode adds \"UpdatedAt\" >= :since to step 1 "
                f"and skips step 5.")
    else:
        raise ValueError(f"unknown procedure {name}")
    lines = [head, "-- Generated from stock_dataload/promotion.py - run by PromotionService inside ONE "
                   "transaction (BEGIN IMMEDIATE ... COMMIT).", "-- The temporary tables live in the "
                   "connection only. SQLite has no stored procedures: this is the procedure.", "",
             "BEGIN IMMEDIATE;"]
    for s in steps:
        if s.comment:
            lines.append("")
            lines.extend("-- " + c for c in _wrap(s.comment, 96))
        lines.append(s.sql + ";")
    lines += ["", "COMMIT;", ""]
    return "\n".join(lines)


def _wrap(text: str, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    if cur:
        lines.append(cur)
    return lines


# ---------------------------------------------------------------------------
# the service
# ---------------------------------------------------------------------------
class PromotionService:
    """Runs the promotion procedures; owns the active tables and Promotion_History."""

    def __init__(self, settings: Settings, db: StageDatabase | None = None,
                 audit: AuditLogger | None = None, data_errors: DataErrorRepository | None = None,
                 calendar: TradingCalendar | None = None):
        self.settings = settings
        self.db = db or StageDatabase(settings)
        self.audit = audit or AuditLogger(settings)
        self.data_errors = data_errors or DataErrorRepository(settings)
        self.calendar = calendar or TradingCalendar(settings, self.db, self.audit)
        self.symbols = SymbolRegistry(settings, self.db, self.audit)
        # the incremental daily promotion picks up rows changed on the correction page
        self.corrections = CorrectionService(settings, self.db, audit=self.audit, data_errors=self.data_errors)
        self._tables_ready = False

    # -- names -------------------------------------------------------------
    @property
    def tables(self) -> dict[str, str]:
        return {"daily": self.settings.daily_table, "weekly": self.settings.weekly_table,
                "monthly": self.settings.monthly_table}

    def _ddl(self) -> list[str]:
        names = {"daily": _q(self.settings.daily_table), "daily_name": self.settings.daily_table,
                 "weekly": _q(self.settings.weekly_table), "weekly_name": self.settings.weekly_table,
                 "monthly": _q(self.settings.monthly_table), "monthly_name": self.settings.monthly_table}
        statements = [DAILY_DDL, *DAILY_INDEXES, WEEKLY_DDL, *WEEKLY_INDEXES, MONTHLY_DDL, *MONTHLY_INDEXES]
        return [s.format(**names) for s in statements] + [HISTORY_DDL, HISTORY_INDEX]

    def render_ddl(self) -> str:
        """DDL of the active tables + Promotion_History as a script (sql/ reference)."""
        return "\n".join(s.strip() + ";" for s in self._ddl()) + "\n"

    # -- connections / DDL ---------------------------------------------------
    def connection(self):
        return self.db.connection()

    def ensure_tables(self, conn: sqlite3.Connection | None = None) -> None:
        """Create the active tables and Promotion_History if missing."""
        if conn is None:
            with self.connection() as c:
                self.ensure_tables(c)
            return
        for ddl in self._ddl():
            conn.execute(ddl)
        tables = {"weekly": self.settings.weekly_table, "monthly": self.settings.monthly_table,
                  "daily": self.settings.daily_table, "history": PROMOTION_HISTORY_TABLE}
        for key, column, ddl in ACTIVE_MIGRATIONS:     # columns added after the first release
            existing = {r[1] for r in conn.execute(f'PRAGMA table_info({_q(tables[key])})')}
            if column not in existing:
                conn.execute(f'ALTER TABLE {_q(tables[key])} ADD COLUMN {ddl}')
        self._tables_ready = True

    # -- history -------------------------------------------------------------
    def last_completed(self, procedure: str, unfiltered_only: bool = True,
                       conn: sqlite3.Connection | None = None) -> PromotionRecord | None:
        """The most recent completed run of *procedure* (the incremental watermark)."""
        if conn is None:
            with self.connection() as c:
                return self.last_completed(procedure, unfiltered_only, c)
        self.ensure_tables(conn)
        cols = ", ".join(_q(c) for c in HISTORY_COLUMNS)
        sql = (f'SELECT {cols} FROM "{PROMOTION_HISTORY_TABLE}" WHERE "Procedure" = ? AND "Status" = ? '
               f'AND "TargetTable" = ?')
        params: list = [procedure, STATUS_COMPLETED, self._target_of(procedure)]
        if unfiltered_only:
            sql += ' AND "Filtered" = 0'
        sql += ' ORDER BY "PromotionId" DESC LIMIT 1'
        row = conn.execute(sql, params).fetchone()
        return PromotionRecord(*row) if row else None

    def history(self, limit: int = 50, procedure: str | None = None) -> list[PromotionRecord]:
        cols = ", ".join(_q(c) for c in HISTORY_COLUMNS)
        sql = f'SELECT {cols} FROM "{PROMOTION_HISTORY_TABLE}"'
        params: list = []
        if procedure:
            sql += ' WHERE "Procedure" = ?'
            params.append(procedure)
        sql += ' ORDER BY "PromotionId" DESC LIMIT ?'
        params.append(int(limit))
        with self.connection() as conn:
            self.ensure_tables(conn)
            return [PromotionRecord(*r) for r in conn.execute(sql, params).fetchall()]

    def count_history(self, status: str | None = None) -> int:
        sql = f'SELECT COUNT(*) FROM "{PROMOTION_HISTORY_TABLE}"'
        params: list = []
        if status:
            sql += ' WHERE "Status" = ?'
            params.append(status)
        with self.connection() as conn:
            self.ensure_tables(conn)
            return int(conn.execute(sql, params).fetchone()[0])

    def _record_history(self, result: PromotionResult) -> None:
        """Append the run to Promotion_History with its own connection (survives a rollback)."""
        try:
            with self.connection() as conn:
                self.ensure_tables(conn)
                cur = conn.execute(
                    f'INSERT INTO "{PROMOTION_HISTORY_TABLE}" ("Procedure", "TargetTable", "Mode", "Filtered", '
                    '"Parameters", "StartedAt", "FinishedAt", "Status", "RowsRead", "RowsInserted", "RowsUpdated", '
                    '"RowsUnchanged", "RowsRejected", "RowsSkipped", "RowsDeleted", "RowsIncomplete", "Message") '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (result.procedure, result.target_table, result.mode, 1 if result.filtered else 0,
                     json.dumps(result.parameters, default=str, sort_keys=True) if result.parameters else None,
                     result.started.replace(microsecond=0).isoformat(sep=" "),
                     (result.finished or datetime.now()).replace(microsecond=0).isoformat(sep=" "),
                     result.status, result.rows_read, result.rows_inserted, result.rows_updated,
                     result.rows_unchanged, result.rows_rejected, result.rows_skipped, result.rows_deleted,
                     result.rows_incomplete, result.message or None))
                result.history_id = int(cur.lastrowid)
        except (sqlite3.Error, DatabaseError, OSError):  # pragma: no cover - bookkeeping must not mask the run
            log.exception("could not record the promotion run in %s", PROMOTION_HISTORY_TABLE)

    def _target_of(self, procedure: str) -> str:
        return {PROC_DAILY: self.settings.daily_table, PROC_WEEKLY: self.settings.weekly_table,
                PROC_MONTHLY: self.settings.monthly_table}[procedure]

    # -- running a procedure -------------------------------------------------
    def _execute(self, conn: sqlite3.Connection, steps: Sequence[_Statement], params: Mapping[str, Any],
                 result: PromotionResult, hooks: Mapping[str, Callable[[sqlite3.Connection], None]] | None = None,
                 ) -> None:
        """Run the statements in order, collecting the counts they produce."""
        counts: dict[str, int] = {}
        for i, step in enumerate(steps, start=1):
            label = step.comment.split(" - ")[0].split(":")[0] if step.comment else ""
            result.step = f"{i}/{len(steps)} {step.sql.split(None, 1)[0].lower()}" + (f" ({label})" if label else "")
            bind = {k: params.get(k, v) for k, v in step.params.items()}
            t0 = time.perf_counter()
            cur = conn.execute(step.sql, bind)
            log.debug("%s step %s: %.2fs", result.procedure, result.step, time.perf_counter() - t0)
            if step.counts == "reject_reasons":
                result.reject_reasons = {str(rule): int(n) for rule, n in cur.fetchall()}
            elif step.counts:
                if step.sql.lstrip().upper().startswith("SELECT"):
                    counts[step.counts] = int(cur.fetchone()[0] or 0)
                else:
                    counts[step.counts] = max(cur.rowcount or 0, 0)
            if step.hook and hooks and step.hook in hooks:
                hooks[step.hook](conn)
        result.rows_read = counts.get("rows_read", 0)
        result.rows_rejected = counts.get("rows_rejected", 0)
        result.rows_skipped = counts.get("rows_skipped", 0)
        result.rows_updated = counts.get("rows_updated", 0)
        result.rows_deleted = counts.get("rows_deleted", 0)
        result.rows_incomplete = counts.get("rows_incomplete", 0)
        matched = counts.get("_matched", 0)
        candidates = result.rows_read - result.rows_rejected - result.rows_skipped
        result.rows_inserted = max(candidates - matched, 0)
        result.rows_unchanged = max(matched - result.rows_updated, 0)

    def _run(self, procedure: str, mode: str, parameters: dict,
             build: Callable[[str, str | None], Sequence[_Statement]],
             hooks: Mapping[str, Callable[[sqlite3.Connection], None]] | None = None) -> PromotionResult:
        result = PromotionResult(procedure, self._target_of(procedure), mode=mode,
                                 parameters={k: v for k, v in parameters.items() if v not in (None, "", False)})
        self.audit.log(COMPONENT, f"{procedure}_started", f"{mode} promotion into {result.target_table}",
                       details=result.parameters)
        try:
            with self.connection() as conn:
                # one writer at a time: the timestamp is taken AFTER the write lock is ours, so
                # UpdatedAt watermarks of concurrent runs are ordered consistently
                result.step = "begin"
                conn.execute("BEGIN IMMEDIATE")
                result.started = datetime.now()
                now = now_iso()
                self.ensure_tables(conn)
                self.data_errors.ensure_table(conn)
                self.corrections.ensure_table(conn)
                self.symbols.ensure_table(conn)
                since = None
                if mode == MODE_INCREMENTAL:
                    last = self.last_completed(procedure, conn=conn)
                    since = last.started_at if last else None
                    if since is None:
                        result.mode = MODE_FULL          # nothing to be incremental to
                steps = build(result.mode, since)
                self._execute(conn, steps, {"now": now, "since": since}, result, hooks)
            result.status = STATUS_COMPLETED
            result.step = ""
        except Exception as exc:  # noqa: BLE001 - whatever failed, the transaction is rolled back
            result.status = STATUS_FAILED
            result.message = str(exc) or type(exc).__name__
            result.finished = datetime.now()
            log.error("%s failed at step %s: %s", procedure, result.step, exc,
                      exc_info=not isinstance(exc, (PromotionError, sqlite3.Error)))
            self.audit.error(COMPONENT, f"{procedure}/{result.step or 'run'}", exc,
                             details={"parameters": result.parameters, "mode": result.mode})
            self.audit.log(COMPONENT, f"{procedure}_failed", f"step {result.step}: {exc}", level="ERROR",
                           details={"parameters": result.parameters, "mode": result.mode})
            self._record_history(result)
            raise PromotionError(f"{procedure} failed ({result.step}): {exc}", result) from exc
        result.finished = datetime.now()
        self._record_history(result)
        self.calendar.flush_audit()
        log.info("%s", result)
        level = "WARNING" if result.rows_rejected else "INFO"
        self.audit.log(COMPONENT, f"{procedure}_complete", str(result), level=level, details=result.as_dict())
        return result

    # -- the three procedures ------------------------------------------------
    def promote_daily(self, source_file: str | None = None, load_id: str | None = None,
                      ticker: str | None = None, date_from: str | date | None = None,
                      date_to: str | date | None = None, full: bool | None = None) -> PromotionResult:
        """``promote_daily``: validate the staging rows and upsert them into the daily table.

        Without filters the run is *incremental* (rows loaded or corrected since
        the last completed unfiltered run; the first run is full); ``full=True``
        re-promotes the whole staging table.  Filters (file, load id, ticker,
        date range) always use the rows they select.  Returns the
        :class:`PromotionResult`; raises :class:`PromotionError` on failure.
        """
        filters = {"source_file": (source_file or "").strip() or None, "load_id": (load_id or "").strip() or None,
                   "ticker": (ticker or "").strip() or None, "date_from": normalise_date(date_from),
                   "date_to": normalise_date(date_to)}
        if filters["date_from"] and filters["date_to"] and filters["date_from"] > filters["date_to"]:
            raise PromotionError("date_from must not be after date_to")
        filtered = any(filters.values())
        mode = MODE_FULL if (full or filtered) else MODE_INCREMENTAL
        parameters = {**filters, "full": bool(full)}

        def fill_closed_days(conn: sqlite3.Connection) -> None:
            """step 2: the No_Trading_Day calendar for every market / trade date of the candidates."""
            if not self.settings.reject_market_closed_days:
                return
            pairs = conn.execute(
                f'SELECT DISTINCT COALESCE("Market", \'\'), "TradeDate" FROM {_T_CANDIDATES} '
                f'WHERE date("TradeDate") = "TradeDate"').fetchall()
            closed = []
            for market, text in pairs:
                try:
                    day = date.fromisoformat(text)
                except ValueError:
                    continue
                reason = self.calendar.closure_reason(market or None, day, conn)
                if reason:
                    closed.append((market, text, reason))
            conn.executemany(f'INSERT OR IGNORE INTO {_T_CLOSED} ("Market", "TradeDate", "Reason") VALUES (?, ?, ?)',
                             closed)

        def build(mode_: str, since: str | None) -> Sequence[_Statement]:
            return daily_procedure(self.settings, filters, since=since if mode_ == MODE_INCREMENTAL else None)

        # the calendar hook fills the closed-days table right after it is created
        return self._run(PROC_DAILY, mode, parameters, build, hooks={"closed_days": fill_closed_days})

    def promote_weekly(self, full: bool | None = None, ticker: str | None = None) -> PromotionResult:
        """``promote_weekly``: aggregate the daily table into one row per stock and ISO week."""
        return self._promote_period(PROC_WEEKLY, weekly_procedure, full, ticker)

    def promote_monthly(self, full: bool | None = None, ticker: str | None = None) -> PromotionResult:
        """``promote_monthly``: aggregate the daily table into one row per stock and month."""
        return self._promote_period(PROC_MONTHLY, monthly_procedure, full, ticker)

    def _promote_period(self, procedure: str, proc: Callable, full: bool | None, ticker: str | None) -> PromotionResult:
        filters = {"ticker": (ticker or "").strip() or None}
        # a filtered run takes everything it selects (like promote_daily) and is no watermark
        mode = MODE_FULL if (full or filters["ticker"]) else MODE_INCREMENTAL
        parameters = {**filters, "full": bool(full)}

        def fill_open_days(conn: sqlite3.Connection) -> None:
            """step 3b: the market-open days over the span of the periods, per market (No_Trading_Day),
            between the earliest and the latest trade date loaded for the market: days before the data
            history begins or after the latest delivery cannot be missing."""
            spans = conn.execute(f'SELECT COALESCE("Market", \'\'), MIN("PeriodStart"), MAX("PeriodEnd") '
                                 f'FROM {_T_ROWS} GROUP BY 1').fetchall()
            loaded = {m: (lo, hi) for m, lo, hi in conn.execute(
                f'SELECT COALESCE("Market", \'\'), MIN("TradeDate"), MAX("TradeDate") '
                f'FROM {_q(self.settings.daily_table)} GROUP BY 1').fetchall()}
            rows = []
            for market, start, end in spans:
                lo, hi = loaded.get(market, (start, end))
                day, last = date.fromisoformat(max(start, lo)), date.fromisoformat(min(end, hi))
                while day <= last:
                    if self.calendar.is_open(market or None, day, conn):
                        rows.append((market, day.isoformat()))
                    day += timedelta(days=1)
            conn.executemany(f'INSERT OR IGNORE INTO {_T_OPEN_DAYS} ("Market", "TradeDate") VALUES (?, ?)', rows)

        def build(mode_: str, since: str | None) -> Sequence[_Statement]:
            return proc(self.settings, filters, since=since if mode_ == MODE_INCREMENTAL else None,
                        full=mode_ == MODE_FULL)

        return self._run(procedure, mode, parameters, build, hooks={"open_days": fill_open_days})

    def promote_all(self, full: bool | None = None) -> list[PromotionResult]:
        """daily -> weekly -> monthly; stops at the first failure (the later ones depend on it)."""
        results = [self.promote_daily(full=full)]
        results.append(self.promote_weekly(full=full))
        results.append(self.promote_monthly(full=full))
        return results

    # -- reading the active tables ---------------------------------------------
    def _columns(self, kind: str) -> tuple[str, ...]:
        return {"daily": DAILY_COLUMNS, "weekly": WEEKLY_COLUMNS, "monthly": MONTHLY_COLUMNS}[kind]

    def _period_column(self, kind: str) -> str:
        return {"daily": "TradeDate", "weekly": "WeekStart", "monthly": "MonthStart"}[kind]

    def count_rows(self, kind: str, conn: sqlite3.Connection | None = None) -> int:
        if conn is None:
            with self.connection() as c:
                return self.count_rows(kind, c)
        self.ensure_tables(conn)
        return int(conn.execute(f"SELECT COUNT(*) FROM {_q(self.tables[kind])}").fetchone()[0])

    def summary(self, kind: str, conn: sqlite3.Connection | None = None) -> ActiveTableSummary:
        """Rows, distinct tickers, first / last period and the last run of the feeding procedure."""
        if conn is None:
            with self.connection() as c:
                return self.summary(kind, c)
        self.ensure_tables(conn)
        table, period = _q(self.tables[kind]), _q(self._period_column(kind))
        row = conn.execute(f'SELECT COUNT(*), COUNT(DISTINCT "Ticker"), MIN({period}), MAX({period}) '
                           f'FROM {table}').fetchone()
        procedure = {"daily": PROC_DAILY, "weekly": PROC_WEEKLY, "monthly": PROC_MONTHLY}[kind]
        cols = ", ".join(_q(c) for c in HISTORY_COLUMNS)
        last = conn.execute(f'SELECT {cols} FROM "{PROMOTION_HISTORY_TABLE}" WHERE "Procedure" = ? '
                            f'AND "TargetTable" = ? ORDER BY "PromotionId" DESC LIMIT 1',
                            (procedure, self.tables[kind])).fetchone()
        return ActiveTableSummary(kind, self.tables[kind], int(row[0]), int(row[1]), row[2], row[3],
                                  PromotionRecord(*last) if last else None)

    def summaries(self) -> list[ActiveTableSummary]:
        with self.connection() as conn:
            return [self.summary(k, conn) for k in ("daily", "weekly", "monthly")]

    def fetch(self, kind: str, ticker: str | None = None, date_from: str | date | None = None,
              date_to: str | date | None = None, limit: int = 100, newest_first: bool = True,
              incomplete_only: bool = False) -> list[dict]:
        """Rows of an active table as dicts (filters: ticker, period range; weekly / monthly:
        only the periods with a trading day missing)."""
        if kind not in self.tables:
            raise PromotionError(f"unknown active table kind '{kind}' (daily, weekly, monthly)")
        cols = self._columns(kind)
        table, period = _q(self.tables[kind]), _q(self._period_column(kind))
        where, params = [], []
        if ticker and ticker.strip():
            where.append('"Ticker" = ?')
            params.append(ticker.strip())
        start, end = normalise_date(date_from), normalise_date(date_to)
        if start:
            where.append(f"{period} >= ?")
            params.append(start)
        if end:
            where.append(f"{period} <= ?")
            params.append(end)
        if incomplete_only and kind != "daily":
            where.append('"Complete" = 0')
        sql = f"SELECT {', '.join(_q(c) for c in cols)} FROM {table}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f' ORDER BY {period} {"DESC" if newest_first else "ASC"}, "Ticker" LIMIT ?'
        params.append(int(limit))
        with self.connection() as conn:
            self.ensure_tables(conn)
            return [dict(zip(cols, r)) for r in conn.execute(sql, params).fetchall()]
