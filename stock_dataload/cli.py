"""
Command line interface.

    python -m stock_dataload dashboard            # web dashboard for business users
    python -m stock_dataload portal               # public site: token shop, customer accounts, data API
    python -m stock_dataload customers list       # customers / API keys of the portal (list, orders, grant, ...)
    python -m stock_dataload customers add-admin EMAIL   # a portal administrator (admin pages need 2FA)
    python -m stock_dataload db size              # what takes the space; db purge-stage / db vacuum shrink it
    python -m stock_dataload load                 # load every file in the loading folder
    python -m stock_dataload load FILE [FILE..]   # load specific file(s)
    python -m stock_dataload load-folder FOLDER   # bulk load: every file in a folder
    python -m stock_dataload promote daily        # staging -> active daily table (validated)
    python -m stock_dataload promote weekly       # daily -> weekly table; also: monthly, all
    python -m stock_dataload promote eod --market NYSE   # Stock_EOD_Stage -> daily table (adjusted prices)
    python -m stock_dataload no-trading-days update   # generate next year's No_Trading_Day rows (weekends + holidays)
    python -m stock_dataload symbols set HCMAU --first-trade-date 2025-08-01   # Stock_Symbol table (trading windows)
    python -m stock_dataload convert SOURCE [..]  # convert (csv/text/xlsx/json/xml) + move to loading folder
    python -m stock_dataload convert-folder       # convert everything in the conversion folder
    python -m stock_dataload inspect SOURCE       # show how a file would be mapped
    python -m stock_dataload formats              # list the custom file formats
    python -m stock_dataload init-db              # create the staging table
    python -m stock_dataload status               # what is in the staging table
    python -m stock_dataload user add NAME ...    # user accounts for the dashboard (add/list/passwd/role/...)
    python -m stock_dataload eod-fetch MRK [..]   # download end-of-day prices from the EOD API ([eod_api]) into a folder
                                                  # and load them into the staging table Stock_EOD_Stage
    python -m stock_dataload eod-load FILE --symbol MRK   # load a saved API response (e.g. docs/SampleAPI_JasonData.txt)
    python -m stock_dataload eod-stage [--symbol MRK]     # what is in Stock_EOD_Stage
    python -m stock_dataload eod-symbols-fetch NYSE       # symbol list of a market from the API -> Stock_Symbol_Stage
    python -m stock_dataload eod-symbols-load NYSE_List.json   # load a saved symbol list
    python -m stock_dataload eod-symbols --q merck        # search / summarise Stock_Symbol_Stage
    python -m stock_dataload eod-limits HD JNJ            # the daily load limits and today's use
    python -m stock_dataload eod-bulk-fetch US --date 2026-10-12   # one day of EVERY stock of a market (one call)
    python -m stock_dataload eod-bulk-load US_bulk_2026-10-12.json --market US   # a saved bulk answer
    python -m stock_dataload eod-bulk-schedule set US --time 18:30      # load US every trading day at 18:30
    python -m stock_dataload eod-bulk-schedule run-due [--loop]          # run the due schedules (cron / service)

Daily limits (load_limits.py): a stock is loaded at most 5 times a day, a whole market at most 3 times a
day, a text file (eod-fetch-market --symbols-file) lists at most 100 symbols and runs at most 5 times a
day.  A refused load prints "REFUSED: ..." (exit code 1); stocks skipped for their limit print a WARNING.
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
from .accounts import PRINCIPAL_CUSTOMER, PRINCIPAL_STAFF, TwoFactorService
from .customers import CustomerError, CustomerStore
from .eod_api import EodApiClient, EodApiError
from .eod_stage import EodStageError, EodStageLoader, load_many
from .eod_symbols import SymbolListLoader, SymbolStageError
from .eod_symbols import load_many as load_symbol_lists
from .messaging import MessagingError
from .oauth import IdentityStore, OAuthError
from .payments import PaymentError
from .db import DatabaseError, StageDatabase
from .file_formats import FILE_TYPES, FileFormat, FormatError, FormatStore
from .loader import LoadError, StageLoader
from .logging_setup import setup_logging
from .promotion import PROC_DAILY, PROC_EOD_DAILY, PROC_MONTHLY, PROC_WEEKLY, PromotionError, PromotionService
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
    p.add_argument("what", choices=("daily", "weekly", "monthly", "all", "eod"),
                   help="daily: staging -> daily table (validated); weekly / monthly: daily -> aggregates; "
                        "all: daily, then weekly, then monthly; eod: the EOD API data (Stock_EOD_Stage) -> daily "
                        "table, converted to adjusted prices (rate = AdjustedClose / Close)")
    p.add_argument("--full", action="store_true",
                   help="re-promote everything (default: only what changed since the last run)")
    p.add_argument("--file", dest="source_file", help="daily: only the rows of this source file")
    p.add_argument("--load-id", help="daily: only the rows of this load id")
    p.add_argument("--ticker", help="only this stock symbol")
    p.add_argument("--from", dest="date_from", metavar="DATE", help="daily: from this trade date (YYYYMMDD / YYYY-MM-DD)")
    p.add_argument("--to", dest="date_to", metavar="DATE", help="daily: up to this trade date")
    p.add_argument("--symbol", help="eod: only this EOD symbol (as requested from the API, e.g. MRK or MRK.US)")
    p.add_argument("--market", help="eod: the market of the rows (e.g. NYSE; default: symbol table, daily table, "
                                    "[eod_api] default_market)")

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
    p = sub.add_parser("dashboard", help="start the web dashboard (and the scheduler of the daily bulk loads)")
    p.add_argument("--host", help="bind address (default from config, 127.0.0.1)")
    p.add_argument("--port", type=int, help="port (default from config, 8050)")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--no-scheduler", action="store_true",
                   help="do not run the daily schedules of 'One day, all stocks' in this process")

    # inspect ------------------------------------------------------------
    p = sub.add_parser("portal", help="start the public portal: token shop, customer accounts and the data API")
    p.add_argument("--host", help="bind address (default: [portal] host)")
    p.add_argument("--port", type=int, help="port (default: [portal] port)")
    p.add_argument("--debug", action="store_true")

    # customers (portal) ---------------------------------------------------
    p = sub.add_parser("customers", help="the customers of the portal / data API: accounts, API keys, orders, usage")
    csub = p.add_subparsers(dest="customers_command", required=True)
    c = csub.add_parser("list", help="list customers with their keys and balances")
    c.add_argument("--q", help="filter by e-mail / name")
    c.add_argument("--limit", type=int, default=100)
    c = csub.add_parser("orders", help="list orders")
    c.add_argument("--status", choices=("paid", "pending", "failed", "cancelled"))
    c.add_argument("--limit", type=int, default=50)
    c = csub.add_parser("usage", help="list data API requests")
    c.add_argument("--key", help="key prefix (sdl_xxxxxxxx)")
    c.add_argument("--limit", type=int, default=50)
    c = csub.add_parser("grant", help="credit tokens to a key by hand (refund, trial, invoiced deal)")
    c.add_argument("key", help="key prefix as shown in 'customers list' (sdl_xxxxxxxx)")
    c.add_argument("tokens", type=int)
    c.add_argument("--note", default="")
    c = csub.add_parser("new-key", help="create a key for an e-mail address (the customer is created as a guest if unknown)")
    c.add_argument("email")
    c.add_argument("--label", default="")
    c.add_argument("--tokens", type=int, default=0, help="tokens to grant to the new key")
    for name, help_text in (("disable-key", "disable a key"), ("enable-key", "re-activate a key")):
        c = csub.add_parser(name, help=help_text)
        c.add_argument("key", help="key prefix (sdl_xxxxxxxx)")
    for name, help_text in (("disable", "disable a customer (all their keys stop working)"),
                            ("enable", "re-activate a customer")):
        c = csub.add_parser(name, help=help_text)
        c.add_argument("email")
    csub.add_parser("stats", help="totals: customers, keys, tokens, revenue, requests")
    c = csub.add_parser("add-admin", help="create a portal ADMINISTRATOR account (or promote an existing account)")
    c.add_argument("email")
    c.add_argument("--password", help="password for a new account (default: prompt, or $STOCK_DATALOAD_PASSWORD)")
    c.add_argument("--name", default="", help="display name")
    c = csub.add_parser("role", help="change the role of a portal account: user | admin")
    c.add_argument("email")
    c.add_argument("role", choices=("user", "admin"))
    c = csub.add_parser("reset-2fa", help="remove all second factors of a customer who is locked out")
    c.add_argument("email")
    c = csub.add_parser("verify", help="mark the e-mail address of an account as verified")
    c.add_argument("email")
    c = csub.add_parser("set-password", help="set a new password for an account")
    c.add_argument("email")
    c.add_argument("--password", help="default: prompt, or $STOCK_DATALOAD_PASSWORD")

    # db -----------------------------------------------------------------------
    p = sub.add_parser("db", help="database housekeeping: size report, purge promoted staging rows, compact the file")
    dsub = p.add_subparsers(dest="db_command", required=True)
    dsub.add_parser("size", help="what takes the space: tables and indexes, largest first")
    d = dsub.add_parser("purge-stage", help="delete staging rows that were promoted into the daily table")
    d.add_argument("--keep-days", type=int, default=30, help="keep rows loaded in the last N days (default 30)")
    d.add_argument("--all", action="store_true", help="delete every promoted staging row, whatever its age")
    d.add_argument("--vacuum", action="store_true", help="compact the database file afterwards")
    d.add_argument("--dry-run", action="store_true", help="only show what would be deleted")
    dsub.add_parser("vacuum", help="compact the database file (reclaims space after deletes)")
    d = dsub.add_parser("reset-data", help="empty the stock data tables (schema kept), rebuild their indexes, compact")
    d.add_argument("--yes", action="store_true", help="do it (without this flag only the plan is shown)")
    d.add_argument("--keep-history", action="store_true",
                   help="keep Load_History / Promotion_History (default: emptied too, they describe the deleted data)")
    d = dsub.add_parser("keep-tickers", help="keep the stock data of the given tickers only: delete the rows of every "
                                             "other ticker from the staging, EOD staging and active tables, "
                                             "Data_Error and Data_Correction, then compact")
    d.add_argument("tickers", nargs="+", metavar="TICKER", help="the tickers to keep, e.g. PFE HD MRK JNJ (commas work too)")
    d.add_argument("--yes", action="store_true", help="do it (without this flag only the plan is shown)")

    # eod-fetch ------------------------------------------------------------------
    p = sub.add_parser("eod-fetch", help="download end-of-day prices of symbol(s) from the EOD data API and save "
                                         "the responses in [eod_api] download_dir")
    p.add_argument("symbols", nargs="+", metavar="SYMBOL", help="stock symbol(s), e.g. MRK or MRK.US")
    p.add_argument("--fmt", help="response format sent as fmt=... (json, csv, ...; default: [eod_api] fmt)")
    p.add_argument("--token", help="API token for this call (default: $STOCK_DATALOAD_EOD_API_TOKEN, "
                                   "then [eod_api] api_token)")
    p.add_argument("--base-url", help="API address for this call (default: [eod_api] base_url, "
                                      "https://eodhd.com/api/eod/)")
    p.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD", help="first date")
    p.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD", help="last date")
    p.add_argument("--period", choices=("d", "w", "m"), help="d(aily), w(eekly) or m(onthly) bars")
    p.add_argument("--order", choices=("a", "d"), help="a(scending) or d(escending) dates")
    p.add_argument("--param", action="append", default=[], metavar="NAME=VALUE",
                   help="any further query parameter, repeatable")
    p.add_argument("--output-dir", type=Path, help="save here instead of [eod_api] download_dir")
    p.add_argument("--fake", action="store_true",
                   help="use the built-in stub (sample data, no network) instead of the live API")
    p.add_argument("--no-load", action="store_true",
                   help="only save the responses, do not load them into [eod_api] stage_table")
    p.add_argument("--skip-bad-rows", action="store_true",
                   help="load the valid records of a response that has invalid ones (default: load nothing)")
    p.add_argument("--promote", action="store_true",
                   help="afterwards move the loaded symbols into the daily table (promote eod)")
    p.add_argument("--market", help="with --promote: the market of the symbols (e.g. NYSE)")

    p = sub.add_parser("eod-fetch-market", help="fetch the end-of-day prices of every stock of a market (e.g. NYSE) "
                                                "from the EOD API - one call per stock, with retries - straight into "
                                                "the staging table (no files); a whole market at most 3 times a day")
    p.add_argument("market", help="market / exchange code of the symbol list, e.g. NYSE")
    p.add_argument("--types", help="symbol types to fetch, comma separated (default: [eod_api] market_types, "
                                   "'Common Stock')")
    p.add_argument("--all-types", action="store_true", help="every type of the list (ETF, FUND, Preferred Stock ...)")
    p.add_argument("--symbols", help="only these symbols, comma separated (instead of the market's list)")
    p.add_argument("--symbols-file", type=Path, metavar="FILE",
                   help="only the symbols of this text file, separated by commas (at most 100 symbols; "
                        "at most 5 text-file loads a day)")
    p.add_argument("--limit", type=int, help="only the first N symbols of the list")
    p.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD", help="first date")
    p.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD", help="last date")
    p.add_argument("--period", choices=("d", "w", "m"), help="d(aily), w(eekly) or m(onthly) bars")
    p.add_argument("--param", action="append", default=[], metavar="NAME=VALUE",
                   help="any further query parameter, repeatable")
    p.add_argument("--retries", type=int, help="repeats of a failed call per stock (default: [eod_api] retries, 3)")
    p.add_argument("--backoff", type=float, help="seconds before the first repeat, doubled each time "
                                                 "(default: [eod_api] retry_backoff)")
    p.add_argument("--pause", type=float, help="seconds between two stocks (default: [eod_api] request_pause)")
    p.add_argument("--refresh-list", action="store_true",
                   help="request the market's symbol list from the API first (default: only when it has none)")
    p.add_argument("--resume", nargs="?", const=True, default=False, metavar="RUN_ID",
                   help="continue the newest unfinished run of the market (or RUN_ID): only the stocks not loaded yet")
    p.add_argument("--skip-bad-rows", action="store_true",
                   help="load the valid records of an answer with invalid ones (default: the stock is not loaded)")
    p.add_argument("--promote", action="store_true",
                   help="afterwards move the loaded stocks into the daily table (promote eod --market MARKET)")
    p.add_argument("--token", help="API token for this run (default: $STOCK_DATALOAD_EOD_API_TOKEN, [eod_api] api_token)")
    p.add_argument("--base-url", help="API address for this run (default: [eod_api] base_url)")
    p.add_argument("--fake", action="store_true",
                   help="use the built-in stub (invented prices per symbol, no network) instead of the live API")
    p.add_argument("--fake-faults", metavar="SPEC",
                   help="with --fake: simulated failures, e.g. 'MRK:503x2,PFE:404,HD:timeout,rate=0.05'")
    p.add_argument("--allow-fake-data", action="store_true",
                   help="let --fake write into a staging table that holds real API data")
    p.add_argument("--summary-only", action="store_true", help="print failures and the summary, not every stock")

    p = sub.add_parser("eod-bulk-fetch", help="one trading day of EVERY stock of a market in one API call "
                                              "(eod-bulk-last-day) - saved and loaded into the staging table; "
                                              "counts as a whole-market load (at most 3 a day)")
    p.add_argument("market", nargs="?", default="US", help="exchange code: US (every US exchange, default), LSE, ...")
    p.add_argument("--date", metavar="YYYY-MM-DD", help="the trading day (default: the API's last trading day)")
    p.add_argument("--fmt", choices=("json", "csv"), help="answer format (default: [eod_api] bulk_fmt, json)")
    p.add_argument("--token", help="API token for this call (default: $STOCK_DATALOAD_EOD_API_TOKEN, [eod_api] api_token)")
    p.add_argument("--base-url", help="API address for this call (default: [eod_api] bulk_url)")
    p.add_argument("--symbols", help="only these stocks, comma separated (then it is no whole-market load)")
    p.add_argument("--force", action="store_true", help="call the API even if the market does not trade that day")
    p.add_argument("--strict", action="store_true",
                   help="load nothing if a record is invalid (default: load the valid records, reject the others)")
    p.add_argument("--no-save", action="store_true", help="do not keep the answer in [eod_api] download_dir")
    p.add_argument("--no-load", action="store_true", help="only save the answer")
    p.add_argument("--output-dir", type=Path, help="save here instead of [eod_api] download_dir")
    p.add_argument("--retries", type=int, help="repeats of a failed call (default: [eod_api] retries)")
    p.add_argument("--promote", action="store_true", help="afterwards promote exactly this load into the daily table")
    p.add_argument("--aggregates", action="store_true", help="with --promote: then promote weekly / monthly")
    p.add_argument("--fake", action="store_true", help="use the built-in stub (invented prices, no network)")
    p.add_argument("--fake-faults", metavar="SPEC", help="with --fake: simulated failures, e.g. 'US:503x2'")
    p.add_argument("--allow-fake-data", action="store_true",
                   help="let --fake write into a staging table that holds real API data")

    p = sub.add_parser("eod-bulk-schedule", help="daily schedules of the one-day bulk load: one per exchange, run "
                                                 "once per trading day at a set time")
    bsub = p.add_subparsers(dest="schedule_command", required=True)
    bsub.add_parser("list", help="the schedules, their next and last run")
    b = bsub.add_parser("set", help="create or change the schedule of an exchange")
    b.add_argument("market", help="exchange code, e.g. US")
    b.add_argument("--time", required=True, metavar="HH:MM", help="when to run (server time, 24-hour clock)")
    b.add_argument("--previous-day", action="store_true",
                   help="load the previous trading day (default: the same trading day - run it after the close)")
    b.add_argument("--fmt", choices=("json", "csv"), default="json")
    b.add_argument("--symbols", help="only these stocks, comma separated (default: every stock)")
    b.add_argument("--no-promote", action="store_true", help="do not promote the loaded stocks")
    b.add_argument("--aggregates", action="store_true", help="promote weekly / monthly afterwards too")
    b.add_argument("--strict", action="store_true", help="load nothing if a record is invalid")
    b.add_argument("--fake", action="store_true", help="use the built-in stub (invented prices, no API calls)")
    b.add_argument("--allow-fake-data", action="store_true", help="with --fake: also into a staging table with real data")
    b.add_argument("--disabled", action="store_true", help="save it disabled")
    for name, help_text in (("enable", "enable the schedule of an exchange"), ("disable", "disable it"),
                            ("delete", "delete it"), ("run", "run the schedule of an exchange now (the daily run stays)")):
        b = bsub.add_parser(name, help=help_text)
        b.add_argument("market")
    b = bsub.add_parser("run-due", help="run the schedules that are due now (for cron / the task scheduler)")
    b.add_argument("--loop", action="store_true", help="keep checking (a service instead of cron)")
    b.add_argument("--interval", type=float, default=30.0, help="seconds between two checks with --loop (default 30)")

    p = sub.add_parser("eod-bulk-load", help="load a saved eod-bulk-last-day answer (JSON or CSV) into the staging "
                                             "table; counts as a whole-market load")
    p.add_argument("file", type=Path, help="the saved answer, e.g. docs/SampleAPI_BulkLastDay_US.json")
    p.add_argument("--market", default="US", help="the exchange code of the answer (default US)")
    p.add_argument("--date", metavar="YYYY-MM-DD", help="the trading day the answer is for (only reported)")
    p.add_argument("--fmt", choices=("json", "csv"), help="default: from the file")
    p.add_argument("--strict", action="store_true", help="load nothing if a record is invalid")
    p.add_argument("--promote", action="store_true", help="afterwards promote exactly this load into the daily table")
    p.add_argument("--aggregates", action="store_true", help="with --promote: then promote weekly / monthly")

    p = sub.add_parser("eod-limits", help="the daily limits of the EOD API loads and today's use: per stock, "
                                          "whole-market loads, text-file loads")
    p.add_argument("symbols", nargs="*", metavar="SYMBOL", help="show these stocks (default: the ones at their limit)")
    p.add_argument("--market", action="append", dest="markets", metavar="MARKET",
                   help="show the whole-market loads of this market (repeatable)")

    p = sub.add_parser("eod-market-runs", help="the registered eod-fetch-market runs (Eod_Market_Run) and the "
                                               "failed stocks of a run")
    p.add_argument("market", nargs="?", help="only the runs of this market")
    p.add_argument("--run", metavar="RUN_ID", help="the stocks of this run (id or a unique prefix)")
    p.add_argument("--all", action="store_true", help="with --run: every stock, not only the failed / skipped ones")
    p.add_argument("--limit", type=int, default=20, help="number of runs (default 20)")

    p = sub.add_parser("eod-load", help="load saved EOD API responses (json / csv) into the staging table "
                                        "[eod_api] stage_table (Stock_EOD_Stage)")
    p.add_argument("files", nargs="+", type=Path, metavar="FILE",
                   help="saved response(s), e.g. data/api_downloads/MRK_20251114_101500.json or "
                        "docs/SampleAPI_JasonData.txt")
    p.add_argument("--symbol", help="the symbol of the data (default: from a file name like MRK_20251114_101500.json)")
    p.add_argument("--fmt", choices=("json", "csv"), help="format of the file (default: by extension / content)")
    p.add_argument("--skip-bad-rows", action="store_true",
                   help="load the valid records of a file that has invalid ones (default: load nothing)")
    p.add_argument("--promote", action="store_true",
                   help="afterwards move the loaded symbols into the daily table (promote eod)")
    p.add_argument("--market", help="with --promote: the market of the symbols (e.g. NYSE)")

    p = sub.add_parser("eod-stage", help="show what is in the EOD staging table (per symbol, or the rows of one)")
    p.add_argument("--symbol", help="show the rows of this symbol")
    p.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD")
    p.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD")
    p.add_argument("--limit", type=int, default=20, help="rows to show (default 20, newest first)")
    p.add_argument("--delete", metavar="SYMBOL", help="delete every staged row of SYMBOL")

    # exchange symbol lists ------------------------------------------------------------
    p = sub.add_parser("eod-symbols-fetch", help="get the list of symbols traded on a market from the "
                                                 "exchange-symbol-list API and load it into [eod_api] symbol_stage_table")
    p.add_argument("markets", nargs="+", metavar="MARKET", help="market / exchange code(s), e.g. NYSE, NASDAQ, LSE")
    p.add_argument("--fmt", help="response format sent as fmt=... (json, csv; default: [eod_api] symbol_list_fmt)")
    p.add_argument("--token", help="API token for this call (default: $STOCK_DATALOAD_EOD_API_TOKEN, then "
                                   "[eod_api] api_token)")
    p.add_argument("--base-url", help="API address for this call (default: [eod_api] symbol_list_url, "
                                      "https://eodhd.com/api/exchange-symbol-list/)")
    p.add_argument("--type", dest="symbol_type", help="only this type, sent as type=... (common_stock, preferred_stock, "
                                                      "stock, etf, fund)")
    p.add_argument("--param", action="append", default=[], metavar="NAME=VALUE",
                   help="any further query parameter, repeatable (e.g. delisted=1)")
    p.add_argument("--output-dir", type=Path, help="save here instead of [eod_api] download_dir")
    p.add_argument("--fake", action="store_true",
                   help="use the built-in stub (the NYSE sample list, no network) instead of the live API")
    p.add_argument("--no-load", action="store_true", help="only save the lists, do not load them")
    p.add_argument("--skip-bad-rows", action="store_true",
                   help="load the valid records of a list that has invalid ones (default: load nothing)")

    p = sub.add_parser("eod-symbols-load", help="load saved symbol lists (json / csv) into [eod_api] symbol_stage_table")
    p.add_argument("files", nargs="+", type=Path, metavar="FILE",
                   help="saved list(s), e.g. docs/SampleAPI_NYSE_List.json or data/api_downloads/NYSE_symbols_....json")
    p.add_argument("--market", help="the market of the list (default: from a file name like NYSE_List.json)")
    p.add_argument("--fmt", choices=("json", "csv"), help="format of the file (default: by extension / content)")
    p.add_argument("--skip-bad-rows", action="store_true",
                   help="load the valid records of a file that has invalid ones (default: load nothing)")

    p = sub.add_parser("eod-symbols", help="the staged symbol lists: summary per market, or search by code / name")
    p.add_argument("--q", help="code prefix or part of the name, e.g. MRK or merck")
    p.add_argument("--market", help="only this market")
    p.add_argument("--type", dest="symbol_type", help="only this type, e.g. 'Common Stock' or ETF")
    p.add_argument("--limit", type=int, default=30, help="rows to show (default 30)")
    p.add_argument("--delete", metavar="MARKET", help="delete every staged symbol of MARKET")

    p = sub.add_parser("inspect", help="show the detected file type/column mapping of a file")
    p.add_argument("source", type=Path)
    _add_format_options(p)

    sub.add_parser("init-db", help="create the staging / active / log / error / history tables if they do not exist")
    sub.add_parser("status", help="show files loaded into the staging table and the active tables")
    p = sub.add_parser("promotions", help="show the promotion run registry (Promotion_History table)")
    p.add_argument("--limit", type=int, default=20, help="number of entries (default 20)")
    p.add_argument("--procedure", choices=(PROC_DAILY, PROC_WEEKLY, PROC_MONTHLY, PROC_EOD_DAILY),
                   help="filter by procedure")

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
    u.add_argument("--email", default="", help="e-mail address (password-reset links, e-mail sign-in codes)")
    usub.add_parser("list", help="list the user accounts")
    u = usub.add_parser("email", help="set (or clear) the e-mail address of a user")
    u.add_argument("username")
    u.add_argument("email", nargs="?", default="", help="the address; omit to clear it")
    u = usub.add_parser("reset-2fa", help="remove all second factors of a user who is locked out")
    u.add_argument("username")
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
            elif name == "eod":
                r = service.promote_eod_daily(full=full, **filters)
            elif name == "weekly":
                r = service.promote_weekly(full=full, ticker=filters.get("ticker"))
            else:
                r = service.promote_monthly(full=full, ticker=filters.get("ticker"))
            print(r, flush=True)
            if r.rows_rejected and name == "eod":
                print(f"  {r.rows_rejected:,} EOD row(s) were rejected - see 'data-errors'"
                      + (" (market_unknown: give --market, register the symbol with 'symbols set TICKER --market M' "
                         "or set [eod_api] default_market)" if "market_unknown" in r.reject_reasons else ""),
                      flush=True)
            elif r.rows_rejected:
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
    if args.what == "eod":
        if args.source_file:
            print("ERROR: --file does not apply to 'promote eod' (use --symbol / --load-id)", file=sys.stderr)
            return 2
        filters = {"symbol": args.symbol, "ticker": args.ticker, "market": args.market, "load_id": args.load_id,
                   "date_from": args.date_from, "date_to": args.date_to}
        return _promote("eod", PromotionService(settings), full=args.full, **filters)
    if args.symbol or args.market:
        print("ERROR: --symbol and --market only apply to 'promote eod'", file=sys.stderr)
        return 2
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


def _print_eod_load(result) -> int:
    """Print an EodLoadResult (+ the first rejected records); 1 if it failed or was refused."""
    if getattr(result, "refused", False):
        print(f"REFUSED: {result}", file=sys.stderr)
        return 1
    print(result, file=sys.stdout if result.ok else sys.stderr)
    for r in result.rejected[:10]:
        print(f"  rejected {r}", file=sys.stderr)
    if len(result.rejected) > 10:
        print(f"  ... {len(result.rejected) - 10} more - see 'data-errors --file {result.source_file}'", file=sys.stderr)
    return 0 if result.ok else 1


def cmd_eod_fetch(args, settings) -> int:
    from .eod_api import FakeEodGateway, parse_params
    params = {"from": args.date_from, "to": args.date_to, "period": args.period, "order": args.order}
    for item in args.param:
        params.update(parse_params(item))
    client = EodApiClient(settings, transport=FakeEodGateway() if args.fake else None, audit=AuditLogger(settings))
    outcomes = client.fetch_many(args.symbols, fmt=args.fmt, api_token=args.token, base_url=args.base_url,
                                 params=params, output_dir=args.output_dir, load=False if args.no_load else None,
                                 skip_bad_rows=True if args.skip_bad_rows else None)
    rc = 0
    loaded = []
    for symbol, result, error in outcomes:
        if result is not None:
            print(result)
            if result.stage is not None:
                rc = max(rc, _print_eod_load(result.stage))
                if result.stage.ok:
                    loaded.append(result.stage.symbol)
        else:
            from .load_limits import LoadLimitError
            label = "REFUSED" if isinstance(error, LoadLimitError) else "ERROR"
            print(f"{label} {symbol}: {error}", file=sys.stderr)
            rc = 1
    if args.promote:
        rc = max(rc, _promote_eod_symbols(settings, loaded, args.market))
    return rc


def _promote_eod_symbols(settings, symbols, market: str | None) -> int:
    """``promote eod`` for each freshly loaded symbol (eod-fetch / eod-load --promote)."""
    if not symbols:
        print("nothing loaded - nothing to promote")
        return 0
    service = PromotionService(settings)
    rc = 0
    for symbol in dict.fromkeys(symbols):
        rc = max(rc, _promote("eod", service, full=False, symbol=symbol, market=market))
    return rc


def cmd_eod_fetch_market(args, settings) -> int:
    """eod-fetch-market: every stock of a market, one API call each (with retries), straight into the stage table."""
    import dataclasses
    from .eod_api import parse_params
    from .eod_market import ITEM_FAILED, ITEM_SKIPPED, EodMarketError, MarketFetcher
    from .load_limits import LoadLimitError
    if args.backoff is not None:
        settings = dataclasses.replace(settings, eod_api_retry_backoff=max(0.0, args.backoff))
    params = {"from": args.date_from, "to": args.date_to, "period": args.period}
    for item in args.param:
        params.update(parse_params(item))
    params = {k: v for k, v in params.items() if v}
    types = [] if args.all_types else ([t.strip() for t in args.types.split(",") if t.strip()] if args.types else None)
    symbols = [t for t in args.symbols.replace(";", ",").split(",")] if args.symbols else None
    source = None
    if args.symbols_file:
        from .eod_market import parse_symbol_text
        if args.symbols:
            print("ERROR: give --symbols or --symbols-file, not both", file=sys.stderr)
            return 2
        try:
            data = args.symbols_file.read_bytes()
        except OSError as exc:
            print(f"ERROR: cannot read {args.symbols_file}: {exc}", file=sys.stderr)
            return 1
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1252", errors="replace")
        try:
            symbols, invalid = parse_symbol_text(text)
        except EodMarketError as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 1
        if invalid:
            print(f"WARNING: not a symbol, left out: {', '.join(invalid[:20])}", file=sys.stderr)
        if not symbols:
            print(f"ERROR: {args.symbols_file.name} contains no stock symbols (separate them with commas)", file=sys.stderr)
            return 1
        from .load_limits import TooManySymbolsError, audit_refusal, check_file_symbols
        try:
            check_file_symbols(symbols, args.symbols_file.name)
        except TooManySymbolsError as exc:
            audit_refusal(AuditLogger(settings), "eod_market", exc, file=args.symbols_file.name, symbols=len(symbols))
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 1
        source = "file"
    fetcher = MarketFetcher(settings, audit=AuditLogger(settings), fake=True if args.fake else None,
                            fake_faults=args.fake_faults, api_token=args.token, base_url=args.base_url)
    if args.fake_faults and not fetcher.fake:
        print("ERROR: --fake-faults needs --fake (or [eod_api] gateway = fake)", file=sys.stderr)
        return 2

    def progress(n: int, total: int, outcome) -> None:
        failed = outcome.status in (ITEM_FAILED, ITEM_SKIPPED)
        if failed or not args.summary_only:
            label = "WARNING " if outcome.limited else ""
            print(f"[{n:>{len(str(total))}}/{total}] {label}{outcome}", file=sys.stderr if failed else sys.stdout,
                  flush=True)

    try:
        run = fetcher.fetch_market(args.market, symbols=symbols, types=types, params=params, limit=args.limit,
                                   refresh_list=args.refresh_list, resume=args.resume,
                                   skip_bad_rows=True if args.skip_bad_rows else None, retries=args.retries,
                                   pause=args.pause, allow_fake_data=args.allow_fake_data, progress=progress,
                                   source=source)
    except LoadLimitError as exc:                          # a daily limit - recorded in App_Log (WARNING)
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    except EodMarketError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(f"interrupted - continue with: eod-fetch-market {args.market.upper()} --resume", file=sys.stderr)
        return 130
    print(run.summary(), flush=True)
    limited = run.limited()
    if limited:
        print(f"WARNING: {len(limited):,} stock(s) skipped - already loaded the maximum number of times today "
              f"(daily limit per stock): {', '.join(o.symbol for o in limited[:20])}"
              + (" ..." if len(limited) > 20 else ""), file=sys.stderr)
    failures = run.failures()
    if failures:
        print(f"{len(failures):,} stock(s) failed - details: eod-market-runs --run {run.run_id}; "
              f"fetch them again: eod-fetch-market {run.market} --resume", file=sys.stderr)
    rc = 0 if run.ok else 1
    if args.promote:
        loaded = [o.symbol for o in run.outcomes.values() if o.status == "loaded"]
        if loaded:                   # exactly the stocks of this run (other markets' staged rows stay untouched)
            rc = max(rc, _promote("eod", PromotionService(settings), full=False, market=run.market, symbols=loaded))
        else:
            print("nothing loaded - nothing to promote")
    return rc


def cmd_eod_market_runs(args, settings) -> int:
    from .eod_market import ITEM_FAILED, ITEM_PENDING, ITEM_SKIPPED, MarketFetcher
    fetcher = MarketFetcher(settings, fake=False)
    if args.run:
        run = fetcher.find_run(args.run)
        if run is None:
            print(f"ERROR: no (unique) run '{args.run}'", file=sys.stderr)
            return 1
        print(f"run {run['RunId']} {run['Market']} ({run['Gateway']}) {run['Status']}: {run['Symbols']:,} symbol(s), "
              f"{run['Loaded']:,} loaded, {run['NoData']:,} without data, {run['Failed']:,} failed, "
              f"{run['Skipped']:,} skipped, {run['Rows']:,} rows, {run['ApiCalls']:,} API call(s)"
              + (f" - {run['Message']}" if run['Message'] else ""))
        items = fetcher.run_items(run["RunId"], None if args.all else (ITEM_FAILED, ITEM_SKIPPED, ITEM_PENDING))
        if not items:
            print("no failed, skipped or pending stocks" if not args.all else "no stocks")
            return 0
        print(f"{'Symbol':<12} {'Status':<8} {'Tries':>5} {'HTTP':>4} {'Rows':>7}  Error")
        for i in items:
            rows = i["Inserted"] + i["Updated"] + i["Unchanged"]
            print(f"{i['Symbol']:<12} {i['Status']:<8} {i['Attempts']:>5} {i['HttpStatus'] or '':>4} {rows:>7,}  "
                  f"{(i['Error'] or '')[:110]}")
        return 0
    runs = fetcher.runs(args.market, args.limit)
    if not runs:
        print("no eod-fetch-market runs yet")
        return 0
    print(f"{'Run':<13} {'Market':<7} {'Gateway':<7} {'Started':<19} {'Status':<22} {'Symbols':>7} {'Loaded':>7} "
          f"{'NoData':>6} {'Failed':>6} {'Rows':>10}")
    for r in runs:
        print(f"{r['RunId']:<13} {r['Market']:<7} {r['Gateway']:<7} {r['StartedAt']:<19} {r['Status']:<22} "
              f"{r['Symbols']:>7,} {r['Loaded']:>7,} {r['NoData']:>6,} {r['Failed']:>6,} {r['Rows']:>10,}")
    quotas = fetcher.market_load_quotas([args.market] if args.market else [])
    if quotas:
        print("whole-market loads today (max per market and day): "
              + ", ".join(f"{m} {q.used}/{q.limit}" for m, q in quotas.items()))
    files = fetcher.file_load_quota()
    print(f"text-file loads today (max per day): {files.used}/{files.limit}")
    return 0


def _print_bulk(result, args, loader) -> int:
    """Report a bulk load (warnings, rejected records), promote it if asked; the exit code."""
    from .eod_bulk import BULK_REFUSED
    if result.status == BULK_REFUSED:
        print(f"REFUSED: {result}", file=sys.stderr)
    else:
        print(result, file=sys.stdout if result.ok else sys.stderr, flush=True)
    for warning in result.warnings():
        print(f"WARNING: {warning}", file=sys.stderr)
    for symbol, r in result.rejected[:10]:
        print(f"  rejected {symbol} {r}", file=sys.stderr)
    if len(result.rejected) > 10:
        print(f"  ... {len(result.rejected) - 10} more - see 'data-errors --file {result.source_file}'", file=sys.stderr)
    rc = 0 if result.ok else 1
    if getattr(args, "promote", False) and result.loaded:
        try:
            for promoted in loader.promote(result, aggregates=args.aggregates):
                print(promoted)
                if getattr(promoted, "rows_rejected", 0):
                    print(f"WARNING: {promoted.rows_rejected:,} row(s) not promoted - see data-errors", file=sys.stderr)
        except PromotionError as exc:
            print(f"ERROR: promotion failed: {exc}", file=sys.stderr)
            rc = 1
    return rc


def cmd_eod_bulk_fetch(args, settings) -> int:
    """eod-bulk-fetch: one trading day of every stock of a market (eod-bulk-last-day) -> the staging table."""
    from .eod_api import EodApiError
    from .eod_bulk import BulkDayLoader, EodBulkError
    from .eod_market import EodMarketError
    from .load_limits import LoadLimitError
    loader = BulkDayLoader(settings, audit=AuditLogger(settings), fake=True if args.fake else None,
                           fake_faults=args.fake_faults)
    if args.fake_faults and not loader.fake:
        print("ERROR: --fake-faults needs --fake (or [eod_api] gateway = fake)", file=sys.stderr)
        return 2
    try:
        result = loader.fetch(args.market, api_token=args.token, fmt=args.fmt, date=args.date, symbols=args.symbols,
                              base_url=args.base_url, save=not args.no_save, output_dir=args.output_dir,
                              load=not args.no_load, skip_bad_rows=False if args.strict else None, force=args.force,
                              retries=args.retries, allow_fake_data=args.allow_fake_data,
                              progress=lambda level, text: print(text, flush=True) if level == "info" else None)
    except LoadLimitError as exc:                          # a daily limit - recorded in App_Log (WARNING)
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    except EodBulkError as exc:                            # nothing was requested
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except (EodMarketError, EodApiError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return _print_bulk(result, args, loader)


def cmd_eod_bulk_load(args, settings) -> int:
    """eod-bulk-load: a saved bulk answer -> the staging table."""
    from .eod_bulk import BulkDayLoader, EodBulkError
    from .load_limits import LoadLimitError
    loader = BulkDayLoader(settings, audit=AuditLogger(settings), fake=False)
    try:
        result = loader.load_file(args.file, args.market, date=args.date, fmt=args.fmt,
                                  skip_bad_rows=False if args.strict else None)
    except LoadLimitError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    except EodBulkError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return _print_bulk(result, args, loader)


def cmd_eod_bulk_schedule(args, settings) -> int:
    """eod-bulk-schedule: the daily schedules of the one-day bulk load."""
    import time as _time
    from datetime import datetime as _dt
    from .bulk_schedule import (STATUS_LOADED, STATUS_WARNINGS, TRADE_DAY_PREVIOUS, TRADE_DAY_SAME, BulkScheduler,
                                ScheduleError, ScheduleStore, next_run)
    store = ScheduleStore(settings)
    audit = AuditLogger(settings)
    cmd = args.schedule_command
    try:
        if cmd == "list":
            schedules = store.list()
            if not schedules:
                print("no daily schedules - add one with: eod-bulk-schedule set US --time 18:30")
                return 0
            now = _dt.now()
            print(f"{'Exchange':<9} {'Time':<5} {'Loads':<9} {'Active':<8} {'Next run':<17} {'Last run':<20} Status")
            for s in schedules:
                upcoming = next_run(s, now, settings.extra_market_holidays)
                print(f"{s.market:<9} {s.run_time:<5} {s.trade_day:<9} {'yes' if s.enabled else 'no':<8} "
                      f"{upcoming.strftime('%Y-%m-%d %H:%M') if upcoming else '-':<17} {s.last_run_at or '-':<20} "
                      f"{s.last_status or 'never ran'}" + (f" - {s.last_message}" if s.last_message else ""))
            return 0
        if cmd == "set":
            schedule = store.save(args.market, args.time, TRADE_DAY_PREVIOUS if args.previous_day else TRADE_DAY_SAME,
                                  args.fmt, args.symbols, promote=not args.no_promote, aggregates=args.aggregates,
                                  strict=args.strict, fake=args.fake, allow_fake_data=args.allow_fake_data,
                                  enabled=not args.disabled, user="cli")
            audit.log("api_load_schedule", "schedule_saved", f"cli saved the daily schedule of {schedule.market}: "
                      f"{schedule.run_time}, {schedule.trade_day_label}", details=schedule.public())
            upcoming = next_run(schedule, _dt.now(), settings.extra_market_holidays)
            print(f"schedule of {schedule.market}: every trading day at {schedule.run_time} ({schedule.trade_day_label})"
                  + (f" - next run {upcoming:%Y-%m-%d %H:%M}" if upcoming else " - disabled"))
            return 0
        if cmd == "run-due":
            scheduler = BulkScheduler(settings, audit_factory=AuditLogger, interval=args.interval)
            while True:
                for outcome in scheduler.tick():
                    ok = outcome.status in (STATUS_LOADED, STATUS_WARNINGS)
                    print(("" if ok else "WARNING: ") + str(outcome), file=sys.stdout if ok else sys.stderr, flush=True)
                if not args.loop:
                    return 0
                _time.sleep(max(1.0, args.interval))
        schedule = store.get_by_market(args.market)
        if schedule is None:
            raise ScheduleError(f"there is no schedule for {args.market.upper()}")
        if cmd in ("enable", "disable"):
            store.set_enabled(schedule.schedule_id, cmd == "enable", "cli")
            audit.log("api_load_schedule", f"schedule_{cmd}d", f"cli {cmd}d the daily schedule of {schedule.market}",
                      level="INFO" if cmd == "enable" else "WARNING", details={"market": schedule.market})
            print(f"schedule of {schedule.market} {cmd}d")
        elif cmd == "delete":
            store.delete(schedule.schedule_id)
            audit.log("api_load_schedule", "schedule_deleted", f"cli deleted the daily schedule of {schedule.market}",
                      level="WARNING", details=schedule.public())
            print(f"schedule of {schedule.market} deleted")
        else:                                                # run now
            outcome = BulkScheduler(settings, audit_factory=AuditLogger).run_now(schedule, "cli (run now)")
            ok = outcome.status in (STATUS_LOADED, STATUS_WARNINGS)
            print(str(outcome), file=sys.stdout if ok else sys.stderr)
            return 0 if ok else 1
        return 0
    except ScheduleError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def cmd_eod_limits(args, settings) -> int:
    """eod-limits: the daily limits of the EOD API loads and today's use."""
    from .eod_market import MAX_MARKET_LOADS_PER_DAY, MarketFetcher
    from .load_limits import MAX_FILE_SYMBOLS, MAX_LOADS_PER_STOCK_PER_DAY
    fetcher = MarketFetcher(settings, fake=False)
    files = fetcher.file_load_quota()
    print(f"daily limits ({files.day}): a stock {MAX_LOADS_PER_STOCK_PER_DAY} loads, a whole market "
          f"{MAX_MARKET_LOADS_PER_DAY} loads, text files {files.limit} loads of at most {MAX_FILE_SYMBOLS} symbols")
    print(f"text-file loads today: {files.used}/{files.limit}"
          + (f" (at {', '.join(t[11:16] for t in files.started)})" if files.started else ""))
    quotas = fetcher.market_load_quotas(args.markets or [])
    print("whole-market loads today: " + (", ".join(f"{m} {q.used}/{q.limit}" for m, q in quotas.items()) or "none"))
    symbols = [s for item in args.symbols for s in item.replace(";", ",").split(",") if s.strip()]
    counts = fetcher.ledger.counts(symbols) if symbols else fetcher.ledger.exhausted()
    if not counts:
        print("stocks at their daily limit: none")
        return 0
    print("stocks" + ("" if symbols else " at their daily limit") + ":")
    for ticker, n in sorted(counts.items()):
        state = "LIMIT REACHED - skipped / refused until tomorrow" if n >= MAX_LOADS_PER_STOCK_PER_DAY else \
            f"{MAX_LOADS_PER_STOCK_PER_DAY - n} load(s) left"
        print(f"  {ticker:<12} {n}/{MAX_LOADS_PER_STOCK_PER_DAY}  {state}")
    return 0


