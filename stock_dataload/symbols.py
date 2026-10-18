"""
The stock symbol table: ``Stock_Symbol``.

One row per stock symbol and market - which market it trades on, the first
day it traded and (if it stopped) the last::

    Ticker   Market   FirstTradeDate   LastTradeDate   FirstSeen     LastSeen      Source
    AAPL     NASDAQ   1980-12-12       NULL            2023-01-03    2023-12-29    import
    HCMAU    NASDAQ   2025-08-01       NULL            2023-01-23    2023-12-04    manual
    ZYXI     NASDAQ   NULL             NULL            2023-01-03    2023-12-29    loader

* The loader **registers every symbol it loads** (source ``loader``) and keeps
  ``FirstSeen`` / ``LastSeen`` - the earliest and latest trade date loaded -
  up to date.  ``FirstTradeDate`` / ``LastTradeDate`` are *facts about the
  listing* and come from you: ``symbols set``, ``symbols import`` (CSV) or
  the dashboard.
* **Data before the first trade date** (or after the last) is invalid: the
  data quality check flags the rows (``before_first_trade_date`` /
  ``after_last_trade_date`` in ``Data_Error``) and the promotion rejects them.
* The **missing-market-day** alert is only raised for a stock on a day inside
  its trading window: before its first trade date (or after its last) no
  data is the correct state, and a stock **without a first trade date is not
  checked at all** - missing data for it is never an alert.
"""

from __future__ import annotations

import csv
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Mapping

from .audit import AuditLogger
from .config import Settings
from .db import StageDatabase, now_iso

log = logging.getLogger(__name__)

COMPONENT = "symbols"
STOCK_SYMBOL_TABLE = "Stock_Symbol"

SOURCE_LOADER = "loader"       # registered automatically while loading
SOURCE_MANUAL = "manual"       # set on the command line / dashboard
SOURCE_IMPORT = "import"       # symbols import FILE.csv

STOCK_SYMBOL_DDL = f"""
CREATE TABLE IF NOT EXISTS "{STOCK_SYMBOL_TABLE}" (
    "StockSymbolId"  INTEGER PRIMARY KEY AUTOINCREMENT,
    "Ticker"         TEXT NOT NULL COLLATE NOCASE,
    "Market"         TEXT NOT NULL DEFAULT '' COLLATE NOCASE,  -- '' = market unknown
    "Name"           TEXT,
    "FirstTradeDate" TEXT,                                    -- YYYY-MM-DD; NULL = unknown (no missing-day check)
    "LastTradeDate"  TEXT,                                    -- YYYY-MM-DD; NULL = still trading
    "FirstSeen"      TEXT,                                    -- earliest trade date loaded for the symbol
    "LastSeen"       TEXT,                                    -- latest trade date loaded
    "Source"         TEXT NOT NULL,                           -- loader | manual | import
    "Note"           TEXT,
    "CreatedAt"      TEXT NOT NULL,
    "UpdatedAt"      TEXT NOT NULL,
    CONSTRAINT "UQ_{STOCK_SYMBOL_TABLE}_Market_Ticker" UNIQUE ("Market", "Ticker"),
    CONSTRAINT "CK_{STOCK_SYMBOL_TABLE}_Dates" CHECK ("FirstTradeDate" IS NULL OR "LastTradeDate" IS NULL
                                                      OR "FirstTradeDate" <= "LastTradeDate")
)
"""
STOCK_SYMBOL_INDEX = (f'CREATE INDEX IF NOT EXISTS "IX_{STOCK_SYMBOL_TABLE}_Ticker" '
                      f'ON "{STOCK_SYMBOL_TABLE}" ("Ticker")')

SYMBOL_COLUMNS = ("StockSymbolId", "Ticker", "Market", "Name", "FirstTradeDate", "LastTradeDate", "FirstSeen",
                  "LastSeen", "Source", "Note", "CreatedAt", "UpdatedAt")

#: header names accepted by ``symbols import`` (compared without case, spaces, '-' and '_')
IMPORT_ALIASES = {
    "ticker": "ticker", "symbol": "ticker", "stock": "ticker",
    "market": "market", "exchange": "market",
    "firsttradedate": "first_trade_date", "ipo": "first_trade_date", "ipodate": "first_trade_date",
    "listed": "first_trade_date", "listingdate": "first_trade_date", "firstday": "first_trade_date",
    "lasttradedate": "last_trade_date", "delisted": "last_trade_date", "delistingdate": "last_trade_date",
    "lastday": "last_trade_date",
    "name": "name", "company": "name", "note": "note",
}


