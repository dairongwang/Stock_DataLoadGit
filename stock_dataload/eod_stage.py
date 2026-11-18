"""
The staging table of the **EOD API** data: ``Stock_EOD_Stage``.

The API (see :mod:`eod_api`) answers one symbol per request with a list of
end-of-day records::

    {"date":"2025-11-14","open":92.2,"high":95.08,"low":91.49,"close":92.92,
     "adjusted_close":90.2693,"volume":19632000}

(``fmt=csv`` gives the same fields as ``Date,Open,High,Low,Close,Adjusted_close,Volume``.)
Every record becomes one row; the symbol comes from the request::

    API field        column           type
    (request)        Symbol           TEXT     as requested: MRK, MRK.US, BRK-B.US
    (request)        Ticker           TEXT     the symbol without the exchange suffix: MRK
    (request)        Exchange         TEXT     the exchange suffix (US, LSE, INDX ...) or NULL
    date             TradeDate        TEXT     YYYY-MM-DD
    open             Open             REAL
    high             High             REAL
    low              Low              REAL
    close            Close            REAL
    adjusted_close   AdjustedClose    REAL     close adjusted for splits and dividends
    volume           Volume           INTEGER

plus the provenance ``SourceFile`` (the saved response), ``LoadId``,
``LoadedAt`` (first loaded) and ``UpdatedAt`` (last changed).

Unlike ``Stock_Stage`` (which lands delivery files as text) the columns are
typed: the API already delivers numbers, and a record that is not a valid
date / number is rejected instead of staged.

**One row per symbol and trade date** (``UNIQUE (Symbol, TradeDate)``): every
API call returns the whole requested history, so responses overlap.  A load
*upserts* - new dates are inserted, dates whose values changed (e.g. a new
``adjusted_close`` after a dividend) are updated, identical ones are left
alone - and reports the three counts.  A load is one transaction.

Rejected records (missing field, not a number, negative volume, a date twice
in one response, ...) are written to ``Data_Error`` (rule ``eod_record``).
By default one bad record fails the whole response (nothing is loaded);
``skip_bad_rows`` loads the valid records and reports the others.  Every
load - successful or failed - is registered in ``Load_History`` (status
``loaded`` / ``failed``; a later load of the same file does not mark the
earlier one ``replaced`` - the rows are upserted per day, not per file).

**A stock is loaded at most 5 times a day** (:mod:`load_limits`): every successful load is
registered in ``Eod_Stock_Load`` in the same transaction; a 6th load of the same stock (ticker) on
the same day is *refused* (status ``refused``, nothing written, a WARNING in ``App_Log``).

    python -m stock_dataload eod-fetch MRK                        # call the API, save, load
    python -m stock_dataload eod-load docs/SampleAPI_JasonData.txt --symbol MRK
    python -m stock_dataload eod-stage                            # what is staged, per symbol
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .config import Settings
from .db import (HISTORY_FAILED, HISTORY_LOADED, DatabaseError, StageDatabase, file_sha256, now_iso,
                 quote_identifier as _q)

log = logging.getLogger(__name__)

DEFAULT_EOD_STAGE_TABLE = "Stock_EOD_Stage"
COMPONENT = "eod_stage"
RULE = "eod_record"

#: (API field, table column) in API order
EOD_FIELDS: tuple[tuple[str, str], ...] = (
    ("date", "TradeDate"), ("open", "Open"), ("high", "High"), ("low", "Low"), ("close", "Close"),
    ("adjusted_close", "AdjustedClose"), ("volume", "Volume"),
)
PRICE_FIELDS = ("open", "high", "low", "close", "adjusted_close")

#: columns of the table in insert order (without the EodStageId key)
EOD_STAGE_COLUMNS: tuple[str, ...] = ("Symbol", "Ticker", "Exchange", "TradeDate", "Open", "High", "Low", "Close",
                                      "AdjustedClose", "Volume", "SourceFile", "LoadId", "LoadedAt", "UpdatedAt")
_VALUE_COLUMNS = ("Open", "High", "Low", "Close", "AdjustedClose", "Volume")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9.\-_^=]{1,32}$")
#: the default download name {symbol}_{timestamp}.{ext}: MRK.US_20251114_101500.json -> MRK.US
_FILE_SYMBOL_RE = re.compile(r"^(?P<symbol>[A-Za-z0-9.\-_^=]+?)_\d{8}_\d{6}\.[A-Za-z0-9]+$")
#: a file named after the stock only: PFE.json, BRK-B.US.json (no "_" - names like SampleAPI_JasonData.txt do not count)
_PLAIN_SYMBOL_RE = re.compile(r"^(?P<symbol>[A-Za-z0-9.\-^=]{1,20})\.(?:json|csv|txt)$", re.IGNORECASE)

_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    "EodStageId"    INTEGER PRIMARY KEY AUTOINCREMENT,
    "Symbol"        TEXT NOT NULL,              -- as requested from the API: MRK, MRK.US
    "Ticker"        TEXT NOT NULL,              -- the symbol without the exchange suffix: MRK
    "Exchange"      TEXT,                       -- the exchange suffix (US, LSE, ...) or NULL
    "TradeDate"     TEXT NOT NULL,              -- API field "date" (YYYY-MM-DD)
    "Open"          REAL NOT NULL,              -- "open"
    "High"          REAL NOT NULL,              -- "high"
    "Low"           REAL NOT NULL,              -- "low"
    "Close"         REAL NOT NULL,              -- "close"
    "AdjustedClose" REAL NOT NULL,              -- "adjusted_close" (splits and dividends)
    "Volume"        INTEGER NOT NULL,           -- "volume"
    "SourceFile"    TEXT NOT NULL,              -- the saved API response the values came from
    "LoadId"        TEXT NOT NULL,              -- one id per load
    "LoadedAt"      TEXT NOT NULL,              -- first loaded
    "UpdatedAt"     TEXT NOT NULL,              -- last changed by a later response
    CONSTRAINT {uq} UNIQUE ("Symbol", "TradeDate"),
    CONSTRAINT {ck_date} CHECK ("TradeDate" GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    CONSTRAINT {ck_volume} CHECK ("Volume" >= 0)
)
"""