def cmd_eod_load(args, settings) -> int:
    from .quality import DataErrorRepository
    loader = EodStageLoader(settings, audit=AuditLogger(settings), data_errors=DataErrorRepository(settings))
    rc = 0
    loaded = []
    for outcome in load_many(loader, args.files, args.symbol, args.fmt, True if args.skip_bad_rows else None):
        if isinstance(outcome, EodStageError):
            print(f"ERROR: {outcome}", file=sys.stderr)
            rc = 1
        else:
            rc = max(rc, _print_eod_load(outcome))
            if outcome.ok:
                loaded.append(outcome.symbol)
    if args.promote:
        rc = max(rc, _promote_eod_symbols(settings, loaded, args.market))
    return rc


def cmd_eod_stage(args, settings) -> int:
    loader = EodStageLoader(settings, audit=AuditLogger(settings))
    try:
        if args.delete:
            n = loader.delete_symbol(args.delete)
            print(f"{n:,} row(s) of {args.delete.upper()} deleted from {loader.table_name}")
            return 0
        if args.symbol:
            rows = loader.rows(args.symbol, args.date_from, args.date_to, args.limit)
            if not rows:
                print(f"no rows of {args.symbol.upper()} in {loader.table_name}")
                return 0
            print(f"{'TradeDate':<11} {'Open':>10} {'High':>10} {'Low':>10} {'Close':>10} {'AdjClose':>10} "
                  f"{'Volume':>13}  SourceFile")
            for r in rows:
                print(f"{r['TradeDate']:<11} {r['Open']:>10g} {r['High']:>10g} {r['Low']:>10g} {r['Close']:>10g} "
                      f"{r['AdjustedClose']:>10g} {r['Volume']:>13,}  {r['SourceFile']}")
            print(f"({len(rows)} of {loader.count(args.symbol):,} row(s), newest first)")
            return 0
        summary = loader.summary()
        if not summary:
            print(f"{loader.table_name} is empty - load data with 'eod-fetch SYMBOL' or 'eod-load FILE --symbol SYMBOL'")
            return 0
        print(f"{'Symbol':<14} {'Ticker':<10} {'Exch':<6} {'Rows':>7}  {'First':<10}  {'Last':<10}  Last loaded")
        for s in summary:
            print(f"{s.symbol:<14} {s.ticker:<10} {s.exchange or '-':<6} {s.rows:>7,}  {s.first_date:<10}  "
                  f"{s.last_date:<10}  {s.last_loaded}")
        print(f"{sum(s.rows for s in summary):,} row(s) of {len(summary)} symbol(s) in {loader.table_name}")
        return 0
    except EodStageError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _print_symbol_load(result) -> int:
    print(result, file=sys.stdout if result.ok else sys.stderr)
    if result.ok and result.types:
        print("  types: " + ", ".join(f"{k} {v:,}" for k, v in sorted(result.types.items(), key=lambda x: -x[1])))
    for r in result.rejected[:10]:
        print(f"  rejected {r}", file=sys.stderr)
    if len(result.rejected) > 10:
        print(f"  ... {len(result.rejected) - 10} more - see 'data-errors --file {result.source_file}'", file=sys.stderr)
    return 0 if result.ok else 1


