"""
Data quality checks for loaded stock data files (strategy design pattern).

Every business rule is one *strategy*; the :class:`DataQualityChecker`
runs a configurable list of strategies, so rules can be added / removed
without touching the checker or the loader::

    checker = DataQualityChecker()                  # the default rule set
    checker.remove_strategy("high_above_low")       # drop a rule
    checker.add_strategy(MyVolumeLimitStrategy())   # add your own

Two kinds of strategies:

* :class:`RowValidationStrategy`     - looks at a single row
  (``date_is_valid``, ``numeric_columns``, ``high_above_low``);
* :class:`DatasetValidationStrategy` - looks at the whole file in context
  (``missing_market_day``: a stock known from previous loads that has no
  valid row for a day the market was open -> **alert**).

Findings are :class:`QualityIssue` objects; the :class:`DataErrorRepository`
persists them in the ``Data_Error`` table.
"""

from __future__ import annotations

import abc
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable, Iterable, Iterator, Mapping, Sequence

from . import formats, market_calendar
from .config import Settings

log = logging.getLogger(__name__)

DATA_ERROR_TABLE = "Data_Error"

SEVERITY_ERROR = "error"
SEVERITY_ALERT = "alert"


# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------
@dataclass
class DataRow:
    """One data row of a file, as delivered (all values text)."""
    line_no: int
    values: dict[str, str]     # keys: ticker, date, open, high, low, close, vol

    @classmethod
    def from_sequence(cls, line_no: int, row: Sequence[str]) -> "DataRow":
        values = {col: (row[i].strip() if i < len(row) else "") for i, col in enumerate(formats.DEFAULT_COLUMNS)}
        return cls(line_no, values)

    @property
    def ticker(self) -> str:
        return self.values.get("ticker", "")

    @property
    def trade_date(self) -> str:
        return self.values.get("date", "")


@dataclass
class QualityIssue:
    """One data quality finding."""
    rule: str
    message: str
    severity: str = SEVERITY_ERROR      # "error" | "alert"
    ticker: str = ""
    trade_date: str = ""
    column: str = ""
    value: str = ""
    line_no: int | None = None

    @property
    def alert(self) -> bool:
        return self.severity == SEVERITY_ALERT

    def __str__(self) -> str:
        where = f" line {self.line_no}" if self.line_no else ""
        who = f" {self.ticker}" if self.ticker else ""
        return f"[{self.rule}]{who}{where}: {self.message}"


@dataclass
class QualityContext:
    """Everything a strategy may need besides the rows themselves."""
    market: str = ""
    source_file: str = ""
    load_id: str = ""
    #: tickers expected to deliver data (e.g. seen in previous loads of the market)
    expected_tickers: frozenset[str] = frozenset()
    #: additional (ad-hoc) market closures from config.ini
    extra_holidays: frozenset[date] = frozenset()
    #: market calendar - replaceable for other exchanges / in tests
    market_open: Callable[[date], bool] | None = None
    #: (ticker, date) keys of rows that failed a row-level rule; set by the checker
    invalid_row_keys: set[tuple[str, str]] = field(default_factory=set)

    def is_market_open(self, day: date) -> bool:
        if self.market_open is not None:
            return self.market_open(day)
        return market_calendar.is_market_open(day, self.extra_holidays)


# ---------------------------------------------------------------------------
# strategy interfaces
# ---------------------------------------------------------------------------
class ValidationStrategy(abc.ABC):
    """Base class of all data quality validation strategies."""

    #: unique rule name - used in Data_Error.Rule and for add/remove
    name: str = ""
    description: str = ""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} name={self.name!r}>"


class RowValidationStrategy(ValidationStrategy):
    """A rule evaluated against every single row."""

    @abc.abstractmethod
    def validate_row(self, row: DataRow, context: QualityContext) -> list[QualityIssue]:
        """Return the issues found in *row* (empty list = ok)."""


class DatasetValidationStrategy(ValidationStrategy):
    """A rule evaluated against the whole file (with context)."""

    @abc.abstractmethod
    def validate_dataset(self, rows: Sequence[DataRow], context: QualityContext) -> list[QualityIssue]:
        """Return the issues found across *rows* (empty list = ok)."""


