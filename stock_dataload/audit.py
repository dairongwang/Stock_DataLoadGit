"""
Application audit trail: the ``App_Log`` and ``App_Error`` tables.

* ``App_Log``   - every noteworthy activity of the app (file picked up,
                  duplicate skipped, rows inserted, quality alerts, ...)
* ``App_Error`` - every operational error, with the component / step it
                  happened in, the exception type and the full stack trace.

The :class:`AuditLogger` writes with its **own short-lived connections**
(one implicit transaction per write), so audit records survive a rollback
of the load transaction - a failed load leaves its App_Error row behind.

Writing to the audit trail must never break the application: any problem
while persisting a record is swallowed (and reported through standard
python logging instead).  Records are always mirrored to the python
logging framework, so the rotating ``logs/stock_dataload.log`` file keeps
the same story.

The :meth:`AuditLogger.step` context manager gives every step of an
operation error-capture for free::

    with audit.step("loader", "read_file", file_name=path.name):
        ...                      # an exception here is recorded in App_Error
"""

from __future__ import annotations

import json
import logging
import sqlite3
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator, Mapping

from .config import Settings

log = logging.getLogger(__name__)

APP_LOG_TABLE = "App_Log"
APP_ERROR_TABLE = "App_Error"

#: Log levels used in App_Log (ALERT is used for data-quality alerts).
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "ALERT")

_LOG_DDL = f"""
CREATE TABLE IF NOT EXISTS "{APP_LOG_TABLE}" (
    "LogId"      INTEGER PRIMARY KEY AUTOINCREMENT,
    "LoggedAt"   TEXT NOT NULL,
    "Level"      TEXT NOT NULL,
    "Component"  TEXT NOT NULL,
    "Event"      TEXT NOT NULL,
    "SourceFile" TEXT,
    "LoadId"     TEXT,
    "Message"    TEXT,
    "Details"    TEXT
)
"""

_ERROR_DDL = f"""
CREATE TABLE IF NOT EXISTS "{APP_ERROR_TABLE}" (
    "ErrorId"      INTEGER PRIMARY KEY AUTOINCREMENT,
    "OccurredAt"   TEXT NOT NULL,
    "Component"    TEXT NOT NULL,
    "Operation"    TEXT NOT NULL,
    "SourceFile"   TEXT,
    "LoadId"       TEXT,
    "ErrorType"    TEXT,
    "ErrorMessage" TEXT NOT NULL,
    "Stacktrace"   TEXT,
    "Details"      TEXT
)
"""

_INDEXES = (
    f'CREATE INDEX IF NOT EXISTS "IX_{APP_LOG_TABLE}_LoggedAt" ON "{APP_LOG_TABLE}" ("LoggedAt")',
    f'CREATE INDEX IF NOT EXISTS "IX_{APP_ERROR_TABLE}_OccurredAt" ON "{APP_ERROR_TABLE}" ("OccurredAt")',
)

LOG_COLUMNS = ("LogId", "LoggedAt", "Level", "Component", "Event", "SourceFile", "LoadId", "Message", "Details")
ERROR_COLUMNS = ("ErrorId", "OccurredAt", "Component", "Operation", "SourceFile", "LoadId",
                 "ErrorType", "ErrorMessage", "Stacktrace", "Details")


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def _as_json(details: Mapping[str, Any] | None) -> str | None:
    if not details:
        return None
    try:
        return json.dumps(details, default=str, sort_keys=True)
    except (TypeError, ValueError):  # pragma: no cover - json.dumps(default=str) rarely fails
        return str(details)


@dataclass
class AppLogRecord:
    log_id: int
    logged_at: str
    level: str
    component: str
    event: str
    source_file: str | None
    load_id: str | None
    message: str | None
    details: str | None


@dataclass
class AppErrorRecord:
    error_id: int
    occurred_at: str
    component: str
    operation: str
    source_file: str | None
    load_id: str | None
    error_type: str | None
    error_message: str
    stacktrace: str | None
    details: str | None


