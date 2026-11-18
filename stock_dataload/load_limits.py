"""
Daily limits of the EOD API loads (business rules - deliberately constants, not settings):

=====================================  ==========================================================
:data:`MAX_LOADS_PER_STOCK_PER_DAY` 5  a stock (ticker: ``MRK`` and ``MRK.US`` are the same stock)
                                       is loaded into ``Stock_EOD_Stage`` at most 5 times a day -
                                       by ``eod-fetch``, ``eod-load``, ``eod-fetch-market`` and the
                                       dashboard's API loading page alike
:data:`MAX_FILE_SYMBOLS` 100           a text file of stock symbols (API loading page *From a
                                       text file*, ``eod-fetch-market --symbols-file``) lists at
                                       most 100 different symbols
:data:`MAX_FILE_LOADS_PER_DAY` 5       loads from a text file run at most 5 times a day (all
                                       markets together)
``MAX_MARKET_LOADS_PER_DAY`` 3         a whole market at most 3 times a day (``eod_market.py``)
:data:`MAX_BULK_CALLS_PER_MARKET_DAY` 3  the EODHD bulk endpoint (``eod_bulk.py``) is called at most
                                       3 times for the same market and the same trade date - every
                                       call counts (also with ``symbols``, failed ones, save-only);
                                       retries within one call do not
=====================================  ==========================================================

"A day" is the local calendar date.  The per-stock count comes from the ledger
``Eod_Stock_Load``: one row per **successful** load of a stock into the staging table, written in
the same transaction as the rows themselves (a failed or refused load does not count).  The
text-file count comes from the run registry (``Eod_Market_Run``, ``Parameters.source = "file"``).

Where it is enforced (the later checks catch what the earlier ones cannot see, e.g. two processes
at the same time):

1. the dashboard page (warns, blocks the start button) and ``ApiLoadManager.start`` (refuses
   before a job is created);
2. ``MarketFetcher.fetch_market`` / ``EodApiClient.fetch``: before any API call - a stock at its
   limit is not called (a run *skips* it with a warning; a single fetch raises
   :class:`StockLoadLimitError`); a run whose stocks are all at their limit is refused;
3. ``EodStageLoader``: inside the load transaction - the count is checked again and the load
   registered atomically; a load over the limit is *refused* (``EodLoadResult.status = refused``).

Every refusal is written to ``App_Log`` (WARNING) by the component that refused it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from .eod_api import EodApiError

log = logging.getLogger(__name__)

#: successful loads of one stock into the EOD staging table per day
MAX_LOADS_PER_STOCK_PER_DAY = 5
#: different stock symbols one text file may list
MAX_FILE_SYMBOLS = 100
#: loads from a text file per day (all markets together)
MAX_FILE_LOADS_PER_DAY = 5
#: calls of the bulk endpoint (eod-bulk-last-day) for the same market and the same trade date
MAX_BULK_CALLS_PER_MARKET_DAY = 3

STOCK_LOAD_TABLE = "Eod_Stock_Load"

STOCK_LOAD_DDL = f"""
CREATE TABLE IF NOT EXISTS "{STOCK_LOAD_TABLE}" (
    "StockLoadId"  INTEGER PRIMARY KEY AUTOINCREMENT,
    "Ticker"       TEXT NOT NULL,              -- the stock: MRK (also for MRK.US)
    "Symbol"       TEXT NOT NULL,              -- as loaded: MRK, MRK.US
    "LoadId"       TEXT NOT NULL,              -- the load (Stock_EOD_Stage.LoadId)
    "SourceFile"   TEXT,                       -- api/NYSE/MRK, MRK_20251114_101500.json ...
    "Records"      INTEGER NOT NULL DEFAULT 0, -- rows loaded (inserted + updated + unchanged)
    "LoadedAt"     TEXT NOT NULL               -- local time; the day counts against the limit
)
"""
STOCK_LOAD_INDEX = (f'CREATE INDEX IF NOT EXISTS "IX_{STOCK_LOAD_TABLE}_Ticker_LoadedAt" '
                    f'ON "{STOCK_LOAD_TABLE}" ("Ticker", "LoadedAt")')


def render_ddl() -> str:
    """The DDL of the ledger (``sql/create_eod_stock_load_sqlite.sql``)."""
    return "\n".join(s.strip() + ";" for s in (STOCK_LOAD_DDL, STOCK_LOAD_INDEX)) + "\n"


def today() -> str:
    return datetime.now().date().isoformat()


def ticker_of(symbol: str) -> str:
    """``mrk.us`` -> ``MRK`` - the stock a symbol stands for (the exchange suffix dropped)."""
    text = (symbol or "").strip().upper()
    base, dot, exchange = text.rpartition(".")
    return base if dot and base and exchange.isalnum() else text


def _times(started: Iterable[str]) -> str:
    return ", ".join(t[11:16] for t in started if t and len(t) >= 16)


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------
class LoadLimitError(EodApiError):
    """A daily limit refuses the load (nothing was called or loaded).  An :class:`EodApiError`, so
    code that reports API problems reports it too - but it is never retried."""

    event = "load_refused"


class StockLoadLimitError(LoadLimitError):
    """The stock(s) were loaded :data:`MAX_LOADS_PER_STOCK_PER_DAY` times today already."""

    event = "stock_load_refused"

    def __init__(self, message: str, tickers: Iterable[str] = ()):
        super().__init__(message)
        self.tickers = list(tickers)


class FileLoadLimitError(LoadLimitError):
    """Text-file loads ran :data:`MAX_FILE_LOADS_PER_DAY` times today already."""

    event = "file_load_refused"

    def __init__(self, quota: "FileLoadQuota"):
        super().__init__(quota.refusal())
        self.quota = quota


class BulkCallLimitError(LoadLimitError):
    """The bulk endpoint was called :data:`MAX_BULK_CALLS_PER_MARKET_DAY` times for the market and trade date."""

    event = "bulk_call_refused"

    def __init__(self, quota: "BulkCallQuota"):
        super().__init__(quota.refusal())
        self.quota = quota


class TooManySymbolsError(LoadLimitError):
    """A text file lists more than :data:`MAX_FILE_SYMBOLS` stock symbols."""

    event = "file_too_many_symbols"

    def __init__(self, count: int, name: str = "the file", limit: int | None = None):
        self.count, self.name = count, name
        self.limit = MAX_FILE_SYMBOLS if limit is None else limit
        super().__init__(f"{name} lists {count:,} stock symbols - a text file may list at most {self.limit} "
                         f"(split it into several files; each load counts against the {MAX_FILE_LOADS_PER_DAY} "
                         f"text-file loads a day)")


def check_file_symbols(symbols: Iterable[str], name: str = "the file") -> None:
    """Raise :class:`TooManySymbolsError` when *symbols* (already unique) are more than 100."""
    count = len(list(symbols))
    if count > MAX_FILE_SYMBOLS:
        raise TooManySymbolsError(count, name)


def stock_refusal(counts: dict[str, int], limit: int | None = None) -> str:
    limit = MAX_LOADS_PER_STOCK_PER_DAY if limit is None else limit
    names = sorted(counts)
    shown = ", ".join(names[:20]) + (f" and {len(names) - 20} more" if len(names) > 20 else "")
    if len(names) == 1:
        return (f"{names[0]} was already loaded {counts[names[0]]} times today - a stock may be loaded at most "
                f"{limit} times a day. Try again tomorrow.")
    return (f"{shown} were already loaded {limit} times today - a stock may be loaded at most {limit} times a day. "
            f"Try again tomorrow.")


def skip_message(ticker: str, used: int, limit: int | None = None) -> str:
    """The error text of a stock a run skips because of the limit."""
    limit = MAX_LOADS_PER_STOCK_PER_DAY if limit is None else limit
    return f"daily limit: {ticker} was already loaded {used} times today (at most {limit} a day) - not called"


# ---------------------------------------------------------------------------
# quotas
# ---------------------------------------------------------------------------
@dataclass
class FileLoadQuota:
    """The text-file loads of one day (all markets)."""

    day: str
    started: list[str] = field(default_factory=list)
    limit: int = MAX_FILE_LOADS_PER_DAY

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
        times = _times(self.started)
        return (f"loading from a text file may run at most {self.limit} times a day - it already ran {self.used} "
                f"time(s) today{f' (at {times})' if times else ''}. Try again tomorrow, or resume an unfinished run.")

    def as_dict(self) -> dict:
        return {"day": self.day, "limit": self.limit, "used": self.used, "remaining": self.remaining,
                "exhausted": self.exhausted, "started": list(self.started), "max_symbols": MAX_FILE_SYMBOLS}


@dataclass
class BulkCallQuota:
    """The calls of the bulk endpoint for one market and one trade date (whenever they were made)."""

    market: str
    trade_date: str
    started: list[str] = field(default_factory=list)        # StartedAt of the calls
    limit: int = MAX_BULK_CALLS_PER_MARKET_DAY

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
        when = ", ".join(t[:16] for t in self.started if t)
        return (f"{self.market} {self.trade_date}: the bulk endpoint may be called at most {self.limit} times for the "
                f"same market and day - it was already called {self.used} time(s) for it"
                f"{f' (at {when})' if when else ''}. Nothing was requested; the data of those calls is in the "
                f"staging table.")

    def as_dict(self) -> dict:
        return {"market": self.market, "trade_date": self.trade_date, "limit": self.limit, "used": self.used,
                "remaining": self.remaining, "exhausted": self.exhausted, "started": list(self.started)}


def bulk_calls(conn: sqlite3.Connection, market: str, trade_date: str, run_table: str = "Eod_Market_Run") -> list[str]:
    """StartedAt of the calls of the bulk endpoint for *market* and *trade_date* (runs with ``source = bulk``
    that called the API - not the loads of a saved answer)."""
    try:
        rows = conn.execute(f'SELECT "StartedAt", "Parameters" FROM "{run_table}" WHERE "Market" = ? AND '
                            f'"Gateway" <> \'file\' AND "Parameters" LIKE \'%"source": "bulk"%\' ORDER BY "StartedAt"',
                            (market,)).fetchall()
    except sqlite3.OperationalError:                 # no run registry yet
        return []
    out = []
    for at, parameters in rows:
        try:
            p = json.loads(parameters or "{}")
        except ValueError:
            continue
        if p.get("source") == "bulk" and (p.get("trade_date") or p.get("date")) == trade_date:
            out.append(at)
    return out


def file_loads(conn: sqlite3.Connection, day: str, run_table: str = "Eod_Market_Run") -> list[str]:
    """StartedAt of the runs of *day* that loaded the stocks of a text file."""
    try:
        rows = conn.execute(f'SELECT "StartedAt", "Parameters" FROM "{run_table}" WHERE substr("StartedAt", 1, 10) = ? '
                            f'ORDER BY "StartedAt"', (day,)).fetchall()
    except sqlite3.OperationalError:                 # no run registry yet
        return []
    out = []
    for at, parameters in rows:
        try:
            source = json.loads(parameters or "{}").get("source")
        except (ValueError, AttributeError):
            source = None
        if source == "file":
            out.append(at)
    return out


class StockLoadLedger:
    """``Eod_Stock_Load``: the successful loads per stock and day."""

    def __init__(self, db, limit: int | None = None):
        self.db = db
        self._limit = limit

    @property
    def limit(self) -> int:
        return MAX_LOADS_PER_STOCK_PER_DAY if self._limit is None else self._limit

    @staticmethod
    def ensure_table(conn: sqlite3.Connection) -> None:
        conn.execute(STOCK_LOAD_DDL)
        conn.execute(STOCK_LOAD_INDEX)

    def counts(self, tickers: Iterable[str] | None = None, day: str | None = None,
               conn: sqlite3.Connection | None = None) -> dict[str, int]:
        """Loads of *day* (default today) per ticker - of *tickers*, or of every ticker loaded that day."""
        if conn is None:
            with self.db.connection() as c:
                return self.counts(tickers, day, c)
        self.ensure_table(conn)
        day = day or today()
        wanted = sorted({ticker_of(t) for t in tickers}) if tickers is not None else None
        sql = (f'SELECT "Ticker", COUNT(*) FROM "{STOCK_LOAD_TABLE}" WHERE "LoadedAt" >= ? AND "LoadedAt" < ?')
        params: list = [day, day + "~"]                # "~" sorts after any time of the day
        out: dict[str, int] = {}
        if wanted is None:
            for ticker, n in conn.execute(sql + ' GROUP BY "Ticker"', params):
                out[ticker] = int(n)
            return out
        for i in range(0, len(wanted), 500):          # SQLite's parameter limit
            chunk = wanted[i:i + 500]
            for ticker, n in conn.execute(sql + f' AND "Ticker" IN ({",".join("?" * len(chunk))}) GROUP BY "Ticker"',
                                          params + chunk):
                out[ticker] = int(n)
        return {t: out.get(t, 0) for t in wanted}

    def count(self, symbol: str, conn: sqlite3.Connection | None = None) -> int:
        return self.counts([symbol], conn=conn)[ticker_of(symbol)]

    def exhausted(self, tickers: Iterable[str] | None = None, day: str | None = None,
                  conn: sqlite3.Connection | None = None) -> dict[str, int]:
        """The tickers (of *tickers*, or all) that reached the limit on *day*: ``{ticker: loads}``."""
        return {t: n for t, n in self.counts(tickers, day, conn).items() if n >= self.limit}

    def check(self, symbols: Iterable[str]) -> None:
        """Raise :class:`StockLoadLimitError` if any of *symbols* reached the limit today."""
        over = self.exhausted(symbols)
        if over:
            raise StockLoadLimitError(stock_refusal(over, self.limit), over)

    def record(self, conn: sqlite3.Connection, symbol: str, load_id: str, source_file: str | None, records: int,
               loaded_at: str) -> None:
        """Register a successful load - inside the load's transaction."""
        self.ensure_table(conn)
        conn.execute(f'INSERT INTO "{STOCK_LOAD_TABLE}" ("Ticker","Symbol","LoadId","SourceFile","Records","LoadedAt") '
                     f'VALUES (?,?,?,?,?,?)', (ticker_of(symbol), symbol, load_id, source_file, int(records), loaded_at))


def record_many(conn: sqlite3.Connection, rows: Iterable[tuple[str, str, str | None, int]], load_id: str,
                loaded_at: str) -> None:
    """Register many successful loads of ONE load (a bulk answer): ``(symbol, ticker, source_file, records)``."""
    StockLoadLedger.ensure_table(conn)
    conn.executemany(f'INSERT INTO "{STOCK_LOAD_TABLE}" ("Ticker","Symbol","LoadId","SourceFile","Records","LoadedAt") '
                     f'VALUES (?,?,?,?,?,?)',
                     [(ticker, symbol, load_id, source, int(n), loaded_at) for symbol, ticker, source, n in rows])


def audit_refusal(audit, component: str, error: LoadLimitError, **details) -> None:
    """Write a refusal to App_Log (WARNING) - never raises."""
    log.warning("%s: %s", component, error)
    if audit is None:
        return
    try:
        audit.log(component, error.event, str(error), level="WARNING",
                  details={k: v for k, v in details.items() if v is not None})
    except Exception:  # noqa: BLE001 - the audit trail must not hide the refusal
        log.warning("could not record the refusal in App_Log")
