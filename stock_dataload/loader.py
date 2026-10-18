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
   unless an explicit market is given; a delivery dated (file name) on a day
   its market was closed - weekend, holiday, ad-hoc closure, see the
   ``No_Trading_Day`` table (:mod:`trading_calendar`) - is **skipped** with a
   warning and moved to ``skipped_dir``;
5. validate the rows and bulk insert them inside ONE transaction;
6. register the symbols seen in the ``Stock_Symbol`` table (:mod:`symbols`)
   and run the data quality strategies (:mod:`quality`) - findings land in
   the ``Data_Error`` table, missing-market-day findings as alerts (only for
   stocks inside their known trading window);
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
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping

from . import formats, market_calendar
from .audit import AuditLogger
from .config import Settings
from .db import HISTORY_FAILED, HISTORY_LOADED, HISTORY_SKIPPED, StageDatabase, StageRow, file_sha256, now_iso
from .quality import (DataErrorRepository, DataQualityChecker, DataRow, QualityContext,
                      QualityIssue, SEVERITY_ERROR)
from .symbols import SymbolRegistry
from .trading_calendar import TradingCalendar

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
    status: str = "pending"          # loaded | failed | skipped
    load_id: str = ""
    market: str = ""
    trade_date: date | None = None   # date in the file name (if any)
    file_hash: str = ""
    rows_loaded: int = 0
    rows_rejected: int = 0
    rows_replaced: int = 0           # rows of a previous load of this file that were deleted
    new_symbols: int = 0             # tickers seen for the first time (added to Stock_Symbol)
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

    @property
    def skipped(self) -> bool:
        """Not loaded on purpose: the file is dated on a no-trading day of its market."""
        return self.status == "skipped"

    def __str__(self) -> str:
        base = f"{self.source_file.name}: {self.status}"
        if self.ok:
            base += f" ({self.rows_loaded} rows, market={self.market or '-'}, load_id={self.load_id})"
            if self.rows_replaced:
                base += f" [re-load: {self.rows_replaced} rows of the previous load replaced]"
            if self.rows_rejected:
                base += f" [{self.rows_rejected} rows skipped]"
            if self.new_symbols:
                base += f" [{self.new_symbols} new symbol(s)]"
            if self.data_errors:
                base += f" [{self.data_errors} data error(s), {self.alerts} alert(s)]"
        if self.message:
            base += f" - {self.message}"
        return base


@dataclass
class FolderLoadResult:
    """Outcome of a bulk load of a folder (see :meth:`StageLoader.load_folder`)."""
    folder: Path
    status: str = "pending"          # completed | stopped | interrupted | failed
    files: list[Path] = field(default_factory=list)          # files found (sorted)
    results: list[LoadResult] = field(default_factory=list)  # one per file attempted
    message: str = ""
    started: datetime = field(default_factory=datetime.now)
    finished: datetime | None = None

    @property
    def loaded(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.failed)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.skipped)

    @property
    def not_attempted(self) -> int:
        return max(len(self.files) - len(self.results), 0)

    @property
    def rows_loaded(self) -> int:
        return sum(r.rows_loaded for r in self.results if r.ok)

    @property
    def rows_rejected(self) -> int:
        return sum(r.rows_rejected for r in self.results)

    @property
    def data_errors(self) -> int:
        return sum(r.data_errors for r in self.results)

    @property
    def alerts(self) -> int:
        return sum(r.alerts for r in self.results)

    @property
    def failures(self) -> list[LoadResult]:
        return [r for r in self.results if r.failed]

    @property
    def ok(self) -> bool:
        """Every file found was loaded (or skipped as a no-trading day)."""
        return self.status == "completed" and not self.failed and not self.not_attempted

    @property
    def duration(self) -> float:
        end = self.finished or datetime.now()
        return (end - self.started).total_seconds()

    def as_dict(self) -> dict:
        return {"folder": str(self.folder), "status": self.status, "files": len(self.files),
                "loaded": self.loaded, "failed": self.failed, "skipped": self.skipped,
                "not_attempted": self.not_attempted,
                "rows_loaded": self.rows_loaded, "rows_rejected": self.rows_rejected,
                "data_errors": self.data_errors, "alerts": self.alerts,
                "seconds": round(self.duration, 1), "message": self.message}

    def __str__(self) -> str:
        text = (f"{self.folder}: {self.status} - {self.loaded} of {len(self.files)} file(s) loaded "
                f"({self.rows_loaded:,} rows)")
        if self.failed:
            text += f", {self.failed} failed"
        if self.skipped:
            text += f", {self.skipped} skipped (no trading day)"
        if self.not_attempted:
            text += f", {self.not_attempted} not attempted"
        if self.rows_rejected:
            text += f", {self.rows_rejected} row(s) skipped"
        if self.data_errors:
            text += f", {self.data_errors} data error(s) / {self.alerts} alert(s)"
        text += f" in {self.duration:.1f}s"
        if self.message:
            text += f" - {self.message}"
        return text