def cmd_eod_symbols_fetch(args, settings) -> int:
    from .eod_api import FakeEodGateway, parse_params
    params: dict = {"type": args.symbol_type}
    for item in args.param:
        params.update(parse_params(item))
    client = EodApiClient(settings, transport=FakeEodGateway() if args.fake else None, audit=AuditLogger(settings))
    rc = 0
    for market, result, error in client.fetch_symbol_lists(args.markets, fmt=args.fmt, api_token=args.token,
                                                           base_url=args.base_url, params=params,
                                                           output_dir=args.output_dir,
                                                           load=False if args.no_load else None,
                                                           skip_bad_rows=True if args.skip_bad_rows else None):
        if result is None:
            print(f"ERROR {market}: {error}", file=sys.stderr)
            rc = 1
            continue
        print(result)
        if result.stage is not None:
            rc = max(rc, _print_symbol_load(result.stage))
    return rc


def cmd_eod_symbols_load(args, settings) -> int:
    from .quality import DataErrorRepository
    loader = SymbolListLoader(settings, audit=AuditLogger(settings), data_errors=DataErrorRepository(settings))
    rc = 0
    for outcome in load_symbol_lists(loader, args.files, args.market, args.fmt, True if args.skip_bad_rows else None):
        if isinstance(outcome, SymbolStageError):
            print(f"ERROR: {outcome}", file=sys.stderr)
            rc = 1
        else:
            rc = max(rc, _print_symbol_load(outcome))
    return rc


