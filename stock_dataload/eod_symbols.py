"""
The staging table of the **exchange symbol lists**: ``Stock_Symbol_Stage``.

The exchange-symbol-list API (see :func:`eod_api.fetch_symbol_list`) answers
one market per request with the symbols traded there::

    GET https://eodhd.com/api/exchange-symbol-list/NYSE?api_token=...&fmt=json
    [{"Code":"MRK","Name":"Merck & Company Inc","Country":"USA","Exchange":"NYSE",
      "Currency":"USD","Type":"Common Stock","Isin":"US58933Y1055"}, ...]

(``fmt=csv`` gives the same fields as a header line.)  Every record becomes one
row per market and code::

    (request)  Market      TEXT   the market the list was requested for: NYSE
    Code       Code        TEXT   the ticker on that market: MRK, BRK-B
    Name       Name        TEXT   company / fund name
    Country    Country     TEXT
    Exchange   Exchange    TEXT   the venue of the record - a NYSE list also holds NYSE ARCA / NYSE MKT
    Currency   Currency    TEXT
    Type       Type        TEXT   Common Stock, ETF, Preferred Stock, FUND, Notes, Warrant, Unit, ...
    Isin       Isin        TEXT   may be empty

plus ``SourceFile``, ``LoadId``, ``FirstSeenAt`` (the first list with the
code), ``LastSeenAt`` (the latest list with it) and ``UpdatedAt`` (the last
change of a value).  ``UNIQUE (Market, Code)``: loading a newer list of the
same market *upserts* - new codes are inserted, changed names / types /
ISINs updated, identical ones only get their ``LastSeenAt`` - and codes of
earlier lists that are missing from the new one are *reported*, not
deleted (a list requested with ``type=etf`` is partial).

Rejected records (no code, a code with blanks, a code twice) are written to
``Data_Error`` (rule ``symbol_record``); by default one bad record fails the
whole list, ``skip_bad_rows`` loads the valid ones.  Every load is
registered in ``Load_History``.

    python -m stock_dataload eod-symbols-fetch NYSE                 # call the API, save, load
    python -m stock_dataload eod-symbols-load docs/SampleAPI_NYSE_List.json --market NYSE
    python -m stock_dataload eod-symbols --q merck                  # search the staged lists
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .config import Settings
from .db import HISTORY_FAILED, HISTORY_LOADED, DatabaseError, StageDatabase, file_sha256, now_iso
from .db import quote_identifier as _q

log = logging.getLogger(__name__)

DEFAULT_SYMBOL_STAGE_TABLE = "Stock_Symbol_Stage"
COMPONENT = "eod_symbols"
RULE = "symbol_record"

#: (API field, column) - the attributes of a symbol, in API order
SYMBOL_FIELDS: tuple[tuple[str, str], ...] = (
    ("Code", "Code"), ("Name", "Name"), ("Country", "Country"), ("Exchange", "Exchange"),
    ("Currency", "Currency"), ("Type", "Type"), ("Isin", "Isin"),
)
_ATTRIBUTE_COLUMNS = ("Name", "Country", "Exchange", "Currency", "Type", "Isin")
SYMBOL_STAGE_COLUMNS: tuple[str, ...] = ("Market", "Code", *_ATTRIBUTE_COLUMNS, "SourceFile", "LoadId",
                                         "FirstSeenAt", "LastSeenAt", "UpdatedAt")

_MARKET_RE = re.compile(r"^[A-Za-z0-9.\-_]{1,20}$")
_CODE_RE = re.compile(r"^\S{1,40}$")
#: NYSE_List.json, NYSE_symbols_20251114_101500.json, nyse-list.csv, SampleAPI_NYSE_List.json -> NYSE
#: (the name part right before "_list" / "_symbols")
_FILE_MARKET_RE = re.compile(r"^(?:.*[_\-])?(?P<market>[A-Za-z0-9.]+?)[_\-](?:symbols|list)(?:[_\-].*)?\.[A-Za-z0-9]+$",
                             re.I)

_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    "SymbolStageId" INTEGER PRIMARY KEY AUTOINCREMENT,
    "Market"        TEXT NOT NULL,              -- the market the list was requested for: NYSE
    "Code"          TEXT NOT NULL,              -- "Code": the ticker on that market (MRK, BRK-B)
    "Name"          TEXT,                       -- "Name": company / fund name
    "Country"       TEXT,                       -- "Country"
    "Exchange"      TEXT,                       -- "Exchange": the venue (NYSE, NYSE ARCA, NYSE MKT ...)
    "Currency"      TEXT,                       -- "Currency"
    "Type"          TEXT,                       -- "Type": Common Stock, ETF, Preferred Stock, FUND ...
    "Isin"          TEXT,                       -- "Isin" (may be empty)
    "SourceFile"    TEXT NOT NULL,              -- the saved list the values came from
    "LoadId"        TEXT NOT NULL,              -- the load that saw the code last
    "FirstSeenAt"   TEXT NOT NULL,              -- first list that contained the code
    "LastSeenAt"    TEXT NOT NULL,              -- latest list that contained the code
    "UpdatedAt"     TEXT NOT NULL,              -- last change of a value
    CONSTRAINT {uq} UNIQUE ("Market", "Code")
)
"""

