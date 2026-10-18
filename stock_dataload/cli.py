"""
Command line interface.

    python -m stock_dataload dashboard            # web dashboard for business users
    python -m stock_dataload load                 # load every file in the loading folder
    python -m stock_dataload load FILE [FILE..]   # load specific file(s)
    python -m stock_dataload load-folder FOLDER   # bulk load: every file in a folder
    python -m stock_dataload promote daily        # staging -> active daily table (validated)
    python -m stock_dataload promote weekly       # daily -> weekly table; also: monthly, all
    python -m stock_dataload no-trading-days update   # generate next year's No_Trading_Day rows (weekends + holidays)
    python -m stock_dataload symbols set HCMAU --first-trade-date 2025-08-01   # Stock_Symbol table (trading windows)
    python -m stock_dataload convert SOURCE [..]  # convert (csv/text/xlsx/json/xml) + move to loading folder
    python -m stock_dataload convert-folder       # convert everything in the conversion folder
    python -m stock_dataload inspect SOURCE       # show how a file would be mapped
    python -m stock_dataload formats              # list the custom file formats
    python -m stock_dataload init-db              # create the staging table
    python -m stock_dataload status               # what is in the staging table
    python -m stock_dataload user add NAME ...    # user accounts for the dashboard (add/list/passwd/role/...)
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from . import __version__, formats
from .config import ConfigError, load_settings
from .audit import AuditLogger
from .auth import ROLES, AuthError, UserStore
from .converter import ConversionError, FileConverter
from .correction import CorrectionService
from .db import DatabaseError, StageDatabase
from .file_formats import FILE_TYPES, FileFormat, FormatError, FormatStore
from .loader import LoadError, StageLoader
from .logging_setup import setup_logging
from .promotion import PROC_DAILY, PROC_MONTHLY, PROC_WEEKLY, PromotionError, PromotionService
from .quality import DataErrorRepository
from .symbols import SymbolError, SymbolRegistry
from .trading_calendar import RULE_SETS, CalendarError, TradingCalendar, default_update_years


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
    p.add_argument("--include-no-trading-days", action="store_true",
                   help="load files dated on a no-trading day too (default: skip them with a warning)")

    # load-folder --------------------------------------------------------
    p = sub.add_parser("load-folder", help="bulk load: every default-format file in a folder (files stay in place)")
    p.add_argument("folder", type=Path, help="folder with default-format files, e.g. ../NASDAQ_2023")
    p.add_argument("--pattern", action="append", dest="patterns", metavar="GLOB",
                   help="file name pattern(s), repeatable (default: [loader] file_patterns, *.txt)")
    p.add_argument("--recursive", action="store_true", help="include sub-folders")
    p.add_argument("--market", help="Market value for all rows (default: derived from each file name)")
    p.add_argument("--truncate", action="store_true", help="empty the staging table before the first file")
    p.add_argument("--move", action="store_true",
                   help="move files to the archive / error folder afterwards (default: leave them where they are)")
    p.add_argument("--skip-bad-rows", action="store_true", help="skip invalid rows instead of failing the file")
    p.add_argument("--stop-on-error", action="store_true", help="stop at the first file that fails (default: continue)")
    p.add_argument("--promote", action="store_true",
                   help="afterwards run promote daily, weekly and monthly (incremental)")
    p.add_argument("--include-no-trading-days", action="store_true",
                   help="load files dated on a no-trading day too (default: skip them with a warning)")

    # no-trading-days ----------------------------------------------------
    p = sub.add_parser("no-trading-days", aliases=["calendar"],
                       help="the trading calendar: No_Trading_Day table (weekends + holidays per market)")
    csub = p.add_subparsers(dest="calendar_command", required=True)
    c = csub.add_parser("update", help="generate the no-trading days of a year (run before the year begins)")
    c.add_argument("--year", type=int, action="append", dest="years", metavar="YYYY",
                   help="year(s) to generate, repeatable (default: this year and next year)")
    c.add_argument("--from", dest="year_from", type=int, metavar="YYYY", help="first year of a range")
    c.add_argument("--to", dest="year_to", type=int, metavar="YYYY", help="last year of a range")
    c.add_argument("--market", action="append", dest="markets", help="only this market (repeatable; default: all active)")
    c = csub.add_parser("list", help="show the no-trading days of a market")
    c.add_argument("market")
    c.add_argument("--year", type=int, help="calendar year (default: this year)")
    c.add_argument("--all", action="store_true", help="include weekends (default: holidays / closures only)")
    c = csub.add_parser("check", help="is the market open on a date?")
    c.add_argument("market")
    c.add_argument("date", help="YYYY-MM-DD or YYYYMMDD")
    c = csub.add_parser("add", help="add an ad-hoc closure (e.g. a day of mourning)")
    c.add_argument("market")
    c.add_argument("date", help="YYYY-MM-DD or YYYYMMDD")
    c.add_argument("--reason", default="", help="why the market is closed")
    c = csub.add_parser("remove", help="remove a closure - the market trades on that day")
    c.add_argument("market")
    c.add_argument("date", help="YYYY-MM-DD or YYYYMMDD")
    csub.add_parser("markets", help="list the markets the calendar is kept for")
    c = csub.add_parser("add-market", help="keep a calendar for another market")
    c.add_argument("market", help="market code as used in file names, e.g. LSE")
    c.add_argument("--name", default="", help="display name")
    c.add_argument("--rules", choices=tuple(RULE_SETS), default="us",
                   help="how its days are generated: us = weekends + US exchange holidays, weekends = weekends only")
    for name, help_text in (("enable", "re-activate a market"), ("disable", "deactivate a market")):
        c = csub.add_parser(name, help=help_text)
        c.add_argument("market")

    # symbols ------------------------------------------------------------
    p = sub.add_parser("symbols", help="the stock symbol table: market, first / last trade date per symbol")
    ssub = p.add_subparsers(dest="symbols_command", required=True)
    c = ssub.add_parser("list", help="list symbols")
    c.add_argument("--ticker", help="ticker prefix, e.g. HCM")
    c.add_argument("--market")
    c.add_argument("--missing-dates", action="store_true", help="only symbols without a first trade date (not checked for missing days)")
    c.add_argument("--suspicious", action="store_true", help="only symbols with loaded data before their first trade date")
    c.add_argument("--limit", type=int, default=100)
    c = ssub.add_parser("show", help="show one symbol")
    c.add_argument("ticker")
    c.add_argument("--market")
    c = ssub.add_parser("set", help="set the first / last trade date (and name) of a symbol; creates it if needed")
    c.add_argument("ticker")
    c.add_argument("--market", help="the market (needed if the symbol is new or exists on several markets)")
    c.add_argument("--first-trade-date", dest="first", metavar="DATE", help="YYYY-MM-DD; '' clears it")
    c.add_argument("--last-trade-date", dest="last", metavar="DATE", help="YYYY-MM-DD (delisting); '' clears it")
    c.add_argument("--name")
    c.add_argument("--note")
    c = ssub.add_parser("delete", help="remove a symbol from the table")
    c.add_argument("ticker")
    c.add_argument("--market")
    c = ssub.add_parser("import", help="load first / last trade dates from a CSV file (ticker, market, first_trade_date, last_trade_date, name)")
    c.add_argument("file", type=Path)
    c.add_argument("--market", help="market for rows without a market column")
    c = ssub.add_parser("export", help="write the table as CSV (edit and import it again)")
    c.add_argument("file", type=Path, nargs="?", help="output file (default: print)")
    c.add_argument("--market")

    # promote ------------------------------------------------------------
    p = sub.add_parser("promote", help="promote data into the active tables (daily / weekly / monthly)")
    p.add_argument("what", choices=("daily", "weekly", "monthly", "all"),
                   help="daily: staging -> daily table (validated); weekly / monthly: daily -> aggregates; "
                        "all: daily, then weekly, then monthly")
    p.add_argument("--full", action="store_true",
                   help="re-promote everything (default: only what changed since the last run)")
    p.add_argument("--file", dest="source_file", help="daily: only the rows of this source file")
    p.add_argument("--load-id", help="daily: only the rows of this load id")
    p.add_argument("--ticker", help="only this stock symbol")
    p.add_argument("--from", dest="date_from", metavar="DATE", help="daily: from this trade date (YYYYMMDD / YYYY-MM-DD)")
    p.add_argument("--to", dest="date_to", metavar="DATE", help="daily: up to this trade date")

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

    sub.add_parser("init-db", help="create the staging / active / log / error / history tables if they do not exist")
    sub.add_parser("status", help="show files loaded into the staging table and the active tables")
    p = sub.add_parser("promotions", help="show the promotion run registry (Promotion_History table)")
    p.add_argument("--limit", type=int, default=20, help="number of entries (default 20)")
    p.add_argument("--procedure", choices=(PROC_DAILY, PROC_WEEKLY, PROC_MONTHLY), help="filter by procedure")

    # audit / quality ----------------------------------------------------
    p = sub.add_parser("logs", help="show the app activity log (App_Log table)")
    p.add_argument("--limit", type=int, default=50, help="number of entries (default 50)")
    p.add_argument("--component", help="filter by component (loader, converter, cli, ...)")
    p.add_argument("--level", help="filter by level (INFO, WARNING, ERROR, ALERT)")
    p = sub.add_parser("errors", help="show operational errors (App_Error table)")
    p.add_argument("--limit", type=int, default=50, help="number of entries (default 50)")
    p.add_argument("--component", help="filter by component")
    p = sub.add_parser("data-errors", help="show data quality findings (Data_Error table)")
    p.add_argument("--limit", type=int, default=50, help="number of entries (default 50)")
    p.add_argument("--file", dest="source_file", help="filter by source file name")
    p.add_argument("--alerts", action="store_true", help="alert-level findings only")
    p = sub.add_parser("history", help="show the file load registry (Load_History table)")
    p.add_argument("--limit", type=int, default=50, help="number of entries (default 50)")
    p.add_argument("--file", dest="file_name", help="filter by file name")

    # users ----------------------------------------------------------------
    p = sub.add_parser("user", help="manage dashboard user accounts (App_User table)")
    usub = p.add_subparsers(dest="user_command", required=True)
    u = usub.add_parser("add", help="add a user (use this to create the initial administrator)")
    u.add_argument("username")
    u.add_argument("--role", choices=ROLES, default="viewer")
    u.add_argument("--name", default="", help="display name")
    u.add_argument("--password", help="password (default: prompt, or $STOCK_DATALOAD_PASSWORD)")
    u.add_argument("--must-change", action="store_true", help="the user must change the password at the first sign-in")
    usub.add_parser("list", help="list the user accounts")
    u = usub.add_parser("passwd", help="set a new password for a user")
    u.add_argument("username")
    u.add_argument("--password", help="password (default: prompt, or $STOCK_DATALOAD_PASSWORD)")
    u.add_argument("--must-change", action="store_true")
    u = usub.add_parser("role", help="change the role of a user")
    u.add_argument("username")
    u.add_argument("role", choices=ROLES)
    for name, help_text in (("enable", "re-activate a user"), ("disable", "deactivate a user (cannot sign in)"),
                            ("delete", "delete a user")):
        u = usub.add_parser(name, help=help_text)
        u.add_argument("username")
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
                         move_files=not args.keep, progress=lambda r: print(r, flush=True),
                         include_no_trading_days=args.include_no_trading_days)
    if not results:
        print(f"No files to load in {settings.loading_dir}")
        return 0
    skipped = [r for r in results if r.skipped]
    if skipped:
        print(f"{len(skipped)} file(s) skipped - dated on a no-trading day (see 'no-trading-days list'; "
              f"--include-no-trading-days loads them anyway)")
    failed = [r for r in results if r.failed]
    return 1 if failed else 0


def cmd_load_folder(args, settings) -> int:
    if args.skip_bad_rows:
        settings.skip_bad_rows = True
    loader = StageLoader(settings)
    try:
        summary = loader.load_folder(args.folder, patterns=args.patterns or None, recursive=args.recursive,
                                     market=args.market, move_files=args.move, truncate=args.truncate,
                                     stop_on_error=args.stop_on_error, progress=lambda r: print(r, flush=True),
                                     include_no_trading_days=args.include_no_trading_days)
    except LoadError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(summary)
    rc = 0 if summary.ok else 1
    if summary.status == "interrupted":
        return 130
    if args.promote and summary.loaded:
        rc = max(rc, _promote("all", PromotionService(settings), full=False))
    return rc


def cmd_no_trading_days(args, settings) -> int:
    cal = TradingCalendar(settings)
    try:
        cmd = args.calendar_command
        if cmd == "update":
            years = list(args.years or [])
            if args.year_from or args.year_to:
                start = args.year_from or args.year_to
                end = args.year_to or args.year_from
                years += list(range(min(start, end), max(start, end) + 1))
            years = sorted(set(years)) or default_update_years()
            results = cal.update_years(years, args.markets)
            for r in results:
                print(r)
            print(f"No_Trading_Day table updated for {', '.join(str(y) for y in years)} "
                  f"({sum(r.added for r in results)} day(s) added)")
            return 0
        if cmd == "list":
            year = args.year or date.today().year
            if cal.get_market(args.market) is None:
                print(f"ERROR: unknown market '{args.market}' - see 'no-trading-days markets'", file=sys.stderr)
                return 1
            rows = cal.days(args.market, year, holidays_only=not args.all)
            if not rows:
                print(f"no no-trading days recorded for {args.market.upper()} in {year} - run "
                      f"'no-trading-days update --year {year}'")
                return 0
            print(f"{args.market.upper()} {year}: {len(rows)} no-trading day(s)"
                  + ("" if args.all else " (weekends not shown, use --all)"))
            for d in rows:
                print(f"  {d.trade_date}  {d.weekday:<9} {d.reason:<45} {d.source}")
            return 0
        if cmd == "check":
            from .trading_calendar import parse_day
            day = parse_day(args.date)
            reason = cal.closure_reason(args.market, day)
            if reason:
                print(f"{args.market.upper()} is CLOSED on {day.isoformat()} ({reason})")
            else:
                print(f"{args.market.upper()} is open on {day.isoformat()}")
            return 0
        if cmd == "add":
            row = cal.add_day(args.market, args.date, args.reason)
            print(f"{row.market} closed on {row.trade_date}: {row.reason}")
            return 0
        if cmd == "remove":
            row = cal.remove_day(args.market, args.date)
            print(f"{row.market} trades on {row.trade_date} ({row.reason} removed)")
            return 0
        if cmd == "markets":
            print(f"{'Market':<10} {'Rules':<10} {'Active':<8} {'Years':<14} Name")
            for m in cal.markets():
                years = cal.years_available(m.market)
                span = f"{years[0]}-{years[-1]}" if years else "-"
                print(f"{m.market:<10} {m.rules:<10} {'yes' if m.is_active else 'no':<8} {span:<14} {m.name or ''}")
            return 0
        if cmd == "add-market":
            m = cal.add_market(args.market, args.name, args.rules)
            print(f"market {m.market} added ({RULE_SETS[m.rules]}); generate its days with "
                  f"'no-trading-days update --market {m.market}'")
            return 0
        m = cal.set_market_active(args.market, args.calendar_command == "enable")
        print(f"market {m.market} {'active' if m.is_active else 'inactive'}")
        return 0
    except CalendarError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def cmd_symbols(args, settings) -> int:
    reg = SymbolRegistry(settings)
    try:
        cmd = args.symbols_command
        if cmd == "list":
            rows = reg.find(args.ticker, args.market, missing_dates=args.missing_dates, suspicious=args.suspicious,
                            limit=args.limit)
            counts = reg.counts(args.market)
            if not rows:
                print("no symbols match")
            else:
                print(f"{'Ticker':<10} {'Market':<8} {'First trade':<12} {'Last trade':<12} {'First seen':<12} "
                      f"{'Last seen':<12} {'Source':<8} Name")
                for s in rows:
                    flag = " !" if s.data_before_listing else ""
                    print(f"{s.ticker:<10} {s.market:<8} {s.first_trade_date or '-':<12} {s.last_trade_date or '-':<12} "
                          f"{s.first_seen or '-':<12} {s.last_seen or '-':<12} {s.source:<8} {s.name or ''}{flag}")
            print(f"{counts['symbols']} symbol(s), {counts['with_first_trade_date']} with a first trade date "
                  f"(checked for missing days), {counts['without_first_trade_date']} without, "
                  f"{counts['data_before_listing']} with data before their first trade date (!)")
            return 0
        if cmd == "show":
            s = reg.get(args.ticker, args.market)
            if s is None:
                print(f"ERROR: unknown symbol {args.ticker}", file=sys.stderr)
                return 1
            for label, value in (("ticker", s.ticker), ("market", s.market or "-"), ("name", s.name or "-"),
                                 ("first trade date", s.first_trade_date or "- (missing-day check skipped)"),
                                 ("last trade date", s.last_trade_date or "- (still trading)"),
                                 ("data loaded", f"{s.first_seen or '-'} .. {s.last_seen or '-'}"),
                                 ("source", s.source), ("note", s.note or "-"), ("updated", s.updated_at)):
                print(f"{label:<17}: {value}")
            if s.data_before_listing:
                print("! loaded data starts before the first trade date - that data is invalid")
            return 0
        if cmd == "set":
            kwargs = {}
            if args.first is not None:
                kwargs["first_trade_date"] = args.first
            if args.last is not None:
                kwargs["last_trade_date"] = args.last
            if args.name is not None:
                kwargs["name"] = args.name
            if args.note is not None:
                kwargs["note"] = args.note
            s = reg.set(args.ticker, args.market, **kwargs)
            print(f"{s.ticker} ({s.market or 'no market'}): first trade {s.first_trade_date or '-'}, "
                  f"last trade {s.last_trade_date or '-'}" + (f", {s.name}" if s.name else ""))
            if s.data_before_listing:
                print(f"! data loaded from {s.first_seen} is before the first trade date - it is invalid and will be "
                      f"flagged / rejected")
            return 0
        if cmd == "delete":
            s = reg.delete(args.ticker, args.market)
            print(f"{s.ticker} ({s.market}) removed")
            return 0
        if cmd == "import":
            result = reg.import_csv(args.file, args.market)
            print(result)
            for e in result.errors[:50]:
                print(f"  {e}", file=sys.stderr)
            return 1 if result.errors else 0
        text = reg.export_csv(args.file, args.market)
        if args.file:
            print(f"{text.count(chr(10)) - 1} symbol(s) written to {args.file}")
        else:
            print(text, end="")
        return 0
    except SymbolError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _promote(what: str, service: PromotionService, full: bool = False, **filters) -> int:
    """Run the procedure(s) in order, printing each result; 1 on failure."""
    names = ("daily", "weekly", "monthly") if what == "all" else (what,)
    try:
        for name in names:
            if name == "daily":
                r = service.promote_daily(full=full, **filters)
            elif name == "weekly":
                r = service.promote_weekly(full=full, ticker=filters.get("ticker"))
            else:
                r = service.promote_monthly(full=full, ticker=filters.get("ticker"))
            print(r, flush=True)
            if r.rows_rejected:
                print(f"  {r.rows_rejected:,} staging row(s) were rejected - see 'data-errors' / the dashboard's "
                      f"data correction page, then promote again", flush=True)
            if r.rows_incomplete:
                print(f"  {r.rows_incomplete:,} {name} period(s) have a trading day missing - recorded in Data_Error "
                      f"(data-errors --file {r.target_table}); load the missing days and promote again", flush=True)
        return 0
    except PromotionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def cmd_promote(args, settings) -> int:
    filters = {}
    if args.what in ("daily", "all"):
        filters = {"source_file": args.source_file, "load_id": args.load_id, "ticker": args.ticker,
                   "date_from": args.date_from, "date_to": args.date_to}
    elif args.ticker:
        filters = {"ticker": args.ticker}
    elif any((args.source_file, args.load_id, args.date_from, args.date_to)):
        print("ERROR: --file, --load-id, --from and --to only apply to 'promote daily'", file=sys.stderr)
        return 2
    return _promote(args.what, PromotionService(settings), full=args.full, **filters)


def cmd_promotions(args, settings) -> int:
    records = PromotionService(settings).history(limit=args.limit, procedure=args.procedure)
    if not records:
        print("no promotion runs yet")
        return 0
    print(f"{'StartedAt':<20} {'Procedure':<16} {'Mode':<12} {'Status':<10} {'Read':>9} {'Ins':>8} {'Upd':>8} "
          f"{'Same':>8} {'Rej':>7} {'Skip':>6} {'Incompl':>7}  Message")
    for r in reversed(records):
        mode = r.mode + ("*" if r.filtered else "")
        print(f"{r.started_at:<20} {r.procedure:<16} {mode:<12} {r.status:<10} {r.rows_read:>9,} "
              f"{r.rows_inserted:>8,} {r.rows_updated:>8,} {r.rows_unchanged:>8,} {r.rows_rejected:>7,} "
              f"{r.rows_skipped:>6,} {r.rows_incomplete:>7,}  {r.message or ''}")
    print("(* = run limited to a file / ticker / date range)")
    return 0


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
            AuditLogger(settings).error("converter", "convert", exc, file_name=Path(src).name)
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
    audit = AuditLogger(settings)
    for src, res, err in outcomes:
        if res is not None:
            print(res)
        else:
            audit.error("converter", "convert_folder", err, file_name=src.name)
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
    db.ensure_history_table()
    AuditLogger(settings).ensure_tables()
    DataErrorRepository(settings).ensure_table()
    CorrectionService(settings, db).ensure_table()
    UserStore(settings).ensure_table()
    PromotionService(settings, db).ensure_tables()
    SymbolRegistry(settings, db).ensure_table()
    calendar = TradingCalendar(settings, db)
    years = default_update_years()
    updated = calendar.update_years(years)
    print(f"staging table {settings.stage_table} is ready (sqlite: {settings.sqlite_path})")
    print(f"no-trading days ready for {', '.join(m.market for m in calendar.markets(active_only=True))} "
          f"({years[0]}-{years[-1]}, {sum(r.added for r in updated)} day(s) added)")
    print(f"active tables ready: {settings.daily_table}, {settings.weekly_table}, {settings.monthly_table}")
    print("support tables ready: Load_History, App_Log, App_Error, Data_Error, Data_Correction, App_User, "
          "Promotion_History, Stock_Symbol")
    return 0


def cmd_logs(args, settings) -> int:
    records = AuditLogger(settings).fetch_logs(limit=args.limit, component=args.component,
                                               level=args.level)
    if not records:
        print("no log entries")
        return 0
    for r in reversed(records):
        extra = f"  file={r.source_file}" if r.source_file else ""
        print(f"{r.logged_at}  {r.level:<7} {r.component:<10} {r.event:<18} {r.message or ''}{extra}")
    return 0


def cmd_errors(args, settings) -> int:
    records = AuditLogger(settings).fetch_errors(limit=args.limit, component=args.component)
    if not records:
        print("no errors recorded")
        return 0
    for r in reversed(records):
        extra = f"  file={r.source_file}" if r.source_file else ""
        kind = f" [{r.error_type}]" if r.error_type else ""
        print(f"{r.occurred_at}  {r.component}/{r.operation}{kind}: {r.error_message}{extra}")
    return 0


def cmd_data_errors(args, settings) -> int:
    repo = DataErrorRepository(settings)
    records = repo.fetch(limit=args.limit, source_file=args.source_file, alerts_only=args.alerts)
    if not records:
        print("no data errors recorded")
        return 0
    for r in reversed(records):
        flag = "ALERT " if r.alert else "error "
        who = f"{r.ticker or '-'} {r.trade_date or ''}".strip()
        line = f" line {r.line_no}" if r.line_no else ""
        print(f"{r.detected_at}  {flag} {r.rule:<20} {who:<16} {r.error_message}"
              f"  (file={r.source_file or '-'}{line})")
    print(f"total: {repo.count(source_file=args.source_file, alerts_only=args.alerts)}")
    return 0


def cmd_history(args, settings) -> int:
    records = StageDatabase(settings).load_history(limit=args.limit, file_name=args.file_name)
    if not records:
        print("no load history")
        return 0
    print(f"{'LoadedAt':<20} {'Status':<18} {'Rows':>8}  {'FileName':<36} FileHash")
    for r in reversed(records):
        print(f"{r.loaded_at:<20} {r.status:<18} {r.rows_loaded:>8}  {r.file_name:<36} {r.file_hash[:12]}")
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
    else:
        print(f"{'SourceFile':<40} {'LoadId':<32} {'Rows':>8}  LoadedAt")
        total = 0
        for src, load_id, n, at in rows:
            print(f"{src:<40} {load_id:<32} {n:>8}  {at}")
            total += n
        print(f"{'total':<40} {'':<32} {total:>8}")
    counts = SymbolRegistry(settings, db).counts()
    print(f"stock symbols: {counts['symbols']:,} ({counts['with_first_trade_date']:,} with a first trade date, "
          f"{counts['without_first_trade_date']:,} without - not checked for missing days"
          + (f", {counts['data_before_listing']} with data before their first trade date" if counts["data_before_listing"] else "")
          + ")")
    print("active tables:")
    for t in PromotionService(settings, db).summaries():
        span = f"{t.first_period} .. {t.last_period}" if t.rows else "empty"
        last = (f"last {t.last_run.procedure} {t.last_run.status} {t.last_run.started_at}"
                if t.last_run else "never promoted")
        print(f"  {t.table:<16} {t.kind:<8} {t.rows:>10,} rows  {t.tickers:>6,} tickers  {span:<26} {last}")
    return 0


def _password_from(args) -> str:
    """--password, $STOCK_DATALOAD_PASSWORD, or an interactive prompt (never echoed)."""
    import getpass
    import os
    password = getattr(args, "password", None) or os.environ.get("STOCK_DATALOAD_PASSWORD")
    if password:
        return password
    if not sys.stdin.isatty():
        raise AuthError("no password given: use --password, $STOCK_DATALOAD_PASSWORD or run interactively")
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Repeat password: "):
        raise AuthError("the passwords do not match")
    return first


def cmd_user(args, settings) -> int:
    store = UserStore(settings)
    audit = AuditLogger(settings)
    try:
        if args.user_command == "add":
            user = store.create_user(args.username, _password_from(args), role=args.role, display_name=args.name,
                                     created_by="cli", must_change_password=args.must_change)
            audit.log("auth", "user_created", f"cli created user '{user.username}' ({user.role})",
                      details={"username": user.username, "role": user.role})
            print(f"user '{user.username}' created with role {user.role}")
            return 0
        if args.user_command == "list":
            users = store.list_users()
            if not users:
                print("no users - create the initial administrator with: user add admin --role admin")
                return 0
            print(f"{'Username':<20} {'Role':<9} {'Status':<9} {'Name':<24} Last sign-in")
            for u in users:
                status = "active" if u.is_active else "disabled"
                if u.must_change_password:
                    status += "*"
                print(f"{u.username:<20} {u.role:<9} {status:<9} {(u.display_name or ''):<24} {u.last_login_at or '-'}")
            print("(* = must change the password at the next sign-in)")
            return 0
        user = store.get_by_username(args.username)
        if user is None:
            raise AuthError(f"user '{args.username}' does not exist")
        if args.user_command == "passwd":
            store.set_password(user.user_id, _password_from(args), must_change=args.must_change)
            audit.log("auth", "password_reset", f"cli reset the password of '{user.username}'", level="WARNING",
                      details={"username": user.username})
            print(f"password of '{user.username}' updated")
        elif args.user_command == "role":
            store.update(user.user_id, role=args.role)
            print(f"'{user.username}' is now {args.role}")
        elif args.user_command in ("enable", "disable"):
            store.update(user.user_id, active=args.user_command == "enable")
            print(f"'{user.username}' {args.user_command}d")
        else:  # delete
            store.delete_user(user.user_id)
            print(f"'{user.username}' deleted")
        audit.log("auth", f"user_{args.user_command}", f"cli: {args.user_command} '{user.username}'",
                  details={"username": user.username})
        return 0
    except AuthError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


COMMANDS = {
    "load": cmd_load,
    "load-folder": cmd_load_folder,
    "promote": cmd_promote,
    "promotions": cmd_promotions,
    "no-trading-days": cmd_no_trading_days,
    "calendar": cmd_no_trading_days,
    "symbols": cmd_symbols,
    "user": cmd_user,
    "logs": cmd_logs,
    "errors": cmd_errors,
    "data-errors": cmd_data_errors,
    "history": cmd_history,
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
    audit = AuditLogger(settings)
    try:
        return COMMANDS[args.command](args, settings)
    except (ConversionError, DatabaseError, ConfigError, FormatError, LoadError, PromotionError) as exc:
        if not getattr(exc, "_audit_recorded", False):
            audit.error("cli", args.command, exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - capture unexpected errors of any step
        if not getattr(exc, "_audit_recorded", False):
            audit.error("cli", args.command, exc)
        print(f"UNEXPECTED ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
