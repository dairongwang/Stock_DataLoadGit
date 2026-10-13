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

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
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


class DatabaseError(Exception):
    """Raised for database connectivity/execution problems."""


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

    # -- queries -------------------------------------------------------------
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