def _header_key(name: str) -> str | None:
    return IMPORT_ALIASES.get("".join(ch for ch in name.strip().strip('"').lower() if ch not in " -_"))


class SymbolError(Exception):
    """Invalid symbol / date / import file."""


@dataclass
class StockSymbol:
    stock_symbol_id: int
    ticker: str
    market: str
    name: str | None
    first_trade_date: str | None
    last_trade_date: str | None
    first_seen: str | None
    last_seen: str | None
    source: str
    note: str | None
    created_at: str
    updated_at: str

    @property
    def window(self) -> tuple[date | None, date | None]:
        first = date.fromisoformat(self.first_trade_date) if self.first_trade_date else None
        last = date.fromisoformat(self.last_trade_date) if self.last_trade_date else None
        return first, last

    @property
    def data_before_listing(self) -> bool:
        """Loaded data starts before the first trade date - something is wrong with it."""
        return bool(self.first_trade_date and self.first_seen and self.first_seen < self.first_trade_date)


@dataclass
class ImportResult:
    file: str
    rows: int = 0
    added: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        text = f"{self.file}: {self.rows} row(s) - {self.added} symbol(s) added, {self.updated} updated"
        if self.skipped:
            text += f", {self.skipped} skipped"
        if self.errors:
            text += f", {len(self.errors)} error(s)"
        return text


def render_ddl() -> str:
    return "\n".join(s.strip() + ";" for s in (STOCK_SYMBOL_DDL, STOCK_SYMBOL_INDEX)) + "\n"


def _iso(value: str | date | None, what: str) -> str | None:
    """YYYY-MM-DD / YYYYMMDD / date -> YYYY-MM-DD; '' / None -> None."""
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text or text.lower() in ("null", "none", "-", "?"):
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d", "%m/%d/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    raise SymbolError(f"{what} '{value}' is not a date (use YYYY-MM-DD)")


