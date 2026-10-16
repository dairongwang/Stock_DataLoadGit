"""
SQLite database layer for the staging table.

Uses only the standard library ``sqlite3`` module - no drivers, no server.
The database is a single file (``sqlite_path`` in config.ini).

The staging table keeps every column as text - exactly like the original
``Stock_Stage`` table in the SSIS package.  A staging table's job is to land
the file *as delivered*; typing/cleansing belongs to the next step of the
pipeline.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from .config import Settings

log = logging.getLogger(__name__)

#: Staging table columns, in insert order.
STAGE_COLUMNS: tuple[str, ...] = (
    "Market",      # derived from the file name (NASDAQ_20250123.txt -> NASDAQ) or --market
    "Ticker",
    "TradeDate",
    "Open",
    "High",
    "Low",
    "Close",
    "Vol",
    "SourceFile",  # name of the file the row came from
    "LoadId",      # one id per load_file() call - lets you trace/undo a load
    "LoadedAt",    # ISO timestamp
)

#: Staging columns a user may change on the data correction page.
STAGE_EDITABLE_COLUMNS: tuple[str, ...] = ("Market", "Ticker", "TradeDate", "Open", "High", "Low", "Close", "Vol")

#: LoadId used for rows added by hand on the data correction page.
MANUAL_LOAD_ID = "manual_correction"

# Map from default file column -> staging column
FILE_TO_STAGE = {
    "ticker": "Ticker",
    "date": "TradeDate",
    "open": "Open",
    "high": "High",
    "low": "Low",
    "close": "Close",
    "vol": "Vol",
}

_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    "StageId"    INTEGER PRIMARY KEY AUTOINCREMENT,
    "Market"     TEXT,
    "Ticker"     TEXT NOT NULL,
    "TradeDate"  TEXT NOT NULL,
    "Open"       TEXT,
    "High"       TEXT,
    "Low"        TEXT,
    "Close"      TEXT,
    "Vol"        TEXT,
    "SourceFile" TEXT NOT NULL,
    "LoadId"     TEXT NOT NULL,
    "LoadedAt"   TEXT NOT NULL
)
"""

_INDEX = 'CREATE INDEX IF NOT EXISTS "IX_{name}_SourceFile" ON {table} ("SourceFile")'

#: Registry of every load attempt (file name, sha-256 content hash, outcome).
#: Re-loading a file deletes its previous rows first, so only one copy of a
#: file ever sits in the staging table; the registry keeps the full story.
LOAD_HISTORY_TABLE = "Load_History"

#: Load_History.Status values
HISTORY_LOADED = "loaded"
HISTORY_FAILED = "failed"
HISTORY_REPLACED = "replaced"    # a later load of the same file superseded this one

_HISTORY_DDL = f"""
CREATE TABLE IF NOT EXISTS "{LOAD_HISTORY_TABLE}" (
    "LoadHistoryId" INTEGER PRIMARY KEY AUTOINCREMENT,
    "FileName"      TEXT NOT NULL,
    "FileHash"      TEXT NOT NULL,
    "FileSize"      INTEGER,
    "LoadId"        TEXT NOT NULL,
    "Market"        TEXT,
    "Status"        TEXT NOT NULL,
    "RowsLoaded"    INTEGER NOT NULL DEFAULT 0,
    "Message"       TEXT,
    "LoadedAt"      TEXT NOT NULL
)
"""

_HISTORY_INDEX = (f'CREATE INDEX IF NOT EXISTS "IX_{LOAD_HISTORY_TABLE}_FileHash" '
                  f'ON "{LOAD_HISTORY_TABLE}" ("FileHash")')

HISTORY_COLUMNS = ("LoadHistoryId", "FileName", "FileHash", "FileSize", "LoadId", "Market",
                   "Status", "RowsLoaded", "Message", "LoadedAt")


class DatabaseError(Exception):
    """Raised for database connectivity/execution problems."""