_INDEXES = (
    'CREATE INDEX IF NOT EXISTS {ix_code} ON {table} ("Code")',
    'CREATE INDEX IF NOT EXISTS {ix_type} ON {table} ("Market", "Type")',
)


def _names(table: str) -> dict[str, str]:
    return {"table": _q(table), "uq": _q(f"UQ_{table}_Market_Code"), "ix_code": _q(f"IX_{table}_Code"),
            "ix_type": _q(f"IX_{table}_Market_Type")}


def render_ddl(table: str = DEFAULT_SYMBOL_STAGE_TABLE) -> str:
    """The DDL as a script (``sql/create_symbol_stage_sqlite.sql``)."""
    names = _names(table)
    return "\n".join(s.format(**names).strip() + ";" for s in (_DDL, *_INDEXES)) + "\n"


class SymbolStageError(Exception):
    """The load cannot start (file missing, no market, bad arguments)."""


def clean_market(market: str | None) -> str:
    text = (market or "").strip().upper()
    if not _MARKET_RE.match(text):
        raise SymbolStageError(f"'{market}' is not a valid market code (letters, digits and . - _ only, e.g. NYSE)")
    return text


def market_from_filename(name: str) -> str | None:
    """``NYSE_List.json`` / ``NYSE_symbols_20251114_101500.json`` -> ``NYSE``."""
    m = _FILE_MARKET_RE.match(Path(name).name)
    return m.group("market").upper() if m else None


# ---------------------------------------------------------------------------
# parsing + validation
# ---------------------------------------------------------------------------
@dataclass
class SymbolRecord:
    code: str
    name: str | None
    country: str | None
    exchange: str | None
    currency: str | None
    type: str | None
    isin: str | None

    def values(self) -> tuple:
        return (self.name, self.country, self.exchange, self.currency, self.type, self.isin)


@dataclass
class RejectedSymbol:
    number: int               # 1-based position in the list (CSV: the line number)
    code: str
    column: str
    value: str
    message: str

    def __str__(self) -> str:
        return f"record {self.number}" + (f" ({self.code})" if self.code else "") + f": {self.message}"


def detect_format(text: str, hint: str | None = None) -> str:
    hint = (hint or "").lower().lstrip(".")
    if hint in ("json", "csv"):
        return hint
    return "json" if text.lstrip()[:1] in ("[", "{") else "csv"


def parse_list(text: str, fmt: str) -> tuple[list, int]:
    """The raw records of a list and the number of the first one's line / position.
    Raises ``ValueError`` if the body is not such a document."""
    if fmt == "json":
        data = json.loads(text)
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            data = data["data"]
        if not isinstance(data, list):
            detail = ""
            if isinstance(data, dict):
                detail = " - the API said: " + str(data.get("message") or data.get("error") or data.get("errors") or data)[:200]
            raise ValueError("the response is not a list of symbols" + detail)
        return data, 1
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return [], 2
    fields = [(f or "").strip() for f in reader.fieldnames]
    if "code" not in {f.lower() for f in fields}:
        raise ValueError(f"the CSV header {fields} has no Code column")
    reader.fieldnames = fields
    return [r for r in reader if any((v or "").strip() for v in r.values() if isinstance(v, str))], 2


