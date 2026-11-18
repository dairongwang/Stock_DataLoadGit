"""
Daily schedules of the one-day bulk load (``bulk_schedule.py``): the store, the calendar of a schedule
(trading days only, once per day, catch-up the same day), the scheduler (claims, outcomes, refusals,
a busy manager, the thread), the API loading page (save / edit / enable / run now / delete, roles,
JSON) and ``eod-bulk-schedule``.

Clocks are fixed; the loads use the built-in stub; every token and account is a dummy.
"""

from __future__ import annotations

import io
import sqlite3
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime
from unittest import mock

from stock_dataload import cli
from stock_dataload.api_jobs import ApiLoadJob, ApiLoadManager, JOB_RUNNING
from stock_dataload.audit import AuditLogger
from stock_dataload.bulk_schedule import (STATUS_LOADED, STATUS_REFUSED, TRADE_DAY_PREVIOUS, BulkScheduler,
                                          ScheduleError, ScheduleStore, is_due, next_run, render_ddl, trade_date_for,
                                          validate_run_time)
from stock_dataload.config import save_settings
from stock_dataload.eod_bulk import BulkDayLoader
from stock_dataload.eod_market import MarketFetcher

from .helpers import PROJECT_ROOT, TempProjectTestCase
from .test_dashboard import OPERATOR, VIEWER, DashboardTestCase

MONDAY_EVENING = datetime(2026, 10, 12, 19, 0)         # a trading day of the stub's sample year
TODAY = date(2026, 11, 17)


def fixed_today(testcase) -> None:
    patcher = mock.patch("stock_dataload.eod_bulk._today", return_value=TODAY)
    patcher.start()
    testcase.addCleanup(patcher.stop)


