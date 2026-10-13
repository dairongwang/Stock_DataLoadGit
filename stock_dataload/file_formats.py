"""
Custom file format definitions.

A :class:`FileFormat` describes how a *source* file is laid out so the
converter can turn it into the default format
(``<ticker>,<date>,<open>,<high>,<low>,<close>,<vol>`` text file).

Formats are stored in a JSON file (``formats.json`` by default, see
``[formats] file`` in config.ini) and are managed either through the
dashboard or by editing that file.

Column mapping - three ways, checked in this order:

1. ``column_map``  explicit: default column -> source field.  The source
   field is a header/field name (``"Symbol"``) or a 1-based position
   (``"#3"``).  This is what the dashboard's "map each column" mode writes.
2. ``columns``     positional: the sequence of source columns, using
   canonical names/aliases and ``skip`` for columns to ignore.
3. *auto*          the header (or JSON/XML field names) is matched against
   :data:`formats.COLUMN_ALIASES`.
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Iterable

from . import formats

FILE_TYPES = ("auto", "csv", "text", "excel", "json", "xml")
FILE_TYPE_LABELS = {
    "auto": "Auto-detect from file",
    "csv": "CSV (comma separated)",
    "text": "Text (delimited: tab, ; | ...)",
    "excel": "Excel (.xlsx)",
    "json": "JSON",
    "xml": "XML",
}

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,63}$")


class FormatError(Exception):
    """Invalid format definition."""


@dataclass
class FileFormat:
    name: str = ""
    description: str = ""
    file_type: str = "auto"                # auto | csv | text | excel | json | xml
    file_pattern: str = ""                 # glob on the file name, e.g. "*.json" or "NYSE_*.csv"
    columns: list[str] | None = None       # positional source column sequence
    column_map: dict[str, str] = field(default_factory=dict)   # default col -> source field / "#n"
    delimiter: str | None = None           # None = auto-detect (csv/text)
    has_header: bool = True
    skip_rows: int = 0                     # rows to skip before the header/data (title lines)
    date_format: str | None = None         # strptime pattern, "auto", or None (already YYYYMMDD)
    encoding: str = "utf-8-sig"
    sheet: str = ""                        # excel: sheet name or 1-based index; "" = first sheet
    record_path: str = ""                  # json: dotted path to the list of records
    record_tag: str = ""                   # xml: tag of one record element
    skip_bad_rows: bool = False
    builtin: bool = False                  # shipped example - can be edited but marks origin

    # ------------------------------------------------------------------
    @property
    def mapping_mode(self) -> str:
        if self.column_map:
            return "map"
        if self.columns:
            return "sequence"
        return "auto"

    def matches(self, filename: str) -> bool:
        if not self.file_pattern:
            return False
        return any(fnmatch.fnmatch(filename.lower(), p.strip().lower())
                   for p in self.file_pattern.split(";") if p.strip())

    def validate(self) -> None:
        if not self.name or not _NAME_RE.match(self.name):
            raise FormatError("format name is required (letters, digits, space, _ . - ; max 64 chars)")
        if self.file_type not in FILE_TYPES:
            raise FormatError(f"file_type must be one of {FILE_TYPES}")
        if self.delimiter is not None and len(self.delimiter) != 1:
            raise FormatError("delimiter must be a single character (leave empty for auto-detect)")
        if self.skip_rows < 0:
            raise FormatError("skip_rows must be >= 0")
        if self.column_map:
            unknown = [k for k in self.column_map if k not in formats.DEFAULT_COLUMNS]
            if unknown:
                raise FormatError(f"column_map has unknown default column(s): {unknown}")
            missing = [c for c in formats.DEFAULT_COLUMNS if not str(self.column_map.get(c, "")).strip()]
            if missing:
                raise FormatError(f"column_map must map every default column; missing: {missing}")
            for c, ref in self.column_map.items():
                ref = str(ref).strip()
                if ref.startswith("#") and not (ref[1:].isdigit() and int(ref[1:]) >= 1):
                    raise FormatError(f"column_map[{c}]: position reference must look like #1, #2, ... (got {ref!r})")
        if self.columns is not None:
            if not self.columns:
                raise FormatError("columns must not be empty (use null for auto-detect)")
            for c in self.columns:
                if formats.resolve_column(c) is None:
                    raise FormatError(f"unknown column name '{c}' in columns; use ticker/date/open/high/low/close/vol, "
                                      f"an alias, or 'skip'")
        if not self.has_header and self.file_type in ("csv", "text", "excel", "auto") and self.mapping_mode == "auto":
            raise FormatError("a file without header needs 'columns' (sequence) or 'column_map' (with #positions)")

    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FileFormat":
        known = {f.name for f in fields(cls)}
        clean = {k: v for k, v in data.items() if k in known}
        if "columns" in clean and isinstance(clean["columns"], str):
            clean["columns"] = [c.strip() for c in clean["columns"].split(",") if c.strip()] or None
        if "column_map" in clean and clean["column_map"] is None:
            clean["column_map"] = {}
        if "delimiter" in clean and clean["delimiter"] in ("", None):
            clean["delimiter"] = None
        if "date_format" in clean and clean["date_format"] in ("",):
            clean["date_format"] = None
        clean["skip_rows"] = int(clean.get("skip_rows") or 0)
        for b in ("has_header", "skip_bad_rows", "builtin"):
            if b in clean and isinstance(clean[b], str):
                clean[b] = clean[b].strip().lower() in ("1", "true", "yes", "on")
        return cls(**clean)

    @classmethod
    def from_form(cls, form: dict[str, Any]) -> "FileFormat":
        """Build a format from the dashboard's HTML form fields."""
        def get(key: str, default: str = "") -> str:
            v = form.get(key, default)
            return (v if v is not None else default).strip() if isinstance(v, str) else v

        delimiter = get("delimiter")
        delimiter = {"tab": "\t", "\\t": "\t", "comma": ",", "semicolon": ";", "pipe": "|"}.get(delimiter.lower(), delimiter)
        mode = get("mapping_mode", "auto")
        columns = None
        column_map: dict[str, str] = {}
        if mode == "sequence":
            columns = [c.strip() for c in get("columns").split(",") if c.strip()] or None
        elif mode == "map":
            column_map = {c: get(f"map_{c}") for c in formats.DEFAULT_COLUMNS}
        fmt = cls(
            name=get("name"),
            description=get("description"),
            file_type=get("file_type", "auto") or "auto",
            file_pattern=get("file_pattern"),
            columns=columns,
            column_map=column_map,
            delimiter=delimiter or None,
            has_header=str(form.get("has_header", "")).lower() in ("1", "true", "on", "yes"),
            skip_rows=int(get("skip_rows", "0") or 0),
            date_format=get("date_format") or None,
            encoding=get("encoding") or "utf-8-sig",
            sheet=get("sheet"),
            record_path=get("record_path"),
            record_tag=get("record_tag"),
            skip_bad_rows=str(form.get("skip_bad_rows", "")).lower() in ("1", "true", "on", "yes"),
        )
        return fmt


