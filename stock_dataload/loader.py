"""
Load default-format stock data files from the loading folder into the
staging table.

This is the Python counterpart of the SSIS ``LoadStage.dtsx`` package:

    Flat File Source  ->  Derived Column (Market)  ->  OLE DB Destination

Work flow for every file found in ``loading_dir``:

1. fingerprint the file (sha-256) and register the attempt in ``Load_History``;
2. if the file was loaded before, **delete its previous rows** from the
   staging table (same transaction as the new insert) - so no matter how
   many times the app loads the same file, only ONE copy ends up in the
   database, and it is always the latest delivery;
3. check the header is the default column sequence
   (``ticker,date,open,high,low,close,vol``) - otherwise the file is moved
   to ``error_dir`` with a hint to run the converter first;
4. derive ``Market`` from the file name (``NASDAQ_20250123.txt`` -> ``NASDAQ``)
   unless an explicit market is given;
5. validate the rows and bulk insert them inside ONE transaction;
6. run the data quality strategies (:mod:`quality`) - findings land in
   the ``Data_Error`` table, missing-market-day findings as alerts;
7. on success move the file to ``archive_dir``, on failure roll back and
   move the file to ``error_dir`` together with a ``.error.txt`` report.

Every step is wrapped in error capture: activities go to ``App_Log``,
operational errors (with the failing step and stack trace) to
``App_Error`` - see :mod:`audit`.
"""

from __future__ import annotations

import csv
import logging
import re
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator

from . import formats, market_calendar
from .audit import AuditLogger
from .config import Settings
from .db import HISTORY_FAILED, HISTORY_LOADED, StageDatabase, StageRow, file_sha256, now_iso
from .quality import (DataErrorRepository, DataQualityChecker, DataRow, QualityContext,
                      QualityIssue, SEVERITY_ERROR)

log = logging.getLogger(__name__)

#: component name used in App_Log / App_Error
COMPONENT = "loader"

#: rule name under which loader-rejected rows are tracked in Data_Error
REJECT_RULE = "row_format"


class LoadError(Exception):
    """A file could not be loaded."""


class FileFormatError(LoadError):
    """The file is not in the default format."""


@dataclass
class RejectedRow:
    line_no: int
    reason: str
    raw: list[str]


@dataclass
class LoadResult:
    source_file: Path
    status: str = "pending"          # loaded | failed
    load_id: str = ""
    market: str = ""
    file_hash: str = ""
    rows_loaded: int = 0
    rows_rejected: int = 0
    rows_replaced: int = 0           # rows of a previous load of this file that were deleted
    replaced_load_id: str = ""       # LoadId of the previous load that was replaced
    data_errors: int = 0             # quality findings recorded in Data_Error
    alerts: int = 0                  # alert-level findings (missing market day)
    message: str = ""
    final_path: Path | None = None
    rejects: list[RejectedRow] = field(default_factory=list)
    quality_issues: list[QualityIssue] = field(default_factory=list)
    started: datetime = field(default_factory=datetime.now)
    finished: datetime | None = None

    @property
    def ok(self) -> bool:
        return self.status == "loaded"

    @property
    def failed(self) -> bool:
        return self.status == "failed"

    def __str__(self) -> str:
        base = f"{self.source_file.name}: {self.status}"
        if self.ok:
            base += f" ({self.rows_loaded} rows, market={self.market or '-'}, load_id={self.load_id})"
            if self.rows_replaced:
                base += f" [re-load: {self.rows_replaced} rows of the previous load replaced]"
            if self.rows_rejected:
                base += f" [{self.rows_rejected} rows skipped]"
            if self.data_errors:
                base += f" [{self.data_errors} data error(s), {self.alerts} alert(s)]"
        elif self.message:
            base += f" - {self.message}"
        return base


def unique_destination(folder: Path, name: str) -> Path:
    """Return ``folder/name``; if it exists add a timestamp to avoid clobbering."""
    dest = folder / name
    if not dest.exists():
        return dest
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    p = Path(name)
    candidate = folder / f"{p.stem}.{stamp}{p.suffix}"
    n = 1
    while candidate.exists():
        n += 1
        candidate = folder / f"{p.stem}.{stamp}_{n}{p.suffix}"
    return candidate


