"""
**Daily schedules** of the one-day bulk load (*One day, all stocks* - :mod:`eod_bulk`).

A schedule says: load exchange ``US`` **every trading day at 18:30** (server local time), the data of the
same trading day (or of the previous one).  It runs **once per day**, only on days the exchange trades
(weekends / US holidays for US exchanges / ``[quality] extra_holidays`` are skipped), at or after its
time - a dashboard that was down at 18:30 catches up when it runs again the same day (never for earlier
days).

Table ``Eod_Bulk_Schedule`` - one schedule per exchange::

    ScheduleId, Market (unique), RunTime (HH:MM), TradeDay (same | previous), Fmt (json | csv),
    Symbols (optional, comma separated), Promote, Aggregates, Strict, Fake, AllowFakeData, Enabled,
    CreatedBy, CreatedAt, UpdatedBy, UpdatedAt,
    LastRunDate (the day it last ran - the once-per-day guard), LastRunAt, LastTradeDate, LastStatus,
    LastMessage, LastRunId

Who runs it
-----------
:class:`BulkScheduler` - a thread the dashboard starts (``python -m stock_dataload dashboard``;
``--no-scheduler`` leaves it off) that checks the schedules every 30 seconds; or
``python -m stock_dataload eod-bulk-schedule run-due`` from cron / the Windows task scheduler
(``--loop`` keeps checking).  A run is **claimed** in the database before it starts (``LastRunDate``
set in one statement), so two processes never run the same schedule twice a day.

A scheduled run is an ordinary load of the API loading page (``ApiLoadManager``, kind ``bulk``): the
live progress, the run registry, promotion and **every daily limit** apply - 3 calls of the bulk
endpoint per market and trade date, 3 whole-market loads per market and day, 5 loads per stock.  A
refused or failed run is recorded (``LastStatus`` / ``LastMessage``, ``App_Log``) and not repeated that
day; a run that finds another load running waits for the next check.  A live schedule needs a usable
API token - with the placeholder ``dummy`` it is refused without calling the API.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from typing import Callable, Iterator

from .config import ENV_EOD_API_TOKEN, Settings

log = logging.getLogger(__name__)

COMPONENT = "api_load_schedule"
SCHEDULE_TABLE = "Eod_Bulk_Schedule"

TRADE_DAY_SAME = "same"            # the data of the day the schedule runs (run it after the close)
TRADE_DAY_PREVIOUS = "previous"    # the data of the trading day before
TRADE_DAYS = {TRADE_DAY_SAME: "the same trading day (run after the close)",
              TRADE_DAY_PREVIOUS: "the previous trading day"}

STATUS_RUNNING = "running"
STATUS_LOADED = "loaded"
STATUS_WARNINGS = "loaded with warnings"
STATUS_FAILED = "failed"
STATUS_REFUSED = "refused"

#: seconds between two checks of the scheduler thread
CHECK_SECONDS = 30

SCHEDULE_DDL = f"""
CREATE TABLE IF NOT EXISTS "{SCHEDULE_TABLE}" (
    "ScheduleId"    INTEGER PRIMARY KEY AUTOINCREMENT,
    "Market"        TEXT NOT NULL UNIQUE,       -- the exchange code of the bulk endpoint: US, LSE ...
    "RunTime"       TEXT NOT NULL,              -- HH:MM, server local time
    "TradeDay"      TEXT NOT NULL DEFAULT 'same',   -- same | previous
    "Fmt"           TEXT NOT NULL DEFAULT 'json',   -- json | csv
    "Symbols"       TEXT,                       -- only these stocks (comma separated) - empty = every stock
    "Promote"       INTEGER NOT NULL DEFAULT 1,
    "Aggregates"    INTEGER NOT NULL DEFAULT 0,
    "Strict"        INTEGER NOT NULL DEFAULT 0,
    "Fake"          INTEGER NOT NULL DEFAULT 0, -- the built-in stub instead of the API
    "AllowFakeData" INTEGER NOT NULL DEFAULT 0,
    "Enabled"       INTEGER NOT NULL DEFAULT 1,
    "CreatedBy"     TEXT,
    "CreatedAt"     TEXT NOT NULL,
    "UpdatedBy"     TEXT,
    "UpdatedAt"     TEXT,
    "LastRunDate"   TEXT,                       -- the day it last ran: once per day
    "LastRunAt"     TEXT,
    "LastTradeDate" TEXT,
    "LastStatus"    TEXT,                       -- running | loaded | loaded with warnings | failed | refused
    "LastMessage"   TEXT,
    "LastRunId"     TEXT                        -- Eod_Market_Run
)
"""

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?$")


def render_ddl() -> str:
    """The DDL (``sql/create_eod_bulk_schedule_sqlite.sql``)."""
    return SCHEDULE_DDL.strip() + ";\n"


class ScheduleError(ValueError):
    """An invalid schedule (market, time, trade day, format, symbols) or an unknown one."""


def validate_run_time(text: str | None) -> str:
    """``"6:30"`` -> ``"06:30"`` (24-hour clock)."""
    m = _TIME_RE.match((text or "").strip())
    if not m:
        raise ScheduleError(f"'{text}' is not a time like 18:30 (24-hour clock)")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


@dataclass
class BulkSchedule:
    schedule_id: int
    market: str
    run_time: str
    trade_day: str
    fmt: str
    symbols: str | None
    promote: bool
    aggregates: bool
    strict: bool
    fake: bool
    allow_fake_data: bool
    enabled: bool
    created_by: str | None
    created_at: str
    updated_by: str | None
    updated_at: str | None
    last_run_date: str | None
    last_run_at: str | None
    last_trade_date: str | None
    last_status: str | None
    last_message: str | None
    last_run_id: str | None

    @property
    def symbol_list(self) -> list[str]:
        return [s for s in (self.symbols or "").split(",") if s]

    @property
    def at(self) -> dtime:
        hours, minutes = self.run_time.split(":")
        return dtime(int(hours), int(minutes))

    @property
    def trade_day_label(self) -> str:
        return TRADE_DAYS.get(self.trade_day, self.trade_day)

    def public(self) -> dict:
        return {"schedule_id": self.schedule_id, "market": self.market, "run_time": self.run_time,
                "trade_day": self.trade_day, "fmt": self.fmt, "symbols": self.symbol_list, "promote": self.promote,
                "aggregates": self.aggregates, "strict": self.strict, "fake": self.fake,
                "allow_fake_data": self.allow_fake_data, "enabled": self.enabled, "created_by": self.created_by,
                "created_at": self.created_at, "updated_by": self.updated_by, "updated_at": self.updated_at,
                "last_run_date": self.last_run_date, "last_run_at": self.last_run_at,
                "last_trade_date": self.last_trade_date, "last_status": self.last_status,
                "last_message": self.last_message, "last_run_id": self.last_run_id}


_COLS = ("ScheduleId", "Market", "RunTime", "TradeDay", "Fmt", "Symbols", "Promote", "Aggregates", "Strict", "Fake",
         "AllowFakeData", "Enabled", "CreatedBy", "CreatedAt", "UpdatedBy", "UpdatedAt", "LastRunDate", "LastRunAt",
         "LastTradeDate", "LastStatus", "LastMessage", "LastRunId")
_BOOLS = {6, 7, 8, 9, 10, 11}


def _row(r) -> BulkSchedule:
    return BulkSchedule(*[bool(v) if i in _BOOLS else v for i, v in enumerate(r)])


# ---------------------------------------------------------------------------
# the calendar of a schedule
# ---------------------------------------------------------------------------
def is_trading_day(market: str, day: date, extra_holidays=()) -> bool:
    from .eod_bulk import closed_reason
    return closed_reason(market, day, extra_holidays) is None


def trade_date_for(schedule: BulkSchedule, day: date, extra_holidays=()) -> str:
    """The trade date a run of *schedule* on *day* loads."""
    from .eod_bulk import previous_trading_day
    if schedule.trade_day == TRADE_DAY_PREVIOUS:
        return previous_trading_day(schedule.market, day, extra_holidays)
    return day.isoformat()


def next_run(schedule: BulkSchedule, now: datetime, extra_holidays=()) -> datetime | None:
    """When *schedule* runs next: today at its time (or now, if that time has passed and it has not run
    today - a catch-up), else its time on the next trading day.  None when disabled."""
    if not schedule.enabled:
        return None
    today = now.date()
    for offset in range(0, 15):
        day = today + timedelta(days=offset)
        if not is_trading_day(schedule.market, day, extra_holidays):
            continue
        if offset == 0 and schedule.last_run_date == today.isoformat():
            continue
        return datetime.combine(day, schedule.at)
    return None


def is_due(schedule: BulkSchedule, now: datetime, extra_holidays=()) -> bool:
    """Enabled, a trading day, its time reached and not run today."""
    return (schedule.enabled and schedule.last_run_date != now.date().isoformat()
            and is_trading_day(schedule.market, now.date(), extra_holidays) and now.time() >= schedule.at)


def token_problem(settings: Settings) -> str | None:
    """Why a live call cannot work (no token / the placeholder) - None when the token is usable."""
    token = (os.environ.get(ENV_EOD_API_TOKEN) or settings.eod_api_token or "").strip()
    if not token:
        return f"the EOD API token is not set - set ${ENV_EOD_API_TOKEN} or [eod_api] api_token"
    if token.lower() == "dummy":
        return (f"the EOD API token is the placeholder 'dummy' - the live API refuses it; set ${ENV_EOD_API_TOKEN} "
                f"(or use the built-in stub)")
    return None


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------
class ScheduleStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._ready = False

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        path = self.settings.sqlite_path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=30.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            if not self._ready:
                conn.execute(SCHEDULE_DDL)
                conn.commit()
                self._ready = True
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def ensure_table(self) -> None:
        with self.connection():
            pass

    def _select(self, where: str = "", params: tuple = ()) -> list[BulkSchedule]:
        cols = ", ".join(f'"{c}"' for c in _COLS)
        with self.connection() as conn:
            rows = conn.execute(f'SELECT {cols} FROM "{SCHEDULE_TABLE}"' + (f" WHERE {where}" if where else "")
                                + ' ORDER BY "RunTime", "Market"', params).fetchall()
        return [_row(r) for r in rows]

    def list(self) -> list[BulkSchedule]:
        return self._select()

    def get(self, schedule_id: int) -> BulkSchedule | None:
        rows = self._select('"ScheduleId" = ?', (int(schedule_id),))
        return rows[0] if rows else None

    def get_by_market(self, market: str) -> BulkSchedule | None:
        rows = self._select('"Market" = ?', ((market or "").strip().upper(),))
        return rows[0] if rows else None

    def _require(self, schedule_id: int) -> BulkSchedule:
        schedule = self.get(schedule_id)
        if schedule is None:
            raise ScheduleError(f"schedule {schedule_id} does not exist")
        return schedule

    def save(self, market: str, run_time: str, trade_day: str = TRADE_DAY_SAME, fmt: str = "json",
             symbols=None, promote: bool = True, aggregates: bool = False, strict: bool = False,
             fake: bool = False, allow_fake_data: bool = False, enabled: bool = True,
             user: str | None = None) -> BulkSchedule:
        """Create the schedule of *market* or change it (one per market)."""
        from .eod_api import EodApiError, validate_market
        from .eod_bulk import clean_symbols
        try:
            market = validate_market(market)
            symbol_list = clean_symbols(symbols)
        except EodApiError as exc:
            raise ScheduleError(str(exc)) from None
        run_time = validate_run_time(run_time)
        if trade_day not in TRADE_DAYS:
            raise ScheduleError(f"the trade day must be one of {', '.join(TRADE_DAYS)}")
        fmt = (fmt or "json").strip().lower()
        if fmt not in ("json", "csv"):
            raise ScheduleError("the format must be json or csv")
        now = _now()
        values = (run_time, trade_day, fmt, ",".join(symbol_list) or None, int(bool(promote)),
                  int(bool(aggregates)), int(bool(strict)), int(bool(fake)), int(bool(fake and allow_fake_data)),
                  int(bool(enabled)))
        with self.connection() as conn:
            existing = conn.execute(f'SELECT "ScheduleId" FROM "{SCHEDULE_TABLE}" WHERE "Market" = ?', (market,)).fetchone()
            if existing:
                conn.execute(f'UPDATE "{SCHEDULE_TABLE}" SET "RunTime" = ?, "TradeDay" = ?, "Fmt" = ?, "Symbols" = ?, '
                             f'"Promote" = ?, "Aggregates" = ?, "Strict" = ?, "Fake" = ?, "AllowFakeData" = ?, '
                             f'"Enabled" = ?, "UpdatedBy" = ?, "UpdatedAt" = ? WHERE "ScheduleId" = ?',
                             (*values, user, now, existing[0]))
                schedule_id = existing[0]
            else:
                cur = conn.execute(f'INSERT INTO "{SCHEDULE_TABLE}" ("Market","RunTime","TradeDay","Fmt","Symbols",'
                                   f'"Promote","Aggregates","Strict","Fake","AllowFakeData","Enabled","CreatedBy",'
                                   f'"CreatedAt") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', (market, *values, user, now))
                schedule_id = cur.lastrowid
        return self._require(schedule_id)

    def set_enabled(self, schedule_id: int, enabled: bool, user: str | None = None) -> BulkSchedule:
        self._require(schedule_id)
        with self.connection() as conn:
            conn.execute(f'UPDATE "{SCHEDULE_TABLE}" SET "Enabled" = ?, "UpdatedBy" = ?, "UpdatedAt" = ? '
                         f'WHERE "ScheduleId" = ?', (int(bool(enabled)), user, _now(), int(schedule_id)))
        return self._require(schedule_id)

    def delete(self, schedule_id: int) -> BulkSchedule:
        schedule = self._require(schedule_id)
        with self.connection() as conn:
            conn.execute(f'DELETE FROM "{SCHEDULE_TABLE}" WHERE "ScheduleId" = ?', (int(schedule_id),))
        return schedule

    def claim(self, schedule_id: int, day: str, trade_date: str) -> bool:
        """Mark today's run as taken - True for exactly one caller per schedule and day."""
        with self.connection() as conn:
            cur = conn.execute(
                f'UPDATE "{SCHEDULE_TABLE}" SET "LastRunDate" = ?, "LastRunAt" = ?, "LastTradeDate" = ?, '
                f'"LastStatus" = ?, "LastMessage" = NULL, "LastRunId" = NULL WHERE "ScheduleId" = ? AND "Enabled" = 1 '
                f'AND ("LastRunDate" IS NULL OR "LastRunDate" <> ?)',
                (day, _now(), trade_date, STATUS_RUNNING, int(schedule_id), day))
            return cur.rowcount == 1

    def record(self, schedule_id: int, status: str, message: str, run_id: str | None = None,
               trade_date: str | None = None, ran_at: str | None = None) -> None:
        """The outcome of a run (scheduled or 'Run now')."""
        with self.connection() as conn:
            conn.execute(f'UPDATE "{SCHEDULE_TABLE}" SET "LastStatus" = ?, "LastMessage" = ?, "LastRunId" = ?, '
                         f'"LastTradeDate" = COALESCE(?, "LastTradeDate"), "LastRunAt" = COALESCE(?, "LastRunAt") '
                         f'WHERE "ScheduleId" = ?', (status, (message or "")[:1000], run_id, trade_date, ran_at,
                                                     int(schedule_id)))


