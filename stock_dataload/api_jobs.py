"""
Background **API loads** for the dashboard's *API loading* page.

A load calls the EOD API once per stock (with retries) and puts every answer straight into the
staging table - :class:`~stock_dataload.eod_market.MarketFetcher` does the work.  Three kinds::

    symbols   the stocks the user typed in (comma separated)
    market    every stock of a market's symbol list (NYSE, ...), of the chosen types
    file      the stocks of an uploaded text file (symbols separated by commas)
    bulk      ONE trading day of every stock of a market in one call (eod-bulk-last-day, eod_bulk.py)

A whole market takes minutes, so the load runs in a **background thread**; the page polls
:meth:`ApiLoadManager.status` for the progress.  One load at a time (they all write the same SQLite
file); every load is an ``eod-fetch-market`` run in ``Eod_Market_Run`` - the dashboard shows the
history from there, and a stopped / failed run can be **resumed**.  A whole market may be loaded at
most 3 times a day (:data:`~stock_dataload.eod_market.MAX_MARKET_LOADS_PER_DAY`): :meth:`ApiLoadManager.start`
refuses a fourth ``market`` load at once, and ``MarketFetcher.fetch_market`` checks again when the run
is registered.  Likewise a text file lists at most 100 symbols and is loaded at most 5 times a day, and
every stock is loaded at most 5 times a day (:mod:`~stock_dataload.load_limits`): ``start`` refuses a
load whose stocks all reached that limit and names the ones that will be skipped
(:attr:`ApiLoadJob.at_limit`).  Afterwards the loaded stocks can
be promoted (``promote eod`` for exactly them, optionally ``promote weekly`` / ``monthly``).

The manager lives as long as the dashboard process: a load survives a Settings save (which rebuilds
the dashboard state) because the manager reads the *current* settings when a load starts.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .config import Settings
from .eod_api import EodApiError
from .eod_market import (ITEM_FAILED, ITEM_LOADED, ITEM_NO_DATA, ITEM_SKIPPED, RUN_COMPLETED, EodMarketError,
                         MarketFetcher, MarketRun, StockOutcome)

log = logging.getLogger(__name__)

COMPONENT = "api_load"

KIND_SYMBOLS = "symbols"
KIND_MARKET = "market"
KIND_FILE = "file"
KIND_RESUME = "resume"
KIND_BULK = "bulk"
KINDS = (KIND_SYMBOLS, KIND_MARKET, KIND_FILE, KIND_RESUME, KIND_BULK)

JOB_STARTING = "starting"
JOB_RUNNING = "running"
JOB_PROMOTING = "promoting"
JOB_FINISHED = "finished"          # the run ended - see run_status (completed / completed_with_errors / ...)
JOB_FAILED = "failed"              # the load could not start or broke off (see message)
ACTIVE = (JOB_STARTING, JOB_RUNNING, JOB_PROMOTING)

LOG_LINES = 300


class ApiLoadBusy(Exception):
    """Another API load is running."""


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


@dataclass
class ApiLoadJob:
    job_id: str
    kind: str
    market: str
    title: str
    started_by: str = ""
    options: dict = field(default_factory=dict)
    status: str = JOB_STARTING
    run_id: str | None = None
    run_status: str | None = None
    total: int = 0
    done: int = 0                   # stocks handled (incl. those of an earlier part of a resumed run)
    loaded: int = 0
    no_data: int = 0
    failed: int = 0
    skipped: int = 0
    rows: int = 0
    api_calls: int = 0
    message: str = ""
    promotion: list[str] = field(default_factory=list)
    started_at: str = field(default_factory=_now)
    finished_at: str | None = None
    stop_requested: bool = False
    #: stocks that reached their daily limit when the load started - they are skipped (not called)
    at_limit: list[str] = field(default_factory=list)
    log: deque = field(default_factory=lambda: deque(maxlen=LOG_LINES))

    @property
    def active(self) -> bool:
        return self.status in ACTIVE

    @property
    def percent(self) -> int:
        return int(self.done * 100 / self.total) if self.total else 0

    def add_log(self, level: str, text: str) -> None:
        self.log.append({"at": datetime.now().strftime("%H:%M:%S"), "level": level, "text": text})

    def as_dict(self, log_lines: int = 100) -> dict:
        return {"job_id": self.job_id, "kind": self.kind, "market": self.market, "title": self.title,
                "started_by": self.started_by, "options": self.options, "status": self.status, "active": self.active,
                "run_id": self.run_id, "run_status": self.run_status, "total": self.total, "done": self.done,
                "percent": self.percent, "loaded": self.loaded, "no_data": self.no_data, "failed": self.failed,
                "skipped": self.skipped, "rows": self.rows, "api_calls": self.api_calls, "message": self.message,
                "promotion": list(self.promotion), "started_at": self.started_at, "finished_at": self.finished_at,
                "stop_requested": self.stop_requested, "at_limit": list(self.at_limit),
                "log": list(self.log)[-log_lines:] if log_lines else []}


class ApiLoadManager:
    """Starts API loads in a background thread (one at a time) and keeps their progress."""

    def __init__(self, settings: Callable[[], Settings] | Settings, audit_factory: Callable[[Settings], object] | None = None,
                 fetcher_factory: Callable[..., MarketFetcher] | None = None, keep: int = 10):
        self._settings = settings if callable(settings) else (lambda: settings)
        self._audit_factory = audit_factory
        self._fetcher_factory = fetcher_factory or MarketFetcher
        self._jobs: deque[ApiLoadJob] = deque(maxlen=keep)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # -- reading ------------------------------------------------------------------------------
    @property
    def current(self) -> ApiLoadJob | None:
        """The running load, else the newest one."""
        with self._lock:
            for job in reversed(self._jobs):
                if job.active:
                    return job
            return self._jobs[-1] if self._jobs else None

    @property
    def busy(self) -> bool:
        job = self.current
        return job is not None and job.active

    def jobs(self) -> list[ApiLoadJob]:
        with self._lock:
            return list(reversed(self._jobs))

    def get(self, job_id: str) -> ApiLoadJob | None:
        with self._lock:
            return next((j for j in self._jobs if j.job_id == job_id), None)

    def status(self, log_lines: int = 100) -> dict:
        job = self.current
        return {"busy": self.busy, "job": job.as_dict(log_lines) if job else None}

    # -- controlling --------------------------------------------------------------------------
    def start(self, kind: str, market: str, title: str, started_by: str = "", symbols: list[str] | None = None,
              types: list[str] | None = None, limit: int | None = None, params: dict | None = None,
              promote: bool = False, aggregates: bool = False, skip_bad_rows: bool = False,
              refresh_list: bool = False, resume: str | None = None, fake: bool | None = None,
              fake_faults: str | None = None, allow_fake_data: bool = False,
              background: bool = True, date: str | None = None, fmt: str | None = None,
              force: bool = False) -> ApiLoadJob:
        """Start a load; raises :class:`ApiLoadBusy` while another one runs, and
        :class:`~stock_dataload.eod_market.MarketLoadLimitError` for a whole-market load (*kind*
        ``market``) when the market already had its 3 whole-market loads today - both before a job is
        created.  *background* = False runs it in the calling thread (tests, scripts)."""
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind}")
        at_limit: dict[str, int] = {}
        if kind == KIND_BULK:                        # the day and the daily limits - before a job exists
            from .eod_bulk import check_trade_day
            settings = self._settings()
            date = check_trade_day(market, date, force, extra_holidays=settings.extra_market_holidays)
            audit = self._audit_factory(settings) if self._audit_factory else None
            from .eod_bulk import BulkDayLoader
            BulkDayLoader(settings, audit=audit, fake=fake).check_bulk_call_allowed(market, date, started_by)
            checker = self._fetcher_factory(settings, audit=audit, fake=fake)
            if symbols:
                at_limit = checker.check_stocks_allowed(symbols, started_by)
            else:
                checker.check_market_load_allowed(market, started_by)
        elif kind != KIND_RESUME:                    # the daily limits - before a job exists
            from .load_limits import check_file_symbols
            settings = self._settings()
            audit = self._audit_factory(settings) if self._audit_factory else None
            checker = self._fetcher_factory(settings, audit=audit, fake=fake)
            if kind == KIND_MARKET:
                checker.check_market_load_allowed(market, started_by)
            else:
                if kind == KIND_FILE:
                    from .load_limits import LoadLimitError, audit_refusal
                    try:
                        check_file_symbols(symbols or [], title)
                    except LoadLimitError as exc:
                        audit_refusal(audit, COMPONENT, exc, symbols=len(symbols or []), requested_by=started_by)
                        raise
                    checker.check_file_load_allowed(started_by)
                at_limit = checker.check_stocks_allowed(symbols or [], started_by)
        with self._lock:
            if any(j.active for j in self._jobs):
                raise ApiLoadBusy("an API load is running - wait for it or stop it first")
            job = ApiLoadJob(uuid.uuid4().hex[:10], kind, market, title, started_by,
                             options={"types": types, "limit": limit, "params": params or {}, "promote": promote,
                                      "aggregates": aggregates, "skip_bad_rows": skip_bad_rows,
                                      "refresh_list": refresh_list, "resume": resume, "fake": bool(fake),
                                      "fake_faults": fake_faults or None, "symbols": len(symbols or []),
                                      "date": date, "fmt": fmt, "force": force})
            job.at_limit = sorted(at_limit)
            self._jobs.append(job)
        job.add_log("info", f"{title} - started by {started_by or 'unknown'}")
        if at_limit:
            job.add_log("warning", f"{len(at_limit)} stock(s) already loaded {max(at_limit.values())} times today "
                                   f"will be skipped (daily limit per stock): {', '.join(sorted(at_limit)[:20])}"
                                   + (" ..." if len(at_limit) > 20 else ""))
        args = dict(symbols=symbols, types=types, limit=limit, params=params, promote=promote, aggregates=aggregates,
                    skip_bad_rows=skip_bad_rows, refresh_list=refresh_list, resume=resume, fake=fake,
                    fake_faults=fake_faults, allow_fake_data=allow_fake_data)
        target = self._run
        if kind == KIND_BULK:
            target = self._run_bulk
            args = dict(symbols=symbols, date=date, fmt=fmt, force=force, promote=promote, aggregates=aggregates,
                        strict=skip_bad_rows is False, fake=fake, fake_faults=fake_faults,
                        allow_fake_data=allow_fake_data)
        if not background:
            target(job, **args)
            return job
        self._thread = threading.Thread(target=target, args=(job,), kwargs=args, name=f"api-load-{job.job_id}",
                                        daemon=True)
        self._thread.start()
        return job

    def stop(self) -> ApiLoadJob | None:
        """Ask the running load to stop after the current stock (the run can be resumed)."""
        job = self.current
        if job is None or not job.active:
            return None
        job.stop_requested = True
        job.add_log("warning", "stop requested - the load ends after the current stock")
        return job

    def join(self, timeout: float | None = None) -> None:
        """Wait for the background thread (tests)."""
        if self._thread is not None:
            self._thread.join(timeout)

    # -- the work -------------------------------------------------------------------------------
    def _run(self, job: ApiLoadJob, symbols, types, limit, params, promote, aggregates, skip_bad_rows,
             refresh_list, resume, fake, fake_faults, allow_fake_data) -> None:
        settings = self._settings()
        audit = self._audit_factory(settings) if self._audit_factory else None
        try:
            fetcher = self._fetcher_factory(settings, audit=audit, fake=fake, fake_faults=fake_faults)
            job.add_log("info", f"gateway: {'the built-in stub (fake)' if fetcher.fake else 'the live API'}; "
                                f"retries per stock: {settings.eod_api_retries}")

            started: list[MarketRun] = []

            def on_start(run: MarketRun) -> None:
                started.append(run)
                job.run_id, job.total, job.api_calls = run.run_id, len(run.symbols), run.api_calls
                job.done = run.resumed_from
                self._count(job, run)
                job.status = JOB_RUNNING
                job.add_log("info", f"run {run.run_id}: {len(run.symbols):,} stock(s)"
                                    + (f", {run.resumed_from:,} done before" if run.resumed_from else ""))

            def progress(n: int, total: int, outcome: StockOutcome) -> None:
                job.done, job.total = n, total
                if started:
                    job.api_calls = started[0].api_calls
                self._tally(job, outcome)
                level = {ITEM_LOADED: "ok", ITEM_NO_DATA: "info"}.get(outcome.status, "error")
                if outcome.limited:
                    level = "warning"                       # the daily limit of the stock - not an error
                job.add_log(level, str(outcome))

            run = fetcher.fetch_market(job.market, symbols=symbols, types=types, params=params, limit=limit,
                                       refresh_list=refresh_list, resume=resume or False,
                                       skip_bad_rows=True if skip_bad_rows else None, allow_fake_data=allow_fake_data,
                                       progress=progress, should_stop=lambda: job.stop_requested, on_start=on_start,
                                       source=None if job.kind == KIND_RESUME else job.kind,
                                       requested_by=job.started_by)
        except (EodMarketError, EodApiError, ValueError) as exc:
            self._finish(job, JOB_FAILED, str(exc), audit)
            return
        except Exception as exc:  # noqa: BLE001 - never let the thread die silently
            log.exception("API load %s broke off", job.job_id)
            self._finish(job, JOB_FAILED, f"the load broke off: {exc}", audit)
            return
        job.run_id, job.run_status, job.api_calls = run.run_id, run.status, run.api_calls
        self._count(job, run)
        job.message = run.summary()
        job.add_log("ok" if run.status == RUN_COMPLETED and not run.limited() else "warning", run.summary())
        loaded = [o.symbol for o in run.outcomes.values() if o.status == ITEM_LOADED]
        if promote and loaded:
            job.status = JOB_PROMOTING
            self._promote(job, settings, run.market, loaded, aggregates, audit)
        elif promote:
            job.add_log("info", "nothing loaded - nothing to promote")
        self._finish(job, JOB_FINISHED, job.message, audit)

    def _run_bulk(self, job: ApiLoadJob, symbols, date, fmt, force, promote, aggregates, strict, fake, fake_faults,
                  allow_fake_data) -> None:
        """A one-day bulk load (eod_bulk.BulkDayLoader): one API call, the whole answer in one transaction."""
        from .eod_bulk import BULK_FAILED, BULK_REFUSED, BulkDayLoader
        settings = self._settings()
        audit = self._audit_factory(settings) if self._audit_factory else None
        try:
            loader = BulkDayLoader(settings, audit=audit, fake=fake, fake_faults=fake_faults)
            job.status, job.total = JOB_RUNNING, 1
            job.add_log("info", f"gateway: {'the built-in stub (fake)' if loader.fake else 'the live API'}; "
                                f"retries: {settings.eod_api_retries}")
            result = loader.fetch(job.market, fmt=fmt, date=date, symbols=symbols, force=force,
                                  skip_bad_rows=False if strict else None, allow_fake_data=allow_fake_data,
                                  requested_by=job.started_by, progress=lambda level, text: job.add_log(level, text))
        except (EodMarketError, EodApiError, ValueError) as exc:
            self._finish(job, JOB_FAILED, str(exc), audit)
            return
        except Exception as exc:  # noqa: BLE001 - never let the thread die silently
            log.exception("API load %s broke off", job.job_id)
            self._finish(job, JOB_FAILED, f"the load broke off: {exc}", audit)
            return
        job.run_id, job.api_calls, job.done = result.run_id, result.api_calls, 1
        job.total = result.symbols or 1
        job.done = job.total
        job.loaded, job.rows, job.skipped = len(result.loaded), result.rows_loaded, len(result.limited)
        job.failed = len({s for s, _ in result.rejected})
        job.no_data = 1 if result.status == "no_data" else 0
        job.at_limit = sorted(result.limited)
        job.run_status = (RUN_COMPLETED if result.ok and not result.rejected else
                          "aborted" if result.status == BULK_FAILED else "completed_with_errors")
        job.message = str(result)
        if promote and result.loaded:
            job.status = JOB_PROMOTING
            try:
                for step in loader.promote(result, aggregates=aggregates, audit=audit):
                    job.promotion.append(str(step))
                    job.add_log("ok" if not step.rows_rejected else "warning", str(step))
            except Exception as exc:  # noqa: BLE001 - PromotionError and the like
                job.promotion.append(f"promotion failed: {exc}")
                job.add_log("error", f"promotion failed: {exc}")
        elif promote:
            job.add_log("info", "nothing loaded - nothing to promote")
        self._finish(job, JOB_FAILED if result.status in (BULK_FAILED, BULK_REFUSED) else JOB_FINISHED,
                     job.message, audit)

    @staticmethod
    def _count(job: ApiLoadJob, run: MarketRun) -> None:
        job.loaded, job.no_data = run.count(ITEM_LOADED), run.count(ITEM_NO_DATA)
        job.failed, job.skipped, job.rows = run.count(ITEM_FAILED), run.count(ITEM_SKIPPED), run.rows

    @staticmethod
    def _tally(job: ApiLoadJob, outcome: StockOutcome) -> None:
        if outcome.status == ITEM_LOADED:
            job.loaded += 1
            job.rows += outcome.rows
        elif outcome.status == ITEM_NO_DATA:
            job.no_data += 1
        elif outcome.status == ITEM_FAILED:
            job.failed += 1
        elif outcome.status == ITEM_SKIPPED:
            job.skipped += 1

    def _promote(self, job: ApiLoadJob, settings: Settings, market: str, symbols: list[str], aggregates: bool,
                 audit) -> None:
        from .promotion import PromotionError, PromotionService
        service = PromotionService(settings, audit=audit)
        try:
            steps = [("eod", lambda: service.promote_eod_daily(market=market, symbols=symbols))]
            if aggregates:
                steps += [("weekly", service.promote_weekly), ("monthly", service.promote_monthly)]
            for name, step in steps:
                job.add_log("info", f"promote {name} ...")
                result = step()
                job.promotion.append(str(result))
                job.add_log("ok" if not result.rows_rejected else "warning", str(result))
        except PromotionError as exc:
            job.promotion.append(f"promotion failed: {exc}")
            job.add_log("error", f"promotion failed: {exc}")

    def _finish(self, job: ApiLoadJob, status: str, message: str, audit) -> None:
        job.status, job.message, job.finished_at = status, message, _now()
        if status == JOB_FAILED:
            job.add_log("error", message)
        if audit is not None:
            try:
                audit.log(COMPONENT, "api_load_finished" if status == JOB_FINISHED else "api_load_failed",
                          f"{job.title}: {message}", level="INFO" if job.run_status == RUN_COMPLETED else "WARNING",
                          details={"job": job.job_id, "run": job.run_id, "kind": job.kind, "market": job.market,
                                   "started_by": job.started_by, "loaded": job.loaded, "failed": job.failed})
            except Exception:  # noqa: BLE001 - the audit trail must not break the load
                log.warning("could not write the audit entry of API load %s", job.job_id)