_INDEXES = (
    'CREATE INDEX IF NOT EXISTS {ix_ticker} ON {table} ("Ticker", "TradeDate")',
    'CREATE INDEX IF NOT EXISTS {ix_source} ON {table} ("SourceFile")',
)


def _names(table: str) -> dict[str, str]:
    return {"table": _q(table), "uq": _q(f"UQ_{table}_Symbol_TradeDate"), "ck_date": _q(f"CK_{table}_TradeDate"),
            "ck_volume": _q(f"CK_{table}_Volume"), "ix_ticker": _q(f"IX_{table}_Ticker_TradeDate"),
            "ix_source": _q(f"IX_{table}_SourceFile")}


def render_ddl(table: str = DEFAULT_EOD_STAGE_TABLE) -> str:
    """The DDL as a script (``sql/create_eod_stage_sqlite.sql``)."""
    names = _names(table)
    return "\n".join(s.format(**names).strip() + ";" for s in (_DDL, *_INDEXES)) + "\n"


class EodStageError(Exception):
    """The load cannot start (file missing, no symbol, bad arguments)."""


#: EodLoadResult.status of a load the daily limit of the stock refused (nothing was written)
LOAD_REFUSED = "refused"


class _LimitReached(Exception):
    def __init__(self, used: int):
        self.used = used


# ---------------------------------------------------------------------------
# symbols
# ---------------------------------------------------------------------------
def split_symbol(symbol: str) -> tuple[str, str | None]:
    """``MRK.US`` -> ``("MRK", "US")``; ``MRK`` -> ``("MRK", None)``; ``BRK-B.US`` -> ``("BRK-B", "US")``."""
    ticker, dot, exchange = symbol.rpartition(".")
    if dot and ticker and exchange.isalnum():
        return ticker, exchange
    return symbol, None


def clean_symbol(symbol: str | None) -> str:
    text = (symbol or "").strip().upper()
    if not _SYMBOL_RE.match(text):
        raise EodStageError(f"'{symbol}' is not a valid symbol (letters, digits and . - _ ^ = only, e.g. MRK or MRK.US)")
    return text


def symbol_from_filename(name: str) -> str | None:
    """The symbol of a saved response: the default name ``{symbol}_{timestamp}.{ext}`` (``eod-fetch``),
    or a file named after the stock only, ``{symbol}.json`` / ``.csv`` / ``.txt`` (e.g. ``PFE.json``)."""
    base = Path(name).name
    m = _FILE_SYMBOL_RE.match(base) or _PLAIN_SYMBOL_RE.match(base)
    return m.group("symbol").upper() if m else None