def file_sha256(path: Path | str) -> str:
    """SHA-256 content hash of *path* - identifies a file regardless of its name."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class LoadHistoryRecord:
    """One row of the Load_History table."""
    history_id: int
    file_name: str
    file_hash: str
    file_size: int | None
    load_id: str
    market: str | None
    status: str
    rows_loaded: int
    message: str | None
    loaded_at: str


@dataclass
class StageRow:
    """One row destined for the staging table (all values kept as text)."""
    market: str
    ticker: str
    trade_date: str
    open: str
    high: str
    low: str
    close: str
    vol: str
    source_file: str
    load_id: str
    loaded_at: str

    def as_tuple(self) -> tuple:
        return (self.market, self.ticker, self.trade_date, self.open, self.high, self.low,
                self.close, self.vol, self.source_file, self.load_id, self.loaded_at)


def quote_identifier(name: str) -> str:
    """Quote an SQL identifier (double quotes, embedded quotes doubled)."""
    return '"' + name.replace('"', '""') + '"'


class StageDatabase:
    """Connection factory + the handful of statements the loader needs."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.q = quote_identifier
        self.table = self.q(settings.stage_table)
        cols = ", ".join(self.q(c) for c in STAGE_COLUMNS)
        marks = ", ".join("?" for _ in STAGE_COLUMNS)
        self._insert_sql = f"INSERT INTO {self.table} ({cols}) VALUES ({marks})"

    # -- connections -------------------------------------------------------
    def _connect_raw(self) -> sqlite3.Connection:
        path = self.settings.sqlite_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path), isolation_level="DEFERRED")
            conn.execute("PRAGMA journal_mode=WAL")
            return conn
        except (OSError, sqlite3.Error) as exc:
            raise DatabaseError(f"could not open sqlite database {path}: {exc}") from exc

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection; commit on success, rollback on error."""
        conn = self._connect_raw()
        try:
            yield conn
            conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except Exception:  # pragma: no cover - best effort
                log.exception("rollback failed")
            raise
        finally:
            conn.close()

    # -- DDL ---------------------------------------------------------------
    def ensure_stage_table(self, conn: sqlite3.Connection | None = None) -> None:
        """Create the staging table (and an index on SourceFile) if missing."""
        if conn is None:
            with self.connection() as c:
                self.ensure_stage_table(c)
            return
        cur = conn.cursor()
        cur.execute(_DDL.format(table=self.table))
        cur.execute(_INDEX.format(name=self.settings.stage_table, table=self.table))
        log.debug("staging table %s is ready", self.table)

    def ensure_history_table(self, conn: sqlite3.Connection | None = None) -> None:
        """Create the Load_History table (the file-load registry) if missing."""
        if conn is None:
            with self.connection() as c:
                self.ensure_history_table(c)
            return
        conn.execute(_HISTORY_DDL)
        conn.execute(_HISTORY_INDEX)
        log.debug("history table %s is ready", LOAD_HISTORY_TABLE)

    # -- DML ---------------------------------------------------------------
    def truncate(self, conn: sqlite3.Connection) -> None:
        conn.execute(f"DELETE FROM {self.table}")
        log.info("staging table %s truncated", self.table)

    def delete_source_file(self, conn: sqlite3.Connection, source_file: str) -> int:
        cur = conn.execute(f"DELETE FROM {self.table} WHERE {self.q('SourceFile')} = ?", (source_file,))
        deleted = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0
        if deleted:
            log.info("removed %d previously loaded rows for %s", deleted, source_file)
        return deleted

    def insert_rows(self, conn: sqlite3.Connection, rows: Iterable[StageRow]) -> int:
        """Insert rows in batches of ``batch_size``. Returns number inserted."""
        cur = conn.cursor()
        total = 0
        batch: list[tuple] = []
        size = self.settings.batch_size
        for row in rows:
            batch.append(row.as_tuple())
            if len(batch) >= size:
                cur.executemany(self._insert_sql, batch)
                total += len(batch)
                log.debug("inserted %d rows so far", total)
                batch.clear()
        if batch:
            cur.executemany(self._insert_sql, batch)
            total += len(batch)
        return total

    # -- load history (file de-duplication) ---------------------------------
    def find_previous_load(self, file_name: str,
                           conn: sqlite3.Connection | None = None) -> LoadHistoryRecord | None:
        """The most recent *successful* load of *file_name* (still in the staging
        table), or None.  Used to report what a re-load replaces."""
        if conn is None:
            with self.connection() as c:
                return self.find_previous_load(file_name, c)
        self.ensure_history_table(conn)
        cols = ", ".join(self.q(c) for c in HISTORY_COLUMNS)
        cur = conn.execute(
            f'SELECT {cols} FROM "{LOAD_HISTORY_TABLE}" '
            f'WHERE {self.q("FileName")} = ? AND {self.q("Status")} = ? '
            f'ORDER BY {self.q("LoadHistoryId")} DESC LIMIT 1',
            (file_name, HISTORY_LOADED))
        row = cur.fetchone()
        return LoadHistoryRecord(*row) if row else None

    def mark_history_replaced(self, conn: sqlite3.Connection, file_name: str) -> int:
        """Demote earlier successful loads of *file_name* to status 'replaced'."""
        self.ensure_history_table(conn)
        cur = conn.execute(
            f'UPDATE "{LOAD_HISTORY_TABLE}" SET {self.q("Status")} = ? '
            f'WHERE {self.q("FileName")} = ? AND {self.q("Status")} = ?',
            (HISTORY_REPLACED, file_name, HISTORY_LOADED))
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def record_load_history(self, file_name: str, file_hash: str, load_id: str, status: str,
                            rows_loaded: int = 0, market: str = "", message: str = "",
                            file_size: int | None = None,
                            conn: sqlite3.Connection | None = None) -> None:
        """Append one row to the Load_History registry."""
        if conn is None:
            with self.connection() as c:
                self.record_load_history(file_name, file_hash, load_id, status, rows_loaded,
                                         market, message, file_size, c)
            return
        self.ensure_history_table(conn)
        conn.execute(
            f'INSERT INTO "{LOAD_HISTORY_TABLE}" '
            '("FileName","FileHash","FileSize","LoadId","Market","Status","RowsLoaded","Message","LoadedAt") '
            'VALUES (?,?,?,?,?,?,?,?,?)',
            (file_name, file_hash, file_size, load_id, market or None, status, rows_loaded,
             message or None, now_iso()))

    def load_history(self, limit: int = 100, file_name: str | None = None) -> list[LoadHistoryRecord]:
        cols = ", ".join(self.q(c) for c in HISTORY_COLUMNS)
        sql = f'SELECT {cols} FROM "{LOAD_HISTORY_TABLE}"'
        params: list = []
        if file_name:
            sql += f' WHERE {self.q("FileName")} = ?'
            params.append(file_name)
        sql += f' ORDER BY {self.q("LoadHistoryId")} DESC LIMIT ?'
        params.append(int(limit))
        with self.connection() as conn:
            self.ensure_history_table(conn)
            return [LoadHistoryRecord(*r) for r in conn.execute(sql, params).fetchall()]

    def count_history(self, status: str | None = None) -> int:
        sql = f'SELECT COUNT(*) FROM "{LOAD_HISTORY_TABLE}"'
        params: list = []
        if status:
            sql += f' WHERE {self.q("Status")} = ?'
            params.append(status)
        with self.connection() as conn:
            self.ensure_history_table(conn)
            return int(conn.execute(sql, params).fetchone()[0])

    # -- single rows (data correction) -----------------------------------------
    def _row_dict(self, row: Sequence) -> dict:
        return dict(zip(("StageId",) + STAGE_COLUMNS, row))

    def get_stage_row(self, stage_id: int, conn: sqlite3.Connection | None = None) -> dict | None:
        """One staging row as a dict (``StageId`` + all columns), or None."""
        if conn is None:
            with self.connection() as c:
                return self.get_stage_row(stage_id, c)
        self.ensure_stage_table(conn)
        cols = ", ".join(self.q(c) for c in ("StageId",) + STAGE_COLUMNS)
        row = conn.execute(f"SELECT {cols} FROM {self.table} WHERE {self.q('StageId')} = ?",
                           (int(stage_id),)).fetchone()
        return self._row_dict(row) if row else None

    def find_stage_rows(self, source_file: str | None = None, ticker: str | None = None,
                        trade_date: str | None = None, market: str | None = None,
                        limit: int = 50, conn: sqlite3.Connection | None = None) -> list[dict]:
        """Staging rows matching the given (exact) criteria, newest first."""
        if conn is None:
            with self.connection() as c:
                return self.find_stage_rows(source_file, ticker, trade_date, market, limit, c)
        self.ensure_stage_table(conn)
        cols = ", ".join(self.q(c) for c in ("StageId",) + STAGE_COLUMNS)
        where, params = [], []
        for column, value in (("SourceFile", source_file), ("Ticker", ticker),
                              ("TradeDate", trade_date), ("Market", market)):
            if value:
                where.append(f"{self.q(column)} = ?")
                params.append(value.strip())
        sql = f"SELECT {cols} FROM {self.table}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY {self.q('StageId')} DESC LIMIT ?"
        params.append(int(limit))
        return [self._row_dict(r) for r in conn.execute(sql, params).fetchall()]

    def update_stage_row(self, conn: sqlite3.Connection, stage_id: int, values: dict) -> int:
        """Update editable columns of one row; returns the number of rows changed."""
        sets, params = [], []
        for column in STAGE_EDITABLE_COLUMNS:
            if column in values:
                sets.append(f"{self.q(column)} = ?")
                params.append(values[column])
        if not sets:
            return 0
        params.append(int(stage_id))
        cur = conn.execute(f"UPDATE {self.table} SET {', '.join(sets)} WHERE {self.q('StageId')} = ?", params)
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def insert_stage_row(self, conn: sqlite3.Connection, row: StageRow) -> int:
        """Insert one row; returns its StageId."""
        cur = conn.execute(self._insert_sql, row.as_tuple())
        return int(cur.lastrowid)

    # -- queries -------------------------------------------------------------
    def distinct_tickers(self, market: str | None = None, exclude_source_file: str | None = None,
                         conn: sqlite3.Connection | None = None) -> frozenset[str]:
        """Every ticker seen in the staging table (optionally per market /
        excluding one source file).  Feeds the missing-market-day check."""
        if conn is None:
            with self.connection() as c:
                return self.distinct_tickers(market, exclude_source_file, c)
        sql = f"SELECT DISTINCT {self.q('Ticker')} FROM {self.table}"
        where, params = [], []
        if market:
            where.append(f"{self.q('Market')} = ?")
            params.append(market)
        if exclude_source_file:
            where.append(f"{self.q('SourceFile')} <> ?")
            params.append(exclude_source_file)
        if where:
            sql += " WHERE " + " AND ".join(where)
        return frozenset(r[0] for r in conn.execute(sql, params).fetchall())

    def count_rows(self, conn: sqlite3.Connection | None = None, source_file: str | None = None) -> int:
        if conn is None:
            with self.connection() as c:
                return self.count_rows(c, source_file)
        if source_file is None:
            cur = conn.execute(f"SELECT COUNT(*) FROM {self.table}")
        else:
            cur = conn.execute(f"SELECT COUNT(*) FROM {self.table} WHERE {self.q('SourceFile')} = ?", (source_file,))
        return int(cur.fetchone()[0])

    def summary(self) -> list[tuple[str, str, int, str]]:
        """Per-file summary: (SourceFile, LoadId, rows, LoadedAt)."""
        sf, li, la = self.q("SourceFile"), self.q("LoadId"), self.q("LoadedAt")
        with self.connection() as conn:
            cur = conn.execute(
                f"SELECT {sf}, {li}, COUNT(*), MAX({la}) FROM {self.table} "
                f"GROUP BY {sf}, {li} ORDER BY MAX({la})"
            )
            return [(r[0], r[1], int(r[2]), r[3]) for r in cur.fetchall()]

    def fetch_all(self, conn: sqlite3.Connection, source_file: str | None = None) -> list[Sequence]:
        cols = ", ".join(self.q(c) for c in STAGE_COLUMNS)
        sid, sf = self.q("StageId"), self.q("SourceFile")
        if source_file is None:
            cur = conn.execute(f"SELECT {cols} FROM {self.table} ORDER BY {sid}")
        else:
            cur = conn.execute(f"SELECT {cols} FROM {self.table} WHERE {sf} = ? ORDER BY {sid}", (source_file,))
        return cur.fetchall()


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")