class KnownTickers:
    """Tickers per source file of one market, kept up to date across the files
    of a multi-file run.

    The missing-market-day check needs "every ticker seen in *other* files of
    the market".  Asking the database after each file means a full scan of the
    growing staging table per file - quadratic for a bulk load of a year.  This
    registry is built from ONE scan (:meth:`StageDatabase.tickers_by_source`)
    and then maintained per file, giving exactly the same answer.
    """

    def __init__(self, by_file: Mapping[str, frozenset[str]]):
        self.by_file: dict[str, frozenset[str]] = dict(by_file)
        self.counts: Counter[str] = Counter()
        for tickers in self.by_file.values():
            self.counts.update(tickers)

    def replace_file(self, source_file: str, tickers: Iterable[str]) -> None:
        """The rows of *source_file* are now exactly *tickers* (re-load aware)."""
        old = self.by_file.pop(source_file, None)
        if old:
            self.counts.subtract(old)
        new = frozenset(t for t in tickers if t)
        self.by_file[source_file] = new
        self.counts.update(new)

    def expected_for(self, source_file: str) -> frozenset[str]:
        """Tickers present in at least one file other than *source_file*."""
        own = self.by_file.get(source_file, frozenset())
        return frozenset(t for t, n in self.counts.items() if n - (1 if t in own else 0) > 0)


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
                 data_errors: DataErrorRepository | None = None, calendar: TradingCalendar | None = None,
                 symbols: SymbolRegistry | None = None):
        self.settings = settings
        self.db = db or StageDatabase(settings)
        self.audit = audit or AuditLogger(settings)
        self.checker = checker or self._default_checker()
        self.data_errors = data_errors or DataErrorRepository(settings)
        self.calendar = calendar or TradingCalendar(settings, self.db, self.audit)
        self.symbols = symbols or SymbolRegistry(settings, self.db, self.audit)
        self._market_re = re.compile(settings.market_from_filename_regex) if settings.market_from_filename_regex else None
        self._date_re = re.compile(settings.date_from_filename_regex) if settings.date_from_filename_regex else None
        # per-market ticker registries, alive for the duration of one run() only
        self._known: dict[str | None, KnownTickers] | None = None

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

    def derive_trade_date(self, path: Path) -> date | None:
        """The trade date in the file name (``NASDAQ_20250123.txt`` -> 2025-01-23), or None."""
        if not self._date_re:
            return None
        m = self._date_re.search(path.name)
        if not m:
            return None
        try:
            text = m.group("date")
        except IndexError:
            text = m.group(1) if m.groups() else m.group(0)
        try:
            return datetime.strptime(text, formats.DEFAULT_DATE_FORMAT).date()
        except ValueError:
            try:
                return date.fromisoformat(text)
            except ValueError:
                return None

    # ------------------------------------------------------------------
    def run(self, files: Iterable[Path] | None = None, market: str | None = None,
            truncate: bool | None = None, move_files: bool = True,
            stop_on_error: bool = False,
            progress: Callable[[LoadResult], None] | None = None,
            include_no_trading_days: bool = False) -> list[LoadResult]:
        """Load every pending file (or the given files). Returns one result per file.

        ``progress`` is called with each :class:`LoadResult` as soon as the file
        is done (the CLI prints them while a long run is going on).  With
        ``stop_on_error`` the run stops after the first failed file.  Files
        dated on a no-trading day of their market are skipped unless
        ``include_no_trading_days`` is set.
        """
        self.settings.ensure_folders()
        targets = [Path(f) for f in files] if files is not None else self.pending_files()
        if not targets:
            log.info("nothing to load - no files in %s", self.settings.loading_dir)
            return []

        self.db.ensure_stage_table()
        self.db.ensure_history_table()
        do_truncate = self.settings.truncate_before_load if truncate is None else truncate
        results: list[LoadResult] = []
        self._known = {}          # known tickers: one scan per market per run, then maintained
        try:
            for i, path in enumerate(targets):
                # truncating applies once per run, before the first file
                res = self.load_file(path, market=market, truncate=(do_truncate and i == 0),
                                     move_file_after=move_files, include_no_trading_days=include_no_trading_days)
                results.append(res)
                if progress is not None:
                    progress(res)
                if res.failed and stop_on_error:
                    log.warning("stopping after the first failure (%s) - %d file(s) not loaded",
                                path.name, len(targets) - i - 1)
                    break
        finally:
            self._known = None
        loaded = sum(1 for r in results if r.ok)
        skipped = sum(1 for r in results if r.skipped)
        failed = len(results) - loaded - skipped
        log.info("run complete: %d file(s) loaded, %d failed, %d skipped", loaded, failed, skipped)
        self.audit.log(COMPONENT, "run_complete", f"{loaded} file(s) loaded, {failed} failed, {skipped} skipped",
                       level="INFO" if not failed else "WARNING",
                       details={"loaded": loaded, "failed": failed, "skipped": skipped})
        return results

    # -- bulk loading a folder -------------------------------------------
    def find_files(self, folder: Path | str, patterns: Iterable[str] | None = None,
                   recursive: bool = False) -> list[Path]:
        """Files in *folder* matching *patterns* (default: the configured
        ``file_patterns``), sorted by name - i.e. by trade date for
        ``<MARKET>_<YYYYMMDD>.txt`` deliveries.  Raises :class:`LoadError` if the
        folder is missing, is not a folder or cannot be read."""
        folder = Path(folder).expanduser()
        if not folder.exists():
            raise LoadError(f"folder not found: {folder}")
        if not folder.is_dir():
            raise LoadError(f"not a folder: {folder}")
        patterns = tuple(p.strip() for p in (patterns or self.settings.file_patterns) if p and p.strip())
        if not patterns:
            raise LoadError("no file pattern given (e.g. *.txt)")
        found: dict[Path, None] = {}
        try:
            for pattern in patterns:
                matches = folder.rglob(pattern) if recursive else folder.glob(pattern)
                for p in matches:
                    try:
                        if p.is_file() and not p.name.startswith("."):
                            found[p] = None
                    except OSError:  # pragma: no cover - vanished/unreadable entry
                        continue
        except OSError as exc:
            raise LoadError(f"cannot read folder {folder}: {exc}") from exc
        return sorted(found, key=lambda p: (str(p.parent), p.name))

    def load_folder(self, folder: Path | str, patterns: Iterable[str] | None = None,
                    recursive: bool = False, market: str | None = None, move_files: bool = False,
                    truncate: bool = False, stop_on_error: bool = False,
                    progress: Callable[[LoadResult], None] | None = None,
                    include_no_trading_days: bool = False) -> FolderLoadResult:
        """Bulk load: every matching file in *folder* (default ``*.txt``), one
        load/transaction per file, files sorted by name.

        Files are left in place by default (``move_files=True`` archives /
        quarantines them like a loading-folder run).  A broken file never stops
        the others unless ``stop_on_error`` is set; every failure is recorded
        in ``App_Error`` / ``Load_History`` and listed in the returned
        :class:`FolderLoadResult`.  A folder-level problem (missing folder,
        unusable database) raises :class:`LoadError` / :class:`DatabaseError`
        after being recorded in ``App_Error``; a keyboard interrupt ends the run
        cleanly (the current file is rolled back) with status ``interrupted``.
        """
        folder = Path(folder).expanduser()
        summary = FolderLoadResult(folder=folder)
        log.info("bulk load of folder %s (patterns=%s, recursive=%s)", folder,
                 ",".join(patterns or self.settings.file_patterns), recursive)
        try:
            summary.files = self.find_files(folder, patterns, recursive)
        except LoadError as exc:
            summary.status = "failed"
            summary.message = str(exc)
            summary.finished = datetime.now()
            self.audit.error(COMPONENT, "bulk_load_folder", exc, details={"folder": str(folder)})
            self.audit.log(COMPONENT, "bulk_load_failed", str(exc), level="ERROR",
                           details={"folder": str(folder)})
            exc._audit_recorded = True  # type: ignore[attr-defined]
            raise
        self.audit.log(COMPONENT, "bulk_load_started",
                       f"{len(summary.files)} file(s) in {folder}", details={
                           "folder": str(folder), "files": len(summary.files), "recursive": recursive,
                           "move_files": move_files, "stop_on_error": stop_on_error, "market": market})
        if not summary.files:
            summary.status = "completed"
            summary.message = f"no files matching {';'.join(patterns or self.settings.file_patterns)} in {folder}"
            summary.finished = datetime.now()
            log.info("%s", summary.message)
            self.audit.log(COMPONENT, "bulk_load_complete", summary.message, details={"folder": str(folder)})
            return summary
        def on_result(res: LoadResult) -> None:
            summary.results.append(res)      # collected as they come (kept if interrupted)
            if progress is not None:
                progress(res)

        try:
            self.run(files=summary.files, market=market, truncate=truncate, move_files=move_files,
                     stop_on_error=stop_on_error, progress=on_result,
                     include_no_trading_days=include_no_trading_days)
            summary.status = "stopped" if summary.not_attempted else "completed"
            if summary.status == "stopped":
                summary.message = (f"stopped after the first failure ({summary.failures[0].source_file.name}) - "
                                   f"{summary.not_attempted} file(s) not attempted")
        except KeyboardInterrupt:
            summary.status = "interrupted"
            summary.message = (f"interrupted after {len(summary.results)} of {len(summary.files)} file(s) - "
                               f"the file being loaded was rolled back")
            log.warning("bulk load of %s %s", folder, summary.message)
        except Exception as exc:  # noqa: BLE001 - a folder-level problem (e.g. database unusable)
            summary.status = "failed"
            summary.message = str(exc)
            self.audit.error(COMPONENT, "bulk_load_folder", exc, details={"folder": str(folder),
                             "files_done": len(summary.results)})
            self.audit.log(COMPONENT, "bulk_load_failed", str(exc), level="ERROR",
                           details={"folder": str(folder), "files_done": len(summary.results)})
            try:
                exc._audit_recorded = True  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover - exceptions without __dict__
                pass
            raise
        finally:
            summary.finished = datetime.now()
        log.info("%s", summary)
        self.audit.log(COMPONENT, "bulk_load_complete", str(summary),
                       level="INFO" if summary.status == "completed" and not summary.failed else "WARNING",
                       details=summary.as_dict())
        return summary

    def load_file(self, path: Path, market: str | None = None, truncate: bool = False,
                  move_file_after: bool = True, include_no_trading_days: bool = False) -> LoadResult:
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
            result.trade_date = self.derive_trade_date(path)

            # -- step 1b: a delivery for a day the market was closed? ----------
            step = "trading_day_check"
            if (result.trade_date is not None and self.settings.skip_no_trading_days
                    and not include_no_trading_days):
                reason = self.calendar.closure_reason(result.market or None, result.trade_date)
                if reason:
                    self._skip_file(path, result, reason, move_file_after)
                    return result

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

            twins = self.db.find_loaded_by_hash(result.file_hash, exclude_file_name=path.name)
            if twins:
                # same content under another name: loaded anyway (it is a different
                # delivery as far as the loading contract goes) but worth a warning -
                # the rows will exist twice in staging, once per source file
                names = ", ".join(sorted({t.file_name for t in twins}))
                log.warning("%s has exactly the same content as %s (already loaded)", path.name, names)
                self.audit.log(COMPONENT, "duplicate_content",
                               f"identical content already loaded as {names} - loading anyway, the rows "
                               f"will exist once per source file", level="WARNING",
                               file_name=path.name, load_id=result.load_id,
                               details={"duplicates": names, "file_hash": result.file_hash})

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

            # -- step 5: symbol registry + data quality checks ----------------
            step = "register_symbols"
            result.new_symbols = self._register_symbols(result, data_rows)
            step = "quality_check"
            self._run_quality_checks(path, result, data_rows)

            # -- step 6: archive the file -----------------------------------
            step = "archive_file"
            result.final_path = path
            if move_file_after:
                try:
                    result.final_path = move_file(path, self.settings.archive_dir)
                except OSError as exc:
                    # the rows are committed and registered - a file that cannot be moved
                    # (e.g. open in another program on Windows) does not undo the load
                    result.message = f"loaded, but the file could not be moved to the archive folder: {exc}"
                    log.warning("%s: %s", path.name, result.message)
                    self.audit.log(COMPONENT, "archive_failed", result.message, level="WARNING",
                                   file_name=path.name, load_id=result.load_id)
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
    def _skip_file(self, path: Path, result: LoadResult, reason: str, move_file_after: bool) -> None:
        """The file is dated on a no-trading day of its market: warn, register, move aside."""
        result.status = "skipped"
        day = result.trade_date.isoformat() if result.trade_date else "?"
        who = result.market or "the market"
        result.message = f"{who} was closed on {day} ({reason}) - no trading day, file not loaded"
        log.warning("%s skipped: %s", path.name, result.message)
        self.audit.log(COMPONENT, "file_skipped", result.message, level="WARNING", file_name=path.name,
                       load_id=result.load_id,
                       details={"market": result.market, "trade_date": day, "reason": reason,
                                "file_hash": result.file_hash})
        try:
            self.db.record_load_history(path.name, result.file_hash, result.load_id, HISTORY_SKIPPED,
                                        market=result.market, message=result.message,
                                        file_size=path.stat().st_size)
        except Exception:  # pragma: no cover - bookkeeping must not break the run
            log.exception("could not record the skipped file %s", path.name)
        result.final_path = path
        if move_file_after:
            try:
                result.final_path = move_file(path, self.settings.skipped_dir)
            except OSError as exc:
                log.warning("%s could not be moved to the skipped folder: %s", path.name, exc)

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
            try:
                result.final_path = move_file(path, self.settings.error_dir)
            except OSError as move_exc:   # leave it where it is; the report still goes to the error folder
                log.warning("%s could not be moved to the error folder: %s", path.name, move_exc)
                result.final_path = path
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

    # -- symbol registry -------------------------------------------------
    def _register_symbols(self, result: LoadResult, data_rows: list[DataRow]) -> int:
        """Every ticker of the load goes into Stock_Symbol (new ones without trade
        dates); FirstSeen / LastSeen are widened to the dates loaded."""
        seen: dict[str, tuple[str, str]] = {}
        for row in data_rows:
            if not row.ticker or len(row.trade_date) != 8 or not row.trade_date.isdigit():
                continue
            first, last = seen.get(row.ticker, (row.trade_date, row.trade_date))
            seen[row.ticker] = (min(first, row.trade_date), max(last, row.trade_date))
        new = self.symbols.register_seen(result.market or None, seen)
        if new:
            log.info("%s: %d symbol(s) seen for the first time", result.source_file.name, new)
        return new

    # -- data quality ----------------------------------------------------
    def _expected_tickers(self, path: Path, result: LoadResult, data_rows: list[DataRow]) -> frozenset[str]:
        """Tickers known from other files of the market (for the missing-market-day rule)."""
        if self._known is None:            # single load_file() call: ask the database
            return self.db.distinct_tickers(market=result.market or None, exclude_source_file=path.name)
        key = result.market or None
        registry = self._known.get(key)
        if registry is None:
            # first file of the run for this market: one scan (already includes this file's rows)
            registry = self._known[key] = KnownTickers(self.db.tickers_by_source(key))
        else:
            registry.replace_file(path.name, (r.ticker for r in data_rows))
        return registry.expected_for(path.name)

    def _quality_context(self, path: Path, result: LoadResult, data_rows: list[DataRow]) -> QualityContext:
        expected = self._expected_tickers(path, result, data_rows)
        holidays = frozenset(market_calendar.parse_holiday(h)
                             for h in self.settings.extra_market_holidays)
        # the market's No_Trading_Day table decides which days count as market-open,
        # the Stock_Symbol table which stocks are expected on a day (their trading window)
        return QualityContext(market=result.market, source_file=path.name, load_id=result.load_id,
                              expected_tickers=expected, extra_holidays=holidays,
                              market_open=self.calendar.opener(result.market or None),
                              trading_windows=self.symbols.trading_windows(result.market or None))

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
            context = self._quality_context(path, result, data_rows)
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
        alerts = []
        for issue in issues:
            if issue.alert:
                log.info("ALERT %s: %s", path.name, issue)
                alerts.append({"event": "data_alert", "message": str(issue), "level": "ALERT",
                               "file_name": path.name, "load_id": result.load_id,
                               "details": {"ticker": issue.ticker, "trade_date": issue.trade_date,
                                           "rule": issue.rule}})
        if alerts:
            # one line per file on the console (a bulk load of a year raises tens of
            # thousands of alerts - every one is in the log file and in App_Log), and
            # one ALERT entry per finding, written in a single transaction
            log.warning("ALERT %s: %d alert(s) - %s", path.name, len(alerts),
                        "; ".join(a["message"] for a in alerts[:3]) + (" ..." if len(alerts) > 3 else ""))
            self.audit.log_many(COMPONENT, alerts)

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
