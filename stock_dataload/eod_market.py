"""
End-of-day prices of a **whole market** (NYSE, NASDAQ, ...) from the EOD data API.

    python -m stock_dataload eod-fetch-market NYSE                 # every common stock of NYSE
    python -m stock_dataload eod-fetch-market NYSE --from 2026-01-01 --promote
    python -m stock_dataload eod-fetch-market NYSE --resume        # continue an interrupted / failed run

    from stock_dataload.eod_market import fetch_market
    run = fetch_market("NYSE")                                     # settings from config.ini
    print(run.summary())

How it works
------------
1. **The symbols of the market** come from the exchange symbol list in ``Stock_Symbol_Stage``
   (``eod-symbols-fetch NYSE``).  When the market has no list yet - or with ``refresh_list`` - the
   list is requested from the exchange-symbol-list API and loaded directly (no file).  Only the
   types of ``[eod_api] market_types`` are fetched (default ``Common Stock``: no ETFs, funds,
   preferred shares, warrants ...).
2. **One API call per stock** - the same endpoint as ``eod-fetch``
   (``<base_url><SYMBOL>?api_token=..&fmt=json``); the answer is **loaded straight into the
   staging table** ``Stock_EOD_Stage`` (validate, type, upsert per symbol and day - see
   :mod:`eod_stage`).  No JSON file is written.
3. **Errors and retries**, per stock:

   ===============================================  =====================================
   network error, timeout, 429, 500/502/503/504,    retried up to ``[eod_api] retries``
   unreadable / truncated answer                    times (default 3), waiting
                                                    ``retry_backoff * 2^(n-1)`` seconds
                                                    (a ``Retry-After`` of the API wins)
   404 (unknown symbol), 400                        not retried - the stock is skipped
   invalid records in the answer                    not retried (a data problem) - the
                                                    stock is reported; ``skip_bad_rows``
                                                    loads its valid records
   an empty answer                                  the stock has no data (not an error)
   401 / 402 / 403 (token, quota)                   the whole run stops - every further
                                                    call would fail the same way
   ``max_consecutive_failures`` stocks failed       the run stops (the API is down)
   in a row
   ===============================================  =====================================

   One stock failing never stops the loop.
4. **At most 3 whole-market loads per market and day** (:data:`MAX_MARKET_LOADS_PER_DAY`): a run
   that fetches the market's symbol list (no ``symbols`` given) is refused with
   :class:`MarketLoadLimitError` once the market already had 3 such runs today (local date of
   ``StartedAt``, any gateway, any outcome).  Loads of selected stocks and ``resume`` of an unfinished
   run do not count.  The check and the registration of the run are one transaction, so the
   dashboard and the command line cannot both slip past the limit.
   The run's ``source`` is ``market`` (the symbol list), ``symbols`` (typed in / ``--symbols``) or
   ``file`` (a text file: at most 100 symbols, at most 5 such runs a day - :mod:`load_limits`).
   **Every stock is loaded at most 5 times a day**: a stock that reached it is *skipped* without an
   API call (status ``skipped``, error ``daily limit: ...``, logged as a warning); a run whose stocks
   all reached it is refused before it is registered (:class:`~stock_dataload.load_limits.StockLoadLimitError`).
5. **Every run is registered** (``Eod_Market_Run``) with the outcome of every stock
   (``Eod_Market_Run_Item``: status, attempts, rows, error), so a stopped, interrupted
   (Ctrl+C) or partly failed run can be continued: ``resume`` fetches only the stocks that were
   not loaded yet.  ``promote`` moves the loaded data into the daily table afterwards
   (``promote eod`` with the market).

The fake gateway (``--fake`` / ``[eod_api] gateway = fake``) serves invented prices per symbol
(:class:`~stock_dataload.eod_api.FakeEodGateway` with ``vary_by_symbol``) and can simulate
failures (``fake_faults``, see :class:`~stock_dataload.eod_api.FaultPlan`).  Loads from the fake are
marked (``SourceFile`` ``fake-api/...``); a fake run refuses to write into a staging table that holds
real API data unless ``allow_fake_data`` is set.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterable, Iterator

from .config import Settings, load_settings
from .db import StageDatabase, quote_identifier as _q
from .eod_api import (GATEWAY_FAKE, EodApiClient, EodApiError, FakeEodGateway, FaultPlan, UrllibGetter, redact,
                      validate_market, validate_symbol)
from .load_limits import (MAX_LOADS_PER_STOCK_PER_DAY, FileLoadLimitError, FileLoadQuota,
                          LoadLimitError, StockLoadLedger, StockLoadLimitError, audit_refusal, check_file_symbols,
                          file_loads, skip_message, stock_refusal, ticker_of)

log = logging.getLogger(__name__)

COMPONENT = "eod_market"
RUN_TABLE = "Eod_Market_Run"
ITEM_TABLE = "Eod_Market_Run_Item"

# the outcome of one stock
ITEM_PENDING = "pending"
ITEM_LOADED = "loaded"            # answer received and loaded (rejected records, if any, are reported)
ITEM_NO_DATA = "no_data"          # the API answered an empty list
ITEM_FAILED = "failed"            # not loaded: error after the retries, 404, invalid records ...
ITEM_SKIPPED = "skipped"          # not called: the code is no valid symbol, or the stock reached its daily limit
ITEM_STATUSES = (ITEM_PENDING, ITEM_LOADED, ITEM_NO_DATA, ITEM_FAILED, ITEM_SKIPPED)

# the outcome of a run
RUN_RUNNING = "running"
RUN_COMPLETED = "completed"           # every stock loaded / without data
RUN_COMPLETED_ERRORS = "completed_with_errors"
RUN_ABORTED = "aborted"               # stopped: token / quota refused, or too many failures in a row
RUN_INTERRUPTED = "interrupted"       # Ctrl+C / the process ended - resume it

#: HTTP statuses worth another try; None = no answer (network problem, timeout)
RETRYABLE_STATUSES = (None, 408, 425, 429, 500, 502, 503, 504)
#: HTTP statuses that stop the whole run - every further call would fail the same way
FATAL_STATUSES = (401, 402, 403)
#: longest wait between two tries
MAX_BACKOFF_SECONDS = 120.0

SOURCE_LIVE = "api"
SOURCE_FAKE = "fake-api"

RUN_DDL = f"""
CREATE TABLE IF NOT EXISTS "{RUN_TABLE}" (
    "RunId"        TEXT PRIMARY KEY,
    "Market"       TEXT NOT NULL,
    "Gateway"      TEXT NOT NULL,              -- live | fake
    "Status"       TEXT NOT NULL,              -- running | completed | completed_with_errors | aborted | interrupted
    "Parameters"   TEXT,                       -- JSON: types, from / to, retries ...
    "Symbols"      INTEGER NOT NULL DEFAULT 0,
    "Loaded"       INTEGER NOT NULL DEFAULT 0,
    "NoData"       INTEGER NOT NULL DEFAULT 0,
    "Failed"       INTEGER NOT NULL DEFAULT 0,
    "Skipped"      INTEGER NOT NULL DEFAULT 0,
    "Rows"         INTEGER NOT NULL DEFAULT 0, -- records loaded into the staging table
    "ApiCalls"     INTEGER NOT NULL DEFAULT 0,
    "Message"      TEXT,
    "StartedAt"    TEXT NOT NULL,
    "FinishedAt"   TEXT
)
"""

ITEM_DDL = f"""
CREATE TABLE IF NOT EXISTS "{ITEM_TABLE}" (
    "RunId"        TEXT NOT NULL REFERENCES "{RUN_TABLE}" ("RunId"),
    "Position"     INTEGER NOT NULL,
    "Symbol"       TEXT NOT NULL,
    "Status"       TEXT NOT NULL,              -- pending | loaded | no_data | failed | skipped
    "Attempts"     INTEGER NOT NULL DEFAULT 0, -- API calls made for the stock
    "HttpStatus"   INTEGER,                    -- of the last failed call
    "Received"     INTEGER NOT NULL DEFAULT 0,
    "Inserted"     INTEGER NOT NULL DEFAULT 0,
    "Updated"      INTEGER NOT NULL DEFAULT 0,
    "Unchanged"    INTEGER NOT NULL DEFAULT 0,
    "Rejected"     INTEGER NOT NULL DEFAULT 0,
    "FirstDate"    TEXT,
    "LastDate"     TEXT,
    "LoadId"       TEXT,
    "Error"        TEXT,
    "FinishedAt"   TEXT,
    CONSTRAINT "PK_{ITEM_TABLE}" PRIMARY KEY ("RunId", "Symbol")
)
"""

INDEXES = (
    f'CREATE INDEX IF NOT EXISTS "IX_{RUN_TABLE}_Market" ON "{RUN_TABLE}" ("Market", "StartedAt")',
    f'CREATE INDEX IF NOT EXISTS "IX_{ITEM_TABLE}_Status" ON "{ITEM_TABLE}" ("RunId", "Status")',
)


def render_ddl() -> str:
    """The DDL of the run registry (``sql/create_eod_market_run_sqlite.sql``)."""
    return "\n".join(s.strip() + ";" for s in (RUN_DDL, ITEM_DDL, *INDEXES)) + "\n"


#: whole-market loads (every stock of the market's symbol list) allowed per market and day
MAX_MARKET_LOADS_PER_DAY = 3


class EodMarketError(Exception):
    """The run cannot start (no symbols, bad arguments, fake data into real data ...)."""


class MarketLoadLimitError(EodMarketError, LoadLimitError):
    """The market already had :data:`MAX_MARKET_LOADS_PER_DAY` whole-market loads today."""

    event = "market_run_refused"

    def __init__(self, quota: "MarketLoadQuota"):
        self.quota = quota
        super().__init__(quota.refusal())


@dataclass
class MarketLoadQuota:
    """The whole-market loads of one market on one day."""

    market: str
    day: str                                      # YYYY-MM-DD (local date)
    started: list[str] = field(default_factory=list)   # StartedAt of today's whole-market runs
    limit: int = MAX_MARKET_LOADS_PER_DAY

    @property
    def used(self) -> int:
        return len(self.started)

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit

    def refusal(self) -> str:
        times = ", ".join(t[11:16] for t in self.started if len(t) >= 16)
        return (f"{self.market}: the bulk load of all stocks of a market may run at most {self.limit} times a day - "
                f"it already ran {self.used} time(s) today{f' (at {times})' if times else ''}. Try again tomorrow, "
                f"load selected stocks instead, or resume an unfinished run.")

    def as_dict(self) -> dict:
        return {"market": self.market, "day": self.day, "limit": self.limit, "used": self.used,
                "remaining": self.remaining, "exhausted": self.exhausted, "started": list(self.started)}


#: most symbols one text / file of symbols may hold
SYMBOL_TEXT_MAX = 10_000


def parse_symbol_text(text: str | None) -> tuple[list[str], list[str]]:
    """The symbols of a text typed in or read from a file - separated by commas (line breaks,
    semicolons and blanks work too; quotes around a symbol are dropped): ``(symbols, invalid)``,
    the symbols upper case, unique, in the given order."""
    import re
    symbols: list[str] = []
    invalid: list[str] = []
    for token in re.split(r"[,;\s]+", (text or "").lstrip("\ufeff")):
        token = token.strip().strip("\"'").strip()
        if not token:
            continue
        try:
            symbol = validate_symbol(token)
        except EodApiError:
            if token not in invalid:
                invalid.append(token[:40])
            continue
        if symbol not in symbols:
            symbols.append(symbol)
    if len(symbols) > SYMBOL_TEXT_MAX:
        raise EodMarketError(f"at most {SYMBOL_TEXT_MAX:,} symbols at once (got {len(symbols):,})")
    return symbols, invalid


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------
@dataclass
class StockOutcome:
    symbol: str
    status: str = ITEM_PENDING
    attempts: int = 0
    http_status: int | None = None
    received: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    rejected: int = 0
    first_date: str | None = None
    last_date: str | None = None
    load_id: str | None = None
    error: str | None = None
    limited: bool = False              # skipped: the stock reached its daily limit (not called)

    @property
    def rows(self) -> int:
        return self.inserted + self.updated + self.unchanged

    def __str__(self) -> str:
        tries = f" after {self.attempts} attempt(s)" if self.attempts > 1 else ""
        if self.status == ITEM_LOADED:
            span = f" {self.first_date} .. {self.last_date}," if self.first_date else ""
            text = (f"{self.symbol}: {self.received:,} record(s){span} {self.inserted:,} inserted, "
                    f"{self.updated:,} updated, {self.unchanged:,} unchanged")
            if self.rejected:
                text += f", {self.rejected:,} rejected"
            return text + tries
        if self.status == ITEM_NO_DATA:
            return f"{self.symbol}: no data (the API answered an empty list){tries}"
        return f"{self.symbol}: {self.status.upper()}{tries} - {self.error}"


@dataclass
class MarketRun:
    run_id: str
    market: str
    gateway: str
    symbols: list[str]
    parameters: dict
    status: str = RUN_RUNNING
    started_at: str = field(default_factory=_now)
    finished_at: str | None = None
    message: str = ""
    api_calls: int = 0
    outcomes: dict[str, StockOutcome] = field(default_factory=dict)
    resumed_from: int = 0                       # stocks already done before this resume
    calls_before: int = 0                       # API calls of the run before this resume

    def count(self, status: str) -> int:
        return sum(1 for o in self.outcomes.values() if o.status == status)

    @property
    def loaded(self) -> int:
        return self.count(ITEM_LOADED)

    @property
    def failed(self) -> int:
        return self.count(ITEM_FAILED)

    @property
    def rows(self) -> int:
        return sum(o.rows for o in self.outcomes.values() if o.status == ITEM_LOADED)

    @property
    def ok(self) -> bool:
        return self.status == RUN_COMPLETED

    def failures(self) -> list[StockOutcome]:
        return [o for o in self.outcomes.values() if o.status == ITEM_FAILED]

    def limited(self) -> list[StockOutcome]:
        """The stocks skipped because they reached their daily limit (not called)."""
        return [o for o in self.outcomes.values() if o.status == ITEM_SKIPPED and o.limited]

    def summary(self) -> str:
        pending = self.count(ITEM_PENDING)
        limited = len(self.limited())
        text = (f"{self.market} ({self.gateway}): {len(self.symbols):,} symbol(s) - {self.loaded:,} loaded "
                f"({self.rows:,} rows), {self.count(ITEM_NO_DATA):,} without data, {self.failed:,} failed, "
                f"{self.count(ITEM_SKIPPED):,} skipped"
                + (f" ({limited:,} at the daily limit of {MAX_LOADS_PER_STOCK_PER_DAY} loads per stock)" if limited else "")
                + (f", {pending:,} not fetched" if pending else "") +
                f"; {self.api_calls:,} API call(s) - run {self.run_id} {self.status}")
        return text + (f": {self.message}" if self.message else "")


# ---------------------------------------------------------------------------
# the fetcher
# ---------------------------------------------------------------------------
class MarketFetcher:
    """Fetches the end-of-day prices of every stock of a market into the staging table."""

    def __init__(self, settings: Settings | None = None, client: EodApiClient | None = None, db: StageDatabase | None = None,
                 audit=None, sleep: Callable[[float], None] = time.sleep, fake_faults: str | FaultPlan | None = None,
                 fake: bool | None = None, api_token: str | None = None, base_url: str | None = None):
        """*api_token* / *base_url* override ``[eod_api]`` for this fetcher (the price calls; the token
        also for the symbol list)."""
        self.settings = settings or load_settings()
        self.api_token, self.base_url = api_token, base_url
        self.db = db or StageDatabase(self.settings)
        self.audit = audit
        self.sleep = sleep
        if client is None:
            use_fake = self.settings.eod_api_gateway == GATEWAY_FAKE if fake is None else fake
            transport = FakeEodGateway(vary_by_symbol=True, faults=fake_faults) if use_fake else UrllibGetter()
            client = EodApiClient(self.settings, transport=transport, audit=None)
        self.client = client
        self.fake = isinstance(self.client.transport, FakeEodGateway)
        self._ready = False
        self._calls = 0                                   # API calls of the current run
        self.ledger = StockLoadLedger(self.db)            # loads per stock and day (the daily limit)
        self._stock_loads: dict[str, int] = {}            # today's loads of the run's stocks (kept current)

    # -- the registry -------------------------------------------------------------------
    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        with self.db.connection() as conn:
            if not self._ready:
                for ddl in (RUN_DDL, ITEM_DDL, *INDEXES):
                    conn.execute(ddl)
                self._ready = True
            yield conn

    def ensure_tables(self) -> None:
        with self.connection():
            pass

    def _save_run(self, run: MarketRun) -> None:
        with self.connection() as conn:
            conn.execute(
                f'INSERT INTO "{RUN_TABLE}" ("RunId","Market","Gateway","Status","Parameters","Symbols","Loaded",'
                f'"NoData","Failed","Skipped","Rows","ApiCalls","Message","StartedAt","FinishedAt") '
                f'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT("RunId") DO UPDATE SET '
                f'"Status"=excluded."Status", "Symbols"=excluded."Symbols", "Loaded"=excluded."Loaded", '
                f'"NoData"=excluded."NoData", "Failed"=excluded."Failed", "Skipped"=excluded."Skipped", '
                f'"Rows"=excluded."Rows", "ApiCalls"=excluded."ApiCalls", "Message"=excluded."Message", '
                f'"FinishedAt"=excluded."FinishedAt"',
                (run.run_id, run.market, run.gateway, run.status, json.dumps(run.parameters, sort_keys=True),
                 len(run.symbols), run.loaded, run.count(ITEM_NO_DATA), run.failed, run.count(ITEM_SKIPPED), run.rows,
                 run.api_calls, run.message or None, run.started_at, run.finished_at))

    def _save_item(self, run: MarketRun, position: int, o: StockOutcome) -> None:
        with self.connection() as conn:
            conn.execute(
                f'INSERT INTO "{ITEM_TABLE}" ("RunId","Position","Symbol","Status","Attempts","HttpStatus","Received",'
                f'"Inserted","Updated","Unchanged","Rejected","FirstDate","LastDate","LoadId","Error","FinishedAt") '
                f'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT("RunId","Symbol") DO UPDATE SET '
                f'"Status"=excluded."Status", "Attempts"="{ITEM_TABLE}"."Attempts" + excluded."Attempts", '
                f'"HttpStatus"=excluded."HttpStatus", "Received"=excluded."Received", "Inserted"=excluded."Inserted", '
                f'"Updated"=excluded."Updated", "Unchanged"=excluded."Unchanged", "Rejected"=excluded."Rejected", '
                f'"FirstDate"=excluded."FirstDate", "LastDate"=excluded."LastDate", "LoadId"=excluded."LoadId", '
                f'"Error"=excluded."Error", "FinishedAt"=excluded."FinishedAt"',
                (run.run_id, position, o.symbol, o.status, o.attempts, o.http_status, o.received, o.inserted, o.updated,
                 o.unchanged, o.rejected, o.first_date, o.last_date, o.load_id, o.error,
                 None if o.status == ITEM_PENDING else _now()))

    def runs(self, market: str | None = None, limit: int = 20) -> list[dict]:
        where, params = ("WHERE \"Market\" = ?", [validate_market(market)]) if market else ("", [])
        with self.connection() as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(f'SELECT * FROM "{RUN_TABLE}" {where} ORDER BY "StartedAt" DESC, '
                                                  f'rowid DESC LIMIT ?', params + [int(limit)])]

    def run_items(self, run_id: str, statuses: Iterable[str] | None = None) -> list[dict]:
        statuses = list(statuses or [])
        where = f' AND "Status" IN ({",".join("?" * len(statuses))})' if statuses else ""
        with self.connection() as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(f'SELECT * FROM "{ITEM_TABLE}" WHERE "RunId" = ?{where} '
                                                  f'ORDER BY "Position"', [run_id, *statuses])]

    # -- the daily limit of whole-market loads ----------------------------------------------
    @staticmethod
    def _market_loads(conn: sqlite3.Connection, market: str, day: str) -> list[str]:
        """StartedAt of the whole-market runs of *market* on *day* (runs without the flag count too)."""
        rows = conn.execute(f'SELECT "StartedAt", "Parameters" FROM "{RUN_TABLE}" WHERE "Market" = ? AND '
                            f'substr("StartedAt", 1, 10) = ? ORDER BY "StartedAt"', (market, day)).fetchall()
        started = []
        for at, parameters in rows:
            try:
                given = bool(json.loads(parameters or "{}").get("symbols_given", False))
            except (ValueError, AttributeError):
                given = False
            if not given:
                started.append(at)
        return started

    def market_load_quota(self, market: str, day: str | None = None) -> MarketLoadQuota:
        """How many whole-market loads *market* had on *day* (default: today) and how many are left."""
        market = validate_market(market)
        day = day or datetime.now().date().isoformat()
        with self.connection() as conn:
            return MarketLoadQuota(market, day, self._market_loads(conn, market, day))

    def market_load_quotas(self, markets: Iterable[str] = (), day: str | None = None) -> dict[str, MarketLoadQuota]:
        """The quota of every market that had a run on *day* plus *markets* (for the dashboard)."""
        day = day or datetime.now().date().isoformat()
        wanted = {validate_market(m) for m in markets if m}
        with self.connection() as conn:
            wanted.update(r[0] for r in conn.execute(f'SELECT DISTINCT "Market" FROM "{RUN_TABLE}" WHERE '
                                                     f'substr("StartedAt", 1, 10) = ?', (day,)))
            return {m: MarketLoadQuota(m, day, self._market_loads(conn, m, day)) for m in sorted(wanted)}

    def check_market_load_allowed(self, market: str, requested_by: str | None = None) -> MarketLoadQuota:
        """Raise :class:`MarketLoadLimitError` (and record the refusal in the audit trail) when the
        market already had :data:`MAX_MARKET_LOADS_PER_DAY` whole-market loads today."""
        quota = self.market_load_quota(market)
        if quota.exhausted:
            self._refused(quota, requested_by)
            raise MarketLoadLimitError(quota)
        return quota

    def _refused(self, quota: MarketLoadQuota, requested_by: str | None = None) -> None:
        log.warning("eod market: %s", quota.refusal())
        if self.audit is not None:
            try:
                self.audit.log(COMPONENT, "market_run_refused", quota.refusal(), level="WARNING",
                               details={**quota.as_dict(), "requested_by": requested_by})
            except Exception:  # noqa: BLE001 - the audit trail must not hide the refusal
                log.warning("could not record the refused market run of %s", quota.market)

    def file_load_quota(self, day: str | None = None) -> FileLoadQuota:
        """The loads from a text file of *day* (default today; all markets) and how many are left."""
        day = day or datetime.now().date().isoformat()
        with self.connection() as conn:
            return FileLoadQuota(day, file_loads(conn, day, RUN_TABLE))

    def check_file_load_allowed(self, requested_by: str | None = None) -> FileLoadQuota:
        """Raise :class:`~stock_dataload.load_limits.FileLoadLimitError` (recorded in the audit trail)
        when text-file loads ran :data:`~stock_dataload.load_limits.MAX_FILE_LOADS_PER_DAY` times today."""
        quota = self.file_load_quota()
        if quota.exhausted:
            error = FileLoadLimitError(quota)
            audit_refusal(self.audit, COMPONENT, error, **quota.as_dict(), requested_by=requested_by)
            raise error
        return quota

    def check_stocks_allowed(self, symbols: Iterable[str], requested_by: str | None = None) -> dict[str, int]:
        """The stocks of *symbols* that reached their daily limit (``{ticker: loads today}``) - they will
        be skipped.  Raises :class:`~stock_dataload.load_limits.StockLoadLimitError` (recorded in the
        audit trail) when that is every one of them."""
        tickers = {ticker_of(s) for s in symbols if s and s.strip()}
        if not tickers:
            return {}
        over = self.ledger.exhausted(tickers)
        if over and len(over) == len(tickers):
            error = StockLoadLimitError("nothing to load: " + stock_refusal(over, self.ledger.limit), over)
            audit_refusal(self.audit, COMPONENT, error, tickers=sorted(over)[:100], requested_by=requested_by)
            raise error
        return over

    def _register_market_run(self, run: MarketRun) -> None:
        """Check the daily limit and register the run in ONE transaction (write lock taken first),
        so two processes cannot both start the last allowed whole-market load."""
        self._register_limited_run(run)

    def _register_limited_run(self, run: MarketRun) -> None:
        """Register a whole-market run / a text-file run: its daily limit is checked again inside the
        transaction that inserts it, so two processes cannot both start the last allowed one."""
        day = run.started_at[:10]
        source = run.parameters.get("source")
        with self.connection() as conn:
            if conn.in_transaction:                     # nothing may be pending before the write lock
                conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            if source == "file":
                file_quota = FileLoadQuota(day, file_loads(conn, day, RUN_TABLE))
                if file_quota.exhausted:
                    conn.rollback()
                    error = FileLoadLimitError(file_quota)
                    audit_refusal(self.audit, COMPONENT, error, **file_quota.as_dict())
                    raise error
            else:
                quota = MarketLoadQuota(run.market, day, self._market_loads(conn, run.market, day))
                if quota.exhausted:
                    conn.rollback()
                    self._refused(quota)
                    raise MarketLoadLimitError(quota)
            self._insert_run(conn, run)

    def _insert_run(self, conn: sqlite3.Connection, run: MarketRun) -> None:
        conn.execute(
            f'INSERT INTO "{RUN_TABLE}" ("RunId","Market","Gateway","Status","Parameters","Symbols","Loaded",'
            f'"NoData","Failed","Skipped","Rows","ApiCalls","Message","StartedAt","FinishedAt") '
            f'VALUES (?,?,?,?,?,?,0,0,0,0,0,0,NULL,?,NULL)',
            (run.run_id, run.market, run.gateway, run.status, json.dumps(run.parameters, sort_keys=True),
             len(run.symbols), run.started_at))
        conn.executemany(f'INSERT INTO "{ITEM_TABLE}" ("RunId","Position","Symbol","Status") VALUES (?,?,?,?)',
                         [(run.run_id, i, s, ITEM_PENDING) for i, s in enumerate(run.symbols, 1)])

    def find_run(self, run_id: str) -> dict | None:
        """A run by its id or a unique id prefix."""
        with self.connection() as conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in conn.execute(f'SELECT * FROM "{RUN_TABLE}" WHERE "RunId" LIKE ? LIMIT 2',
                                                  ((run_id or "").strip() + "%",))]
        return rows[0] if len(rows) == 1 else None

    # -- the symbols ------------------------------------------------------------------------
    def market_symbols(self, market: str, types: Iterable[str] | None = None, refresh_list: bool = False) -> list[str]:
        """The codes of the market's symbol list (Stock_Symbol_Stage), of *types* (default
        ``[eod_api] market_types``; empty = every type) - fetched from the API first when the
        market has no list yet or *refresh_list* is set."""
        from .eod_symbols import SymbolListLoader
        market = validate_market(market)
        types = list(self.settings.eod_market_types if types is None else types)
        loader = SymbolListLoader(self.settings, self.db, audit=self.audit)
        if refresh_list or loader.count(market) == 0:
            self.refresh_symbol_list(market)
        table = _q(self.settings.eod_symbol_stage_table)
        where, params = ['"Market" = ?'], [market]
        if types:
            where.append(f'LOWER(COALESCE("Type", \'\')) IN ({",".join("?" * len(types))})')
            params += [t.lower() for t in types]
        with self.db.connection() as conn:
            loader.ensure_table(conn)
            return [r[0] for r in conn.execute(f'SELECT "Code" FROM {table} WHERE {" AND ".join(where)} '
                                               f'ORDER BY "Code"', params)]

    def refresh_symbol_list(self, market: str) -> None:
        """Request the market's symbol list from the exchange-symbol-list API and load it straight
        into Stock_Symbol_Stage (no file) - retried like the price calls."""
        from .eod_symbols import SymbolListLoader
        url = self.client.symbol_list_url_for(market, "json", api_token=self.api_token)
        response, attempts, error = self._get_with_retries(url, f"the symbol list of {market}")
        if error is not None:
            raise EodMarketError(f"the symbol list of {market} could not be fetched after {attempts} attempt(s): "
                                 f"{error}")
        result = SymbolListLoader(self.settings, self.db, audit=self.audit).load_text(
            market, response.body.decode("utf-8-sig", errors="replace"), "json",
            source_file=f"{self._source()}/{market}/symbol-list")
        if not result.ok:
            raise EodMarketError(f"the symbol list of {market} could not be loaded: {result.message}")
        log.info("%s", result)

    # -- one stock ---------------------------------------------------------------------------
    def _source(self) -> str:
        return SOURCE_FAKE if self.fake else SOURCE_LIVE

    def _backoff(self, attempt: int, error: EodApiError) -> float:
        if error.retry_after is not None:
            return min(float(error.retry_after), MAX_BACKOFF_SECONDS)
        return min(self.settings.eod_api_retry_backoff * (2 ** (attempt - 1)), MAX_BACKOFF_SECONDS)

    def _get_with_retries(self, url: str, what: str, retries: int | None = None):
        """GET *url*; a retryable failure is repeated up to *retries* times.  Returns
        ``(response | None, attempts, EodApiError | None)``; a fatal status (401/402/403) is raised."""
        retries = self.settings.eod_api_retries if retries is None else retries
        attempt, last = 0, None
        while attempt <= retries:
            attempt += 1
            self._calls += 1
            try:
                response = self.client.transport.get(url, float(self.settings.eod_api_timeout))
                if not 200 <= response.status < 300:
                    raise EodApiError(f"the API answered {response.status}", response.status)
                body = response.body or b""
                try:
                    json.loads(body.decode("utf-8-sig", errors="replace"))
                except ValueError:
                    snippet = body[:120].decode("utf-8", errors="replace").strip()
                    raise EodApiError(f"the API did not answer with valid JSON: {redact(snippet)!r}") from None
                return response, attempt, None
            except EodApiError as exc:
                last = exc
                if exc.status in FATAL_STATUSES:
                    raise
                if exc.status not in RETRYABLE_STATUSES or attempt > retries:
                    break
                wait = self._backoff(attempt, exc)
                log.info("eod market: %s - attempt %d failed (%s), retrying in %.1fs", what, attempt, exc, wait)
                if wait > 0:
                    self.sleep(wait)
        return None, attempt, last

    def fetch_stock(self, symbol: str, params: dict | None = None, skip_bad_rows: bool | None = None,
                    retries: int | None = None, market: str = "") -> StockOutcome:
        """Call the API for one stock (with retries) and load the answer straight into the staging table."""
        from .eod_stage import EodStageLoader
        from .quality import DataErrorRepository
        outcome = StockOutcome(symbol)
        try:
            symbol = validate_symbol(symbol)
        except EodApiError as exc:
            outcome.status, outcome.error = ITEM_SKIPPED, str(exc)
            return outcome
        outcome.symbol = symbol
        ticker = ticker_of(symbol)
        if ticker not in self._stock_loads:
            self._stock_loads[ticker] = self.ledger.count(symbol)
        if self._stock_loads[ticker] >= self.ledger.limit:      # the daily limit: not even called
            outcome.status, outcome.limited = ITEM_SKIPPED, True
            outcome.error = skip_message(ticker, self._stock_loads[ticker], self.ledger.limit)
            return outcome
        url = self.client.url_for(symbol, "json", api_token=self.api_token, base_url=self.base_url, params=params)
        before = self._calls
        response, outcome.attempts, error = self._get_with_retries(url, symbol, retries)
        outcome.attempts = self._calls - before
        if error is not None:
            outcome.status, outcome.http_status, outcome.error = ITEM_FAILED, error.status, str(error)
            return outcome
        text = response.body.decode("utf-8-sig", errors="replace")
        if text.strip() in ("[]", "{}"):
            outcome.status = ITEM_NO_DATA
            return outcome
        loader = EodStageLoader(self.settings, self.db, audit=None, data_errors=DataErrorRepository(self.settings))
        result = loader.load_text(symbol, text, "json", source_file=f"{self._source()}/{market or '-'}/{symbol}",
                                  skip_bad_rows=skip_bad_rows)
        outcome.load_id, outcome.received = result.load_id, result.received
        outcome.rejected = result.rejected_records
        if result.refused:                          # another process loaded it meanwhile: the limit is reached
            self._stock_loads[ticker] = self.ledger.limit
            outcome.status, outcome.limited = ITEM_SKIPPED, True
            outcome.error = skip_message(ticker, self.ledger.limit, self.ledger.limit)
            return outcome
        if not result.ok:
            outcome.status, outcome.error = ITEM_FAILED, result.message
            return outcome
        outcome.status = ITEM_LOADED
        self._stock_loads[ticker] = self._stock_loads.get(ticker, 0) + 1
        outcome.inserted, outcome.updated, outcome.unchanged = result.inserted, result.updated, result.unchanged
        outcome.first_date, outcome.last_date = result.first_date, result.last_date
        return outcome

    # -- the whole market ----------------------------------------------------------------------
    def _check_fake_target(self) -> None:
        """The fake invents prices: never mix them into real API data by accident."""
        from .eod_stage import EodStageLoader
        loader = EodStageLoader(self.settings, self.db)
        with self.db.connection() as conn:
            loader.ensure_table(conn)
            real = conn.execute(f'SELECT COUNT(*) FROM {loader.table} WHERE "SourceFile" NOT LIKE ?',
                                (SOURCE_FAKE + "/%",)).fetchone()[0]
        if real:
            raise EodMarketError(
                f"the fake gateway invents prices, and {loader.table_name} holds {real:,} row(s) of real data that "
                f"it would overwrite - run the fake against a test database (--config test.ini) or allow it "
                f"explicitly (allow_fake_data / --allow-fake-data)")

    def fetch_market(self, market: str, symbols: Iterable[str] | None = None, types: Iterable[str] | None = None,
                     params: dict | None = None, limit: int | None = None, refresh_list: bool = False,
                     resume: bool | str = False, skip_bad_rows: bool | None = None, retries: int | None = None,
                     pause: float | None = None, allow_fake_data: bool = False,
                     progress: Callable[[int, int, StockOutcome], None] | None = None,
                     should_stop: Callable[[], bool] | None = None,
                     on_start: Callable[["MarketRun"], None] | None = None, source: str | None = None,
                     requested_by: str | None = None) -> MarketRun:
        """Fetch every stock of *market* (or just *symbols*) into the staging table - see the module
        docstring.  *resume*: True = the newest unfinished run of the market, or a run id.
        *should_stop* is asked before every stock: True ends the run as ``interrupted`` (resumable);
        *on_start* gets the run once the symbols are known.  Returns the run; its ``status`` says how it
        ended.

        *source*: ``market`` (default without *symbols*), ``symbols`` (default with them) or ``file``
        (the symbols of a text file).  The daily limits (:mod:`load_limits`) are checked before any API
        call: a whole-market load beyond :data:`MAX_MARKET_LOADS_PER_DAY` per market raises
        :class:`MarketLoadLimitError`; a text-file load with more than 100 symbols raises
        :class:`~stock_dataload.load_limits.TooManySymbolsError`, beyond 5 a day
        :class:`~stock_dataload.load_limits.FileLoadLimitError`; when every stock of the run was
        loaded 5 times today already :class:`~stock_dataload.load_limits.StockLoadLimitError` - otherwise
        the stocks at their limit are skipped.  *requested_by* is recorded with a refusal."""
        market = validate_market(market)
        self._calls = 0
        retries = self.settings.eod_api_retries if retries is None else int(retries)
        pause = self.settings.eod_request_pause if pause is None else float(pause)
        if retries < 0:
            raise EodMarketError("retries must be 0 or more")
        if self.fake and not allow_fake_data:
            self._check_fake_target()
        source = (source or ("symbols" if symbols is not None else "market")).strip().lower()
        if source not in ("market", "symbols", "file"):
            raise EodMarketError(f"unknown source '{source}' - market, symbols or file")
        if source == "market" and symbols is not None:
            raise EodMarketError("a whole-market load takes the market's symbol list - give no symbols")
        if source != "market" and symbols is None and not resume:
            raise EodMarketError(f"a '{source}' load needs the symbols")
        whole_market = not resume and symbols is None
        from_file = not resume and source == "file"
        self._stock_loads = {}
        if whole_market:                        # before the symbol list is requested: no API call when refused
            self.check_market_load_allowed(market, requested_by)
        run = self._resume(market, resume) if resume else None
        if run is None:
            if symbols is not None:
                wanted = list(dict.fromkeys(s.strip().upper() for s in symbols if s and s.strip()))
            else:
                wanted = self.market_symbols(market, types, refresh_list)
            if limit:
                wanted = wanted[: int(limit)]
            if not wanted:
                raise EodMarketError(f"no symbols to fetch for {market} - load its symbol list first "
                                     f"(eod-symbols-fetch {market}) or check [eod_api] market_types")
            if from_file:
                try:
                    check_file_symbols(wanted)
                except LoadLimitError as exc:
                    audit_refusal(self.audit, COMPONENT, exc, symbols=len(wanted), requested_by=requested_by)
                    raise
                self.check_file_load_allowed(requested_by)
            self.check_stocks_allowed(wanted, requested_by)     # all at their limit: refused, nothing registered
            parameters = {"types": list(self.settings.eod_market_types if types is None else types),
                          "params": params or {}, "retries": retries, "limit": limit,
                          "symbols_given": symbols is not None, "source": source}
            run = MarketRun(uuid.uuid4().hex[:12], market, "fake" if self.fake else "live", wanted, parameters)
            run.outcomes = {s: StockOutcome(s) for s in wanted}
            if whole_market or from_file:
                self._register_limited_run(run)         # the daily limit, checked again atomically
            else:
                with self.connection() as conn:
                    self._insert_run(conn, run)
        else:
            self.check_stocks_allowed([s for s, o in run.outcomes.items() if o.status == ITEM_PENDING], requested_by)
        params = run.parameters.get("params") or params or {}
        if self.audit is not None:
            self.audit.log(COMPONENT, "market_run_started", f"{market}: {len(run.symbols):,} symbol(s), run {run.run_id}"
                           + (f" (resumed, {run.resumed_from:,} done before)" if run.resumed_from else ""),
                           details={"run": run.run_id, "market": market, "gateway": run.gateway,
                                    "symbols": len(run.symbols), "retries": retries})
        log.info("eod market: %s - %d symbol(s), run %s", market, len(run.symbols), run.run_id)
        failed_in_row = 0
        todo = [(i, s) for i, s in enumerate(run.symbols, 1) if run.outcomes[s].status == ITEM_PENDING]
        if on_start is not None:
            on_start(run)
        try:
            for n, (position, symbol) in enumerate(todo):
                if should_stop is not None and should_stop():
                    run.status, run.message = RUN_INTERRUPTED, "stopped by the user - resume it to continue"
                    break
                if n and pause > 0:
                    self.sleep(pause)
                try:
                    outcome = self.fetch_stock(symbol, params, skip_bad_rows, retries, market)
                except EodApiError as exc:                          # 401 / 402 / 403: stop everything
                    outcome = StockOutcome(symbol, ITEM_PENDING, 1, exc.status, error=str(exc))
                    self._save_item(run, position, outcome)
                    run.api_calls = run.calls_before + self._calls
                    run.status, run.message = RUN_ABORTED, f"stopped at {symbol}: {exc}"
                    break
                outcome.symbol = symbol
                run.outcomes[symbol] = outcome
                run.api_calls = run.calls_before + self._calls
                self._save_item(run, position, outcome)
                if progress is not None:
                    progress(run.resumed_from + n + 1, len(run.symbols), outcome)
                failed_in_row = failed_in_row + 1 if outcome.status == ITEM_FAILED else 0
                if failed_in_row >= self.settings.eod_max_consecutive_failures:
                    run.status = RUN_ABORTED
                    run.message = (f"{failed_in_row} stocks failed in a row (the last: {outcome.error}) - the API "
                                   f"seems to be unavailable; continue later with --resume")
                    break
                if (n + 1) % 50 == 0:
                    self._save_run(run)                             # progress visible in the registry
        except KeyboardInterrupt:
            run.status, run.message = RUN_INTERRUPTED, "interrupted - continue with --resume"
            run.finished_at = _now()
            self._save_run(run)
            self._log_end(run)
            raise
        if run.status == RUN_RUNNING:
            run.status = RUN_COMPLETED_ERRORS if run.failed else RUN_COMPLETED
        if run.status == RUN_INTERRUPTED:
            run.api_calls = run.calls_before + self._calls
        run.finished_at = _now()
        self._save_run(run)
        self._log_end(run)
        return run

    def _resume(self, market: str, resume: bool | str) -> MarketRun | None:
        """The run to continue: *resume* = a run id, or True = the newest unfinished run of *market*."""
        if isinstance(resume, str):
            row = self.find_run(resume)
            if row is None:
                raise EodMarketError(f"no (unique) run '{resume}'")
        else:
            found = [r for r in self.runs(market, limit=50) if r["Status"] != RUN_COMPLETED
                     and '"source": "bulk"' not in (r["Parameters"] or "")]
            if not found:
                raise EodMarketError(f"no unfinished run of {market} to resume - start a new one without --resume")
            row = found[0]
        if row["Market"] != market:
            raise EodMarketError(f"run {row['RunId']} is a run of {row['Market']}, not {market}")
        try:
            source = json.loads(row["Parameters"] or "{}").get("source")
        except ValueError:
            source = None
        if source == "bulk":
            raise EodMarketError(f"run {row['RunId']} is a one-day bulk load (eod-bulk-last-day) - it is one API call "
                                 f"and cannot be resumed; run eod-bulk-fetch again")
        items = self.run_items(row["RunId"])
        run = MarketRun(row["RunId"], market, row["Gateway"], [i["Symbol"] for i in items],
                        json.loads(row["Parameters"] or "{}"), status=RUN_RUNNING, started_at=row["StartedAt"],
                        api_calls=int(row["ApiCalls"] or 0), calls_before=int(row["ApiCalls"] or 0))
        for i in items:
            o = StockOutcome(i["Symbol"], i["Status"], int(i["Attempts"]), i["HttpStatus"], int(i["Received"]),
                             int(i["Inserted"]), int(i["Updated"]), int(i["Unchanged"]), int(i["Rejected"]),
                             i["FirstDate"], i["LastDate"], i["LoadId"], i["Error"])
            o.limited = o.status == ITEM_SKIPPED and (o.error or "").startswith("daily limit")
            if o.status == ITEM_FAILED or o.limited:        # a resume tries the failed / limited stocks again
                o.status, o.limited = ITEM_PENDING, False
            run.outcomes[o.symbol] = o
        run.resumed_from = sum(1 for o in run.outcomes.values() if o.status != ITEM_PENDING)
        self._calls = 0
        return run

    def _log_end(self, run: MarketRun) -> None:
        log.info("eod market: %s", run.summary())
        if self.audit is None:
            return
        level = "INFO" if run.ok else "WARNING"
        self.audit.log(COMPONENT, "market_run_finished", run.summary(), level=level,
                       details={"run": run.run_id, "market": run.market, "status": run.status, "loaded": run.loaded,
                                "failed": run.failed, "rows": run.rows, "api_calls": run.api_calls})
        for o in run.failures()[:200]:
            self.audit.log(COMPONENT, "market_stock_failed", str(o), level="WARNING",
                           details={"run": run.run_id, "symbol": o.symbol, "status": o.http_status,
                                    "attempts": o.attempts})
        limited = run.limited()
        if limited:
            names = [o.symbol for o in limited]
            self.audit.log(COMPONENT, "stock_load_skipped",
                           f"run {run.run_id}: {len(names):,} stock(s) skipped - already loaded "
                           f"{MAX_LOADS_PER_STOCK_PER_DAY} times today: {', '.join(names[:20])}"
                           + (f" and {len(names) - 20} more" if len(names) > 20 else ""), level="WARNING",
                           details={"run": run.run_id, "market": run.market, "symbols": names[:200],
                                    "limit": MAX_LOADS_PER_STOCK_PER_DAY})


def fetch_market(market: str, settings: Settings | None = None, **kwargs) -> MarketRun:
    """One call: ``fetch_market("NYSE")`` fetches the end-of-day prices of every stock of NYSE
    (the types of ``[eod_api] market_types``) and loads them into the staging table.  Keyword
    arguments: see :meth:`MarketFetcher.fetch_market` (``symbols``, ``types``, ``params``, ``limit``,
    ``resume``, ``retries`` ...); ``fake=True`` / ``fake_faults=...`` use the built-in stub."""
    fetcher = MarketFetcher(settings, audit=kwargs.pop("audit", None), fake=kwargs.pop("fake", None),
                            fake_faults=kwargs.pop("fake_faults", None), api_token=kwargs.pop("api_token", None),
                            base_url=kwargs.pop("base_url", None))
    return fetcher.fetch_market(market, **kwargs)
