"""
Data correction of rows in the staging table.

Business users fix wrong values (a swapped high/low, a mistyped date, a
missing stock) on the dashboard's *Data correction* page.  This module
is the logic behind it:

* :meth:`CorrectionService.validate` runs the **row-level data quality
  strategies** (the same ones the loader uses) over the proposed values -
  a correction must itself be valid data;
* :meth:`CorrectionService.correct` updates one staging row,
  :meth:`CorrectionService.add_row` inserts a missing one;
* every change is written to the ``Data_Correction`` table (old / new
  value per column), the related ``Data_Error`` findings are marked
  resolved and the activity is logged in ``App_Log``.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator, Mapping

from .audit import AuditLogger
from .config import Settings
from .db import MANUAL_LOAD_ID, STAGE_EDITABLE_COLUMNS, StageDatabase, StageRow, now_iso
from .quality import (DataErrorRecord, DataErrorRepository, DataQualityChecker, DataRow,
                      QualityContext, QualityIssue, RowValidationStrategy, default_strategies)

log = logging.getLogger(__name__)

COMPONENT = "correction"
DATA_CORRECTION_TABLE = "Data_Correction"

#: staging column -> default file column (for the quality strategies)
_STAGE_TO_FILE = {"Ticker": "ticker", "TradeDate": "date", "Open": "open", "High": "high",
                  "Low": "low", "Close": "close", "Vol": "vol"}

_DDL = f"""
CREATE TABLE IF NOT EXISTS "{DATA_CORRECTION_TABLE}" (
    "CorrectionId" INTEGER PRIMARY KEY AUTOINCREMENT,
    "CorrectedAt"  TEXT NOT NULL,
    "Action"       TEXT NOT NULL,
    "StageId"      INTEGER,
    "SourceFile"   TEXT,
    "Ticker"       TEXT,
    "TradeDate"    TEXT,
    "ColumnName"   TEXT NOT NULL,
    "OldValue"     TEXT,
    "NewValue"     TEXT,
    "DataErrorId"  INTEGER,
    "Note"         TEXT
)
"""
_INDEX = (f'CREATE INDEX IF NOT EXISTS "IX_{DATA_CORRECTION_TABLE}_StageId" '
          f'ON "{DATA_CORRECTION_TABLE}" ("StageId")')

CORRECTION_COLUMNS = ("CorrectionId", "CorrectedAt", "Action", "StageId", "SourceFile", "Ticker",
                      "TradeDate", "ColumnName", "OldValue", "NewValue", "DataErrorId", "Note")


class CorrectionError(Exception):
    """The correction could not be applied (invalid values, unknown row, ...)."""


@dataclass
class CorrectionRecord:
    correction_id: int
    corrected_at: str
    action: str
    stage_id: int | None
    source_file: str | None
    ticker: str | None
    trade_date: str | None
    column_name: str
    old_value: str | None
    new_value: str | None
    data_error_id: int | None
    note: str | None


@dataclass
class CorrectionResult:
    action: str                                   # updated | inserted | unchanged
    stage_id: int
    row: dict                                     # the row after the correction
    changes: list[tuple[str, str | None, str]] = field(default_factory=list)  # (column, old, new)
    resolved_errors: int = 0

    def __str__(self) -> str:
        if self.action == "unchanged":
            return f"row {self.stage_id}: nothing changed"
        what = ", ".join(f"{c}: {o!r} -> {n!r}" for c, o, n in self.changes) if self.action == "updated" \
            else f"{self.row.get('Ticker')} {self.row.get('TradeDate')}"
        extra = f", {self.resolved_errors} data error(s) resolved" if self.resolved_errors else ""
        return f"row {self.stage_id} {self.action} ({what}){extra}"


def clean_values(values: Mapping[str, object]) -> dict[str, str]:
    """Keep only editable columns, as stripped text."""
    return {c: str(values[c]).strip() for c in STAGE_EDITABLE_COLUMNS if c in values and values[c] is not None}


def values_from_data_error(record: DataErrorRecord) -> dict[str, str]:
    """Pre-fill a new row from a data error (ticker/date, raw row of a rejected line)."""
    values = {"Market": record.market or "", "Ticker": record.ticker or "", "TradeDate": record.trade_date or ""}
    if record.rule == "row_format" and record.value:
        parts = [p.strip() for p in record.value.split(",")]
        if len(parts) == 7:
            for column, part in zip(("Ticker", "TradeDate", "Open", "High", "Low", "Close", "Vol"), parts):
                values[column] = part
    return values


class CorrectionService:
    """Apply user corrections to the staging table, with validation and tracking."""

    def __init__(self, settings: Settings, db: StageDatabase | None = None,
                 audit: AuditLogger | None = None, data_errors: DataErrorRepository | None = None,
                 checker: DataQualityChecker | None = None):
        self.settings = settings
        self.db = db or StageDatabase(settings)
        self.audit = audit or AuditLogger(settings)
        self.data_errors = data_errors or DataErrorRepository(settings)
        # corrections are validated with the row-level business rules only
        self.checker = checker or DataQualityChecker(
            [s for s in default_strategies() if isinstance(s, RowValidationStrategy)])
        self._table_ready = False

    # -- persistence ------------------------------------------------------
    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        with self.db.connection() as conn:
            yield conn

    def ensure_table(self, conn: sqlite3.Connection | None = None) -> None:
        if conn is None:
            with self.connection() as c:
                self.ensure_table(c)
            return
        conn.execute(_DDL)
        conn.execute(_INDEX)
        self._table_ready = True

    def _record(self, conn: sqlite3.Connection, action: str, stage_id: int | None, row: Mapping,
                changes: list[tuple[str, str | None, str]], data_error_id: int | None, note: str) -> None:
        if not self._table_ready:
            self.ensure_table(conn)
        when = now_iso()
        conn.executemany(
            f'INSERT INTO "{DATA_CORRECTION_TABLE}" ("CorrectedAt","Action","StageId","SourceFile","Ticker",'
            '"TradeDate","ColumnName","OldValue","NewValue","DataErrorId","Note") VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            [(when, action, stage_id, row.get("SourceFile"), row.get("Ticker"), row.get("TradeDate"),
              column, old, new, data_error_id, note or None) for column, old, new in changes])

    def history(self, limit: int = 50, stage_id: int | None = None) -> list[CorrectionRecord]:
        cols = ", ".join(f'"{c}"' for c in CORRECTION_COLUMNS)
        sql = f'SELECT {cols} FROM "{DATA_CORRECTION_TABLE}"'
        params: list = []
        if stage_id is not None:
            sql += ' WHERE "StageId" = ?'
            params.append(int(stage_id))
        sql += ' ORDER BY "CorrectionId" DESC LIMIT ?'
        params.append(int(limit))
        with self.connection() as conn:
            self.ensure_table(conn)
            return [CorrectionRecord(*r) for r in conn.execute(sql, params).fetchall()]

    def count(self) -> int:
        with self.connection() as conn:
            self.ensure_table(conn)
            return int(conn.execute(f'SELECT COUNT(*) FROM "{DATA_CORRECTION_TABLE}"').fetchone()[0])

    # -- validation ---------------------------------------------------------
    def validate(self, values: Mapping[str, str]) -> list[QualityIssue]:
        """Issues the row-level business rules find in *values* (empty = valid)."""
        issues: list[QualityIssue] = []
        if not str(values.get("Ticker", "")).strip():
            issues.append(QualityIssue("required", "ticker is required", column="Ticker"))
        if not str(values.get("TradeDate", "")).strip():
            issues.append(QualityIssue("required", "trade date is required", column="TradeDate"))
        row = DataRow(0, {file_col: str(values.get(stage_col, "")).strip()
                          for stage_col, file_col in _STAGE_TO_FILE.items()})
        issues += self.checker.check([row], QualityContext())
        return issues

    def _validated(self, values: Mapping[str, str]) -> dict[str, str]:
        issues = self.validate(values)
        if issues:
            raise CorrectionError("invalid values: " + "; ".join(i.message for i in issues))
        return clean_values(values)

    # -- corrections ----------------------------------------------------------
    def correct(self, stage_id: int, values: Mapping[str, object], note: str = "",
                data_error_id: int | None = None) -> CorrectionResult:
        """Change the editable columns of one staging row to *values*."""
        new = self._validated(clean_values(values))
        try:
            with self.connection() as conn:
                self.db.ensure_stage_table(conn)
                current = self.db.get_stage_row(stage_id, conn)
                if current is None:
                    raise CorrectionError(f"staging row {stage_id} does not exist")
                changes = [(c, current[c], new[c]) for c in STAGE_EDITABLE_COLUMNS
                           if c in new and (current[c] or "") != new[c]]
                if not changes:
                    return CorrectionResult("unchanged", int(stage_id), current)
                self.db.update_stage_row(conn, stage_id, {c: n for c, _, n in changes})
                self._record(conn, "updated", int(stage_id), current, changes, data_error_id, note)
                resolved = self._resolve(conn, current, new, data_error_id, note)
                updated = self.db.get_stage_row(stage_id, conn)
        except CorrectionError:
            raise
        except Exception as exc:  # noqa: BLE001 - every step is captured
            self.audit.error(COMPONENT, "correct_row", exc, details={"stage_id": stage_id})
            raise CorrectionError(f"could not correct row {stage_id}: {exc}") from exc
        result = CorrectionResult("updated", int(stage_id), updated, changes, resolved)
        self._log(result, data_error_id, note)
        return result

    def add_row(self, values: Mapping[str, object], source_file: str = "", note: str = "",
                data_error_id: int | None = None) -> CorrectionResult:
        """Insert a row that is missing from the staging table (e.g. a missing-market-day alert)."""
        new = self._validated(clean_values(values))
        source_file = (source_file or "").strip() or MANUAL_LOAD_ID
        row = StageRow(new.get("Market", ""), new["Ticker"], new["TradeDate"], new.get("Open", ""),
                       new.get("High", ""), new.get("Low", ""), new.get("Close", ""), new.get("Vol", ""),
                       source_file, MANUAL_LOAD_ID, now_iso())
        try:
            with self.connection() as conn:
                self.db.ensure_stage_table(conn)
                stage_id = self.db.insert_stage_row(conn, row)
                inserted = self.db.get_stage_row(stage_id, conn)
                changes = [(c, None, inserted[c]) for c in STAGE_EDITABLE_COLUMNS if inserted[c]]
                self._record(conn, "inserted", stage_id, inserted, changes, data_error_id, note)
                resolved = self._resolve(conn, inserted, new, data_error_id, note)
        except Exception as exc:  # noqa: BLE001
            self.audit.error(COMPONENT, "add_row", exc, details={"values": new})
            raise CorrectionError(f"could not add the row: {exc}") from exc
        result = CorrectionResult("inserted", stage_id, inserted, changes, resolved)
        self._log(result, data_error_id, note)
        return result

    def _resolve(self, conn: sqlite3.Connection, row: Mapping, new: Mapping, data_error_id: int | None,
                 note: str) -> int:
        """Resolve the linked data error plus every open finding for the corrected row
        (findings were recorded under the row's *original* ticker/date - and, if those
        were corrected too, under the new key, e.g. a missing-market-day alert)."""
        why = note or f"corrected on the data correction page (row {row.get('StageId', '?')})"
        ids = [data_error_id] if data_error_id else []
        keys = {(row.get("Ticker"), row.get("TradeDate")),
                (new.get("Ticker") or row.get("Ticker"), new.get("TradeDate") or row.get("TradeDate"))}
        total = 0
        for ticker, trade_date in keys:
            total += self.data_errors.resolve(ids, why, source_file=row.get("SourceFile"), ticker=ticker,
                                              trade_date=trade_date, conn=conn)
            ids = []
        return total

    def _log(self, result: CorrectionResult, data_error_id: int | None, note: str) -> None:
        self.audit.log(COMPONENT, f"row_{result.action}", str(result), level="WARNING",
                       file_name=result.row.get("SourceFile"), load_id=result.row.get("LoadId"),
                       details={"stage_id": result.stage_id, "data_error_id": data_error_id, "note": note,
                                "changes": [{"column": c, "old": o, "new": n} for c, o, n in result.changes]})
