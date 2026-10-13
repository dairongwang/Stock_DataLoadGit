"""
Load default-format stock data files from the loading folder into the
staging table.

This is the Python counterpart of the SSIS ``LoadStage.dtsx`` package:

    Flat File Source  ->  Derived Column (Market)  ->  OLE DB Destination

Work flow for every file found in ``loading_dir``:

1. check the header is the default column sequence
   (``ticker,date,open,high,low,close,vol``) - otherwise the file is moved
   to ``error_dir`` with a hint to run the converter first;
2. derive ``Market`` from the file name (``NASDAQ_20250123.txt`` -> ``NASDAQ``)
   unless an explicit market is given;
3. validate the rows and bulk insert them inside ONE transaction;
4. on success move the file to ``archive_dir``, on failure roll back and
   move the file to ``error_dir`` together with a ``.error.txt`` report.
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

from . import formats
from .config import Settings
from .db import StageDatabase, StageRow, now_iso

log = logging.getLogger(__name__)


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
    rows_loaded: int = 0
    rows_rejected: int = 0
    rows_replaced: int = 0
    message: str = ""
    final_path: Path | None = None
    rejects: list[RejectedRow] = field(default_factory=list)
    started: datetime = field(default_factory=datetime.now)
    finished: datetime | None = None

    @property
    def ok(self) -> bool:
        return self.status == "loaded"

    def __str__(self) -> str:
        base = f"{self.source_file.name}: {self.status}"
        if self.ok:
            base += f" ({self.rows_loaded} rows, market={self.market or '-'}, load_id={self.load_id})"
            if self.rows_rejected:
                base += f" [{self.rows_rejected} rows skipped]"
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

    def __init__(self, settings: Settings, db: StageDatabase | None = None):
        self.settings = settings
        self.db = db or StageDatabase(settings)
        self._market_re = re.compile(settings.market_from_filename_regex) if settings.market_from_filename_regex else None

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
        do_truncate = self.settings.truncate_before_load if truncate is None else truncate
        results: list[LoadResult] = []
        for i, path in enumerate(targets):
            # truncating applies once per run, before the first file
            res = self.load_file(path, market=market, truncate=(do_truncate and i == 0), move_file_after=move_files)
            results.append(res)
        loaded = sum(1 for r in results if r.ok)
        log.info("run complete: %d file(s) loaded, %d failed", loaded, len(results) - loaded)
        return results

    def load_file(self, path: Path, market: str | None = None, truncate: bool = False,
                  move_file_after: bool = True) -> LoadResult:
        path = Path(path)
        result = LoadResult(source_file=path, load_id=uuid.uuid4().hex)
        log.info("loading %s (load_id=%s)", path.name, result.load_id)
        try:
            if not path.is_file():
                raise LoadError(f"file not found: {path}")
            result.market = self.derive_market(path, market)
            self.db.ensure_stage_table()
            with self.db.connection() as conn:
                if truncate:
                    self.db.truncate(conn)
                elif self.settings.replace_existing_file_rows:
                    result.rows_replaced = self.db.delete_source_file(conn, path.name)
                result.rows_loaded = self.db.insert_rows(conn, self._rows(path, result))
                if result.rejects and not self.settings.skip_bad_rows:
                    raise LoadError(
                        f"{len(result.rejects)} invalid row(s), first: line {result.rejects[0].line_no}: "
                        f"{result.rejects[0].reason}  (set skip_bad_rows = true to load the good rows)"
                    )
                if result.rows_loaded == 0:
                    raise LoadError("file contains no data rows")
            result.status = "loaded"
            result.rows_rejected = len(result.rejects)
            if move_file_after:
                result.final_path = move_file(path, self.settings.archive_dir)
            else:
                result.final_path = path
            if result.rejects:
                self._write_reject_report(path, result)
            log.info("%s", result)
        except Exception as exc:  # noqa: BLE001 - we want to quarantine on any failure
            result.status = "failed"
            result.rows_loaded = 0
            result.rows_rejected = len(result.rejects)
            result.message = str(exc)
            log.error("failed to load %s: %s", path.name, exc, exc_info=not isinstance(exc, LoadError))
            if path.is_file() and move_file_after:
                result.final_path = move_file(path, self.settings.error_dir)
                self._write_error_report(result)
        finally:
            result.finished = datetime.now()
        return result

    # ------------------------------------------------------------------
    def _rows(self, path: Path, result: LoadResult) -> Iterator[StageRow]:
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
                ticker, date, open_, high, low, close, vol = row
                yield StageRow(result.market, ticker, date, open_, high, low, close, vol,
                               path.name, result.load_id, loaded_at)

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

    def _write_reject_report(self, path: Path, result: LoadResult) -> None:
        target = self.settings.error_dir / f"{path.name}.rejects.txt"
        self.settings.error_dir.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="") as fh:
            fh.write(f"# rows skipped while loading {path.name} (load_id={result.load_id})\n")
            fh.write("# line,reason,data\n")
            for r in result.rejects:
                fh.write(f"{r.line_no},{r.reason},{','.join(r.raw)}\n")
        log.warning("%d row(s) skipped - see %s", len(result.rejects), target)