# ---------------------------------------------------------------------------
# built-in strategies (the business rules)
# ---------------------------------------------------------------------------
def _parse_number(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_trade_date(value: str) -> date | None:
    try:
        return datetime.strptime(value.strip(), formats.DEFAULT_DATE_FORMAT).date()
    except (TypeError, ValueError):
        return None


class DateIsValidStrategy(RowValidationStrategy):
    """``date`` must be real calendar date (YYYYMMDD)."""

    name = "date_is_valid"
    description = "the date column must contain a valid YYYYMMDD calendar date"

    def validate_row(self, row: DataRow, context: QualityContext) -> list[QualityIssue]:
        value = row.trade_date
        if _parse_trade_date(value) is None:
            return [QualityIssue(self.name, f"date '{value}' is not a valid YYYYMMDD calendar date",
                                 ticker=row.ticker, trade_date=value, column="date", value=value,
                                 line_no=row.line_no)]
        return []


class NumericColumnsStrategy(RowValidationStrategy):
    """``open,high,low,close,vol`` must be numbers."""

    name = "numeric_columns"
    description = "open, high, low, close and vol must be numeric"

    #: the columns checked - override/extend when subclassing
    columns: tuple[str, ...] = ("open", "high", "low", "close", "vol")

    def validate_row(self, row: DataRow, context: QualityContext) -> list[QualityIssue]:
        issues = []
        for column in self.columns:
            value = row.values.get(column, "")
            if not value:
                issues.append(QualityIssue(self.name, f"{column} is missing", ticker=row.ticker,
                                           trade_date=row.trade_date, column=column, value=value,
                                           line_no=row.line_no))
            elif _parse_number(value) is None:
                issues.append(QualityIssue(self.name, f"{column} '{value}' is not a number",
                                           ticker=row.ticker, trade_date=row.trade_date, column=column,
                                           value=value, line_no=row.line_no))
        return issues


class HighAboveLowStrategy(RowValidationStrategy):
    """Within a row, ``high`` must not be smaller than ``low``."""

    name = "high_above_low"
    description = "high must be bigger than (or equal to) low on the same row"

    def validate_row(self, row: DataRow, context: QualityContext) -> list[QualityIssue]:
        high = _parse_number(row.values.get("high", ""))
        low = _parse_number(row.values.get("low", ""))
        if high is None or low is None:    # numeric_columns reports these
            return []
        if high < low:
            return [QualityIssue(self.name,
                                 f"high ({row.values['high']}) is smaller than low ({row.values['low']})",
                                 ticker=row.ticker, trade_date=row.trade_date, column="high",
                                 value=f"high={row.values['high']},low={row.values['low']}",
                                 line_no=row.line_no)]
        return []


class MissingMarketDayStrategy(DatasetValidationStrategy):
    """ALERT: a known stock has no valid data for a day the market was open.

    "Known" stocks are ``context.expected_tickers`` (the loader fills this
    with every ticker previously loaded for the same market).  A stock is
    covered for a day only by a row that passed all row-level rules -
    a stock whose only row is broken is as missing as an absent one.
    """

    name = "missing_market_day"
    description = "every known stock must deliver valid data for each market-open day"

    def validate_dataset(self, rows: Sequence[DataRow], context: QualityContext) -> list[QualityIssue]:
        if not context.expected_tickers:
            return []
        issues: list[QualityIssue] = []
        trade_dates = sorted({r.trade_date for r in rows if _parse_trade_date(r.trade_date)})
        for date_str in trade_dates:
            day = _parse_trade_date(date_str)
            if day is None or not context.is_market_open(day):
                continue
            covered = {r.ticker for r in rows
                       if r.ticker and r.trade_date == date_str
                       and (r.ticker, date_str) not in context.invalid_row_keys}
            for ticker in sorted(context.expected_tickers - covered):
                issues.append(QualityIssue(
                    self.name,
                    f"no valid data for {ticker} on {day.isoformat()} although the market was open",
                    severity=SEVERITY_ALERT, ticker=ticker, trade_date=date_str, column="", value="",
                ))
        return issues


def default_strategies() -> list[ValidationStrategy]:
    """The standard business rule set."""
    return [DateIsValidStrategy(), NumericColumnsStrategy(), HighAboveLowStrategy(),
            MissingMarketDayStrategy()]


# ---------------------------------------------------------------------------
# the checker (strategy context)
# ---------------------------------------------------------------------------
class DataQualityChecker:
    """Runs a configurable list of validation strategies over a file's rows."""

    def __init__(self, strategies: Iterable[ValidationStrategy] | None = None):
        self._strategies: list[ValidationStrategy] = (
            list(strategies) if strategies is not None else default_strategies())

    @property
    def strategies(self) -> tuple[ValidationStrategy, ...]:
        return tuple(self._strategies)

    def strategy_names(self) -> list[str]:
        return [s.name for s in self._strategies]

    def add_strategy(self, strategy: ValidationStrategy) -> None:
        if not isinstance(strategy, (RowValidationStrategy, DatasetValidationStrategy)):
            raise TypeError("strategy must subclass RowValidationStrategy or DatasetValidationStrategy")
        if not strategy.name:
            raise ValueError("strategy needs a name")
        if strategy.name in self.strategy_names():
            raise ValueError(f"a strategy named '{strategy.name}' is already registered")
        self._strategies.append(strategy)

    def remove_strategy(self, name: str) -> bool:
        """Remove the strategy called *name*; True if it was registered."""
        for s in list(self._strategies):
            if s.name == name:
                self._strategies.remove(s)
                return True
        return False

    def check(self, rows: Sequence[DataRow], context: QualityContext | None = None) -> list[QualityIssue]:
        """Run all strategies; row rules first, then dataset rules."""
        context = context or QualityContext()
        issues: list[QualityIssue] = []
        row_rules = [s for s in self._strategies if isinstance(s, RowValidationStrategy)]
        dataset_rules = [s for s in self._strategies if isinstance(s, DatasetValidationStrategy)]
        for row in rows:
            for rule in row_rules:
                found = rule.validate_row(row, context)
                if found:
                    issues.extend(found)
                    if row.ticker:
                        context.invalid_row_keys.add((row.ticker, row.trade_date))
        for rule in dataset_rules:
            issues.extend(rule.validate_dataset(rows, context))
        return issues


# ---------------------------------------------------------------------------
# persistence: the Data_Error table
# ---------------------------------------------------------------------------
_DATA_ERROR_DDL = f"""
CREATE TABLE IF NOT EXISTS "{DATA_ERROR_TABLE}" (
    "DataErrorId" INTEGER PRIMARY KEY AUTOINCREMENT,
    "DetectedAt"  TEXT NOT NULL,
    "SourceFile"  TEXT,
    "LoadId"      TEXT,
    "Market"      TEXT,
    "Ticker"      TEXT,
    "TradeDate"   TEXT,
    "ColumnName"  TEXT,
    "Value"       TEXT,
    "Rule"        TEXT NOT NULL,
    "Severity"    TEXT NOT NULL,
    "Alert"       INTEGER NOT NULL DEFAULT 0,
    "ErrorMessage" TEXT NOT NULL,
    "LineNo"      INTEGER,
    "Resolved"    INTEGER NOT NULL DEFAULT 0,
    "ResolvedAt"  TEXT,
    "ResolutionNote" TEXT
)
"""

#: columns added after the first release - added to existing databases on the fly
_DATA_ERROR_MIGRATIONS = (
    ("Resolved", '"Resolved" INTEGER NOT NULL DEFAULT 0'),
    ("ResolvedAt", '"ResolvedAt" TEXT'),
    ("ResolutionNote", '"ResolutionNote" TEXT'),
)

_DATA_ERROR_INDEX = (f'CREATE INDEX IF NOT EXISTS "IX_{DATA_ERROR_TABLE}_SourceFile" '
                     f'ON "{DATA_ERROR_TABLE}" ("SourceFile")')

DATA_ERROR_COLUMNS = ("DataErrorId", "DetectedAt", "SourceFile", "LoadId", "Market", "Ticker",
                      "TradeDate", "ColumnName", "Value", "Rule", "Severity", "Alert",
                      "ErrorMessage", "LineNo", "Resolved", "ResolvedAt", "ResolutionNote")


@dataclass
class DataErrorRecord:
    data_error_id: int
    detected_at: str
    source_file: str | None
    load_id: str | None
    market: str | None
    ticker: str | None
    trade_date: str | None
    column_name: str | None
    value: str | None
    rule: str
    severity: str
    alert: int
    error_message: str
    line_no: int | None
    resolved: int = 0
    resolved_at: str | None = None
    resolution_note: str | None = None


class DataErrorRepository:
    """Stores :class:`QualityIssue` findings in the ``Data_Error`` table."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._table_ready = False

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        path = self.settings.sqlite_path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def ensure_table(self, conn: sqlite3.Connection | None = None) -> None:
        if conn is None:
            with self.connection() as c:
                self.ensure_table(c)
            return
        conn.execute(_DATA_ERROR_DDL)
        conn.execute(_DATA_ERROR_INDEX)
        existing = {r[1] for r in conn.execute(f'PRAGMA table_info("{DATA_ERROR_TABLE}")')}
        for column, ddl in _DATA_ERROR_MIGRATIONS:
            if column not in existing:
                conn.execute(f'ALTER TABLE "{DATA_ERROR_TABLE}" ADD COLUMN {ddl}')
        self._table_ready = True

    def record(self, issues: Iterable[QualityIssue], source_file: str = "", load_id: str = "",
               market: str = "") -> int:
        """Persist *issues*; returns the number of rows written."""
        detected = datetime.now().replace(microsecond=0).isoformat(sep=" ")
        rows = [(detected, source_file or None, load_id or None, market or None,
                 i.ticker or None, i.trade_date or None, i.column or None, i.value or None,
                 i.rule, i.severity, 1 if i.alert else 0, i.message, i.line_no)
                for i in issues]
        if not rows:
            return 0
        with self.connection() as conn:
            if not self._table_ready:
                self.ensure_table(conn)
            conn.executemany(
                f'INSERT INTO "{DATA_ERROR_TABLE}" '
                '("DetectedAt","SourceFile","LoadId","Market","Ticker","TradeDate","ColumnName",'
                '"Value","Rule","Severity","Alert","ErrorMessage","LineNo") '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', rows)
        return len(rows)

    @staticmethod
    def _where(source_file: str | None, alerts_only: bool, open_only: bool) -> tuple[str, list]:
        where, params = [], []
        if source_file:
            where.append('"SourceFile" = ?')
            params.append(source_file)
        if alerts_only:
            where.append('"Alert" = 1')
        if open_only:
            where.append('"Resolved" = 0')
        return (" WHERE " + " AND ".join(where)) if where else "", params

    def fetch(self, limit: int = 100, source_file: str | None = None,
              alerts_only: bool = False, open_only: bool = False) -> list[DataErrorRecord]:
        cols = ", ".join(f'"{c}"' for c in DATA_ERROR_COLUMNS)
        where, params = self._where(source_file, alerts_only, open_only)
        sql = f'SELECT {cols} FROM "{DATA_ERROR_TABLE}"{where} ORDER BY "DataErrorId" DESC LIMIT ?'
        params.append(int(limit))
        with self.connection() as conn:
            self.ensure_table(conn)
            return [DataErrorRecord(*r) for r in conn.execute(sql, params).fetchall()]

    def count(self, source_file: str | None = None, alerts_only: bool = False,
              open_only: bool = False) -> int:
        where, params = self._where(source_file, alerts_only, open_only)
        with self.connection() as conn:
            self.ensure_table(conn)
            return int(conn.execute(f'SELECT COUNT(*) FROM "{DATA_ERROR_TABLE}"{where}', params).fetchone()[0])

    def get(self, data_error_id: int) -> DataErrorRecord | None:
        cols = ", ".join(f'"{c}"' for c in DATA_ERROR_COLUMNS)
        with self.connection() as conn:
            self.ensure_table(conn)
            row = conn.execute(f'SELECT {cols} FROM "{DATA_ERROR_TABLE}" WHERE "DataErrorId" = ?',
                               (int(data_error_id),)).fetchone()
        return DataErrorRecord(*row) if row else None

    def resolve(self, data_error_ids: Iterable[int] | None = None, note: str = "",
                source_file: str | None = None, ticker: str | None = None,
                trade_date: str | None = None, conn: sqlite3.Connection | None = None) -> int:
        """Mark data errors as resolved - by id and/or every open finding for a
        (source file, ticker, trade date) row.  Returns the number resolved."""
        if conn is None:
            with self.connection() as c:
                return self.resolve(data_error_ids, note, source_file, ticker, trade_date, c)
        self.ensure_table(conn)
        now = datetime.now().replace(microsecond=0).isoformat(sep=" ")
        sql = (f'UPDATE "{DATA_ERROR_TABLE}" SET "Resolved" = 1, "ResolvedAt" = ?, "ResolutionNote" = ? '
               'WHERE "Resolved" = 0 AND ')
        total = 0
        ids = [int(i) for i in (data_error_ids or [])]
        if ids:
            marks = ",".join("?" for _ in ids)
            cur = conn.execute(sql + f'"DataErrorId" IN ({marks})', [now, note or None, *ids])
            total += max(cur.rowcount or 0, 0)
        if ticker and trade_date:
            where, params = ['"Ticker" = ?', '"TradeDate" = ?'], [ticker, trade_date]
            if source_file:
                where.append('"SourceFile" = ?')
                params.append(source_file)
            cur = conn.execute(sql + " AND ".join(where), [now, note or None, *params])
            total += max(cur.rowcount or 0, 0)
        return total