# ---------------------------------------------------------------------------
# parsing + validation
# ---------------------------------------------------------------------------
@dataclass
class EodRecord:
    trade_date: str
    open: float
    high: float
    low: float
    close: float
    adjusted_close: float
    volume: int

    def values(self) -> tuple:
        return (self.open, self.high, self.low, self.close, self.adjusted_close, self.volume)


@dataclass
class RejectedRecord:
    number: int               # 1-based position in the response (CSV: the line number)
    trade_date: str
    column: str
    value: str
    message: str

    def __str__(self) -> str:
        return f"record {self.number}" + (f" ({self.trade_date})" if self.trade_date else "") + f": {self.message}"


def detect_format(text: str, hint: str | None = None) -> str:
    """``json`` or ``csv`` - from *hint* (fmt / file extension) if it is one of them, else sniffed."""
    hint = (hint or "").lower().lstrip(".")
    if hint in ("json", "csv"):
        return hint
    return "json" if text.lstrip()[:1] in ("[", "{") else "csv"


def parse_response(text: str, fmt: str) -> tuple[list[dict], int]:
    """The raw records of an API response and the number of the first record's line / position.

    JSON: a list of objects (or ``{"data": [...]}``); CSV: a header line with the field names
    (any case).  Raises ``ValueError`` if the body is not such a document."""
    if fmt == "json":
        data = json.loads(text)
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            data = data["data"]
        if not isinstance(data, list):
            detail = ""
            if isinstance(data, dict):
                detail = " - the API said: " + str(data.get("message") or data.get("error") or data.get("errors") or data)[:200]
            raise ValueError("the response is not a list of end-of-day records" + detail)
        return data, 1
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return [], 2
    reader.fieldnames = [(f or "").strip().lower() for f in reader.fieldnames]
    missing = [f for f, _ in EOD_FIELDS if f not in reader.fieldnames]
    if missing:
        raise ValueError(f"the CSV header {reader.fieldnames} lacks {missing}")
    rows = [r for r in reader if any((v or "").strip() for v in r.values() if isinstance(v, str))]
    return rows, 2