def move_file(src: Path, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    dest = unique_destination(folder, src.name)
    shutil.move(str(src), str(dest))
    return dest


class StageLoader:
    """Loads default-format files into the staging table."""

    def __init__(self, settings: Settings, db: StageDatabase | None = None,
                 audit: AuditLogger | None = None, checker: DataQualityChecker | None = None,
                 data_errors: DataErrorRepository | None = None):
        self.settings = settings
        self.db = db or StageDatabase(settings)
        self.audit = audit or AuditLogger(settings)
        self.checker = checker or self._default_checker()
        self.data_errors = data_errors or DataErrorRepository(settings)
        self._market_re = re.compile(settings.market_from_filename_regex) if settings.market_from_filename_regex else None

    def _default_checker(self) -> DataQualityChecker:
        checker = DataQualityChecker()
        if not self.settings.alert_missing_days:
            checker.remove_strategy("missing_market_day")
        return checker

    # ------------------------------------------------------------------
    def pending_files(self) -> list[Path]:
        """Files in the loading folder that match the configured patterns."""
        folder = self.settings.loading_dir
        if not folder.is_dir():
            return []
        found: set[Path] = set()
        for pattern in self.settings.file_patterns:
            found.update(p for p in folder.glob(pattern) if p.is_file())
        return sorted(found)

    def derive_market(self, path: Path, explicit: str | None = None) -> str:
        if explicit:
            return explicit
        if self._market_re:
            m = self._market_re.search(path.name)
            if m:
                try:
                    return m.group("market").upper()
                except IndexError:
                    return m.group(1).upper()
        return self.settings.default_market

    # ------------------------------------------------------------------
    def run(self, files: Iterable[Path] | None = None, market: str | None = None,
            truncate: bool | None = None, move_files: bool = True) -> list[LoadResult]:
        """Load every pending file (or the given files). Returns one result per file."""
        self.settings.ensure_folders()
        targets = [Path(f) for f in files] if files is not None else self.pending_files()
        if not targets:
            log.info("nothing to load - no files in %s", self.settings.loading_dir)
            return []

        self.db.ensure_stage_table()
        self.db.ensure_history_table()
        do_truncate = self.settings.truncate_before_load if truncate is None else truncate
        results: list[LoadResult] = []
        for i, path in enumerate(targets):
            # truncating applies once per run, before the first file
            res = self.load_file(path, market=market, truncate=(do_truncate and i == 0), move_file_after=move_files)
            results.append(res)
        loaded = sum(1 for r in results if r.ok)
        failed = len(results) - loaded
        log.info("run complete: %d file(s) loaded, %d failed", loaded, failed)
        self.audit.log(COMPONENT, "run_complete", f"{loaded} file(s) loaded, {failed} failed",
                       level="INFO" if not failed else "WARNING",
                       details={"loaded": loaded, "failed": failed})
        return results

    def load_file(self, path: Path, market: str | None = None, truncate: bool = False,
                  move_file_after: bool = True) -> LoadResult:
        path = Path(path)
        result = LoadResult(source_file=path, load_id=uuid.uuid4().hex)
        step = "start"
        log.info("loading %s (load_id=%s)", path.name, result.load_id)
        self.audit.log(COMPONENT, "load_started", file_name=path.name, load_id=result.load_id)
        try:
            # -- step 1: locate + fingerprint the file ----------------------
            step = "check_file"
            if not path.is_file():
                raise LoadError(f"file not found: {path}")
            result.file_hash = file_sha256(path)
            result.market = self.derive_market(path, market)

            # -- step 2: was this file loaded before? -----------------------
            step = "previous_load_check"
            self.db.ensure_stage_table()
            previous = self.db.find_previous_load(path.name)
            if previous is not None:
                result.replaced_load_id = previous.load_id
                same = "identical content" if previous.file_hash == result.file_hash else "changed content"
                log.info("%s was loaded before on %s (load_id=%s, %s) - its rows will be replaced",
                         path.name, previous.loaded_at, previous.load_id, same)
                self.audit.log(COMPONENT, "reload_detected",
                               f"loaded before on {previous.loaded_at} ({same}) - previous rows will be "
                               f"deleted and the file loaded again", file_name=path.name,
                               load_id=result.load_id,
                               details={"previous_load_id": previous.load_id,
                                        "previous_hash": previous.file_hash,
                                        "file_hash": result.file_hash, "content": same})

            # -- step 3: delete previous rows + insert the new ones in ONE transaction
            step = "replace_rows" if previous is not None else "insert_rows"
            data_rows: list[DataRow] = []
            with self.db.connection() as conn:
                if truncate:
                    self.db.truncate(conn)
                else:
                    # one copy per file: whatever this file loaded before goes away first
                    result.rows_replaced = self.db.delete_source_file(conn, path.name)
                if result.rows_replaced or previous is not None:
                    self.db.mark_history_replaced(conn, path.name)
                step = "insert_rows"
                result.rows_loaded = self.db.insert_rows(conn, self._rows(path, result, data_rows))
                if result.rejects and not self.settings.skip_bad_rows:
                    raise LoadError(
                        f"{len(result.rejects)} invalid row(s), first: line {result.rejects[0].line_no}: "
                        f"{result.rejects[0].reason}  (set skip_bad_rows = true to load the good rows)"
                    )
                if result.rows_loaded == 0:
                    raise LoadError("file contains no data rows")
            result.status = "loaded"
            result.rows_rejected = len(result.rejects)

            # -- step 4: record the successful load in the registry ---------
            step = "record_history"
            self.db.record_load_history(path.name, result.file_hash, result.load_id, HISTORY_LOADED,
                                        rows_loaded=result.rows_loaded, market=result.market,
                                        file_size=path.stat().st_size)

            # -- step 5: data quality checks --------------------------------
            step = "quality_check"
            self._run_quality_checks(path, result, data_rows)

            # -- step 6: archive the file -----------------------------------
            step = "archive_file"
            if move_file_after:
                result.final_path = move_file(path, self.settings.archive_dir)
            else:
                result.final_path = path
            if result.rejects:
                self._write_reject_report(path.name, result)
            log.info("%s", result)
            if result.rows_replaced:
                self.audit.log(COMPONENT, "previous_load_replaced",
                               f"{result.rows_replaced} rows of the previous load deleted, "
                               f"{result.rows_loaded} rows loaded again",
                               file_name=path.name, load_id=result.load_id,
                               details={"rows_replaced": result.rows_replaced,
                                        "previous_load_id": result.replaced_load_id})
            self.audit.log(COMPONENT, "file_loaded", str(result), file_name=path.name,
                           load_id=result.load_id,
                           details={"rows_loaded": result.rows_loaded, "market": result.market,
                                    "rows_replaced": result.rows_replaced,
                                    "rows_rejected": result.rows_rejected,
                                    "data_errors": result.data_errors, "alerts": result.alerts,
                                    "file_hash": result.file_hash})
        except Exception as exc:  # noqa: BLE001 - we want to quarantine on any failure
            self._handle_failure(path, result, step, exc, move_file_after)
        finally:
            result.finished = datetime.now()
        return result

    # ------------------------------------------------------------------
    def _handle_failure(self, path: Path, result: LoadResult, step: str, exc: Exception,
                        move_file_after: bool) -> None:
        result.status = "failed"
        result.rows_loaded = 0
        result.rows_rejected = len(result.rejects)
        result.message = str(exc)
        log.error("failed to load %s (step %s): %s", path.name, step, exc,
                  exc_info=not isinstance(exc, LoadError))
        self.audit.error(COMPONENT, step, exc, file_name=path.name, load_id=result.load_id,
                         details={"market": result.market, "file_hash": result.file_hash})
        self.audit.log(COMPONENT, "load_failed", f"step {step}: {exc}", level="ERROR",
                       file_name=path.name, load_id=result.load_id)
        try:
            if result.file_hash:
                self.db.record_load_history(path.name, result.file_hash, result.load_id,
                                            HISTORY_FAILED, market=result.market,
                                            message=f"step {step}: {exc}")
            if result.rejects:
                self._record_reject_data_errors(path.name, result)
        except Exception:  # pragma: no cover - bookkeeping must not mask the load error
            log.exception("could not record failure details for %s", path.name)
        if path.is_file() and move_file_after:
            result.final_path = move_file(path, self.settings.error_dir)
            self._write_error_report(result)

    # ------------------------------------------------------------------
    def _rows(self, path: Path, result: LoadResult,
              data_rows: list[DataRow] | None = None) -> Iterator[StageRow]:
        """Yield StageRow objects from *path*; invalid rows go to result.rejects."""
        loaded_at = now_iso()
        with open(path, "r", newline="", encoding=self.settings.encoding) as fh:
            reader = csv.reader(fh, delimiter=formats.DEFAULT_DELIMITER)
            try:
                header = next(reader)
            except StopIteration:
                raise FileFormatError("file is empty") from None
            if not formats.is_default_header(header):
                raise FileFormatError(
                    f"header '{formats.describe_header(header)}' is not the default column sequence "
                    f"'{formats.DEFAULT_HEADER_LINE}' - run the converter first"
                )
            for line_no, row in enumerate(reader, start=2):
                if not row or all(not v.strip() for v in row):
                    continue  # blank line
                row = [v.strip() for v in row]
                problem = formats.validate_default_row(row)
                if problem:
                    result.rejects.append(RejectedRow(line_no, problem, row))
                    if not self.settings.skip_bad_rows and len(result.rejects) >= 1:
                        # strict mode: no point reading further, the load will be rolled back
                        return
                    continue
                if data_rows is not None:
                    data_rows.append(DataRow.from_sequence(line_no, row))
                ticker, date, open_, high, low, close, vol = row
                yield StageRow(result.market, ticker, date, open_, high, low, close, vol,
                               path.name, result.load_id, loaded_at)

    # -- data quality ----------------------------------------------------
    def _quality_context(self, path: Path, result: LoadResult) -> QualityContext:
        expected = self.db.distinct_tickers(market=result.market or None,
                                            exclude_source_file=path.name)
        holidays = frozenset(market_calendar.parse_holiday(h)
                             for h in self.settings.extra_market_holidays)
        return QualityContext(market=result.market, source_file=path.name, load_id=result.load_id,
                              expected_tickers=expected, extra_holidays=holidays)

    def _reject_issues(self, result: LoadResult) -> list[QualityIssue]:
        """Rows the loader rejected are data errors too - track every one."""
        issues = []
        for r in result.rejects:
            issues.append(QualityIssue(
                REJECT_RULE, r.reason, severity=SEVERITY_ERROR,
                ticker=r.raw[0] if len(r.raw) > 0 else "",
                trade_date=r.raw[1] if len(r.raw) > 1 else "",
                value=",".join(r.raw), line_no=r.line_no))
        return issues

    def _run_quality_checks(self, path: Path, result: LoadResult, data_rows: list[DataRow]) -> None:
        """Run the validation strategies; persist findings in Data_Error."""
        issues: list[QualityIssue] = self._reject_issues(result)
        if self.settings.quality_checks_enabled:
            context = self._quality_context(path, result)
            # rejected rows never made it into the load - they must not count
            # as valid coverage for the missing-market-day rule
            for issue in issues:
                if issue.ticker:
                    context.invalid_row_keys.add((issue.ticker, issue.trade_date))
            issues += self.checker.check(data_rows, context)
        result.quality_issues = issues
        result.data_errors = len(issues)
        result.alerts = sum(1 for i in issues if i.alert)
        if not issues:
            return
        self.data_errors.record(issues, source_file=path.name, load_id=result.load_id,
                                market=result.market)
        self.audit.log(COMPONENT, "data_errors_found",
                       f"{result.data_errors} data error(s), {result.alerts} alert(s) "
                       f"recorded in Data_Error",
                       level="WARNING", file_name=path.name, load_id=result.load_id,
                       details={"data_errors": result.data_errors, "alerts": result.alerts,
                                "rules": sorted({i.rule for i in issues})})
        for issue in issues:
            if issue.alert:
                log.warning("ALERT %s: %s", path.name, issue)
                self.audit.log(COMPONENT, "data_alert", str(issue), level="ALERT",
                               file_name=path.name, load_id=result.load_id,
                               details={"ticker": issue.ticker, "trade_date": issue.trade_date,
                                        "rule": issue.rule})

    def _record_reject_data_errors(self, file_name: str, result: LoadResult) -> None:
        issues = self._reject_issues(result)
        if issues:
            self.data_errors.record(issues, source_file=file_name, load_id=result.load_id,
                                    market=result.market)

    # -- reports ----------------------------------------------------------
    def _write_error_report(self, result: LoadResult) -> None:
        target = self.settings.error_dir / f"{result.source_file.name}.error.txt"
        lines = [f"file     : {result.source_file.name}",
                 f"time     : {datetime.now():%Y-%m-%d %H:%M:%S}",
                 f"load_id  : {result.load_id}",
                 f"error    : {result.message}", ""]
        if result.rejects:
            lines.append("rejected rows (line: reason | data):")
            lines += [f"  {r.line_no}: {r.reason} | {','.join(r.raw)}" for r in result.rejects[:200]]
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _write_reject_report(self, file_name: str, result: LoadResult) -> None:
        target = self.settings.error_dir / f"{file_name}.rejects.txt"
        self.settings.error_dir.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="") as fh:
            fh.write(f"# rows skipped while loading {file_name} (load_id={result.load_id})\n")
            fh.write("# line,reason,data\n")
            for r in result.rejects:
                fh.write(f"{r.line_no},{r.reason},{','.join(r.raw)}\n")
        log.warning("%d row(s) skipped - see %s", len(result.rejects), target)