def cmd_eod_symbols(args, settings) -> int:
    loader = SymbolListLoader(settings, audit=AuditLogger(settings))
    try:
        if args.delete:
            n = loader.delete_market(args.delete)
            print(f"{n:,} symbol(s) of {args.delete.upper()} deleted from {loader.table_name}")
            return 0
        if args.q or args.symbol_type or args.market:
            rows = loader.search(args.q, args.market, args.symbol_type, args.limit)
            if not rows:
                print("no symbols match")
                return 0
            print(f"{'Market':<8} {'Code':<12} {'Type':<16} {'Exchange':<10} {'Cur':<4} {'ISIN':<13} Name")
            for r in rows:
                print(f"{r['Market']:<8} {r['Code']:<12} {(r['Type'] or '-'):<16} {(r['Exchange'] or '-'):<10} "
                      f"{(r['Currency'] or '-'):<4} {(r['Isin'] or '-'):<13} {r['Name'] or ''}")
            print(f"({len(rows)} shown - --limit for more)")
            return 0
        summary = loader.summary()
        if not summary:
            print(f"{loader.table_name} is empty - load a list with 'eod-symbols-fetch NYSE' or "
                  f"'eod-symbols-load docs/SampleAPI_NYSE_List.json --market NYSE'")
            return 0
        for m in summary:
            print(f"{m.market}: {m.codes:,} symbol(s), last list {m.last_loaded}"
                  + (f", {m.missing_from_latest:,} not in the latest list" if m.missing_from_latest else ""))
            print("  types:     " + ", ".join(f"{k} {v:,}" for k, v in m.types.items()))
            print("  exchanges: " + ", ".join(f"{k} {v:,}" for k, v in m.exchanges.items()))
        return 0
    except SymbolStageError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def cmd_dashboard(args, settings) -> int:
    from .dashboard import run
    run(settings, host=args.host, port=args.port, debug=args.debug, scheduler=not args.no_scheduler)
    return 0