def _number(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("not a number")
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            number = float(value.strip())
        except ValueError:
            raise ValueError("not a number") from None
    else:
        raise ValueError("missing" if value in (None, "") else "not a number")
    if not math.isfinite(number):
        raise ValueError("not a finite number")
    return number


def validate_records(raw: Sequence[Any], first_number: int = 1) -> tuple[list[EodRecord], list[RejectedRecord]]:
    """Typed records + the rejected ones (with the reason).  A date that appears twice in one
    response is rejected the second time."""
    good: list[EodRecord] = []
    bad: list[RejectedRecord] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        number = first_number + i
        if not isinstance(item, Mapping):
            bad.append(RejectedRecord(number, "", "", str(item)[:60], "the record is not an object"))
            continue
        rec = {str(k).strip().lower(): v for k, v in item.items()}
        trade_date = str(rec.get("date") or "").strip()
        problems: list[tuple[str, str, str]] = []
        if not _DATE_RE.match(trade_date):
            problems.append(("date", trade_date, "date must look like YYYY-MM-DD"))
        else:
            try:
                date.fromisoformat(trade_date)
            except ValueError:
                problems.append(("date", trade_date, f"'{trade_date}' is not a calendar date"))
        values: dict[str, float | int] = {}
        for name in PRICE_FIELDS:
            try:
                values[name] = _number(rec.get(name))
                if values[name] < 0:
                    problems.append((name, str(rec.get(name)), f"{name} must not be negative"))
            except ValueError as exc:
                problems.append((name, "" if rec.get(name) is None else str(rec.get(name)), f"{name} is {exc}"))
        try:
            volume = _number(rec.get("volume"))
            if volume != int(volume):
                problems.append(("volume", str(rec.get("volume")), "volume must be a whole number"))
            elif volume < 0:
                problems.append(("volume", str(rec.get("volume")), "volume must not be negative"))
            values["volume"] = int(volume)
        except ValueError as exc:
            problems.append(("volume", "" if rec.get("volume") is None else str(rec.get("volume")), f"volume is {exc}"))
        if not problems and trade_date in seen:
            problems.append(("date", trade_date, f"{trade_date} appears more than once in the response"))
        if problems:
            for column, value, message in problems:
                bad.append(RejectedRecord(number, trade_date, column, value, message))
            continue
        seen.add(trade_date)
        good.append(EodRecord(trade_date, values["open"], values["high"], values["low"], values["close"],
                              values["adjusted_close"], int(values["volume"])))
    return good, bad


# ---------------------------------------------------------------------------
# the load
# ---------------------------------------------------------------------------
@dataclass
class EodLoadResult:
    symbol: str
    source_file: str
    load_id: str
    status: str = HISTORY_LOADED                # loaded | failed
    received: int = 0                           # records in the response
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    rejected: list[RejectedRecord] = field(default_factory=list)
    first_date: str | None = None
    last_date: str | None = None
    message: str = ""
    table: str = DEFAULT_EOD_STAGE_TABLE

    @property
    def ok(self) -> bool:
        return self.status == HISTORY_LOADED

    @property
    def refused(self) -> bool:
        """The daily limit of the stock refused the load (not an error of the data)."""
        return self.status == LOAD_REFUSED

    @property
    def rejected_records(self) -> int:
        return len({r.number for r in self.rejected})

    @property
    def rows_loaded(self) -> int:
        return self.inserted + self.updated + self.unchanged

    def __str__(self) -> str:
        if self.refused:
            return f"{self.symbol}: {self.source_file} NOT loaded into {self.table} (refused) - {self.message}"
        if not self.ok:
            return f"{self.symbol}: {self.source_file} NOT loaded into {self.table} - {self.message}"
        span = f" {self.first_date} .. {self.last_date}," if self.first_date else ""
        text = (f"{self.symbol}: {self.received:,} record(s){span} loaded into {self.table} - "
                f"{self.inserted:,} inserted, {self.updated:,} updated, {self.unchanged:,} unchanged")
        if self.rejected:
            text += f", {self.rejected_records:,} rejected (see data-errors)"
        return text


@dataclass
class EodSymbolSummary:
    symbol: str
    ticker: str
    exchange: str | None
    rows: int
    first_date: str
    last_date: str
    last_loaded: str


class EodStageLoader:
    """Loads EOD API responses into the ``[eod_api] stage_table`` (``Stock_EOD_Stage``)."""

    def __init__(self, settings: Settings, db: StageDatabase | None = None, audit=None, data_errors=None):
        from .load_limits import StockLoadLedger
        self.settings = settings
        self.db = db or StageDatabase(settings)
        self.ledger = StockLoadLedger(self.db)
        self.table_name = getattr(settings, "eod_stage_table", DEFAULT_EOD_STAGE_TABLE) or DEFAULT_EOD_STAGE_TABLE
        self.table = _q(self.table_name)
        self.audit = audit
        self.data_errors = data_errors

    # -- DDL ----------------------------------------------------------------------
    def ensure_table(self, conn=None) -> None:
        if conn is None:
            with self.db.connection() as c:
                self.ensure_table(c)
            return
        names = _names(self.table_name)
        conn.execute(_DDL.format(**names))
        for ddl in _INDEXES:
            conn.execute(ddl.format(**names))

    # -- loading ------------------------------------------------------------------
    def load_file(self, path: str | Path, symbol: str | None = None, fmt: str | None = None,
                  skip_bad_rows: bool | None = None) -> EodLoadResult:
        """Load a saved API response (JSON or CSV).  The symbol is *symbol*, else taken from a
        file name like ``MRK_20251114_101500.json``."""
        path = Path(path)
        if not path.is_file():
            raise EodStageError(f"file not found: {path}")
        symbol = symbol or symbol_from_filename(path.name)
        if not symbol:
            raise EodStageError(f"which symbol is in {path.name}? The API response has no symbol field - "
                                f"give it with --symbol (e.g. --symbol MRK)")
        symbol = clean_symbol(symbol)
        raw_bytes = path.read_bytes()
        text = raw_bytes.decode("utf-8-sig", errors="replace")
        return self._load(symbol, text, fmt or path.suffix, path.name, file_hash=file_sha256(path),
                          file_size=len(raw_bytes), skip_bad_rows=skip_bad_rows)

    def load_text(self, symbol: str, text: str, fmt: str = "json", source_file: str = "",
                  skip_bad_rows: bool | None = None) -> EodLoadResult:
        """Load a response body that is already in memory."""
        symbol = clean_symbol(symbol)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return self._load(symbol, text, fmt, source_file or f"{symbol}_response.{fmt}", file_hash=digest,
                          file_size=len(text.encode("utf-8")), skip_bad_rows=skip_bad_rows)

    def _load(self, symbol: str, text: str, fmt_hint: str | None, source_file: str, file_hash: str,
              file_size: int | None, skip_bad_rows: bool | None) -> EodLoadResult:
        skip = self.settings.skip_bad_rows if skip_bad_rows is None else skip_bad_rows
        result = EodLoadResult(symbol, source_file, uuid.uuid4().hex, table=self.table_name)
        fmt = detect_format(text, fmt_hint)
        try:
            raw, first = parse_response(text, fmt)
        except ValueError as exc:
            return self._fail(result, f"the {fmt.upper()} response cannot be read: {exc}", file_hash, file_size)
        result.received = len(raw)
        records, rejected = validate_records(raw, first)
        result.rejected = rejected
        if rejected and not skip:
            return self._fail(result, f"{result.rejected_records} of {len(raw)} record(s) are invalid "
                                      f"(first: {rejected[0]}) - nothing was loaded; fix the data or load with "
                                      f"--skip-bad-rows", file_hash, file_size)
        ticker, exchange = split_symbol(symbol)
        now = now_iso()
        try:
            with self.db.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")           # read-compare-write as one unit
                self.ensure_table(conn)
                used = self.ledger.count(symbol, conn=conn)     # the daily limit of the stock, checked atomically
                if used >= self.ledger.limit:
                    raise _LimitReached(used)
                existing = {r[0]: tuple(r[1:]) for r in conn.execute(
                    f'SELECT "TradeDate", {", ".join(_q(c) for c in _VALUE_COLUMNS)} FROM {self.table} '
                    f'WHERE "Symbol" = ?', (symbol,))}
                inserts, updates = [], []
                for rec in records:
                    old = existing.get(rec.trade_date)
                    if old is None:
                        inserts.append((symbol, ticker, exchange, rec.trade_date, *rec.values(), source_file,
                                        result.load_id, now, now))
                    elif tuple(old) != rec.values():
                        updates.append((*rec.values(), source_file, result.load_id, now, symbol, rec.trade_date))
                    else:
                        result.unchanged += 1
                cols = ", ".join(_q(c) for c in EOD_STAGE_COLUMNS)
                conn.executemany(f"INSERT INTO {self.table} ({cols}) VALUES ({', '.join('?' * len(EOD_STAGE_COLUMNS))})",
                                 inserts)
                sets = ", ".join(f"{_q(c)} = ?" for c in (*_VALUE_COLUMNS, "SourceFile", "LoadId", "UpdatedAt"))
                conn.executemany(f'UPDATE {self.table} SET {sets} WHERE "Symbol" = ? AND "TradeDate" = ?', updates)
                result.inserted, result.updated = len(inserts), len(updates)
                if records:
                    dates = [r.trade_date for r in records]
                    result.first_date, result.last_date = min(dates), max(dates)
                result.message = str(result)
                # every load stays 'loaded' in Load_History: rows are upserted, not replaced per file
                self.db.record_load_history(source_file, file_hash, result.load_id, HISTORY_LOADED,
                                            rows_loaded=len(records), market=exchange or "",
                                            message=result.message, file_size=file_size, conn=conn)
                self.ledger.record(conn, symbol, result.load_id, source_file, len(records), now)
        except _LimitReached as exc:
            return self._refuse(result, exc.used)
        except DatabaseError:
            raise
        except Exception as exc:  # noqa: BLE001 - sqlite3 errors etc.: recorded, reported
            return self._fail(result, f"database problem: {exc}", file_hash, file_size)
        self._record_rejected(result)
        log.info("%s", result)
        if self.audit is not None:
            self.audit.log(COMPONENT, "eod_loaded", result.message, file_name=source_file, load_id=result.load_id,
                           level="WARNING" if result.rejected else "INFO",
                           details={"symbol": symbol, "table": self.table_name, "received": result.received,
                                    "inserted": result.inserted, "updated": result.updated,
                                    "unchanged": result.unchanged, "rejected": result.rejected_records})
        return result

    def _refuse(self, result: EodLoadResult, used: int) -> EodLoadResult:
        """The stock reached its daily limit: nothing written, a WARNING (not an error) in App_Log."""
        from .load_limits import StockLoadLimitError, audit_refusal, stock_refusal, ticker_of
        ticker = ticker_of(result.symbol)
        result.status = LOAD_REFUSED
        result.message = stock_refusal({ticker: used}, self.ledger.limit)
        result.inserted = result.updated = result.unchanged = 0
        result.rejected = []
        audit_refusal(self.audit, COMPONENT, StockLoadLimitError(result.message, [ticker]), symbol=result.symbol,
                      ticker=ticker, loads_today=used, limit=self.ledger.limit, source_file=result.source_file)
        return result

    def _fail(self, result: EodLoadResult, message: str, file_hash: str, file_size: int | None) -> EodLoadResult:
        result.status, result.message = HISTORY_FAILED, message
        try:
            self.db.record_load_history(result.source_file, file_hash, result.load_id, HISTORY_FAILED,
                                        market=split_symbol(result.symbol)[1] or "", message=f"{self.table_name}: {message}",
                                        file_size=file_size)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not register the failed load in Load_History: %s", exc)
        self._record_rejected(result)
        log.info("%s", result)                        # the audit trail / the caller report it
        if self.audit is not None:
            self.audit.error(COMPONENT, "eod_load", message, file_name=result.source_file,
                             load_id=result.load_id, details={"symbol": result.symbol, "table": self.table_name})
        return result

    def _record_rejected(self, result: EodLoadResult) -> None:
        if not result.rejected or self.data_errors is None:
            return
        from .quality import QualityIssue
        issues = [QualityIssue(RULE, f"{self.table_name}: {r.message}" + ("" if result.ok else " (response not loaded)"),
                               ticker=result.symbol, trade_date=r.trade_date, column=r.column, value=r.value,
                               line_no=r.number) for r in result.rejected]
        try:
            self.data_errors.record(issues, source_file=result.source_file, load_id=result.load_id,
                                    market=split_symbol(result.symbol)[1] or "")
        except Exception as exc:  # noqa: BLE001
            log.warning("could not record the rejected records in Data_Error: %s", exc)

    # -- reading ------------------------------------------------------------------
    def summary(self) -> list[EodSymbolSummary]:
        """Per symbol: rows, first / last trade date, last load."""
        with self.db.connection() as conn:
            self.ensure_table(conn)
            return [EodSymbolSummary(*r) for r in conn.execute(
                f'SELECT "Symbol", MIN("Ticker"), MIN("Exchange"), COUNT(*), MIN("TradeDate"), MAX("TradeDate"), '
                f'MAX("UpdatedAt") FROM {self.table} GROUP BY "Symbol" ORDER BY "Symbol"').fetchall()]

    def rows(self, symbol: str, date_from: str | None = None, date_to: str | None = None,
             limit: int = 50, newest_first: bool = True) -> list[dict]:
        where, params = ['"Symbol" = ?'], [clean_symbol(symbol)]
        if date_from:
            where.append('"TradeDate" >= ?')
            params.append(date_from)
        if date_to:
            where.append('"TradeDate" <= ?')
            params.append(date_to)
        cols = ("EodStageId",) + EOD_STAGE_COLUMNS
        sql = (f'SELECT {", ".join(_q(c) for c in cols)} FROM {self.table} WHERE {" AND ".join(where)} '
               f'ORDER BY "TradeDate" {"DESC" if newest_first else "ASC"} LIMIT ?')
        with self.db.connection() as conn:
            self.ensure_table(conn)
            return [dict(zip(cols, r)) for r in conn.execute(sql, params + [int(limit)]).fetchall()]

    def count(self, symbol: str | None = None) -> int:
        with self.db.connection() as conn:
            self.ensure_table(conn)
            if symbol:
                return int(conn.execute(f'SELECT COUNT(*) FROM {self.table} WHERE "Symbol" = ?',
                                        (clean_symbol(symbol),)).fetchone()[0])
            return int(conn.execute(f"SELECT COUNT(*) FROM {self.table}").fetchone()[0])

    def delete_symbol(self, symbol: str) -> int:
        """Remove every staged row of *symbol*; returns how many."""
        with self.db.connection() as conn:
            self.ensure_table(conn)
            n = conn.execute(f'DELETE FROM {self.table} WHERE "Symbol" = ?', (clean_symbol(symbol),)).rowcount
        if self.audit is not None and n:
            self.audit.log(COMPONENT, "eod_deleted", f"{n} staged row(s) of {clean_symbol(symbol)} deleted from "
                           f"{self.table_name}", level="WARNING", details={"symbol": clean_symbol(symbol), "rows": n})
        return int(n or 0)


def load_many(loader: EodStageLoader, paths: Iterable[str | Path], symbol: str | None = None,
              fmt: str | None = None, skip_bad_rows: bool | None = None) -> list[EodLoadResult | EodStageError]:
    out: list[EodLoadResult | EodStageError] = []
    for p in paths:
        try:
            out.append(loader.load_file(p, symbol, fmt, skip_bad_rows))
        except EodStageError as exc:
            out.append(exc)
    return out