# ---------------------------------------------------------------------------
# the scheduler
# ---------------------------------------------------------------------------
@dataclass
class ScheduleOutcome:
    schedule_id: int
    market: str
    trade_date: str
    status: str
    message: str
    run_id: str | None = None

    def __str__(self) -> str:
        return f"schedule {self.schedule_id} ({self.market} {self.trade_date}): {self.status} - {self.message}"


class BulkScheduler:
    """Runs the due schedules.  :meth:`tick` checks once (tests, ``eod-bulk-schedule run-due``);
    :meth:`start` checks every :data:`CHECK_SECONDS` in a daemon thread (the dashboard)."""

    def __init__(self, settings: Callable[[], Settings] | Settings, api_loads=None,
                 audit_factory: Callable[[Settings], object] | None = None, interval: float = CHECK_SECONDS,
                 clock: Callable[[], datetime] | None = None):
        from .api_jobs import ApiLoadManager
        self._settings = settings if callable(settings) else (lambda: settings)
        self.api_loads = api_loads or ApiLoadManager(self._settings, audit_factory=audit_factory)
        self._audit_factory = audit_factory
        self.interval = interval
        self.clock = clock or (lambda: datetime.now())      # looked up when called (server local time)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_check: datetime | None = None
        #: the thread of the last 'Run now' (the dashboard starts it)
        self.worker: threading.Thread | None = None

    @property
    def settings(self) -> Settings:
        return self._settings()

    def store(self) -> ScheduleStore:
        return ScheduleStore(self.settings)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _audit(self):
        return self._audit_factory(self.settings) if self._audit_factory else None

    def _log(self, event: str, message: str, level: str = "INFO", **details) -> None:
        (log.warning if level == "WARNING" else log.info)("%s", message)
        audit = self._audit()
        if audit is not None:
            try:
                audit.log(COMPONENT, event, message, level=level, details=details)
            except Exception:  # noqa: BLE001 - the audit trail must not stop the scheduler
                log.warning("could not write the audit entry of the schedule")

    # -- checking -------------------------------------------------------------------------------
    def due(self, now: datetime | None = None) -> list[BulkSchedule]:
        now = now or self.clock()
        extra = self.settings.extra_market_holidays
        return [s for s in self.store().list() if is_due(s, now, extra)]

    def tick(self, now: datetime | None = None) -> list[ScheduleOutcome]:
        """Run every due schedule (one after the other).  A schedule that finds another load running
        waits for the next check; every other outcome - loaded, refused, failed - counts as today's run."""
        now = now or self.clock()
        self.last_check = now
        outcomes: list[ScheduleOutcome] = []
        for schedule in self.due(now):
            if self.api_loads.busy:
                log.info("schedule %s (%s) is due - another API load is running, next check", schedule.schedule_id,
                         schedule.market)
                break
            trade_date = trade_date_for(schedule, now.date(), self.settings.extra_market_holidays)
            if not self.store().claim(schedule.schedule_id, now.date().isoformat(), trade_date):
                continue                                 # another process took it
            outcomes.append(self.execute(schedule, trade_date, started_by=f"schedule {schedule.schedule_id} "
                                                                          f"({schedule.created_by or '-'})"))
        return outcomes

    def run_now(self, schedule: BulkSchedule, started_by: str, now: datetime | None = None) -> ScheduleOutcome:
        """'Run now': the schedule's load at once (the daily run still takes place)."""
        now = now or self.clock()
        trade_date = trade_date_for(schedule, now.date(), self.settings.extra_market_holidays)
        if schedule.trade_day == TRADE_DAY_SAME and not is_trading_day(schedule.market, now.date(),
                                                                        self.settings.extra_market_holidays):
            from .eod_bulk import last_trading_day
            trade_date = last_trading_day(schedule.market, now.date(), self.settings.extra_market_holidays)
        self.store().record(schedule.schedule_id, STATUS_RUNNING, f"run now by {started_by}", None, trade_date,
                            now.replace(microsecond=0).isoformat(sep=" "))
        return self.execute(schedule, trade_date, started_by=started_by, manual=True)

    def execute(self, schedule: BulkSchedule, trade_date: str, started_by: str, manual: bool = False) -> ScheduleOutcome:
        """Start the schedule's load (synchronously, through the API-load manager) and record the outcome."""
        from .api_jobs import JOB_FAILED, KIND_BULK, ApiLoadBusy
        from .eod_api import EodApiError
        what = "run now" if manual else "scheduled run"
        title = f"{schedule.market} {trade_date}: " + (f"{len(schedule.symbol_list):,} stock(s) (bulk, {what})"
                                                       if schedule.symbol_list else f"every stock (bulk, {what})")
        problem = None if schedule.fake else token_problem(self.settings)
        if self.settings.eod_api_gateway == "fake":
            problem = None
        if problem:
            outcome = ScheduleOutcome(schedule.schedule_id, schedule.market, trade_date, STATUS_REFUSED,
                                      f"not started - {problem}")
        else:
            try:
                job = self.api_loads.start(KIND_BULK, schedule.market, title, started_by,
                                           symbols=schedule.symbol_list or None, date=trade_date, fmt=schedule.fmt,
                                           promote=schedule.promote, aggregates=schedule.aggregates,
                                           skip_bad_rows=False if schedule.strict else None,
                                           fake=True if schedule.fake else None,
                                           allow_fake_data=schedule.allow_fake_data, background=False)
            except ApiLoadBusy as exc:
                outcome = ScheduleOutcome(schedule.schedule_id, schedule.market, trade_date, STATUS_FAILED,
                                          f"not started - {exc}")
            except EodApiError as exc:                   # a daily limit, the day, the token: nothing requested
                outcome = ScheduleOutcome(schedule.schedule_id, schedule.market, trade_date, STATUS_REFUSED,
                                          f"not started - {exc}")
            except Exception as exc:  # noqa: BLE001 - never let the scheduler die
                log.exception("schedule %s broke off", schedule.schedule_id)
                outcome = ScheduleOutcome(schedule.schedule_id, schedule.market, trade_date, STATUS_FAILED,
                                          f"the run broke off: {exc}")
            else:
                warned = any(l["level"] in ("warning", "error") for l in job.log)
                status = (STATUS_FAILED if job.status == JOB_FAILED else STATUS_WARNINGS if warned or job.at_limit
                          else STATUS_LOADED)
                outcome = ScheduleOutcome(schedule.schedule_id, schedule.market, trade_date, status, job.message,
                                          job.run_id)
        self.store().record(schedule.schedule_id, outcome.status, outcome.message, outcome.run_id, trade_date)
        level = "INFO" if outcome.status == STATUS_LOADED else "WARNING"
        self._log("schedule_run" if outcome.status in (STATUS_LOADED, STATUS_WARNINGS) else "schedule_run_" + outcome.status,
                  f"{what} of {schedule.market} ({schedule.run_time}, {trade_date}): {outcome.status} - {outcome.message}",
                  level=level, schedule=schedule.schedule_id, market=schedule.market, trade_date=trade_date,
                  run=outcome.run_id, started_by=started_by)
        return outcome

    # -- the thread -----------------------------------------------------------------------------------
    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="bulk-scheduler", daemon=True)
        self._thread.start()
        log.info("bulk load scheduler started (checks every %ss)", self.interval)

    def join(self, timeout: float | None = None) -> None:
        """Wait for the last 'Run now' (tests)."""
        if self.worker is not None:
            self.worker.join(timeout)

    def stop(self, timeout: float | None = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - keep checking
                log.exception("the bulk load scheduler check failed")
            self._stop.wait(self.interval)
