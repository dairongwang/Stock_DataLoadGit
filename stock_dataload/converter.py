"""
Convert stock data files delivered in *other* layouts / file types into the
default format and drop the result into the loading folder.

Supported sources (see :mod:`readers`): CSV, delimited text (tab ; |),
Excel (.xlsx), JSON and XML.  Whatever the source, the output is always the
same: a comma separated **text file** with the default column sequence

    <ticker>,<date>,<open>,<high>,<low>,<close>,<vol>

The layout of a source is described by a :class:`FileFormat` (custom
formats are managed in the dashboard / ``formats.json``).  Mapping rules,
in order of precedence: ``column_map`` -> ``columns`` -> header aliases.

Folder workflow::

    conversion folder  --convert-->  loading folder  --load-->  staging table
         (source)                     (default .txt)
    source moved to converted/ after a successful conversion

The converted file is first written to ``work_dir`` and only *moved* into
``loading_dir`` when complete, so the loader never sees a half-written file.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import formats
from .config import Settings
from .file_formats import FileFormat, FormatError, FormatStore
from .readers import ReadError, SourceTable, detect_delimiter, read_source  # noqa: F401 (re-export)

log = logging.getLogger(__name__)

#: Backwards compatible name - a FileFormat *is* the conversion spec.
ConversionSpec = FileFormat


class ConversionError(Exception):
    """The source file could not be converted."""


@dataclass
class ConversionResult:
    source_file: Path
    output_file: Path
    file_type: str
    delimiter: str | None
    mapping: dict[str, int]                 # default column -> source index
    source_header: list[str] | None
    format_name: str = ""
    rows_written: int = 0
    rows_skipped: int = 0
    rejects: list[tuple[int, str]] = field(default_factory=list)
    moved_to_loading: bool = False
    source_moved_to: Path | None = None

    def __str__(self) -> str:
        where = "loading folder" if self.moved_to_loading else str(self.output_file.parent)
        fmt = f", format={self.format_name}" if self.format_name else ""
        return (f"{self.source_file.name} -> {self.output_file.name} ({self.rows_written} rows, "
                f"{self.file_type}{fmt}) in {where}")


_AUTO_DATE_FORMATS = ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y", "%d.%m.%Y",
                      "%d-%b-%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%m/%d/%y", "%b %d %Y",
                      "%Y-%m-%dT%H:%M:%SZ")


def _header_index(header: Sequence[str]) -> dict[str, int]:
    idx: dict[str, int] = {}
    for i, h in enumerate(header):
        for key in (h.strip(), h.strip().lower(), formats.normalise_name(h)):
            idx.setdefault(key, i)
    return idx


def build_mapping(source_columns: Sequence[str]) -> dict[str, int]:
    """Positional/alias mapping: default column -> index in *source_columns*."""
    mapping: dict[str, int] = {}
    unknown: list[str] = []
    for idx, name in enumerate(source_columns):
        canonical = formats.resolve_column(name)
        if canonical is None:
            unknown.append(name)
            continue
        if canonical == formats.SKIP_COLUMN:
            continue
        if canonical in mapping:
            raise ConversionError(
                f"column '{canonical}' matched twice (source columns {mapping[canonical] + 1} and {idx + 1})")
        mapping[canonical] = idx
    missing = [c for c in formats.DEFAULT_COLUMNS if c not in mapping]
    if missing:
        msg = f"could not find column(s) {missing} in source columns {list(source_columns)}"
        if unknown:
            msg += f"; unrecognised field(s): {unknown}"
        msg += (". Define a format with a column sequence / column map (dashboard: Formats, CLI: --columns), "
               "or extend formats.COLUMN_ALIASES.")
        raise ConversionError(msg)
    if unknown:
        log.info("ignoring unrecognised source column(s): %s", unknown)
    return mapping


def constants(fmt: FileFormat) -> dict[str, str]:
    """Columns of the column_map given as a constant: ``"=YHOO"`` -> ticker is always YHOO."""
    return {c: str(v).strip()[1:].strip() for c, v in (fmt.column_map or {}).items()
            if str(v).strip().startswith("=")}


def resolve_mapping(fmt: FileFormat, header: Sequence[str] | None) -> dict[str, int]:
    """Apply the format's mapping rules to the source header.

    Returns default column -> source index.  Columns set to a constant
    (``=VALUE``) are not in the mapping; see :func:`constants`.
    """
    if fmt.column_map:
        mapping: dict[str, int] = {}
        index = _header_index(header) if header else {}
        consts = constants(fmt)
        for col in formats.DEFAULT_COLUMNS:
            ref = str(fmt.column_map.get(col, "")).strip()
            if not ref:
                raise ConversionError(f"column_map does not define '{col}'")
            if col in consts:
                continue
            if ref.startswith("#") and ref[1:].isdigit():
                mapping[col] = int(ref[1:]) - 1
                continue
            if not header:
                raise ConversionError(f"column_map refers to field '{ref}' but the source has no header - use #positions")
            for key in (ref, ref.lower(), formats.normalise_name(ref)):
                if key in index:
                    mapping[col] = index[key]
                    break
            else:
                raise ConversionError(f"column_map: source field '{ref}' (for {col}) not found in {list(header)}")
        return mapping
    if fmt.columns:
        return build_mapping(list(fmt.columns))
    if header:
        return build_mapping(header)
    raise ConversionError("file has no header - the format needs a column sequence or a column map")


def _convert_date(value: str, date_format: str | None, line_no: int) -> str:
    value = value.strip()
    if not date_format:
        # no explicit format: keep YYYYMMDD as is, otherwise try the common patterns
        if len(value) == 8 and value.isdigit():
            return value
        date_format = "auto"
    if date_format == "auto":
        for candidate in _AUTO_DATE_FORMATS:
            try:
                return datetime.strptime(value, candidate).strftime(formats.DEFAULT_DATE_FORMAT)
            except ValueError:
                continue
        raise ConversionError(f"line {line_no}: cannot parse date '{value}' with any known format")
    try:
        return datetime.strptime(value, date_format).strftime(formats.DEFAULT_DATE_FORMAT)
    except ValueError as exc:
        raise ConversionError(f"line {line_no}: date '{value}' does not match format '{date_format}' ({exc})") from exc


class FileConverter:
    """Converts files of other layouts/types into the default text format."""

    def __init__(self, settings: Settings, store: FormatStore | None = None):
        self.settings = settings
        self._store = store

    @property
    def store(self) -> FormatStore:
        if self._store is None:
            self._store = FormatStore(self.settings.formats_file, self.settings.named_formats)
        return self._store

    # ------------------------------------------------------------------
    def resolve_format(self, fmt: FileFormat | str | None, source: Path | None = None) -> FileFormat:
        if isinstance(fmt, FileFormat):
            return fmt
        if isinstance(fmt, str) and fmt:
            try:
                return self.store.get(fmt)
            except FormatError as exc:
                raise ConversionError(str(exc)) from exc
        if source is not None:
            matched = self.store.match(source.name)
            if matched is not None:
                return matched
        return FileFormat(name="")

    def resolve_named_format(self, name: str) -> list[str]:
        """Backwards compatible: column sequence of a named format."""
        fmt = self.resolve_format(name)
        if fmt.columns:
            return list(fmt.columns)
        raise ConversionError(f"format '{name}' has no column sequence")

    # ------------------------------------------------------------------
    def _open(self, source: Path, fmt: FileFormat) -> tuple[SourceTable, dict[str, int]]:
        try:
            table = read_source(source, fmt)
        except ReadError as exc:
            raise ConversionError(str(exc)) from exc
        try:
            mapping = resolve_mapping(fmt, table.header)
        except Exception:
            table.close()          # do not leave the source file open (Windows: cannot move/delete it)
            raise
        return table, mapping

    def inspect(self, source: Path, fmt: FileFormat | str | None = None) -> tuple[str | None, list[str] | None, dict[str, int]]:
        """Return (delimiter, header fields or None, mapping) without converting."""
        source = Path(source)
        fmt = self.resolve_format(fmt, source)
        table, mapping = self._open(source, fmt)
        try:
            return table.delimiter, table.header, mapping
        finally:
            table.close()

    def preview(self, source: Path, fmt: FileFormat | str | None = None, limit: int = 10) -> dict[str, Any]:
        """Dry run for the dashboard: source sample, mapping and converted sample."""
        source = Path(source)
        fmt = self.resolve_format(fmt, source)
        out: dict[str, Any] = {
            "source": source.name, "format": fmt.name or "(auto-detect)", "ok": False,
            "file_type": None, "delimiter": None, "detail": "", "source_header": None,
            "source_rows": [], "mapping": {}, "converted_header": list(formats.DEFAULT_HEADER_FIELDS),
            "converted_rows": [], "errors": [], "total_rows": None,
        }
        try:
            table = read_source(source, fmt)
        except ReadError as exc:
            out["errors"].append(str(exc))
            return out
        out.update(file_type=table.file_type, delimiter=table.delimiter, detail=table.detail,
                   source_header=table.header)
        try:
            mapping = resolve_mapping(fmt, table.header)
        except ConversionError as exc:
            out["errors"].append(str(exc))
            mapping = {}
        out["mapping"] = {c: i + 1 for c, i in mapping.items()}
        out["mapping"].update({c: f"={v}" for c, v in constants(fmt).items()})
        total = 0
        start = 1 + fmt.skip_rows + (1 if fmt.has_header else 0)
        try:
            for line_no, row in enumerate(table.rows, start=start):
                if not row or all(not v.strip() for v in row):
                    continue
                total += 1
                if len(out["source_rows"]) < limit:
                    out["source_rows"].append(row)
                if mapping and len(out["converted_rows"]) < limit:
                    try:
                        out["converted_rows"].append(self._map_row(row, mapping, fmt, line_no))
                    except ConversionError as exc:
                        out["errors"].append(str(exc))
        finally:
            table.close()
        out["total_rows"] = total
        out["ok"] = bool(out["mapping"]) and not out["errors"]
        return out

    # ------------------------------------------------------------------
    @staticmethod
    def _map_row(row: list[str], mapping: dict[str, int], fmt: FileFormat, line_no: int) -> list[str]:
        needed = max(mapping.values()) + 1 if mapping else 0
        if len(row) < needed:
            raise ConversionError(f"line {line_no}: expected at least {needed} fields, found {len(row)}")
        consts = constants(fmt)
        out = [row[mapping[c]].strip() if c in mapping else consts[c] for c in formats.DEFAULT_COLUMNS]
        out[1] = _convert_date(out[1], fmt.date_format, line_no)
        problem = formats.validate_default_row(out)
        if problem:
            raise ConversionError(f"line {line_no}: {problem}")
        return out

    @staticmethod
    def _unique_name(folder: Path, source: Path, out_name: str) -> str:
        """NASDAQ_20250123.txt taken -> NASDAQ_20250123_json.txt -> NASDAQ_20250123_json_2.txt ..."""
        if not (folder / out_name).exists():
            return out_name
        stem = Path(out_name).stem
        tag = source.suffix.lstrip(".").lower() or "converted"
        candidate = f"{stem}_{tag}.txt"
        n = 1
        while (folder / candidate).exists():
            n += 1
            candidate = f"{stem}_{tag}_{n}.txt"
        return candidate

    def convert(self, source: Path, spec: FileFormat | str | None = None, output_name: str | None = None,
                move_to_loading: bool = True, overwrite: bool = False, move_source: bool = False,
                on_conflict: str | None = None) -> ConversionResult:
        """Convert *source* into the default format.

        spec             a FileFormat, a format name from the store, or None
                         (= format matched by file pattern, else auto-detect)
        move_to_loading  True: result goes to loading_dir, False: stays in work_dir
        on_conflict      what to do if the converted file already exists:
                         "error" (default), "overwrite", or "rename"
                         (NASDAQ_20250123.txt -> NASDAQ_20250123_json.txt)
        overwrite        shorthand for on_conflict="overwrite"
        move_source      move the source file to converted_dir afterwards

        Note: by design conversion never touches the database (requirement
        R3); activities go to the rotating log file, and conversion errors
        are recorded in App_Error by the caller (CLI / dashboard).
        """
        source = Path(source)
        if not source.is_file():
            raise ConversionError(f"source file not found: {source}")
        on_conflict = on_conflict or ("overwrite" if overwrite else "error")
        if on_conflict not in ("error", "overwrite", "rename"):
            raise ConversionError(f"on_conflict must be error/overwrite/rename, got {on_conflict!r}")
        fmt = self.resolve_format(spec, source)
        self.settings.ensure_folders()

        table, mapping = self._open(source, fmt)
        out_name = output_name or (source.stem + ".txt")
        final_dir = self.settings.loading_dir if move_to_loading else self.settings.work_dir
        if on_conflict == "rename":
            out_name = self._unique_name(final_dir, source, out_name)
        final_path = final_dir / out_name
        if final_path.exists() and on_conflict == "error":
            table.close()
            raise ConversionError(f"target already exists: {final_path} (choose overwrite or rename)")

        tmp_path = self.settings.work_dir / f"{out_name}.{os.getpid()}.converting"
        result = ConversionResult(source, final_path, table.file_type, table.delimiter, mapping, table.header,
                                  format_name=fmt.name)
        log.info("converting %s (%s%s, format=%s, mapping=%s)", source.name, table.file_type,
                 f" delimiter={table.delimiter!r}" if table.delimiter else "", fmt.name or "auto",
                 {k: v + 1 for k, v in mapping.items()})
        start = 1 + fmt.skip_rows + (1 if fmt.has_header else 0)
        try:
            import csv
            with open(tmp_path, "w", newline="", encoding="utf-8") as dst:
                writer = csv.writer(dst, delimiter=formats.DEFAULT_DELIMITER, lineterminator="\n",
                                    quoting=csv.QUOTE_MINIMAL)
                writer.writerow(formats.DEFAULT_HEADER_FIELDS)
                for line_no, row in enumerate(table.rows, start=start):
                    if not row or all(not v.strip() for v in row):
                        continue
                    try:
                        out = self._map_row(row, mapping, fmt, line_no)
                    except ConversionError as exc:
                        if fmt.skip_bad_rows:
                            result.rejects.append((line_no, str(exc)))
                            continue
                        raise
                    writer.writerow(out)
                    result.rows_written += 1
            if result.rows_written == 0:
                raise ConversionError("no data rows found in source file")
            if final_path.exists():
                final_path.unlink()
            shutil.move(str(tmp_path), str(final_path))
            result.moved_to_loading = move_to_loading
            result.rows_skipped = len(result.rejects)
        finally:
            table.close()
            if tmp_path.exists():
                tmp_path.unlink()
        if move_source:
            result.source_moved_to = self._archive_source(source)
        if result.rejects:
            log.warning("%d row(s) skipped during conversion of %s", len(result.rejects), source.name)
        log.info("%s", result)
        return result

    def _archive_source(self, source: Path) -> Path:
        folder = self.settings.converted_dir
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / source.name
        if dest.exists():
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            dest = folder / f"{source.stem}.{stamp}{source.suffix}"
        shutil.move(str(source), str(dest))
        return dest

    def convert_many(self, sources: Iterable[Path], spec: FileFormat | str | None = None, **kw) -> list[ConversionResult]:
        return [self.convert(Path(s), spec, **kw) for s in sources]

    # ------------------------------------------------------------------
    def pending_sources(self) -> list[Path]:
        """Files waiting in the conversion folder."""
        folder = self.settings.conversion_dir
        if not folder.is_dir():
            return []
        return sorted(p for p in folder.iterdir() if p.is_file() and not p.name.startswith(".")
                      and not p.name.endswith((".converting", ".error.txt")))

    def convert_folder(self, spec: FileFormat | str | None = None, on_conflict: str = "rename",
                       move_source: bool = True) -> list[tuple[Path, ConversionResult | None, str]]:
        """Convert every file in the conversion folder.

        Returns (source, result or None, error message) per file.  Files that
        fail stay in the conversion folder with a ``<name>.error.txt`` note.
        By default a name clash in the loading folder is resolved by renaming
        (so two sources never overwrite each other's output).
        """
        outcomes: list[tuple[Path, ConversionResult | None, str]] = []
        for src in self.pending_sources():
            try:
                res = self.convert(src, spec, on_conflict=on_conflict, move_source=move_source)
                err_note = src.with_name(src.name + ".error.txt")
                if err_note.exists():
                    err_note.unlink()
                outcomes.append((src, res, ""))
            except ConversionError as exc:
                log.error("conversion of %s failed: %s", src.name, exc)
                try:
                    src.with_name(src.name + ".error.txt").write_text(
                        f"{datetime.now():%Y-%m-%d %H:%M:%S}  {exc}\n", encoding="utf-8")
                except OSError:  # pragma: no cover
                    pass
                outcomes.append((src, None, str(exc)))
        return outcomes