def cmd_portal(args, settings) -> int:
    from .portal import run
    run(settings, host=args.host, port=args.port, debug=args.debug)
    return 0


def cmd_customers(args, settings) -> int:
    store = CustomerStore(settings)
    audit = AuditLogger(settings)
    cmd = args.customers_command

    def key_by_prefix(prefix: str):
        key = store.find_key_by_prefix(prefix)
        if key is None:
            raise CustomerError(f"no key matches '{prefix}' (use the prefix shown by 'customers list')")
        return key

    try:
        if cmd == "list":
            customers = store.list_customers(limit=args.limit, q=args.q)
            if not customers:
                print("no customers")
                return 0
            two_factor = TwoFactorService(settings)
            for c in customers:
                kind = "guest" if c.is_guest else c.role
                flags = []
                if not c.is_guest:
                    flags.append("verified" if c.is_email_verified else "UNVERIFIED")
                    methods = ",".join(m.method for m in two_factor.methods(PRINCIPAL_CUSTOMER, c.customer_id))
                    flags.append("2fa:" + (methods or "none"))
                print(f"{c.email:<40} {kind:<8} {'active' if c.is_active else 'DISABLED':<9} "
                      f"since {c.created_at[:10]}  {' '.join(flags)}{'  ' + c.display_name if c.display_name else ''}")
                for k in store.keys_of(c.customer_id):
                    print(f"    {k.key_prefix}…  balance {k.balance:>8,}  bought {k.tokens_purchased:>8,}  "
                          f"used {k.tokens_used:>8,}  {'active' if k.is_active else 'revoked'}"
                          f"{'  (' + k.label + ')' if k.label else ''}")
            return 0
        if cmd == "orders":
            orders = store.list_orders(limit=args.limit, status=args.status)
            if not orders:
                print("no orders")
                return 0
            print(f"{'Order':<16} {'When':<17} {'Status':<10} {'Pack':<14} {'Tokens':>8} {'Amount':>14}  Customer")
            for o in orders:
                print(f"{o.order_ref:<16} {(o.paid_at or o.created_at)[:16]:<17} {o.status:<10} {o.pack:<14} "
                      f"{o.tokens:>8,} {o.price:>14}  {o.email}{' (guest)' if o.is_guest else ''}")
            return 0
        if cmd == "usage":
            key_id = key_by_prefix(args.key).api_key_id if args.key else None
            rows = store.usage(api_key_id=key_id, limit=args.limit)
            if not rows:
                print("no requests")
                return 0
            prefixes = {k.api_key_id: k.key_prefix for k in store.list_keys(limit=5000)}
            print(f"{'When':<17} {'Key':<14} {'Data':<8} {'Year':<5} {'Tokens':>6} {'Rows':>8} {'Status':<20} Tickers")
            for u in rows:
                print(f"{u.requested_at[:16]:<17} {prefixes.get(u.api_key_id, '?'):<14} {u.endpoint:<8} {u.year or '':<5} "
                      f"{u.tokens:>6} {u.rows:>8,} {u.status:<20} {(u.tickers or '')[:40]}")
            return 0
        if cmd == "stats":
            st = store.stats()
            print(f"customers: {st['customers']} ({st['accounts']} accounts, {st['guests']} guests)")
            print(f"keys: {st['active_keys']} active of {st['keys']}")
            print(f"orders: {st['paid_orders']} paid of {st['orders']} - revenue {st['revenue'] / 100:,.2f} {st['currency']}")
            print(f"tokens: {st['tokens_sold']:,} sold / granted, {st['tokens_used']:,} used, "
                  f"{st['tokens_outstanding']:,} outstanding")
            print(f"requests: {st['requests']:,} ({st['refused_requests']:,} refused for lack of tokens)")
            return 0
        if cmd == "grant":
            key = key_by_prefix(args.key)
            order, key = store.grant_tokens(key.api_key_id, args.tokens, note=args.note, granted_by="cli")
            audit.log("portal", "tokens_granted", f"cli granted {args.tokens} tokens to key {key.key_prefix} "
                      f"({order.order_ref})", level="WARNING",
                      details={"key": key.key_prefix, "tokens": args.tokens, "order": order.order_ref})
            print(f"{args.tokens:,} tokens granted to {key.key_prefix}… - balance now {key.balance:,} ({order.order_ref})")
            return 0
        if cmd == "new-key":
            customer = store.get_or_create_guest(args.email)
            key, plain = store.create_key(customer.customer_id, args.label)
            if args.tokens > 0:
                _, key = store.grant_tokens(key.api_key_id, args.tokens, note="granted with the key", granted_by="cli")
            audit.log("portal", "key_created", f"cli created key {key.key_prefix} for {customer.email}",
                      details={"key": key.key_prefix, "email": customer.email, "tokens": args.tokens})
            print(f"key for {customer.email}: {plain}   (balance {key.balance:,} tokens - shown only once)")
            return 0
        if cmd in ("disable-key", "enable-key"):
            key = store.set_key_active(key_by_prefix(args.key).api_key_id, cmd == "enable-key")
            print(f"key {key.key_prefix}… {'re-activated' if key.is_active else 'disabled'}")
            return 0
        if cmd == "add-admin":
            customer = store.get_customer_by_email(args.email)
            if customer is None:
                try:
                    password = _password_from(args)
                except AuthError as exc:
                    raise CustomerError(str(exc)) from exc
                customer = store.register(args.email, password, display_name=args.name, role="admin", email_verified=True)
                print(f"administrator account {customer.email} created")
            elif customer.is_guest:
                raise CustomerError(f"{customer.email} is a guest (no password) - use 'customers set-password' first")
            else:
                customer = store.set_role(customer.customer_id, "admin")
                print(f"{customer.email} is now an administrator")
            audit.log("portal", "customer_role", f"cli made {customer.email} an administrator", level="WARNING",
                      details={"email": customer.email, "role": "admin"})
            if settings.admin_requires_2fa:
                print("note: the admin pages open once the account has set up two-factor authentication "
                      "(/account/security)")
            return 0
        customer = store.get_customer_by_email(args.email)
        if customer is None:
            raise CustomerError(f"no customer with e-mail '{args.email}'")
        if cmd == "role":
            customer = store.set_role(customer.customer_id, args.role)
            audit.log("portal", "customer_role", f"cli: {customer.email} is now {customer.role}", level="WARNING",
                      details={"email": customer.email, "role": customer.role})
            print(f"{customer.email} is now {customer.role}")
            return 0
        if cmd == "reset-2fa":
            n = TwoFactorService(settings).disable_all(PRINCIPAL_CUSTOMER, customer.customer_id)
            audit.log("portal", "2fa_reset", f"cli removed {n} second factor(s) of {customer.email}", level="WARNING",
                      details={"email": customer.email})
            print(f"two-factor authentication of {customer.email} reset ({n} method(s) removed)")
            return 0
        if cmd == "verify":
            store.mark_email_verified(customer.customer_id)
            print(f"{customer.email} marked as verified")
            return 0
        if cmd == "set-password":
            if customer.is_guest:
                # a guest becomes an account with the password (the e-mail was used for purchases already)
                with store.connection() as conn:
                    from .auth import check_password_strength, hash_password
                    try:
                        password = _password_from(args)
                        check_password_strength(password)
                    except AuthError as exc:
                        raise CustomerError(str(exc)) from exc
                    conn.execute('UPDATE "Api_Customer" SET "PasswordHash" = ?, "EmailVerified" = 1 WHERE "CustomerId" = ?',
                                 (hash_password(password), customer.customer_id))
            else:
                try:
                    store.set_password(customer.customer_id, _password_from(args))
                except AuthError as exc:
                    raise CustomerError(str(exc)) from exc
            audit.log("portal", "password_reset", f"cli set the password of {customer.email}", level="WARNING",
                      details={"email": customer.email})
            print(f"password of {customer.email} updated")
            return 0
        customer = store.update_customer(customer.customer_id, active=cmd == "enable")
        audit.log("portal", "customer_updated", f"cli {cmd}d customer {customer.email}", level="WARNING",
                  details={"email": customer.email, "active": customer.is_active})
        print(f"customer {customer.email} {'enabled' if customer.is_active else 'disabled'}")
        return 0
    except CustomerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def cmd_db(args, settings) -> int:
    import sqlite3
    path = settings.sqlite_path
    if not path.is_file():
        print(f"no database at {path}")
        return 1
    if args.db_command == "size":
        size = path.stat().st_size
        print(f"{path}: {size / 1048576:,.1f} MB")
        for extra in (path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
            if extra.exists():
                print(f"{extra.name}: {extra.stat().st_size / 1048576:,.1f} MB")
        with sqlite3.connect(str(path)) as conn:
            try:
                rows = conn.execute('SELECT name, SUM(pgsize) FROM dbstat GROUP BY name ORDER BY 2 DESC').fetchall()
            except sqlite3.OperationalError:
                rows = []
            free = conn.execute("PRAGMA freelist_count").fetchone()[0] * conn.execute("PRAGMA page_size").fetchone()[0]
            counts = {}
            for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
                counts[name] = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        if rows:
            print(f"{'object':<44} {'MB':>8} {'rows':>10}")
            for name, bytes_ in rows[:25]:
                print(f"{name:<44} {bytes_ / 1048576:>8.1f} {counts.get(name, ''):>10}")
        else:
            print("(this SQLite build has no dbstat table - per-object sizes unavailable)")
            for name, n in sorted(counts.items(), key=lambda x: -x[1])[:25]:
                print(f"{name:<44} {n:>10,} rows")
        print(f"free pages: {free / 1048576:,.1f} MB (reclaim with 'db vacuum')")
        stage = counts.get(settings.stage_table, 0)
        if stage:
            print(f"hint: the staging table holds {stage:,} rows - 'db purge-stage' removes the ones already promoted")
        return 0
    db = StageDatabase(settings)
    if args.db_command == "purge-stage":
        from datetime import datetime, timedelta
        daily = db.q(settings.daily_table)
        # IN (subquery): SQLite materialises the promoted stage ids once (no index on Stock_Daily.StageId needed)
        sql = f'FROM {db.table} WHERE "StageId" IN (SELECT "StageId" FROM {daily} WHERE "StageId" IS NOT NULL)'
        params: list = []
        if not args.all:
            cutoff = (datetime.now() - timedelta(days=args.keep_days)).replace(microsecond=0).isoformat(sep=" ")
            sql += ' AND "LoadedAt" < ?'
            params.append(cutoff)
        with db.connection() as conn:
            db.ensure_stage_table(conn)
            PromotionService(settings, db).ensure_tables(conn)
            n = conn.execute("SELECT COUNT(*) " + sql, params).fetchone()[0]
            total = conn.execute(f"SELECT COUNT(*) FROM {db.table}").fetchone()[0]
            if args.dry_run:
                print(f"would delete {n:,} of {total:,} staging rows (promoted"
                      f"{'' if args.all else f', loaded more than {args.keep_days} days ago'})")
                return 0
            conn.execute("DELETE " + sql, params)
        AuditLogger(settings).log("db", "stage_purged", f"{n:,} promoted staging rows deleted",
                                  details={"deleted": n, "keep_days": None if args.all else args.keep_days})
        print(f"deleted {n:,} of {total:,} staging rows (the daily table keeps the data; Load_History keeps the registry)")
        if not args.vacuum:
            print("run 'db vacuum' to give the space back to the file system")
    if args.db_command == "keep-tickers":
        return _db_keep_tickers(args, settings, path)
    if args.db_command == "reset-data":
        import re as _re
        tables = [settings.stage_table, settings.daily_table, settings.weekly_table, settings.monthly_table,
                  settings.eod_stage_table, "Data_Error"]
        if not args.keep_history:
            tables += ["Load_History", "Promotion_History"]
        with sqlite3.connect(str(path)) as conn:
            existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            tables = [t for t in tables if t in existing]
            counts = {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
            indexes = [(r[0], r[1], r[2]) for r in conn.execute(
                "SELECT name, tbl_name, sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL "
                "AND tbl_name IN (%s)" % ",".join("?" * len(tables)), tables)]
        print("tables to empty:   " + ", ".join(f"{t} ({counts[t]:,} rows)" for t in tables))
        print("indexes to rebuild: " + ", ".join(n for n, _, _ in indexes) +
              " (+ the UNIQUE / PRIMARY KEY indexes, rebuilt by REINDEX)")
        if not args.yes:
            print("dry run - add --yes to do it")
            return 0
        before = path.stat().st_size
        conn = sqlite3.connect(str(path), isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            for t in tables:
                conn.execute(f'DELETE FROM "{t}"')
                conn.execute('DELETE FROM "sqlite_sequence" WHERE "name" = ?', (t,))
            for name, _, _ in indexes:
                conn.execute(f'DROP INDEX IF EXISTS "{name}"')
            for _, _, ddl in indexes:
                conn.execute(_re.sub(r"^CREATE INDEX", "CREATE INDEX IF NOT EXISTS", ddl, flags=_re.I))
            for t in tables:
                conn.execute(f'REINDEX "{t}"')
            conn.execute("COMMIT")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
        except BaseException:
            conn.execute("ROLLBACK") if conn.in_transaction else None
            raise
        finally:
            conn.close()                                     # the file size is final once closed
        AuditLogger(settings).log("db", "data_reset", "stock data tables emptied and their indexes rebuilt",
                                  level="WARNING", details={"tables": counts})
        print(f"done: {sum(counts.values()):,} rows deleted, {len(indexes)} indexes rebuilt, "
              f"file {before / 1048576:,.1f} MB -> {path.stat().st_size / 1048576:,.1f} MB")
        return 0
    if args.db_command == "vacuum" or getattr(args, "vacuum", False):
        before = path.stat().st_size
        conn = sqlite3.connect(str(path), isolation_level=None)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
        finally:
            conn.close()
        after = path.stat().st_size
        print(f"vacuum: {before / 1048576:,.1f} MB -> {after / 1048576:,.1f} MB")
    return 0


def _db_keep_tickers(args, settings, path) -> int:
    """db keep-tickers: the stock data of the given tickers stays, every other ticker's rows are deleted."""
    import re as _re
    import sqlite3
    keep: list[str] = []
    for item in args.tickers:
        for ticker in item.replace(";", ",").split(","):
            ticker = ticker.strip().upper()
            if not ticker:
                continue
            if not _re.match(r"^[A-Z0-9.\-_^=]{1,32}$", ticker):
                print(f"ERROR: '{ticker}' is not a ticker symbol", file=sys.stderr)
                return 2
            if ticker not in keep:
                keep.append(ticker)
    if not keep:
        print("ERROR: name at least one ticker to keep", file=sys.stderr)
        return 2
    # every table with stock data per ticker (rows without a ticker - file-level findings - stay)
    tables = list(dict.fromkeys([settings.stage_table, settings.eod_stage_table, settings.daily_table,
                                 settings.weekly_table, settings.monthly_table, "Data_Error", "Data_Correction"]))
    marks = ",".join("?" * len(keep))
    where = f'"Ticker" IS NOT NULL AND UPPER("Ticker") NOT IN ({marks})'
    plan: dict[str, tuple[int, int, list[str]]] = {}
    with sqlite3.connect(str(path)) as conn:
        existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        for table in tables:
            if table not in existing:
                continue
            columns = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
            if "Ticker" not in columns:
                continue
            total = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            gone = conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE {where}', keep).fetchone()[0]
            others = [r[0] for r in conn.execute(f'SELECT DISTINCT "Ticker" FROM "{table}" WHERE {where} '
                                                 f'ORDER BY 1 LIMIT 6', keep)]
            plan[table] = (gone, total, others)
    print(f"keep the stock data of: {', '.join(keep)}")
    for table, (gone, total, others) in plan.items():
        sample = (f" (e.g. {', '.join(others[:5])}{' ...' if len(others) > 5 else ''})" if others else "")
        print(f"  {table:<20} delete {gone:>10,} of {total:>10,} rows{sample}")
    deleted = sum(g for g, _, _ in plan.values())
    if not args.yes:
        print("dry run - add --yes to delete them")
        return 0
    before = path.stat().st_size
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for table in plan:
            conn.execute(f'DELETE FROM "{table}" WHERE {where}', keep)
        conn.execute("COMMIT")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    AuditLogger(settings).log("db", "tickers_kept", f"stock data kept for {', '.join(keep)} only - "
                              f"{deleted:,} rows of other tickers deleted", level="WARNING",
                              details={"keep": keep, "deleted": {t: g for t, (g, _, _) in plan.items()}})
    print(f"done: {deleted:,} rows deleted, file {before / 1048576:,.1f} MB -> {path.stat().st_size / 1048576:,.1f} MB")
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
    CustomerStore(settings).ensure_tables()
    TwoFactorService(settings).ensure_tables()
    IdentityStore(settings).ensure_table()
    EodStageLoader(settings, db).ensure_table()
    SymbolListLoader(settings, db).ensure_table()
    from .eod_market import MarketFetcher
    MarketFetcher(settings, db=db, fake=False).ensure_tables()
    from .load_limits import StockLoadLedger
    with db.connection() as conn:
        StockLoadLedger.ensure_table(conn)
    from .bulk_schedule import ScheduleStore
    ScheduleStore(settings).ensure_table()
    calendar = TradingCalendar(settings, db)
    years = default_update_years()
    updated = calendar.update_years(years)
    print(f"staging table {settings.stage_table} is ready (sqlite: {settings.sqlite_path})")
    print(f"no-trading days ready for {', '.join(m.market for m in calendar.markets(active_only=True))} "
          f"({years[0]}-{years[-1]}, {sum(r.added for r in updated)} day(s) added)")
    print(f"active tables ready: {settings.daily_table}, {settings.weekly_table}, {settings.monthly_table}")
    print("support tables ready: Load_History, App_Log, App_Error, Data_Error, Data_Correction, App_User, "
          "Promotion_History, Stock_Symbol")
    print("portal tables ready: Api_Customer, Api_Key, Api_Order, Api_Usage")
    print("account tables ready: Auth_TwoFactor, Auth_Code, Auth_RecoveryCode, Auth_Identity")
    print(f"EOD API staging tables ready: {settings.eod_stage_table}, {settings.eod_symbol_stage_table} "
          f"(+ the eod-fetch-market run registry Eod_Market_Run, Eod_Market_Run_Item, and Eod_Stock_Load - the "
          f"daily load limit per stock, and Eod_Bulk_Schedule - the daily schedules)")
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
    eod = EodStageLoader(settings, db).summary()
    print(f"EOD API staging table {settings.eod_stage_table}: "
          + (f"{sum(s.rows for s in eod):,} rows of {len(eod)} symbol(s) ({', '.join(s.symbol for s in eod[:10])}"
             f"{', ...' if len(eod) > 10 else ''})" if eod else "empty"))
    lists = SymbolListLoader(settings, db).summary()
    print(f"symbol list staging table {settings.eod_symbol_stage_table}: "
          + (", ".join(f"{m.market} {m.codes:,} symbols (list of {m.last_loaded[:10]})" for m in lists) if lists
             else "empty"))
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
                                     created_by="cli", must_change_password=args.must_change, email=args.email)
            audit.log("auth", "user_created", f"cli created user '{user.username}' ({user.role})",
                      details={"username": user.username, "role": user.role})
            print(f"user '{user.username}' created with role {user.role}")
            return 0
        if args.user_command == "list":
            users = store.list_users()
            if not users:
                print("no users - create the initial administrator with: user add admin --role admin")
                return 0
            two_factor = TwoFactorService(settings)
            print(f"{'Username':<20} {'Role':<9} {'Status':<9} {'2FA':<16} {'E-mail':<28} {'Name':<20} Last sign-in")
            for u in users:
                status = "active" if u.is_active else "disabled"
                if u.must_change_password:
                    status += "*"
                methods = ",".join(m.method for m in two_factor.methods(PRINCIPAL_STAFF, u.user_id)) or "-"
                print(f"{u.username:<20} {u.role:<9} {status:<9} {methods:<16} {(u.email or '-'):<28} "
                      f"{(u.display_name or ''):<20} {u.last_login_at or '-'}")
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
        elif args.user_command == "email":
            updated = store.update(user.user_id, email=args.email)
            print(f"e-mail of '{user.username}' is now {updated.email or '(none)'}")
        elif args.user_command == "reset-2fa":
            n = TwoFactorService(settings).disable_all(PRINCIPAL_STAFF, user.user_id)
            print(f"two-factor authentication of '{user.username}' reset ({n} method(s) removed)")
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
    "portal": cmd_portal,
    "customers": cmd_customers,
    "db": cmd_db,
    "init-db": cmd_init_db,
    "eod-fetch": cmd_eod_fetch,
    "eod-fetch-market": cmd_eod_fetch_market,
    "eod-market-runs": cmd_eod_market_runs,
    "eod-limits": cmd_eod_limits,
    "eod-bulk-fetch": cmd_eod_bulk_fetch,
    "eod-bulk-load": cmd_eod_bulk_load,
    "eod-bulk-schedule": cmd_eod_bulk_schedule,
    "eod-load": cmd_eod_load,
    "eod-stage": cmd_eod_stage,
    "eod-symbols-fetch": cmd_eod_symbols_fetch,
    "eod-symbols-load": cmd_eod_symbols_load,
    "eod-symbols": cmd_eod_symbols,
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
    from .load_limits import LoadLimitError
    try:
        return COMMANDS[args.command](args, settings)
    except LoadLimitError as exc:                # a daily limit: a refusal (App_Log WARNING), not an error
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    except (ConversionError, DatabaseError, ConfigError, FormatError, LoadError, PromotionError, PaymentError,
            CustomerError, MessagingError, OAuthError, EodApiError, EodStageError, SymbolStageError) as exc:
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