def _text(value: Any) -> str | None:
    """The value as delivered, without leading / trailing blanks; empty -> None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def validate_symbols(raw: Sequence[Any], first_number: int = 1) -> tuple[list[SymbolRecord], list[RejectedSymbol]]:
    """Clean records + the rejected ones.  A code that appears twice is rejected the second time."""
    good: list[SymbolRecord] = []
    bad: list[RejectedSymbol] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        number = first_number + i
        if not isinstance(item, Mapping):
            bad.append(RejectedSymbol(number, "", "", str(item)[:60], "the record is not an object"))
            continue
        rec = {str(k).strip().lower(): v for k, v in item.items()}
        raw_code = rec.get("code")
        code = str(raw_code).strip() if raw_code is not None else ""
        if not code:
            bad.append(RejectedSymbol(number, "", "Code", "", "the code is missing"))
            continue
        if not _CODE_RE.match(code):
            bad.append(RejectedSymbol(number, code, "Code", code[:60], f"the code '{code[:60]}' contains blanks or is "
                                                                        f"longer than 40 characters"))
            continue
        if code.upper() in seen:
            bad.append(RejectedSymbol(number, code, "Code", code, f"the code {code} appears more than once in the list"))
            continue
        seen.add(code.upper())
        isin = _text(rec.get("isin"))
        good.append(SymbolRecord(code, _text(rec.get("name")), _text(rec.get("country")), _text(rec.get("exchange")),
                                 _text(rec.get("currency")), _text(rec.get("type")), isin.upper() if isin else None))
    return good, bad


# ---------------------------------------------------------------------------
# the load
# ---------------------------------------------------------------------------
@dataclass
class SymbolLoadResult:
    market: str
    source_file: str
    load_id: str
    status: str = HISTORY_LOADED                 # loaded | failed
    received: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    not_in_list: int = 0                         # codes of earlier lists of the market missing from this one
    rejected: list[RejectedSymbol] = field(default_factory=list)
    types: dict[str, int] = field(default_factory=dict)
    message: str = ""
    table: str = DEFAULT_SYMBOL_STAGE_TABLE

    @property
    def ok(self) -> bool:
        return self.status == HISTORY_LOADED

    @property
    def rejected_records(self) -> int:
        return len({r.number for r in self.rejected})

    def __str__(self) -> str:
        if not self.ok:
            return f"{self.market}: {self.source_file} NOT loaded into {self.table} - {self.message}"
        text = (f"{self.market}: {self.received:,} symbol(s) loaded into {self.table} - {self.inserted:,} inserted, "
                f"{self.updated:,} updated, {self.unchanged:,} unchanged")
        if self.rejected:
            text += f", {self.rejected_records:,} rejected (see data-errors)"
        if self.not_in_list:
            text += f"; {self.not_in_list:,} code(s) of earlier lists are not in this one"
        return text


@dataclass
class MarketSummary:
    market: str
    codes: int
    types: dict[str, int]
    exchanges: dict[str, int]
    first_seen: str
    last_loaded: str
    missing_from_latest: int


class SymbolListLoader:
    """Loads exchange symbol lists into ``[eod_api] symbol_stage_table`` (``Stock_Symbol_Stage``)."""

    def __init__(self, settings: Settings, db: StageDatabase | None = None, audit=None, data_errors=None):
        self.settings = settings
        self.db = db or StageDatabase(settings)
        self.table_name = getattr(settings, "eod_symbol_stage_table", DEFAULT_SYMBOL_STAGE_TABLE) \
            or DEFAULT_SYMBOL_STAGE_TABLE
        self.table = _q(self.table_name)
        self.audit = audit
        self.data_errors = data_errors

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
    def load_file(self, path: str | Path, market: str | None = None, fmt: str | None = None,
                  skip_bad_rows: bool | None = None) -> SymbolLoadResult:
        """Load a saved list (JSON or CSV).  The market is *market*, else taken from a file name like
        ``NYSE_List.json`` / ``NYSE_symbols_20251114_101500.json``."""
        path = Path(path)
        if not path.is_file():
            raise SymbolStageError(f"file not found: {path}")
        market = market or market_from_filename(path.name)
        if not market:
            raise SymbolStageError(f"which market is {path.name} a list of? The records carry no market - give it "
                                   f"with --market (e.g. --market NYSE)")
        market = clean_market(market)
        raw = path.read_bytes()
        return self._load(market, raw.decode("utf-8-sig", errors="replace"), fmt or path.suffix, path.name,
                          file_sha256(path), len(raw), skip_bad_rows)

    def load_text(self, market: str, text: str, fmt: str = "json", source_file: str = "",
                  skip_bad_rows: bool | None = None) -> SymbolLoadResult:
        market = clean_market(market)
        data = text.encode("utf-8")
        return self._load(market, text, fmt, source_file or f"{market}_symbols.{fmt}",
                          hashlib.sha256(data).hexdigest(), len(data), skip_bad_rows)

    def _load(self, market: str, text: str, fmt_hint: str | None, source_file: str, file_hash: str,
              file_size: int | None, skip_bad_rows: bool | None) -> SymbolLoadResult:
        skip = self.settings.skip_bad_rows if skip_bad_rows is None else skip_bad_rows
        result = SymbolLoadResult(market, source_file, uuid.uuid4().hex, table=self.table_name)
        fmt = detect_format(text, fmt_hint)
        try:
            raw, first = parse_list(text, fmt)
        except ValueError as exc:
            return self._fail(result, f"the {fmt.upper()} list cannot be read: {exc}", file_hash, file_size)
        result.received = len(raw)
        records, rejected = validate_symbols(raw, first)
        result.rejected = rejected
        if rejected and not skip:
            return self._fail(result, f"{result.rejected_records} of {len(raw)} record(s) are invalid "
                                      f"(first: {rejected[0]}) - nothing was loaded; fix the list or load with "
                                      f"--skip-bad-rows", file_hash, file_size)
        for r in records:
            key = r.type or "(none)"
            result.types[key] = result.types.get(key, 0) + 1
        now = now_iso()
        try:
            with self.db.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self.ensure_table(conn)
                existing = {r[0]: tuple(r[1:]) for r in conn.execute(
                    f'SELECT "Code", {", ".join(_q(c) for c in _ATTRIBUTE_COLUMNS)} FROM {self.table} '
                    f'WHERE "Market" = ?', (market,))}
                inserts, updates, seen = [], [], []
                for rec in records:
                    old = existing.get(rec.code)
                    if old is None:
                        inserts.append((market, rec.code, *rec.values(), source_file, result.load_id, now, now, now))
                    elif tuple(old) != rec.values():
                        updates.append((*rec.values(), source_file, result.load_id, now, now, market, rec.code))
                    else:
                        seen.append((source_file, result.load_id, now, market, rec.code))
                cols = ", ".join(_q(c) for c in SYMBOL_STAGE_COLUMNS)
                conn.executemany(f"INSERT INTO {self.table} ({cols}) VALUES ({', '.join('?' * len(SYMBOL_STAGE_COLUMNS))})",
                                 inserts)
                sets = ", ".join(f"{_q(c)} = ?" for c in (*_ATTRIBUTE_COLUMNS, "SourceFile", "LoadId", "LastSeenAt",
                                                          "UpdatedAt"))
                conn.executemany(f'UPDATE {self.table} SET {sets} WHERE "Market" = ? AND "Code" = ?', updates)
                conn.executemany(f'UPDATE {self.table} SET "SourceFile" = ?, "LoadId" = ?, "LastSeenAt" = ? '
                                 f'WHERE "Market" = ? AND "Code" = ?', seen)
                result.inserted, result.updated, result.unchanged = len(inserts), len(updates), len(seen)
                in_list = {r.code for r in records}
                result.not_in_list = sum(1 for code in existing if code not in in_list)
                result.message = str(result)
                self.db.record_load_history(source_file, file_hash, result.load_id, HISTORY_LOADED,
                                            rows_loaded=len(records), market=market, message=result.message,
                                            file_size=file_size, conn=conn)
        except DatabaseError:
            raise
        except Exception as exc:  # noqa: BLE001 - sqlite3 errors etc.: recorded, reported
            return self._fail(result, f"database problem: {exc}", file_hash, file_size)
        self._record_rejected(result)
        log.info("%s", result)
        if self.audit is not None:
            self.audit.log(COMPONENT, "symbols_loaded", result.message, file_name=source_file, load_id=result.load_id,
                           level="WARNING" if result.rejected else "INFO",
                           details={"market": market, "table": self.table_name, "received": result.received,
                                    "inserted": result.inserted, "updated": result.updated,
                                    "unchanged": result.unchanged, "not_in_list": result.not_in_list,
                                    "rejected": result.rejected_records, "types": result.types})
        return result

    def _fail(self, result: SymbolLoadResult, message: str, file_hash: str, file_size: int | None) -> SymbolLoadResult:
        result.status, result.message = HISTORY_FAILED, message
        try:
            self.db.record_load_history(result.source_file, file_hash, result.load_id, HISTORY_FAILED,
                                        market=result.market, message=f"{self.table_name}: {message}",
                                        file_size=file_size)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not register the failed load in Load_History: %s", exc)
        self._record_rejected(result)
        log.info("%s", result)
        if self.audit is not None:
            self.audit.error(COMPONENT, "symbols_load", message, file_name=result.source_file,
                             load_id=result.load_id, details={"market": result.market, "table": self.table_name})
        return result

    def _record_rejected(self, result: SymbolLoadResult) -> None:
        if not result.rejected or self.data_errors is None:
            return
        from .quality import QualityIssue
        issues = [QualityIssue(RULE, f"{self.table_name}: {r.message}" + ("" if result.ok else " (list not loaded)"),
                               ticker=r.code, column=r.column, value=r.value, line_no=r.number)
                  for r in result.rejected]
        try:
            self.data_errors.record(issues, source_file=result.source_file, load_id=result.load_id,
                                    market=result.market)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not record the rejected symbols in Data_Error: %s", exc)

    # -- reading ------------------------------------------------------------------
    def summary(self) -> list[MarketSummary]:
        """Per market: codes, codes per type / exchange, first seen, last load, codes missing from the latest list."""
        with self.db.connection() as conn:
            self.ensure_table(conn)
            out = []
            for market, codes, first, last in conn.execute(
                    f'SELECT "Market", COUNT(*), MIN("FirstSeenAt"), MAX("LastSeenAt") FROM {self.table} '
                    f'GROUP BY "Market" ORDER BY "Market"').fetchall():
                types = dict(conn.execute(f'SELECT COALESCE("Type", \'(none)\'), COUNT(*) FROM {self.table} '
                                          f'WHERE "Market" = ? GROUP BY 1 ORDER BY 2 DESC', (market,)).fetchall())
                exchanges = dict(conn.execute(f'SELECT COALESCE("Exchange", \'(none)\'), COUNT(*) FROM {self.table} '
                                              f'WHERE "Market" = ? GROUP BY 1 ORDER BY 2 DESC', (market,)).fetchall())
                missing = conn.execute(f'SELECT COUNT(*) FROM {self.table} WHERE "Market" = ? AND "LastSeenAt" < ?',
                                       (market, last)).fetchone()[0]
                out.append(MarketSummary(market, int(codes), types, exchanges, first, last, int(missing)))
            return out

    def search(self, q: str | None = None, market: str | None = None, type_: str | None = None,
               limit: int = 50) -> list[dict]:
        """Symbols whose code starts with *q* or whose name contains it (exact code first)."""
        where, params = [], []
        if market:
            where.append('"Market" = ?')
            params.append(clean_market(market))
        if type_:
            where.append('"Type" = ? COLLATE NOCASE')
            params.append(type_.strip())
        order = '"Market", "Code"'
        if q and q.strip():
            text = q.strip()
            where.append('("Code" LIKE ? OR "Name" LIKE ?)')
            params += [text.replace("%", "").upper() + "%", f"%{text}%"]
            order = f'CASE WHEN UPPER("Code") = ? THEN 0 WHEN "Code" LIKE ? THEN 1 ELSE 2 END, {order}'
            params_order = [text.upper(), text.upper() + "%"]
        else:
            params_order = []
        cols = ("Market", "Code", *_ATTRIBUTE_COLUMNS, "FirstSeenAt", "LastSeenAt", "SourceFile")
        sql = (f'SELECT {", ".join(_q(c) for c in cols)} FROM {self.table}'
               + (" WHERE " + " AND ".join(where) if where else "") + f" ORDER BY {order} LIMIT ?")
        with self.db.connection() as conn:
            self.ensure_table(conn)
            return [dict(zip(cols, r)) for r in conn.execute(sql, params + params_order + [int(limit)]).fetchall()]

    def get(self, code: str, market: str | None = None) -> list[dict]:
        """The staged rows of *code* (one per market that lists it)."""
        return [r for r in self.search(code, market, limit=50) if r["Code"].upper() == code.strip().upper()]

    def count(self, market: str | None = None) -> int:
        with self.db.connection() as conn:
            self.ensure_table(conn)
            if market:
                return int(conn.execute(f'SELECT COUNT(*) FROM {self.table} WHERE "Market" = ?',
                                        (clean_market(market),)).fetchone()[0])
            return int(conn.execute(f"SELECT COUNT(*) FROM {self.table}").fetchone()[0])

    def delete_market(self, market: str) -> int:
        market = clean_market(market)
        with self.db.connection() as conn:
            self.ensure_table(conn)
            n = conn.execute(f'DELETE FROM {self.table} WHERE "Market" = ?', (market,)).rowcount
        if self.audit is not None and n:
            self.audit.log(COMPONENT, "symbols_deleted", f"{n} staged symbol(s) of {market} deleted from "
                           f"{self.table_name}", level="WARNING", details={"market": market, "rows": n})
        return int(n or 0)


def load_many(loader: SymbolListLoader, paths: Iterable[str | Path], market: str | None = None,
              fmt: str | None = None, skip_bad_rows: bool | None = None) -> list[SymbolLoadResult | SymbolStageError]:
    out: list[SymbolLoadResult | SymbolStageError] = []
    for p in paths:
        try:
            out.append(loader.load_file(p, market, fmt, skip_bad_rows))
        except SymbolStageError as exc:
            out.append(exc)
    return out