class AuditLogger:
    """Persists app activity to ``App_Log`` and operational errors to ``App_Error``."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._tables_ready = False

    # -- connections -------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        path = self.settings.sqlite_path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -- DDL ---------------------------------------------------------------
    def ensure_tables(self, conn: sqlite3.Connection | None = None) -> None:
        """Create App_Log / App_Error if they do not exist."""
        if conn is None:
            with self.connection() as c:
                self.ensure_tables(c)
            return
        conn.execute(_LOG_DDL)
        conn.execute(_ERROR_DDL)
        for ddl in _INDEXES:
            conn.execute(ddl)
        self._tables_ready = True

    # -- writing -----------------------------------------------------------
    def log(self, component: str, event: str, message: str = "", level: str = "INFO",
            file_name: str | None = None, load_id: str | None = None,
            details: Mapping[str, Any] | None = None) -> None:
        """Record an application activity in ``App_Log`` (never raises)."""
        level = level.upper() if level.upper() in LOG_LEVELS else "INFO"
        py_level = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING,
                    "ERROR": logging.ERROR, "ALERT": logging.WARNING}[level]
        log.log(py_level, "[%s] %s%s%s", component, event,
                f" {file_name}" if file_name else "", f" - {message}" if message else "")
        self._write(f'INSERT INTO "{APP_LOG_TABLE}" '
                    '("LoggedAt","Level","Component","Event","SourceFile","LoadId","Message","Details") '
                    'VALUES (?,?,?,?,?,?,?,?)',
                    (_now(), level, component, event, file_name, load_id, message or None, _as_json(details)))

    def error(self, component: str, operation: str, error: BaseException | str,
              file_name: str | None = None, load_id: str | None = None,
              details: Mapping[str, Any] | None = None) -> None:
        """Record an operational error in ``App_Error`` (never raises)."""
        if isinstance(error, BaseException):
            error_type = type(error).__name__
            message = str(error) or error_type
            stack = "".join(traceback.format_exception(type(error), error, error.__traceback__)).strip()
        else:
            error_type, message, stack = None, str(error), None
        log.error("[%s] %s failed%s: %s", component, operation, f" ({file_name})" if file_name else "", message)
        self._write(f'INSERT INTO "{APP_ERROR_TABLE}" '
                    '("OccurredAt","Component","Operation","SourceFile","LoadId",'
                    '"ErrorType","ErrorMessage","Stacktrace","Details") '
                    'VALUES (?,?,?,?,?,?,?,?,?)',
                    (_now(), component, operation, file_name, load_id, error_type, message, stack,
                     _as_json(details)))

    def _write(self, sql: str, params: tuple) -> None:
        """Fail-safe insert: the audit trail must never break the app."""
        try:
            with self.connection() as conn:
                if not self._tables_ready:
                    self.ensure_tables(conn)
                conn.execute(sql, params)
        except (sqlite3.Error, OSError):
            log.exception("could not write audit record (continuing)")

    # -- step error capture --------------------------------------------------
    @contextmanager
    def step(self, component: str, operation: str, file_name: str | None = None,
             load_id: str | None = None, details: Mapping[str, Any] | None = None,
             reraise: bool = True) -> Iterator[None]:
        """Run one step of an operation; on exception record it in App_Error.

        The exception is tagged (``_audit_recorded``) so outer handlers can
        avoid recording the same error twice, and re-raised unless
        ``reraise=False``.
        """
        try:
            yield
        except BaseException as exc:
            if not getattr(exc, "_audit_recorded", False):
                self.error(component, operation, exc, file_name=file_name, load_id=load_id, details=details)
                try:
                    exc._audit_recorded = True  # type: ignore[attr-defined]
                except Exception:  # pragma: no cover - exceptions without __dict__
                    pass
            if reraise:
                raise

    # -- reading -----------------------------------------------------------
    def fetch_logs(self, limit: int = 100, component: str | None = None,
                   level: str | None = None) -> list[AppLogRecord]:
        cols = ", ".join(f'"{c}"' for c in LOG_COLUMNS)
        sql = f'SELECT {cols} FROM "{APP_LOG_TABLE}"'
        where, params = [], []
        if component:
            where.append('"Component" = ?')
            params.append(component)
        if level:
            where.append('"Level" = ?')
            params.append(level.upper())
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += ' ORDER BY "LogId" DESC LIMIT ?'
        params.append(int(limit))
        with self.connection() as conn:
            self.ensure_tables(conn)
            rows = conn.execute(sql, params).fetchall()
        return [AppLogRecord(*r) for r in rows]

    def fetch_errors(self, limit: int = 100, component: str | None = None) -> list[AppErrorRecord]:
        cols = ", ".join(f'"{c}"' for c in ERROR_COLUMNS)
        sql = f'SELECT {cols} FROM "{APP_ERROR_TABLE}"'
        params: list = []
        if component:
            sql += ' WHERE "Component" = ?'
            params.append(component)
        sql += ' ORDER BY "ErrorId" DESC LIMIT ?'
        params.append(int(limit))
        with self.connection() as conn:
            self.ensure_tables(conn)
            rows = conn.execute(sql, params).fetchall()
        return [AppErrorRecord(*r) for r in rows]

    def count_logs(self) -> int:
        with self.connection() as conn:
            self.ensure_tables(conn)
            return int(conn.execute(f'SELECT COUNT(*) FROM "{APP_LOG_TABLE}"').fetchone()[0])

    def count_errors(self) -> int:
        with self.connection() as conn:
            self.ensure_tables(conn)
            return int(conn.execute(f'SELECT COUNT(*) FROM "{APP_ERROR_TABLE}"').fetchone()[0])
