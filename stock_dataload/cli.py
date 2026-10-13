"""
Command line interface.

    python -m stock_dataload dashboard            # web dashboard for business users
    python -m stock_dataload load                 # load every file in the loading folder
    python -m stock_dataload load FILE [FILE..]   # load specific file(s)
    python -m stock_dataload convert SOURCE [..]  # convert (csv/text/xlsx/json/xml) + move to loading folder
    python -m stock_dataload convert-folder       # convert everything in the conversion folder
    python -m stock_dataload inspect SOURCE       # show how a file would be mapped
    python -m stock_dataload formats              # list the custom file formats
    python -m stock_dataload init-db              # create the staging table
    python -m stock_dataload status               # what is in the staging table
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, formats
from .config import ConfigError, load_settings
from .converter import ConversionError, FileConverter
from .db import DatabaseError, StageDatabase
from .file_formats import FILE_TYPES, FileFormat, FormatError, FormatStore
from .loader import StageLoader
from .logging_setup import setup_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stock_dataload",
        description="Load stock trading data files into a database staging table.",
        epilog=f"default file format: {formats.DEFAULT_HEADER_LINE}",
    )
    parser.add_argument("--config", metavar="FILE", help="path to config.ini")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug output")
    parser.add_argument("-q", "--quiet", action="store_true", help="warnings/errors only")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    # load ---------------------------------------------------------------
    p = sub.add_parser("load", help="load default-format files into the staging table")
    p.add_argument("files", nargs="*", type=Path, help="file(s) to load (default: everything in the loading folder)")
    p.add_argument("--market", help="Market value for all rows (default: derived from the file name)")
    p.add_argument("--truncate", action="store_true", help="empty the staging table before loading")
    p.add_argument("--keep", action="store_true", help="do not move files to archive/error after loading")
    p.add_argument("--skip-bad-rows", action="store_true", help="skip invalid rows instead of failing the file")

    # convert ------------------------------------------------------------
    p = sub.add_parser("convert", help="convert file(s) with a different layout to the default format")
    p.add_argument("sources", nargs="+", type=Path, help="source file(s)")
    _add_format_options(p)
    p.add_argument("--output-name", help="name of the converted file (only for a single source)")
    p.add_argument("--no-move", action="store_true", help="leave the converted file in the work folder")
    p.add_argument("--overwrite", action="store_true", help="replace an existing converted file")
    p.add_argument("--rename", action="store_true", help="if the converted file exists, use a new name instead of failing")
    p.add_argument("--move-source", action="store_true", help="move the source to the converted folder afterwards")

    # convert-folder -----------------------------------------------------
    p = sub.add_parser("convert-folder", help="convert every file in the conversion folder into the loading folder")
    p.add_argument("--format", dest="named_format", help="force this format for all files (default: match by file pattern / auto)")
    p.add_argument("--on-conflict", choices=("rename", "overwrite", "error"), default="rename",
                   help="if the converted file already exists: rename (default, NASDAQ_20250123_json.txt), overwrite or error")
    p.add_argument("--keep-source", action="store_true", help="leave sources in the conversion folder")

    # formats / dashboard ------------------------------------------------
    sub.add_parser("formats", help="list the custom file formats")
    p = sub.add_parser("dashboard", help="start the web dashboard")
    p.add_argument("--host", help="bind address (default from config, 127.0.0.1)")
    p.add_argument("--port", type=int, help="port (default from config, 8050)")
    p.add_argument("--debug", action="store_true")

    # inspect ------------------------------------------------------------
    p = sub.add_parser("inspect", help="show the detected file type/column mapping of a file")
    p.add_argument("source", type=Path)
    _add_format_options(p)

    sub.add_parser("init-db", help="create the staging table if it does not exist")
    sub.add_parser("status", help="show files loaded into the staging table")
    return parser


def _add_format_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--format", dest="named_format", help="use a saved format (see 'formats' / the dashboard)")
    p.add_argument("--type", dest="file_type", choices=FILE_TYPES, help="source file type (default: auto-detect)")
    p.add_argument("--columns", help="comma separated source column sequence, e.g. date,ticker,vol,close,open,high,low "
                                     "(use 'skip' for columns to ignore). Default: detect from header/field names")
    p.add_argument("--map", dest="column_map", help="explicit mapping default=source, e.g. "
                                                    "'ticker=Symbol,date=#2,open=Open,high=High,low=Low,close=Close,vol=Volume' (=VALUE for a constant)")
    p.add_argument("--delimiter", help="csv/text delimiter (default: auto-detect). Use 'tab' for tab")
    p.add_argument("--no-header", action="store_true", help="source has no header line (requires --columns or --map with #positions)")
    p.add_argument("--skip-rows", type=int, help="rows to skip before the header/data")
    p.add_argument("--date-format", help="strptime pattern of the source date, e.g. %%Y-%%m-%%d (default: auto)")
    p.add_argument("--sheet", help="excel: sheet name or number (default: first)")
    p.add_argument("--record-path", help="json: dotted path to the list of records (default: auto)")
    p.add_argument("--record-tag", help="xml: tag of one record element (default: auto)")
    p.add_argument("--encoding", help="source encoding (default utf-8-sig)")
    p.add_argument("--skip-bad-rows", action="store_true", help="skip rows that cannot be converted")


def _spec_from_args(args, converter: FileConverter) -> FileFormat | None:
    """Build a FileFormat from CLI options; None = match by file pattern / auto-detect."""
    named = getattr(args, "named_format", None)
    base = converter.resolve_format(named) if named else None
    overrides = {
        "file_type": getattr(args, "file_type", None),
        "delimiter": getattr(args, "delimiter", None),
        "skip_rows": getattr(args, "skip_rows", None),
        "date_format": getattr(args, "date_format", None),
        "sheet": getattr(args, "sheet", None),
        "record_path": getattr(args, "record_path", None),
        "record_tag": getattr(args, "record_tag", None),
        "encoding": getattr(args, "encoding", None),
    }
    if overrides["delimiter"]:
        d = overrides["delimiter"]
        d = {"tab": "\t", "\\t": "\t", "comma": ",", "semicolon": ";", "pipe": "|"}.get(d.lower(), d)
        if len(d) != 1:
            raise ConversionError("delimiter must be a single character")
        overrides["delimiter"] = d
    columns = None
    if getattr(args, "columns", None):
        columns = [c.strip() for c in args.columns.split(",")]
    column_map: dict[str, str] = {}
    if getattr(args, "column_map", None):
        for pair in args.column_map.split(","):
            if "=" not in pair:
                raise ConversionError(f"--map entries look like default=source, got '{pair}'")
            k, v = pair.split("=", 1)
            column_map[k.strip().lower()] = v.strip()
    no_header = getattr(args, "no_header", False)
    skip_bad = getattr(args, "skip_bad_rows", False)
    if base is None and columns is None and not column_map and not no_header and not skip_bad \
            and not any(v for v in overrides.values()):
        return None
    fmt = FileFormat.from_dict(base.to_dict()) if base else FileFormat(name="(command line)")
    if base:
        fmt.name = base.name
    for k, v in overrides.items():
        if v not in (None, ""):
            setattr(fmt, k, v)
    if columns is not None:
        fmt.columns, fmt.column_map = columns, {}
    if column_map:
        fmt.column_map, fmt.columns = column_map, None
    if no_header:
        fmt.has_header = False
    if skip_bad:
        fmt.skip_bad_rows = True
    if not fmt.has_header and fmt.mapping_mode == "auto" and fmt.file_type in ("auto", "csv", "text", "excel"):
        raise ConversionError("--no-header requires --columns, --map (with #positions) or a --format that defines them")
    try:
        fmt.validate() if fmt.name and fmt.name != "(command line)" else None
    except FormatError as exc:
        raise ConversionError(str(exc)) from exc
    return fmt


def cmd_load(args, settings) -> int:
    if args.skip_bad_rows:
        settings.skip_bad_rows = True
    loader = StageLoader(settings)
    results = loader.run(files=args.files or None, market=args.market, truncate=args.truncate,
                         move_files=not args.keep)
    if not results:
        print(f"No files to load in {settings.loading_dir}")
        return 0
    for r in results:
        print(r)
    failed = [r for r in results if not r.ok]
    return 1 if failed else 0


def cmd_convert(args, settings) -> int:
    converter = FileConverter(settings)
    spec = _spec_from_args(args, converter)
    if args.output_name and len(args.sources) > 1:
        raise ConversionError("--output-name can only be used with a single source file")
    rc = 0
    for src in args.sources:
        try:
            res = converter.convert(src, spec, output_name=args.output_name, move_to_loading=not args.no_move,
                                    on_conflict="overwrite" if args.overwrite else "rename" if args.rename else "error",
                                    move_source=args.move_source)
            print(res)
        except ConversionError as exc:
            print(f"ERROR {src}: {exc}", file=sys.stderr)
            rc = 1
    return rc


def cmd_convert_folder(args, settings) -> int:
    converter = FileConverter(settings)
    outcomes = converter.convert_folder(args.named_format or None, on_conflict=args.on_conflict,
                                        move_source=not args.keep_source)
    if not outcomes:
        print(f"Nothing to convert in {settings.conversion_dir}")
        return 0
    rc = 0
    for src, res, err in outcomes:
        if res is not None:
            print(res)
        else:
            print(f"ERROR {src.name}: {err}", file=sys.stderr)
            rc = 1
    return rc


def cmd_inspect(args, settings) -> int:
    converter = FileConverter(settings)
    spec = _spec_from_args(args, converter)
    p = converter.preview(args.source, spec, limit=3)
    print(f"file      : {args.source}")
    print(f"format    : {p['format']}")
    print(f"type      : {p['file_type']}" + (f"  delimiter={p['delimiter']!r}" if p["delimiter"] else "") +
          (f"  {p['detail']}" if p["detail"] else ""))
    print(f"header    : {p['source_header']}")
    print("mapping   : (default column <- source column #)")
    header = p["source_header"]
    for col in formats.DEFAULT_COLUMNS:
        n = p["mapping"].get(col)
        if isinstance(n, str):
            print(f"   {col:<7} <- constant {n[1:]!r}")
            continue
        src_name = header[n - 1] if header and n and n - 1 < len(header) else "?"
        print(f"   {col:<7} <- #{n if n else '?'} ({src_name})")
    print(f"rows      : {p['total_rows']}")
    for row in p["converted_rows"]:
        print("   ", ",".join(row))
    for e in p["errors"]:
        print(f"ERROR     : {e}", file=sys.stderr)
    return 0 if p["ok"] else 1


def cmd_formats(args, settings) -> int:
    store = FormatStore(settings.formats_file, settings.named_formats)
    print(f"formats file: {store.path}")
    for f in store.all():
        mapping = ("map " + ",".join(f"{k}={v}" for k, v in f.column_map.items()) if f.column_map
                   else "sequence " + ",".join(f.columns) if f.columns else "auto")
        print(f"  {f.name:<18} {f.file_type:<6} pattern={f.file_pattern or '-':<18} {mapping}")
        if f.description:
            print(f"  {'':<18} {f.description}")
    return 0


def cmd_dashboard(args, settings) -> int:
    from .dashboard import run
    run(settings, host=args.host, port=args.port, debug=args.debug)
    return 0


def cmd_init_db(args, settings) -> int:
    settings.ensure_folders()
    db = StageDatabase(settings)
    db.ensure_stage_table()
    print(f"staging table {settings.stage_table} is ready (sqlite: {settings.sqlite_path})")
    return 0


def cmd_status(args, settings) -> int:
    db = StageDatabase(settings)
    db.ensure_stage_table()
    rows = db.summary()
    print(f"database: {settings.sqlite_path}   table: {settings.stage_table}")
    print(f"conversion folder: {settings.conversion_dir}")
    sources = FileConverter(settings).pending_sources()
    print(f"files waiting for conversion: {len(sources)}" + (" - " + ", ".join(p.name for p in sources) if sources else ""))
    print(f"loading folder: {settings.loading_dir}")
    pending = StageLoader(settings, db).pending_files()
    print(f"files waiting to be loaded: {len(pending)}" + (" - " + ", ".join(p.name for p in pending) if pending else ""))
    if not rows:
        print("staging table is empty")
        return 0
    print(f"{'SourceFile':<40} {'LoadId':<32} {'Rows':>8}  LoadedAt")
    total = 0
    for src, load_id, n, at in rows:
        print(f"{src:<40} {load_id:<32} {n:>8}  {at}")
        total += n
    print(f"{'total':<40} {'':<32} {total:>8}")
    return 0


COMMANDS = {
    "load": cmd_load,
    "convert": cmd_convert,
    "convert-folder": cmd_convert_folder,
    "inspect": cmd_inspect,
    "formats": cmd_formats,
    "dashboard": cmd_dashboard,
    "init-db": cmd_init_db,
    "status": cmd_status,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    setup_logging(settings.log_dir, verbose=args.verbose, quiet=args.quiet)
    try:
        return COMMANDS[args.command](args, settings)
    except (ConversionError, DatabaseError, ConfigError, FormatError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
