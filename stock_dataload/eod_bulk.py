"""
**One trading day of every stock of a market** in one API call - the EODHD *bulk last day* endpoint -
loaded into the same staging table as every other EOD load (``Stock_EOD_Stage``).

    python -m stock_dataload eod-bulk-fetch US --date 2026-10-12          # call, save, load
    python -m stock_dataload eod-bulk-fetch US --date 2026-10-12 --fake   # the built-in stub (no network)
    python -m stock_dataload eod-bulk-load docs/SampleAPI_BulkLastDay_US.json --market US

    from stock_dataload.eod_bulk import fetch_bulk_last_day
    result = fetch_bulk_last_day("US", api_token="...", fmt="json", date="2026-10-12")
    print(result)                     # US 2026-10-12: 5,641 record(s) ... loaded into Stock_EOD_Stage ...

The request (``[eod_api] bulk_url``, ``bulk_fmt``; the token as for every EOD call)::

    <bulk_url><MARKET>?api_token=<token>&fmt=<json|csv>[&date=YYYY-MM-DD][&symbols=A,B]
    https://eodhd.com/api/eod-bulk-last-day/US?api_token=***&fmt=json&date=2026-10-12

Without ``date`` the API answers its last trading day.  The answer - JSON::

    [{"code": "MSFT", "exchange_short_name": "US", "date": "2026-08-18", "open": 481.54, "high": 484.27,
      "low": 477.15, "close": 481.63, "adjusted_close": 481.63, "volume": 24059700}, ...]

or CSV (``Code,Ex,Date,Open,High,Low,Close,Adjusted_close,Volume``).  Every record becomes one row of
``Stock_EOD_Stage`` with the symbol ``<code>.<exchange_short_name>`` (``MSFT.US``: ``Ticker`` MSFT,
``Exchange`` US) - the notation of the single-stock endpoint, so ``promote eod`` treats both alike
(one daily row per ticker and day, the newest staging row wins).  Rows are upserted per symbol and day
in ONE transaction; the whole answer has one ``LoadId`` (``promote eod --load-id``).

Rules and checks
----------------
* the **date** must be a real date, not in the future, and - unless ``force`` - a trading day of the
  market (weekends; US holidays for US markets): a bulk call costs many API credits and a closed day
  has no data, so such a request is refused *before* the call (:class:`EodBulkError`);
* the bulk endpoint is called **at most 3 times for the same market and the same trade date**
  (:data:`~stock_dataload.load_limits.MAX_BULK_CALLS_PER_MARKET_DAY`) - every call counts, whatever it
  loaded; a request without a date counts for the latest trading day until the answer tells its day.
  Refused with :class:`~stock_dataload.load_limits.BulkCallLimitError` before the call, checked again
  atomically when the run is registered;
* a bulk load of a whole market **counts as a whole-market load** - at most 3 a day per market
  (``eod_market.MAX_MARKET_LOADS_PER_DAY``), checked before the call and again atomically when the
  run is registered (``Eod_Market_Run``, ``Parameters.source = "bulk"``); with ``symbols`` it is no
  whole-market load;
* every stock is loaded at most **5 times a day** (:mod:`load_limits`): the records of stocks at their
  limit are **skipped** (a warning, ``App_Log`` ``stock_load_skipped``); every loaded stock is counted;
* invalid records (no / bad code, a field missing or not a number, a stock twice) are **rejected** into
  ``Data_Error`` (rule ``eod_record``) and the valid ones loaded - or, with ``skip_bad_rows=False``,
  nothing is loaded;
* transient API problems (network, timeout, 429, 5xx, an unreadable answer) are retried like the other
  EOD calls (``[eod_api] retries`` / ``retry_backoff``); 401 / 402 / 403 are not.

``[eod_api] gateway = fake`` / ``fake=True`` use the built-in stub
(:meth:`~stock_dataload.eod_api.FakeEodGateway._bulk_last_day`): for ``US`` / ``NYSE`` every symbol of
the NYSE sample list with invented prices (the same as its single-stock calls), ``[]`` for days without
data and other markets; ``fake_faults="US:503x2"`` simulates failures.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .config import Settings, load_settings
from .db import HISTORY_FAILED, HISTORY_LOADED, StageDatabase, now_iso, quote_identifier as _q
from .eod_api import (GATEWAY_FAKE, EodApiClient, EodApiError, FakeEodGateway, UrllibGetter, build_url, redact,
                      validate_market, validate_symbol)
from .eod_stage import RULE, EOD_STAGE_COLUMNS, EodRecord, EodStageLoader, RejectedRecord, detect_format, split_symbol, \
    validate_records
from .load_limits import (BulkCallLimitError, BulkCallQuota, audit_refusal, bulk_calls, record_many,
                          stock_refusal)

log = logging.getLogger(__name__)

COMPONENT = "eod_bulk"
SOURCE_BULK = "bulk"

BULK_LOADED = "loaded"          # loaded (rejected records / skipped stocks, if any, are reported)
BULK_NO_DATA = "no_data"        # the API answered an empty list
BULK_SAVED = "saved"            # saved only (load=False)
BULK_REFUSED = "refused"        # every stock of the answer reached its daily limit - nothing loaded
BULK_FAILED = "failed"          # the answer could not be read / strict mode and invalid records

#: market codes that trade on the US calendar (weekends + US exchange holidays)
US_MARKETS = ("US", "NYSE", "NASDAQ", "NYSE ARCA", "NYSE MKT", "AMEX", "BATS", "OTC")

#: CSV header names -> the JSON field names
CSV_FIELDS = {"code": "code", "symbol": "code", "ex": "exchange_short_name", "exchange": "exchange_short_name",
              "exchange_short_name": "exchange_short_name", "date": "date", "open": "open", "high": "high",
              "low": "low", "close": "close", "adjusted_close": "adjusted_close", "adj_close": "adjusted_close",
              "volume": "volume"}
REQUIRED_FIELDS = ("code", "date", "open", "high", "low", "close", "adjusted_close", "volume")

_VALUE_COLUMNS = ("Open", "High", "Low", "Close", "AdjustedClose", "Volume")
#: HTTP statuses worth another try (None = no answer)
_RETRYABLE = (None, 408, 425, 429, 500, 502, 503, 504)


class EodBulkError(EodApiError):
    """The bulk request is not possible (bad market / date / symbols, a closed day, no token ...) -
    nothing was requested."""


# ---------------------------------------------------------------------------
# the day
# ---------------------------------------------------------------------------
def closed_reason(market: str, day: _date, extra_holidays: Iterable[str] = ()) -> str | None:
    """Why *market* does not trade on *day* (weekend, US exchange holiday for US markets, an extra
    closure) - None if it trades."""
    from .market_calendar import us_market_holiday_names
    if day.weekday() >= 5:
        return f"a {day.strftime('%A')}"
    if day.isoformat() in set(extra_holidays):
        return "an extra closure ([quality] extra_holidays)"
    if market.upper() in US_MARKETS:
        name = us_market_holiday_names(day.year).get(day)
        if name:
            return name
    return None


def _today() -> _date:
    return _date.today()


def check_trade_day(market: str, day: str | _date | None, force: bool = False, today: _date | None = None,
                    extra_holidays: Iterable[str] = ()) -> str | None:
    """The trade date to ask for as ``YYYY-MM-DD`` (None = the API's last day).  Raises
    :class:`EodBulkError` for a date that is not one, lies in the future, or - unless *force* - is a
    day the market does not trade."""
    if day in (None, ""):
        return None
    if isinstance(day, _date):
        value = day
    else:
        text = str(day).strip()
        try:
            if len(text) == 8 and text.isdigit():
                text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
            value = _date.fromisoformat(text)
        except ValueError:
            raise EodBulkError(f"'{day}' is not a date like 2026-10-12") from None
    today = today or _today()
    if value > today:
        raise EodBulkError(f"{value.isoformat()} is in the future - there is no end-of-day data for it yet")
    if not force:
        reason = closed_reason(market, value, extra_holidays)
        if reason:
            raise EodBulkError(f"{market} does not trade on {value.isoformat()} ({reason}) - the API has no data for "
                               f"that day, so nothing was requested (force the call with --force / force=True)")
    return value.isoformat()


def last_trading_day(market: str, today: _date | None = None, extra_holidays: Iterable[str] = ()) -> str:
    """The latest day up to *today* that *market* trades (weekends / holidays skipped)."""
    day = today or _today()
    for _ in range(15):
        if closed_reason(market, day, extra_holidays) is None:
            return day.isoformat()
        day = _date.fromordinal(day.toordinal() - 1)
    return day.isoformat()


def previous_trading_day(market: str, today: _date | None = None, extra_holidays: Iterable[str] = ()) -> str:
    """The trading day of *market* before *today*."""
    day = today or _today()
    return last_trading_day(market, _date.fromordinal(day.toordinal() - 1), extra_holidays)


def clean_symbols(symbols: Iterable[str] | str | None) -> list[str]:
    """``"msft, HD.US"`` -> ``["MSFT", "HD.US"]`` (unique, validated)."""
    if symbols is None:
        return []
    items = symbols.replace(";", ",").split(",") if isinstance(symbols, str) else list(symbols)
    out: list[str] = []
    for item in items:
        if not str(item).strip():
            continue
        try:
            symbol = validate_symbol(str(item))
        except EodApiError as exc:
            raise EodBulkError(str(exc)) from None
        if symbol not in out:
            out.append(symbol)
    return out


def promotion_market(market: str) -> str | None:
    """The market ``promote eod`` gives the rows of a bulk load: none for ``US`` (all US exchanges - the
    symbol table / the daily table / ``[eod_api] default_market`` decide per stock), else the code."""
    return None if market.upper() == "US" else market.upper()


# ---------------------------------------------------------------------------
# reading the answer
# ---------------------------------------------------------------------------
def parse_bulk_response(text: str, fmt: str | None = None) -> tuple[list[Any], int]:
    """The raw records of a bulk answer and the number of the first one (JSON: 1, CSV: line 2).

    JSON: a list of objects (or ``{"data": [...]}``); CSV: a header with the field names (``Code,Ex,
    Date,Open,High,Low,Close,Adjusted_close,Volume``, any case).  Raises ``ValueError`` otherwise."""
    fmt = detect_format(text, fmt)
    if fmt == "json":
        data = json.loads(text)
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            data = data["data"]
        if not isinstance(data, list):
            detail = ""
            if isinstance(data, dict):
                detail = " - the API said: " + str(data.get("message") or data.get("error") or data.get("errors")
                                                    or data)[:200]
            raise ValueError("the answer is not a list of end-of-day records" + detail)
        return data, 1
    reader = csv.reader(io.StringIO(text))
    try:
        header = [CSV_FIELDS.get((h or "").strip().lower(), (h or "").strip().lower()) for h in next(reader)]
    except StopIteration:
        return [], 2
    missing = [f for f in REQUIRED_FIELDS if f not in header]
    if missing:
        raise ValueError(f"the CSV header {header} lacks {missing}")
    rows = [dict(zip(header, r)) for r in reader if any(c.strip() for c in r)]
    return rows, 2


def validate_bulk(raw: list[Any], market: str, first_number: int = 1
                  ) -> tuple[dict[str, list[EodRecord]], list[tuple[str, RejectedRecord]]]:
    """``({symbol: [records]}, [(symbol, rejected)])`` - the symbol is ``CODE.EXCHANGE`` (the record's
    ``exchange_short_name``, else *market*); a stock with a day twice is rejected the second time."""
    groups: dict[str, list[EodRecord]] = {}
    rejected: list[tuple[str, RejectedRecord]] = []
    seen: set[tuple[str, str]] = set()
    for i, item in enumerate(raw):
        number = first_number + i
        if not isinstance(item, Mapping):
            rejected.append(("?", RejectedRecord(number, "", "", str(item)[:60], "the record is not an object")))
            continue
        rec = {str(k).strip().lower(): v for k, v in item.items()}
        rec = {CSV_FIELDS.get(k, k): v for k, v in rec.items()}
        code = str(rec.get("code") or "").strip()
        trade_date = str(rec.get("date") or "").strip()
        try:
            code = validate_symbol(code)
        except EodApiError:
            rejected.append((code or "?", RejectedRecord(number, trade_date, "code", code,
                                                         "code is missing or not a stock symbol")))
            continue
        exchange = str(rec.get("exchange_short_name") or "").strip().upper()
        symbol = f"{code}.{exchange if exchange.isalnum() else market}"
        good, bad = validate_records([rec], number)
        if bad:
            rejected.extend((symbol, r) for r in bad)
            continue
        record = good[0]
        if (symbol, record.trade_date) in seen:
            rejected.append((symbol, RejectedRecord(number, record.trade_date, "code", code,
                                                    f"{symbol} appears more than once for {record.trade_date}")))
            continue
        seen.add((symbol, record.trade_date))
        groups.setdefault(symbol, []).append(record)
    return groups, rejected


# ---------------------------------------------------------------------------
# the result
# ---------------------------------------------------------------------------
@dataclass
class BulkLoadResult:
    market: str
    day: str | None                               # the trade date asked for (None = the API's last day)
    url: str = ""                                 # the request, token redacted
    source_file: str = ""
    path: Path | None = None                      # the saved answer
    load_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    run_id: str | None = None
    status: str = "pending"
    api_calls: int = 0
    received: int = 0                             # records in the answer
    symbols: int = 0                              # stocks with valid records
    loaded: list[str] = field(default_factory=list)          # the symbols loaded (MSFT.US ...)
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    rejected: list[tuple[str, RejectedRecord]] = field(default_factory=list)
    limited: dict[str, int] = field(default_factory=dict)    # tickers skipped: loads today
    dates: list[str] = field(default_factory=list)           # the trade dates of the answer
    message: str = ""
    table: str = "Stock_EOD_Stage"

    @property
    def ok(self) -> bool:
        return self.status in (BULK_LOADED, BULK_NO_DATA, BULK_SAVED)

    @property
    def rows_loaded(self) -> int:
        return self.inserted + self.updated + self.unchanged

    @property
    def rejected_records(self) -> int:
        return len({(s, r.number) for s, r in self.rejected})

    @property
    def tickers(self) -> list[str]:
        return sorted({split_symbol(s)[0] for s in self.loaded})

    @property
    def other_dates(self) -> list[str]:
        """Trade dates of the answer other than the one asked for."""
        return [d for d in self.dates if self.day and d != self.day]

    def warnings(self) -> list[str]:
        out = []
        if self.status == BULK_NO_DATA:
            out.append(f"the API has no data for {self.market} on {self.day or 'its last day'} (an empty answer)")
        if self.limited:
            names = sorted(self.limited)
            out.append(f"{len(names):,} stock(s) skipped - already loaded the maximum number of times today (daily "
                       f"limit per stock): {', '.join(names[:20])}" + (" ..." if len(names) > 20 else ""))
        if self.rejected and self.status == BULK_LOADED:
            out.append(f"{self.rejected_records:,} invalid record(s) rejected - see data-errors --file {self.source_file}")
        if self.other_dates:
            out.append(f"the answer also has record(s) dated {', '.join(self.other_dates[:5])} (asked for {self.day})")
        return out

    def __str__(self) -> str:
        shown = self.day or (self.dates[0] if len(self.dates) == 1 else "(last day)")
        what = f"{self.market} {shown}"
        if self.status == BULK_FAILED:
            return f"{what}: NOT loaded into {self.table} - {self.message}"
        if self.status == BULK_REFUSED:
            return f"{what}: NOT loaded into {self.table} (refused) - {self.message}"
        if self.status == BULK_NO_DATA:
            return f"{what}: no data (the API answered an empty list)" + (f" - run {self.run_id}" if self.run_id else "")
        if self.status == BULK_SAVED:
            return f"{what}: {self.received:,} record(s) saved to {self.path} (not loaded)"
        text = (f"{what}: {self.received:,} record(s), {len(self.loaded):,} stock(s) loaded into {self.table} - "
                f"{self.inserted:,} inserted, {self.updated:,} updated, {self.unchanged:,} unchanged")
        if self.rejected:
            text += f", {self.rejected_records:,} rejected"
        if self.limited:
            text += f", {len(self.limited):,} skipped (daily limit)"
        return text + (f"; run {self.run_id}" if self.run_id else "")


# ---------------------------------------------------------------------------
# the loader
# ---------------------------------------------------------------------------
class BulkDayLoader:
    """Calls the bulk endpoint for a market and a day, saves the answer and loads it into the staging table."""

    def __init__(self, settings: Settings | None = None, client: EodApiClient | None = None, db: StageDatabase | None = None,
                 audit=None, data_errors=None, fake: bool | None = None, fake_faults=None,
                 sleep: Callable[[float], None] = time.sleep):
        from .eod_market import MarketFetcher
        self.settings = settings or load_settings()
        self.db = db or StageDatabase(self.settings)
        if client is None:
            use_fake = self.settings.eod_api_gateway == GATEWAY_FAKE if fake is None else fake
            transport = FakeEodGateway(vary_by_symbol=True, faults=fake_faults) if use_fake else UrllibGetter()
            client = EodApiClient(self.settings, transport=transport)
        self.client = client
        self.fake = isinstance(client.transport, FakeEodGateway)
        self.audit = audit
        if data_errors is None:
            from .quality import DataErrorRepository
            data_errors = DataErrorRepository(self.settings)
        self.data_errors = data_errors
        self.sleep = sleep
        self.stage = EodStageLoader(self.settings, self.db)
        self.registry = MarketFetcher(self.settings, client=client, db=self.db, audit=audit, sleep=sleep)

    # -- the request ----------------------------------------------------------------------
    def url_for(self, market: str, fmt: str | None = None, api_token: str | None = None, day: str | None = None,
                symbols: Iterable[str] | None = None, base_url: str | None = None) -> str:
        """``<bulk_url><MARKET>?api_token=..&fmt=..[&date=..][&symbols=..]`` - with the real token, do not log it."""
        fmt = (fmt or self.settings.eod_bulk_fmt or "json").strip().lower()
        if fmt not in ("json", "csv"):
            raise EodBulkError(f"fmt must be json or csv, not '{fmt}'")
        token = self.client.token(api_token)
        if not token:
            raise EodBulkError("no API token: set [eod_api] api_token in config.ini or $STOCK_DATALOAD_EOD_API_TOKEN")
        params = {"date": day, "symbols": ",".join(symbols) if symbols else None}
        return build_url(base_url or self.settings.eod_bulk_url, validate_market(market), token, fmt, params,
                         placeholder="market")

    def _get(self, url: str, fmt: str, retries: int | None):
        """GET with retries of transient failures; returns ``(response, attempts)`` or raises EodApiError."""
        retries = self.settings.eod_api_retries if retries is None else max(0, int(retries))
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self.client.transport.get(url, float(self.settings.eod_api_timeout))
                if not 200 <= response.status < 300:
                    raise EodApiError(f"the API answered {response.status}", response.status)
                if fmt == "json":
                    try:
                        json.loads((response.body or b"").decode("utf-8-sig", errors="replace"))
                    except ValueError:
                        snippet = (response.body or b"")[:120].decode("utf-8", errors="replace").strip()
                        raise EodApiError(f"the API did not answer with valid JSON: {redact(snippet)!r}") from None
                return response, attempt
            except EodApiError as exc:
                if exc.status not in _RETRYABLE or attempt > retries:
                    exc.attempts = attempt                                  # type: ignore[attr-defined]
                    raise
                wait = exc.retry_after if exc.retry_after is not None else \
                    self.settings.eod_api_retry_backoff * (2 ** (attempt - 1))
                log.info("eod bulk: attempt %d failed (%s), retrying in %.1fs", attempt, exc, wait)
                if wait > 0:
                    self.sleep(min(float(wait), 120.0))

    # -- the limit of calls per market and trade date -------------------------------------------
    def trade_date_of(self, market: str, day: str | None) -> str:
        """The trade date a call counts for: the date asked for, else the latest trading day."""
        return day or last_trading_day(validate_market(market), extra_holidays=self.settings.extra_market_holidays)

    def bulk_call_quota(self, market: str, day: str | None = None) -> BulkCallQuota:
        """The calls of the bulk endpoint for *market* and the trade date *day* (default: the latest
        trading day) and how many are left (:data:`~stock_dataload.load_limits.MAX_BULK_CALLS_PER_MARKET_DAY`)."""
        market = validate_market(market)
        trade_date = self.trade_date_of(market, day)
        with self.registry.connection() as conn:
            return BulkCallQuota(market, trade_date, bulk_calls(conn, market, trade_date))

    def check_bulk_call_allowed(self, market: str, day: str | None = None,
                                requested_by: str | None = None) -> BulkCallQuota:
        """Raise :class:`~stock_dataload.load_limits.BulkCallLimitError` (recorded in App_Log) when the
        endpoint was called 3 times for the market and trade date already."""
        quota = self.bulk_call_quota(market, day)
        if quota.exhausted:
            error = BulkCallLimitError(quota)
            audit_refusal(self.audit, COMPONENT, error, **quota.as_dict(), requested_by=requested_by)
            raise error
        return quota

    # -- the run registry ---------------------------------------------------------------------
    def _register(self, result: BulkLoadResult, fmt: str, symbols: list[str], gateway: str,
                  file_name: str | None = None, trade_date: str | None = None) -> None:
        """Register the run - the daily limits checked again in the same transaction (write lock first):
        a whole-market load (no *symbols*) against the 3 a day per market, an API call (*trade_date*)
        against the 3 calls per market and trade date."""
        from .eod_market import MarketLoadLimitError, MarketLoadQuota, MarketRun
        parameters = {"source": SOURCE_BULK, "date": result.day, "fmt": fmt, "symbols_given": bool(symbols),
                      "symbols": symbols[:50], "params": {}, "file": file_name, "trade_date": trade_date}
        run = MarketRun(uuid.uuid4().hex[:12], result.market, gateway, [], parameters)
        registry = self.registry
        with registry.connection() as conn:
            if conn.in_transaction:
                conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            if not symbols:
                quota = MarketLoadQuota(run.market, run.started_at[:10],
                                        registry._market_loads(conn, run.market, run.started_at[:10]))
                if quota.exhausted:
                    conn.rollback()
                    registry._refused(quota)
                    raise MarketLoadLimitError(quota)
            if trade_date:
                calls = BulkCallQuota(run.market, trade_date, bulk_calls(conn, run.market, trade_date))
                if calls.exhausted:
                    conn.rollback()
                    error = BulkCallLimitError(calls)
                    audit_refusal(self.audit, COMPONENT, error, **calls.as_dict())
                    raise error
            registry._insert_run(conn, run)
        result.run_id = run.run_id

    def _set_trade_date(self, result: BulkLoadResult) -> None:
        """The answer tells the day it is for: a call without a date counts for that day."""
        from .eod_market import RUN_TABLE
        if result.run_id is None or len(result.dates) != 1:
            return
        with self.registry.connection() as conn:
            row = conn.execute(f'SELECT "Parameters" FROM "{RUN_TABLE}" WHERE "RunId" = ?', (result.run_id,)).fetchone()
            if row is None:
                return
            parameters = json.loads(row[0] or "{}")
            if parameters.get("trade_date") and parameters["trade_date"] != result.dates[0]:
                parameters["trade_date"] = result.dates[0]
                conn.execute(f'UPDATE "{RUN_TABLE}" SET "Parameters" = ? WHERE "RunId" = ?',
                             (json.dumps(parameters, sort_keys=True), result.run_id))

    def _finish_run(self, result: BulkLoadResult) -> None:
        from .eod_market import RUN_ABORTED, RUN_COMPLETED, RUN_COMPLETED_ERRORS, RUN_TABLE
        if result.run_id is None:
            return
        if result.status == BULK_FAILED:
            status = RUN_ABORTED
        elif result.status == BULK_REFUSED or result.rejected:
            status = RUN_COMPLETED_ERRORS
        else:
            status = RUN_COMPLETED
        message = "; ".join([result.message] if not result.ok else result.warnings())
        with self.registry.connection() as conn:
            conn.execute(
                f'UPDATE "{RUN_TABLE}" SET "Status" = ?, "Symbols" = ?, "Loaded" = ?, "NoData" = ?, "Failed" = ?, '
                f'"Skipped" = ?, "Rows" = ?, "ApiCalls" = ?, "Message" = ?, "FinishedAt" = ? WHERE "RunId" = ?',
                (status, result.symbols, len(result.loaded), 1 if result.status == BULK_NO_DATA else 0,
                 len({s for s, _ in result.rejected}), len(result.limited), result.rows_loaded, result.api_calls,
                 message or None, now_iso(), result.run_id))

    # -- calling the API ------------------------------------------------------------------------
    def fetch(self, market: str = "US", api_token: str | None = None, fmt: str | None = None,
              date: str | _date | None = None, symbols: Iterable[str] | str | None = None, base_url: str | None = None,
              save: bool = True, output_dir: str | Path | None = None, load: bool = True,
              skip_bad_rows: bool | None = None, force: bool = False, retries: int | None = None,
              allow_fake_data: bool = False, requested_by: str | None = None,
              progress: Callable[[str, str], None] | None = None) -> BulkLoadResult:
        """Call ``eod-bulk-last-day`` for *market* and *date*, save the answer (``[eod_api] download_dir``,
        ``bulk_file_name``) and load it into the staging table.  Returns the result (``status``:
        loaded / no_data / saved / refused / failed - see ``warnings()``).  Raises :class:`EodBulkError`
        for an impossible request, a ``LoadLimitError`` when a daily limit refuses it (both before the
        call) and :class:`EodApiError` when the API call fails (the run is registered as aborted)."""
        from .load_limits import LoadLimitError
        say = progress or (lambda level, text: None)
        market = validate_market(market)
        fmt = (fmt or self.settings.eod_bulk_fmt or "json").strip().lower()
        day = check_trade_day(market, date, force, extra_holidays=self.settings.extra_market_holidays)
        wanted = clean_symbols(symbols)
        result = BulkLoadResult(market, day, table=self.stage.table_name)
        url = self.url_for(market, fmt, api_token, day, wanted, base_url)
        result.url = redact(url)
        if load and self.fake and not allow_fake_data:
            self.registry._check_fake_target()
        trade_date = self.trade_date_of(market, day)
        try:
            self.check_bulk_call_allowed(market, trade_date, requested_by)     # 3 calls per market and day
            if not wanted:
                self.registry.check_market_load_allowed(market, requested_by)
            elif load:
                self.registry.check_stocks_allowed(wanted, requested_by)
        except LoadLimitError:
            raise
        self._register(result, fmt, wanted, "fake" if self.fake else "live", trade_date=trade_date)
        if self.audit is not None:
            self.audit.log(COMPONENT, "bulk_started", f"{market} {day or '(last day)'}: GET {result.url} - run "
                           f"{result.run_id}", details={"market": market, "date": day, "run": result.run_id,
                                                         "symbols": wanted[:50], "requested_by": requested_by})
        say("info", f"GET {result.url}")
        try:
            response, result.api_calls = self._get(url, fmt, retries)
        except EodApiError as exc:
            result.api_calls = getattr(exc, "attempts", 1)
            result.status, result.message = BULK_FAILED, f"the API call failed: {exc}"
            self._finish_run(result)
            log.warning("eod bulk: %s", result)
            if self.audit is not None:
                self.audit.log(COMPONENT, "bulk_failed", str(result), level="WARNING",
                               details={"market": market, "date": day, "run": result.run_id, "status": exc.status})
            raise
        body = response.body or b""
        say("info", f"answer received: {len(body):,} bytes after {result.api_calls} call(s)")
        if save:
            result.path = self._save(body, market, day, fmt, output_dir)
            # invented prices are marked, so they are never taken for real data (see MarketFetcher)
            result.source_file = ("fake-api/" if self.fake else "") + result.path.name
            say("info", f"saved to {result.path}")
        else:
            result.source_file = f"{'fake-api' if self.fake else 'api'}/bulk/{market}/{day or 'last'}"
        text = body.decode("utf-8-sig", errors="replace")
        if not load:
            try:
                result.received = len(parse_bulk_response(text, fmt)[0])
            except ValueError:
                result.received = 0
            result.status = BULK_SAVED
        else:
            self._load(result, text, fmt, skip_bad_rows, hashlib.sha256(body).hexdigest(), len(body))
        if not day:
            self._set_trade_date(result)
        self._finish_run(result)
        self._report(result, say)
        return result

    def _save(self, body: bytes, market: str, day: str | None, fmt: str, output_dir) -> Path:
        path = self.client._target(self.settings.eod_bulk_file_name, "bulk_file_name",
                                   {"market": market, "day": day or "last"}, fmt, output_dir, None)
        part = path.with_name(path.name + ".part")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            part.write_bytes(body)
            part.replace(path)
        except OSError as exc:
            part.unlink(missing_ok=True)
            raise EodBulkError(f"could not save the answer to {path}: {exc}") from None
        return path

    # -- a saved answer ----------------------------------------------------------------------------
    def load_file(self, path: str | Path, market: str = "US", date: str | None = None, fmt: str | None = None,
                  skip_bad_rows: bool | None = None, requested_by: str | None = None) -> BulkLoadResult:
        """Load a saved bulk answer (JSON or CSV).  It is a bulk load of the market: it counts as a
        whole-market load (3 a day) and every stock against its 5 loads a day."""
        path = Path(path)
        if not path.is_file():
            raise EodBulkError(f"file not found: {path}")
        market = validate_market(market)
        day = check_trade_day(market, date, force=True) if date else None
        raw = path.read_bytes()
        fmt = detect_format(raw[:200].decode("utf-8-sig", errors="replace"), fmt or path.suffix)
        result = BulkLoadResult(market, day, source_file=path.name, path=path, table=self.stage.table_name)
        self.registry.check_market_load_allowed(market, requested_by)
        self._register(result, fmt, [], "file", path.name)
        self._load(result, raw.decode("utf-8-sig", errors="replace"), fmt, skip_bad_rows,
                   hashlib.sha256(raw).hexdigest(), len(raw))
        self._finish_run(result)
        self._report(result, lambda level, text: None)
        return result

    # -- loading ---------------------------------------------------------------------------------
    def _load(self, result: BulkLoadResult, text: str, fmt: str, skip_bad_rows: bool | None, file_hash: str,
              file_size: int) -> None:
        skip = True if skip_bad_rows is None else bool(skip_bad_rows)
        try:
            raw, first = parse_bulk_response(text, fmt)
        except ValueError as exc:
            self._fail(result, f"the {fmt.upper()} answer cannot be read: {exc}", file_hash, file_size)
            return
        result.received = len(raw)
        if not raw:
            result.status = BULK_NO_DATA
            return
        groups, rejected = validate_bulk(raw, result.market, first)
        result.rejected = rejected
        result.symbols = len(groups)
        result.dates = sorted({r.trade_date for recs in groups.values() for r in recs})
        if rejected and not skip:
            self._fail(result, f"{result.rejected_records:,} of {len(raw):,} record(s) are invalid (first: "
                               f"{rejected[0][0]} {rejected[0][1]}) - nothing was loaded; fix the data or load "
                               f"without --strict", file_hash, file_size)
            return
        table = _q(self.stage.table_name)
        ledger = self.stage.ledger
        now = now_iso()
        try:
            with self.db.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")                     # the whole answer is one unit
                self.stage.ensure_table(conn)
                tickers = {split_symbol(s)[0] for s in groups}
                counts = ledger.counts(tickers, conn=conn)          # the daily limit per stock, atomically
                result.limited = {t: n for t, n in counts.items() if n >= ledger.limit}
                existing: dict[tuple[str, str], tuple] = {}
                for day in result.dates:
                    for row in conn.execute(f'SELECT "Symbol", "TradeDate", {", ".join(_q(c) for c in _VALUE_COLUMNS)} '
                                            f'FROM {table} WHERE "TradeDate" = ?', (day,)):
                        existing[(row[0], row[1])] = tuple(row[2:])
                inserts, updates, ledger_rows = [], [], []
                for symbol in sorted(groups):
                    ticker, exchange = split_symbol(symbol)
                    if ticker in result.limited:
                        continue
                    for rec in groups[symbol]:
                        old = existing.get((symbol, rec.trade_date))
                        if old is None:
                            inserts.append((symbol, ticker, exchange, rec.trade_date, *rec.values(), result.source_file,
                                            result.load_id, now, now))
                        elif tuple(old) != rec.values():
                            updates.append((*rec.values(), result.source_file, result.load_id, now, symbol,
                                            rec.trade_date))
                        else:
                            result.unchanged += 1
                    ledger_rows.append((symbol, ticker, result.source_file, len(groups[symbol])))
                    result.loaded.append(symbol)
                cols = ", ".join(_q(c) for c in EOD_STAGE_COLUMNS)
                conn.executemany(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' * len(EOD_STAGE_COLUMNS))})",
                                 inserts)
                sets = ", ".join(f"{_q(c)} = ?" for c in (*_VALUE_COLUMNS, "SourceFile", "LoadId", "UpdatedAt"))
                conn.executemany(f'UPDATE {table} SET {sets} WHERE "Symbol" = ? AND "TradeDate" = ?', updates)
                result.inserted, result.updated = len(inserts), len(updates)
                if ledger_rows:
                    record_many(conn, ledger_rows, result.load_id, now)
                if not result.loaded:
                    result.status = BULK_REFUSED
                    result.message = "nothing loaded: " + stock_refusal(result.limited, ledger.limit)
                else:
                    result.status = BULK_LOADED
                    result.message = str(result)
                self.db.record_load_history(result.source_file, file_hash, result.load_id,
                                            HISTORY_LOADED if result.loaded else HISTORY_FAILED,
                                            rows_loaded=result.rows_loaded, market=result.market,
                                            message=result.message, file_size=file_size, conn=conn)
        except Exception as exc:  # noqa: BLE001 - sqlite3 errors etc.: recorded, reported
            result.loaded, result.inserted, result.updated, result.unchanged = [], 0, 0, 0
            self._fail(result, f"database problem: {exc}", file_hash, file_size)
            return
        self._record_rejected(result)

    def _fail(self, result: BulkLoadResult, message: str, file_hash: str, file_size: int | None) -> None:
        result.status, result.message = BULK_FAILED, message
        try:
            self.db.record_load_history(result.source_file, file_hash, result.load_id, HISTORY_FAILED,
                                        market=result.market, message=f"{self.stage.table_name}: {message}",
                                        file_size=file_size)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not register the failed bulk load in Load_History: %s", exc)
        self._record_rejected(result)

    def _record_rejected(self, result: BulkLoadResult) -> None:
        if not result.rejected or self.data_errors is None:
            return
        from .quality import QualityIssue
        suffix = "" if result.status == BULK_LOADED else " (answer not loaded)"
        issues = [QualityIssue(RULE, f"{self.stage.table_name}: {symbol}: {r.message}{suffix}", ticker=symbol,
                               trade_date=r.trade_date, column=r.column, value=r.value, line_no=r.number)
                  for symbol, r in result.rejected]
        try:
            self.data_errors.record(issues, source_file=result.source_file, load_id=result.load_id,
                                    market=result.market)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not record the rejected records in Data_Error: %s", exc)

    def _report(self, result: BulkLoadResult, say: Callable[[str, str], None]) -> None:
        log.info("eod bulk: %s", result)
        level = "ok" if result.status == BULK_LOADED and not result.warnings() else \
            "error" if result.status in (BULK_FAILED, BULK_REFUSED) else "warning"
        say(level, str(result))
        for warning in result.warnings():
            say("warning", warning)
        if self.audit is None:
            return
        try:
            self.audit.log(COMPONENT, "bulk_loaded" if result.ok else "bulk_not_loaded", str(result),
                           level="INFO" if level == "ok" else "WARNING", file_name=result.source_file,
                           load_id=result.load_id,
                           details={"market": result.market, "date": result.day, "run": result.run_id,
                                    "received": result.received, "loaded": len(result.loaded),
                                    "inserted": result.inserted, "updated": result.updated,
                                    "unchanged": result.unchanged, "rejected": result.rejected_records,
                                    "skipped": len(result.limited)})
            if result.limited:
                names = sorted(result.limited)
                self.audit.log(COMPONENT, "stock_load_skipped", f"run {result.run_id}: {len(names):,} stock(s) of "
                               f"the bulk answer skipped - already loaded the maximum number of times today: "
                               f"{', '.join(names[:20])}" + (" ..." if len(names) > 20 else ""), level="WARNING",
                               details={"run": result.run_id, "market": result.market, "symbols": names[:200]})
        except Exception:  # noqa: BLE001 - the audit trail must not break the load
            log.warning("could not write the audit entries of the bulk load")

    # -- afterwards -----------------------------------------------------------------------------------
    def promote(self, result: BulkLoadResult, aggregates: bool = False, audit=None) -> list:
        """``promote eod`` for exactly this load (its LoadId), optionally weekly / monthly afterwards."""
        from .promotion import PromotionService
        if not result.loaded:
            return []
        service = PromotionService(self.settings, audit=audit or self.audit)
        out = [service.promote_eod_daily(load_id=result.load_id, market=promotion_market(result.market))]
        if aggregates:
            out += [service.promote_weekly(), service.promote_monthly()]
        return out


def fetch_bulk_last_day(market: str = "US", api_token: str | None = None, fmt: str = "json",
                        date: str | _date | None = None, settings: Settings | None = None, **kwargs) -> BulkLoadResult:
    """**One trading day of every stock of a market**, loaded into the staging table ``Stock_EOD_Stage``::

        fetch_bulk_last_day("US", api_token="...", fmt="json", date="2026-10-12")

    *market* the exchange code (``US`` = every US exchange), *api_token* (default
    ``$STOCK_DATALOAD_EOD_API_TOKEN`` / ``[eod_api] api_token``), *fmt* ``json`` or ``csv``, *date*
    ``YYYY-MM-DD`` (None = the API's last trading day).  Further keyword arguments: ``symbols``,
    ``save``, ``output_dir``, ``load``, ``skip_bad_rows``, ``force``, ``retries``, ``base_url``,
    ``requested_by``; ``fake=True`` / ``fake_faults="US:503x2"`` use the built-in stub, ``audit`` an
    AuditLogger.  See :meth:`BulkDayLoader.fetch`."""
    loader = BulkDayLoader(settings, audit=kwargs.pop("audit", None), fake=kwargs.pop("fake", None),
                           fake_faults=kwargs.pop("fake_faults", None))
    return loader.fetch(market, api_token=api_token, fmt=fmt, date=date, **kwargs)