class ScheduleTestCase(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        fixed_today(self)
        self.settings.eod_api_token = "dummy-schedule-token"
        self.settings.eod_api_retry_backoff = 0.0
        self.store = ScheduleStore(self.settings)
        self.audit = AuditLogger(self.settings)

    def scheduler(self, **kwargs) -> BulkScheduler:
        return BulkScheduler(self.settings, audit_factory=AuditLogger, **kwargs)

    def stub_schedule(self, **kwargs):
        values = dict(market="US", run_time="18:30", symbols="MRK,HD", fake=True, promote=False, user="dummy_operator")
        values.update(kwargs)
        return self.store.save(**values)


class StoreTests(ScheduleTestCase):
    def test_save_change_enable_delete(self):
        self.assertEqual(validate_run_time("6:05"), "06:05")
        for bad in ("24:00", "18", "noon", ""):
            with self.assertRaises(ScheduleError):
                validate_run_time(bad)
        s = self.store.save("us", "18:30", user="dummy_operator")
        self.assertEqual((s.market, s.run_time, s.trade_day, s.fmt, s.promote, s.enabled), ("US", "18:30", "same",
                                                                                            "json", True, True))
        changed = self.store.save("US", "07:15", trade_day=TRADE_DAY_PREVIOUS, fmt="csv", symbols="msft, hd",
                                  promote=False, user="dummy_admin")
        self.assertEqual(changed.schedule_id, s.schedule_id)                   # one schedule per exchange
        self.assertEqual((changed.run_time, changed.trade_day, changed.symbol_list, changed.updated_by),
                         ("07:15", "previous", ["MSFT", "HD"], "dummy_admin"))
        self.store.save("LSE", "19:00")
        self.assertEqual([x.market for x in self.store.list()], ["US", "LSE"])     # by time
        self.assertFalse(self.store.set_enabled(s.schedule_id, False).enabled)
        self.store.delete(s.schedule_id)
        self.assertIsNone(self.store.get_by_market("US"))
        for kwargs in ({"market": "NY SE"}, {"trade_day": "tomorrow"}, {"fmt": "xml"}, {"symbols": "BRK/B"}):
            with self.assertRaises(ScheduleError):
                self.store.save(**{"market": "US", "run_time": "18:30", **kwargs})
        with self.assertRaises(ScheduleError):
            self.store.delete(999)

    def test_reference_script(self):
        script = (PROJECT_ROOT / "sql" / "create_eod_bulk_schedule_sqlite.sql").read_text(encoding="utf-8")
        self.assertTrue(script.endswith(render_ddl()))


class CalendarTests(ScheduleTestCase):
    def test_next_run_and_due(self):
        s = self.store.save("US", "18:30")
        friday = datetime(2026, 10, 9, 10, 0)
        self.assertEqual(next_run(s, friday), datetime(2026, 10, 9, 18, 30))
        self.assertFalse(is_due(s, friday))
        self.assertTrue(is_due(s, datetime(2026, 10, 9, 18, 30)))
        self.assertTrue(is_due(s, datetime(2026, 10, 9, 23, 0)))                 # catch-up the same day
        self.assertTrue(self.store.claim(s.schedule_id, "2026-10-09", "2026-10-09"))
        self.assertFalse(self.store.claim(s.schedule_id, "2026-10-09", "2026-10-09"))    # once per day
        s = self.store.get(s.schedule_id)
        self.assertFalse(is_due(s, datetime(2026, 10, 9, 23, 0)))
        self.assertEqual(next_run(s, datetime(2026, 10, 9, 23, 0)), datetime(2026, 10, 12, 18, 30))   # weekend skipped
        self.assertFalse(is_due(s, datetime(2026, 10, 10, 19, 0)))               # Saturday
        self.assertEqual(next_run(s, datetime(2026, 11, 25, 20, 0)), datetime(2026, 11, 25, 18, 30))
        self.store.claim(s.schedule_id, "2026-11-25", "2026-11-25")
        self.assertEqual(next_run(self.store.get(s.schedule_id), datetime(2026, 11, 25, 20, 0)),
                         datetime(2026, 11, 27, 18, 30))                          # Thanksgiving skipped
        self.assertIsNone(next_run(self.store.set_enabled(s.schedule_id, False), friday))

    def test_the_trade_date_of_a_run(self):
        same = self.store.save("US", "18:30")
        previous = self.store.save("LSE", "08:00", trade_day=TRADE_DAY_PREVIOUS)
        self.assertEqual(trade_date_for(same, date(2026, 10, 12)), "2026-10-12")
        self.assertEqual(trade_date_for(previous, date(2026, 10, 12)), "2026-10-09")   # Monday loads Friday


class SchedulerTests(ScheduleTestCase):
    def test_a_due_schedule_runs_once_per_trading_day(self):
        s = self.stub_schedule()
        scheduler = self.scheduler()
        self.assertEqual(scheduler.tick(datetime(2026, 10, 12, 18, 0)), [])     # not yet
        outcomes = scheduler.tick(MONDAY_EVENING)
        self.assertEqual(len(outcomes), 1)
        self.assertEqual((outcomes[0].status, outcomes[0].trade_date), (STATUS_LOADED, "2026-10-12"))
        after = self.store.get(s.schedule_id)
        self.assertEqual((after.last_run_date, after.last_status, after.last_trade_date),
                         ("2026-10-12", STATUS_LOADED, "2026-10-12"))
        run = MarketFetcher(self.settings, fake=False).find_run(after.last_run_id)
        self.assertEqual((run["Market"], run["Loaded"]), ("US", 2))
        self.assertEqual(scheduler.tick(datetime(2026, 10, 12, 23, 0)), [])     # once per day
        self.assertEqual(scheduler.tick(datetime(2026, 10, 13, 19, 0))[0].trade_date, "2026-10-13")
        events = [l.event for l in self.audit.fetch_logs(limit=20, component="api_load_schedule")]
        self.assertEqual(events.count("schedule_run"), 2)

    def test_refusals_are_recorded_and_not_repeated_that_day(self):
        live = self.store.save("US", "18:30", symbols="MRK", promote=False)      # live, but the token is a dummy
        self.settings.eod_api_token = "dummy"
        outcome = self.scheduler().tick(MONDAY_EVENING)[0]
        self.assertEqual(outcome.status, STATUS_REFUSED)
        self.assertIn("placeholder 'dummy'", outcome.message)
        self.assertEqual(self.scheduler().tick(datetime(2026, 10, 12, 20, 0)), [])
        self.store.delete(live.schedule_id)
        # the limit of 3 calls per market and trading day
        for _ in range(3):
            BulkDayLoader(self.settings, fake=True).fetch("US", date="2026-10-13", symbols="PFE")
        self.stub_schedule()
        outcome = self.scheduler().tick(datetime(2026, 10, 13, 19, 0))[0]
        self.assertEqual(outcome.status, STATUS_REFUSED)
        self.assertIn("at most 3 times for the same market and day", outcome.message)
        self.assertIn("schedule_run_refused", [l.event for l in self.audit.fetch_logs(limit=10)])

    def test_a_busy_manager_waits_for_the_next_check(self):
        self.stub_schedule()
        manager = ApiLoadManager(self.settings)
        busy = ApiLoadJob("dummyjob", "symbols", "NYSE", "another load", status=JOB_RUNNING)
        manager._jobs.append(busy)
        scheduler = BulkScheduler(self.settings, api_loads=manager)
        self.assertEqual(scheduler.tick(MONDAY_EVENING), [])
        self.assertIsNone(self.store.get_by_market("US").last_run_date)         # not claimed
        busy.status = "finished"
        self.assertEqual(len(scheduler.tick(MONDAY_EVENING)), 1)

    def test_run_now_keeps_the_daily_run(self):
        s = self.stub_schedule(trade_day=TRADE_DAY_PREVIOUS)
        outcome = self.scheduler().run_now(s, "dummy_operator", now=MONDAY_EVENING)
        self.assertEqual((outcome.status, outcome.trade_date), (STATUS_LOADED, "2026-10-09"))
        after = self.store.get(s.schedule_id)
        self.assertIsNone(after.last_run_date)
        self.assertTrue(is_due(after, MONDAY_EVENING))

    def test_the_thread(self):
        self.stub_schedule()
        scheduler = self.scheduler(interval=0.05, clock=lambda: MONDAY_EVENING)
        scheduler.start()
        try:
            for _ in range(200):
                if self.store.get_by_market("US").last_status == STATUS_LOADED:
                    break
                time.sleep(0.05)
        finally:
            scheduler.stop()
        self.assertEqual(self.store.get_by_market("US").last_status, STATUS_LOADED)
        self.assertFalse(scheduler.running)


class DashboardScheduleTests(DashboardTestCase):
    def setUp(self):
        super().setUp()
        fixed_today(self)
        self.scheduler: BulkScheduler = self.app.extensions["bulk_scheduler"]
        self.scheduler.clock = lambda: MONDAY_EVENING                            # 'Run now' on a trading day

    def save(self, **form):
        data = {"market": "US", "run_time": "18:30", "trade_day": "same", "fmt": "json", "symbols": "MRK",
                "fake": "on", "enabled": "on"}
        data.update(form)
        return self.client.post("/api-load/schedules", data=data)

    def test_the_schedule_card(self):
        page = self.client.get("/api-load").get_data(as_text=True)
        self.assertIn("Daily schedule", page)
        self.assertIn("No daily schedule yet.", page)
        self.assertIn("scheduler not running in this process", page)
        r = self.save()
        messages = self.flashes(r)
        self.assertIn("Schedule saved: US every trading day at 18:30", messages)
        self.assertIn("The scheduler does not run in this process", messages)
        schedule = ScheduleStore(self.state.settings).get_by_market("US")
        page = self.client.get(f"/api-load?schedule={schedule.schedule_id}").get_data(as_text=True)
        self.assertIn("Change the schedule of US", page)
        self.assertIn('value="18:30"', page)
        self.assertIn("never ran", page)
        self.assertIn("Schedule not saved - '25:00' is not a time", self.flashes(self.save(run_time="25:00")))
        data = self.client.get("/api/api-load/schedules").get_json()
        self.assertEqual((data["count"], data["records"][0]["market"], data["scheduler_running"]), (1, "US", False))
        r = self.client.post(f"/api-load/schedules/{schedule.schedule_id}/toggle")
        self.assertIn("Schedule of US disabled", self.flashes(r))
        r = self.client.post(f"/api-load/schedules/{schedule.schedule_id}/toggle")
        self.assertIn("Schedule of US enabled", self.flashes(r))
        r = self.client.post(f"/api-load/schedules/{schedule.schedule_id}/run")
        self.scheduler.join(60)
        self.assertIn("Run now of US", self.flashes(r))
        after = ScheduleStore(self.state.settings).get(schedule.schedule_id)
        self.assertEqual(after.last_status, STATUS_LOADED, after.last_message)
        page = self.client.get("/api-load").get_data(as_text=True)
        self.assertIn(after.last_run_id, page)
        r = self.client.post(f"/api-load/schedules/{schedule.schedule_id}/delete")
        self.assertIn("Schedule of US deleted", self.flashes(r))
        self.assertEqual(self.client.post("/api-load/schedules/999/delete").status_code, 404)

    def test_a_live_schedule_with_the_placeholder_token_is_flagged(self):
        self.state.settings.eod_api_token = "dummy"
        r = self.save(fake="")
        self.assertIn("The schedule will be refused until the token is set", self.flashes(r))
        outcome = self.scheduler.tick(MONDAY_EVENING)[0]
        self.assertEqual(outcome.status, STATUS_REFUSED)

    def test_the_scheduler_of_the_dashboard_runs_the_load(self):
        self.save()
        outcome = self.scheduler.tick(MONDAY_EVENING)[0]
        self.assertEqual(outcome.status, STATUS_LOADED, outcome.message)
        job = self.app.extensions["api_loads"].current
        self.assertIn("(bulk, scheduled run)", job.title)
        self.assertTrue(job.started_by.startswith("schedule "))

    def test_bulk_quota_json(self):
        q = self.client.get("/api/api-load/bulk-quota?market=us&date=2026-10-12").get_json()
        self.assertEqual((q["market"], q["trade_date"], q["used"], q["limit"]), ("US", "2026-10-12", 0, 3))
        self.assertEqual(self.client.get("/api/api-load/bulk-quota?date=garbage").status_code, 400)

    def test_roles(self):
        self.logout()
        self.login(*VIEWER)
        self.assertEqual(self.client.get("/api/api-load/schedules").status_code, 200)
        self.assertEqual(self.save().status_code, 403)
        self.assertNotIn('id="schedule-form"', self.client.get("/api-load").get_data(as_text=True))
        self.logout()
        self.login(*OPERATOR)
        self.assertEqual(self.save().status_code, 302)


class CliScheduleTests(ScheduleTestCase):
    def setUp(self):
        super().setUp()
        self.settings.config_file = self.root / "config.ini"
        save_settings(self.settings, self.settings.config_file)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["--config", str(self.settings.config_file), "-q", *args])
        return rc, out.getvalue(), err.getvalue()

    def test_eod_bulk_schedule(self):
        rc, out, _ = self.run_cli("eod-bulk-schedule", "list")
        self.assertIn("no daily schedules", out)
        rc, out, err = self.run_cli("eod-bulk-schedule", "set", "US", "--time", "18:30", "--symbols", "MRK",
                                    "--fake", "--no-promote")
        self.assertEqual(rc, 0, err)
        self.assertIn("schedule of US: every trading day at 18:30", out)
        rc, out, _ = self.run_cli("eod-bulk-schedule", "list")
        self.assertIn("US        18:30 same", out)
        self.assertEqual(self.run_cli("eod-bulk-schedule", "disable", "US")[0], 0)
        self.assertFalse(self.store.get_by_market("US").enabled)
        self.assertEqual(self.run_cli("eod-bulk-schedule", "enable", "US")[0], 0)
        with mock.patch("stock_dataload.bulk_schedule.datetime") as clock:
            clock.now.return_value = MONDAY_EVENING
            clock.combine = datetime.combine
            rc, out, err = self.run_cli("eod-bulk-schedule", "run-due")
        self.assertEqual(rc, 0, err)
        self.assertIn("schedule 1 (US 2026-10-12): loaded", out)
        rc, out, err = self.run_cli("eod-bulk-schedule", "run", "US")
        self.assertIn("(US ", out + err)
        rc, _, err = self.run_cli("eod-bulk-schedule", "set", "US", "--time", "7pm")
        self.assertEqual(rc, 1)
        self.assertIn("is not a time", err)
        self.assertEqual(self.run_cli("eod-bulk-schedule", "delete", "US")[0], 0)
        rc, _, err = self.run_cli("eod-bulk-schedule", "run", "US")
        self.assertIn("there is no schedule for US", err)
        rc, out, _ = self.run_cli("init-db")
        self.assertIn("Eod_Bulk_Schedule", out)


if __name__ == "__main__":
    unittest.main()