class SymbolRegistry:
    """Keeps and answers the ``Stock_Symbol`` table."""

    def __init__(self, settings: Settings, db: StageDatabase | None = None, audit: AuditLogger | None = None):
        self.settings = settings
        self.db = db or StageDatabase(settings)
        self.audit = audit or AuditLogger(settings)
        self._tables_ready = False

    # -- connections / DDL ---------------------------------------------------
    def connection(self):
        return self.db.connection()

    def ensure_table(self, conn: sqlite3.Connection | None = None) -> None:
        if conn is None:
            with self.connection() as c:
                self.ensure_table(c)
            return
        conn.execute(STOCK_SYMBOL_DDL)
        conn.execute(STOCK_SYMBOL_INDEX)
        self._tables_ready = True

    # -- registration by the loader ------------------------------------------
    def register_seen(self, market: str | None, seen: Mapping[str, tuple[str, str]],
                      conn: sqlite3.Connection | None = None) -> int:
        """The loader saw these symbols: ``{ticker: (earliest YYYYMMDD/ISO date, latest)}``.

        New symbols are added (source ``loader``, no trade dates); for all of
        them ``FirstSeen`` / ``LastSeen`` are widened.  Returns the number of
        symbols that were new.
        """
        if not seen:
            return 0
        if conn is None:
            with self.connection() as c:
                return self.register_seen(market, seen, c)
        self.ensure_table(conn)
        market = (market or "").strip()
        now = now_iso()
        rows = []
        for ticker, (first, last) in seen.items():
            ticker = (ticker or "").strip()
            if not ticker:
                continue
            try:
                first_iso, last_iso = _iso(first, "date"), _iso(last, "date")
            except SymbolError:
                first_iso = last_iso = None
            rows.append((ticker, market, first_iso, last_iso, now, now))
        before = int(conn.execute(f'SELECT COUNT(*) FROM "{STOCK_SYMBOL_TABLE}" WHERE "Market" = ?',
                                  (market,)).fetchone()[0])
        conn.executemany(
            f'INSERT INTO "{STOCK_SYMBOL_TABLE}" ("Ticker", "Market", "FirstSeen", "LastSeen", "Source", "CreatedAt", '
            f'"UpdatedAt") VALUES (?, ?, ?, ?, \'{SOURCE_LOADER}\', ?, ?) '
            'ON CONFLICT ("Market", "Ticker") DO UPDATE SET '
            '"FirstSeen" = CASE WHEN excluded."FirstSeen" IS NOT NULL AND ("FirstSeen" IS NULL OR excluded."FirstSeen" < "FirstSeen") '
            '                   THEN excluded."FirstSeen" ELSE "FirstSeen" END, '
            '"LastSeen" = CASE WHEN excluded."LastSeen" IS NOT NULL AND ("LastSeen" IS NULL OR excluded."LastSeen" > "LastSeen") '
            '                  THEN excluded."LastSeen" ELSE "LastSeen" END '
            'WHERE excluded."FirstSeen" IS NOT NULL AND ("FirstSeen" IS NULL OR excluded."FirstSeen" < "FirstSeen") '
            '   OR excluded."LastSeen" IS NOT NULL AND ("LastSeen" IS NULL OR excluded."LastSeen" > "LastSeen")',
            rows)
        after = int(conn.execute(f'SELECT COUNT(*) FROM "{STOCK_SYMBOL_TABLE}" WHERE "Market" = ?',
                                 (market,)).fetchone()[0])
        return after - before

    # -- look-ups --------------------------------------------------------------
    def trading_windows(self, market: str | None, tickers: Iterable[str] | None = None,
                        conn: sqlite3.Connection | None = None) -> dict[str, tuple[date | None, date | None]]:
        """``{ticker: (first trade date or None, last trade date or None)}`` of the
        market's symbols (all of them, or only *tickers*).  A symbol not in the
        table is not in the result."""
        if conn is None:
            with self.connection() as c:
                return self.trading_windows(market, tickers, c)
        self.ensure_table(conn)
        rows = conn.execute(f'SELECT "Ticker", "FirstTradeDate", "LastTradeDate" FROM "{STOCK_SYMBOL_TABLE}" '
                            f'WHERE "Market" = ?', ((market or "").strip(),)).fetchall()
        wanted = None if tickers is None else {t for t in tickers}
        out = {}
        for ticker, first, last in rows:
            if wanted is not None and ticker not in wanted:
                continue
            out[ticker] = (date.fromisoformat(first) if first else None, date.fromisoformat(last) if last else None)
        return out

    def get(self, ticker: str, market: str | None = None, conn: sqlite3.Connection | None = None) -> StockSymbol | None:
        """The symbol on *market* (or, without a market, the single symbol of that name)."""
        if conn is None:
            with self.connection() as c:
                return self.get(ticker, market, c)
        self.ensure_table(conn)
        cols = ", ".join(f'"{c}"' for c in SYMBOL_COLUMNS)
        ticker = (ticker or "").strip()
        if market is not None:
            row = conn.execute(f'SELECT {cols} FROM "{STOCK_SYMBOL_TABLE}" WHERE "Ticker" = ? AND "Market" = ?',
                               (ticker, market.strip())).fetchone()
            return StockSymbol(*row) if row else None
        rows = conn.execute(f'SELECT {cols} FROM "{STOCK_SYMBOL_TABLE}" WHERE "Ticker" = ? ORDER BY "Market"',
                            (ticker,)).fetchall()
        if len(rows) > 1:
            raise SymbolError(f"{ticker} exists on {len(rows)} markets ({', '.join(r[2] or '?' for r in rows)}) "
                              f"- say which one")
        return StockSymbol(*rows[0]) if rows else None

    def find(self, ticker: str | None = None, market: str | None = None, missing_dates: bool = False,
             suspicious: bool = False, limit: int = 200) -> list[StockSymbol]:
        """Search: ticker prefix, market, only symbols without a first trade date,
        only symbols whose loaded data starts before their first trade date."""
        cols = ", ".join(f'"{c}"' for c in SYMBOL_COLUMNS)
        where, params = [], []
        if ticker and ticker.strip():
            where.append('"Ticker" LIKE ?')
            params.append(ticker.strip().upper() + "%")
        if market and market.strip():
            where.append('"Market" = ?')
            params.append(market.strip())
        if missing_dates:
            where.append('"FirstTradeDate" IS NULL')
        if suspicious:
            where.append('"FirstTradeDate" IS NOT NULL AND "FirstSeen" IS NOT NULL AND "FirstSeen" < "FirstTradeDate"')
        sql = f'SELECT {cols} FROM "{STOCK_SYMBOL_TABLE}"'
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += ' ORDER BY "Market", "Ticker" LIMIT ?'
        params.append(int(limit))
        with self.connection() as conn:
            self.ensure_table(conn)
            return [StockSymbol(*r) for r in conn.execute(sql, params).fetchall()]

    def counts(self, market: str | None = None) -> dict[str, int]:
        where, params = "", []
        if market:
            where, params = ' WHERE "Market" = ?', [market]
        with self.connection() as conn:
            self.ensure_table(conn)
            total, dated, suspicious = conn.execute(
                f'SELECT COUNT(*), SUM("FirstTradeDate" IS NOT NULL), '
                f'SUM("FirstTradeDate" IS NOT NULL AND "FirstSeen" IS NOT NULL AND "FirstSeen" < "FirstTradeDate") '
                f'FROM "{STOCK_SYMBOL_TABLE}"{where}', params).fetchone()
        return {"symbols": int(total or 0), "with_first_trade_date": int(dated or 0),
                "without_first_trade_date": int(total or 0) - int(dated or 0), "data_before_listing": int(suspicious or 0)}

    def markets(self) -> list[str]:
        with self.connection() as conn:
            self.ensure_table(conn)
            return [r[0] for r in conn.execute(f'SELECT DISTINCT "Market" FROM "{STOCK_SYMBOL_TABLE}" ORDER BY 1')]

    # -- maintenance -------------------------------------------------------------
    def set(self, ticker: str, market: str | None = None, first_trade_date: str | date | None = ...,
            last_trade_date: str | date | None = ..., name: str | None = ..., note: str | None = ...,
            source: str = SOURCE_MANUAL) -> StockSymbol:
        """Create or update a symbol; arguments left at ``...`` are not touched.

        Pass ``None`` (or ``""``) for a date to clear it.
        """
        ticker = (ticker or "").strip().upper()
        if not ticker:
            raise SymbolError("the ticker must not be empty")
        first = _iso(first_trade_date, "first trade date") if first_trade_date is not ... else ...
        last = _iso(last_trade_date, "last trade date") if last_trade_date is not ... else ...
        with self.connection() as conn:
            self.ensure_table(conn)
            current = self.get(ticker, market, conn)
            market_value = current.market if current else (market or "").strip()
            now = now_iso()
            if current is None:
                conn.execute(f'INSERT INTO "{STOCK_SYMBOL_TABLE}" ("Ticker", "Market", "Source", "CreatedAt", "UpdatedAt") '
                             'VALUES (?, ?, ?, ?, ?)', (ticker, market_value, source, now, now))
                current = self.get(ticker, market_value, conn)
            new_first = current.first_trade_date if first is ... else first
            new_last = current.last_trade_date if last is ... else last
            if new_first and new_last and new_first > new_last:
                raise SymbolError(f"the first trade date ({new_first}) is after the last trade date ({new_last})")
            sets, params = ['"UpdatedAt" = ?', '"Source" = ?'], [now, source]
            for column, value in (("FirstTradeDate", first), ("LastTradeDate", last), ("Name", name), ("Note", note)):
                if value is not ...:
                    sets.append(f'"{column}" = ?')
                    params.append((value.strip() or None) if isinstance(value, str) and column in ("Name", "Note") else value)
            params.append(current.stock_symbol_id)
            conn.execute(f'UPDATE "{STOCK_SYMBOL_TABLE}" SET {", ".join(sets)} WHERE "StockSymbolId" = ?', params)
            updated = self.get(ticker, market_value, conn)
        self.audit.log(COMPONENT, "symbol_updated",
                       f"{updated.ticker} ({updated.market or 'no market'}): first trade {updated.first_trade_date or '-'}, "
                       f"last trade {updated.last_trade_date or '-'}",
                       details={"ticker": updated.ticker, "market": updated.market,
                                "first_trade_date": updated.first_trade_date, "last_trade_date": updated.last_trade_date,
                                "source": source})
        if updated.data_before_listing:
            log.warning("%s: loaded data starts %s, before its first trade date %s - that data is invalid "
                        "(see 'data-errors' after the next load / promotion)", updated.ticker, updated.first_seen,
                        updated.first_trade_date)
        return updated

    def delete(self, ticker: str, market: str | None = None) -> StockSymbol:
        with self.connection() as conn:
            current = self.get(ticker, market, conn)
            if current is None:
                raise SymbolError(f"unknown symbol {ticker}")
            conn.execute(f'DELETE FROM "{STOCK_SYMBOL_TABLE}" WHERE "StockSymbolId" = ?', (current.stock_symbol_id,))
        self.audit.log(COMPONENT, "symbol_deleted", f"{current.ticker} ({current.market}) removed", level="WARNING",
                       details={"ticker": current.ticker, "market": current.market})
        return current

    def import_csv(self, path: Path | str, market: str | None = None, source: str = SOURCE_IMPORT,
                   display_name: str | None = None) -> ImportResult:
        """Load first / last trade dates from a CSV file.

        Header names are flexible (``ticker``/``symbol``, ``market``/``exchange``,
        ``first_trade_date``/``ipo``/``listed``, ``last_trade_date``/``delisted``,
        ``name``); the market column is optional if *market* is given.  Bad
        lines are reported in the result, the good ones are applied.
        """
        path = Path(path)
        result = ImportResult(display_name or path.name)
        if not path.is_file():
            raise SymbolError(f"file not found: {path}")
        try:
            with open(path, "r", newline="", encoding="utf-8-sig") as fh:
                sample = fh.read(4096)
                fh.seek(0)
                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
                except csv.Error:
                    dialect = csv.excel
                reader = csv.reader(fh, dialect)
                header = next(reader, None)
                if not header:
                    raise SymbolError("the file is empty")
                mapping: dict[str, int] = {}
                for i, name in enumerate(header):
                    key = _header_key(name)
                    if key and key not in mapping:
                        mapping[key] = i
                if "ticker" not in mapping:
                    raise SymbolError(f"no ticker column in the header {header} (expected ticker / symbol)")
                if "first_trade_date" not in mapping and "last_trade_date" not in mapping:
                    raise SymbolError("no first_trade_date / last_trade_date column in the header")
                lines = list(reader)
        except OSError as exc:
            raise SymbolError(f"cannot read {path}: {exc}") from exc

        def cell(row, key):
            i = mapping.get(key)
            return row[i].strip() if i is not None and i < len(row) else ""

        for line_no, row in enumerate(lines, start=2):
            if not row or all(not c.strip() for c in row):
                continue
            result.rows += 1
            ticker = cell(row, "ticker").upper()
            row_market = cell(row, "market") or (market or "")
            if not ticker:
                result.errors.append(f"line {line_no}: ticker is empty")
                continue
            kwargs = {}
            try:
                if "first_trade_date" in mapping:
                    kwargs["first_trade_date"] = _iso(cell(row, "first_trade_date"), "first trade date")
                if "last_trade_date" in mapping:
                    kwargs["last_trade_date"] = _iso(cell(row, "last_trade_date"), "last trade date")
                if "name" in mapping and cell(row, "name"):
                    kwargs["name"] = cell(row, "name")
                if "note" in mapping and cell(row, "note"):
                    kwargs["note"] = cell(row, "note")
                existed = self.get(ticker, row_market) is not None
                self.set(ticker, row_market, source=source, **kwargs)
            except SymbolError as exc:
                result.errors.append(f"line {line_no}: {exc}")
                continue
            if existed:
                result.updated += 1
            else:
                result.added += 1
        self.audit.log(COMPONENT, "symbols_imported", str(result), level="WARNING" if result.errors else "INFO",
                       details={"file": path.name, "rows": result.rows, "added": result.added, "updated": result.updated,
                                "errors": result.errors[:20]})
        return result

    def export_csv(self, path: Path | str | None = None, market: str | None = None) -> str:
        """The table as CSV text (also written to *path* if given) - edit it and import it again."""
        import io
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(["ticker", "market", "first_trade_date", "last_trade_date", "name", "first_seen", "last_seen",
                         "source", "note"])
        for s in self.find(market=market, limit=10 ** 7):
            writer.writerow([s.ticker, s.market, s.first_trade_date or "", s.last_trade_date or "", s.name or "",
                             s.first_seen or "", s.last_seen or "", s.source, s.note or ""])
        text = out.getvalue()
        if path:
            Path(path).write_text(text, encoding="utf-8")
        return text