# ---------------------------------------------------------------------------
# Built-in example formats (shipped in formats.json, editable by users)
# ---------------------------------------------------------------------------
BUILTIN_FORMATS: list[FileFormat] = [
    FileFormat(name="default", description="Already in the default layout (ticker,date,open,high,low,close,vol)",
               file_type="auto", builtin=True),
    FileFormat(name="yahoo", description="Yahoo Finance history CSV for ONE symbol (no ticker column): "
                                         "clone it and set <ticker> to =SYMBOL, e.g. =YHOO",
               file_type="csv", date_format="%Y-%m-%d", builtin=True,
               column_map={"ticker": "=YHOO", "date": "Date", "open": "Open", "high": "High", "low": "Low",
                           "close": "Close", "vol": "Volume"}),
    FileFormat(name="date_first", description="date,ticker,vol,close,open,high,low", file_type="auto",
               columns=["date", "ticker", "vol", "close", "open", "high", "low"], builtin=True),
]


class FormatStore:
    """Load/save :class:`FileFormat` definitions from a JSON file."""

    def __init__(self, path: Path, legacy: dict[str, list[str]] | None = None):
        self.path = Path(path)
        self._formats: dict[str, FileFormat] = {}
        self.load(legacy or {})

    # -- persistence -------------------------------------------------------
    def load(self, legacy: dict[str, list[str]] | None = None) -> None:
        self._formats = {}
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise FormatError(f"{self.path} is not valid JSON: {exc}") from exc
            items = data.get("formats", data) if isinstance(data, dict) else data
            if isinstance(items, dict):
                items = [dict(v, name=k) for k, v in items.items()]
            for item in items:
                fmt = FileFormat.from_dict(item)
                self._formats[fmt.name] = fmt
        else:
            for fmt in BUILTIN_FORMATS:
                self._formats[fmt.name] = FileFormat.from_dict(fmt.to_dict())
        # legacy [formats] section from config.ini: name = col,col,...
        for name, cols in (legacy or {}).items():
            if name not in self._formats:
                self._formats[name] = FileFormat(name=name, columns=list(cols), description="from config.ini")

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"formats": [f.to_dict() for f in self._formats.values()]}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # -- access ------------------------------------------------------------
    def all(self) -> list[FileFormat]:
        return sorted(self._formats.values(), key=lambda f: (not f.builtin, f.name.lower()))

    def names(self) -> list[str]:
        return [f.name for f in self.all()]

    def get(self, name: str) -> FileFormat:
        try:
            return self._formats[name]
        except KeyError:
            known = ", ".join(self.names()) or "(none)"
            raise FormatError(f"unknown format '{name}'. Known formats: {known}") from None

    def exists(self, name: str) -> bool:
        return name in self._formats

    def put(self, fmt: FileFormat, old_name: str | None = None) -> None:
        fmt.validate()
        if old_name and old_name != fmt.name and old_name in self._formats:
            del self._formats[old_name]
        self._formats[fmt.name] = fmt
        self.save()

    def delete(self, name: str) -> None:
        if name not in self._formats:
            raise FormatError(f"unknown format '{name}'")
        del self._formats[name]
        self.save()

    def match(self, filename: str) -> FileFormat | None:
        """First user format whose file_pattern matches *filename* (builtins last)."""
        for fmt in sorted(self._formats.values(), key=lambda f: (f.builtin, f.name.lower())):
            if fmt.matches(Path(filename).name):
                return fmt
        return None

    def suggest(self, filename: str) -> FileFormat:
        """Format to use for *filename*: a matching custom one, else auto-detect."""
        return self.match(filename) or FileFormat(name="(auto-detect)")
