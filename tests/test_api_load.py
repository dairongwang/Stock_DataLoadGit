"""
The dashboard's "API loading" page (``api_jobs.py`` + ``/api-load``): loads from the EOD API in a
background job - typed-in stocks, a whole market, a text file of comma-separated symbols - with
live progress, stop / resume, the run pages and the JSON status, promotion afterwards.

Everything runs against the built-in fake gateway (invented prices, injected faults); no network,
every token is a dummy.
"""

from __future__ import annotations

import json
import sqlite3
from io import BytesIO
from unittest import mock

from stock_dataload.api_jobs import (JOB_FAILED, JOB_FINISHED, KIND_MARKET, KIND_RESUME, KIND_SYMBOLS,
                                     ApiLoadBusy, ApiLoadManager)
from stock_dataload.eod_api import SAMPLE_RESPONSE
from stock_dataload.eod_market import (ITEM_LOADED, RUN_COMPLETED, RUN_COMPLETED_ERRORS,
                                       RUN_INTERRUPTED, MarketFetcher, parse_symbol_text)
from stock_dataload.eod_stage import EodStageLoader
from stock_dataload.eod_symbols import SymbolListLoader

from .helpers import TempProjectTestCase
from .test_dashboard import OPERATOR, VIEWER, DashboardTestCase

LIST = [("HD", "Common Stock"), ("JNJ", "Common Stock"), ("JPM", "Common Stock"), ("MRK", "Common Stock"),
        ("PFE", "Common Stock"), ("SPY", "ETF")]


def load_list(settings) -> None:
    result = SymbolListLoader(settings).load_text("NYSE", json.dumps([
        {"Code": c, "Name": c, "Country": "USA", "Exchange": "NYSE", "Currency": "USD", "Type": t, "Isin": None}
        for c, t in LIST]), "json", source_file="test-list.json")
    assert result.ok, result.message


def real_data(settings) -> None:
    """A stock as the live API would have loaded it (source api/...)."""
    result = EodStageLoader(settings).load_text("IBM", SAMPLE_RESPONSE.read_text(encoding="utf-8"),
                                                source_file="api/NYSE/IBM")
    assert result.ok, result.message


def staged(settings) -> dict[str, int]:
    with sqlite3.connect(settings.sqlite_path) as conn:
        return dict(conn.execute('SELECT "Symbol", COUNT(*) FROM "Stock_EOD_Stage" GROUP BY 1').fetchall())


def daily(settings) -> set[str]:
    with sqlite3.connect(settings.sqlite_path) as conn:
        try:
            return {r[0] for r in conn.execute('SELECT DISTINCT "Ticker" FROM "Stock_Daily"')}
        except sqlite3.OperationalError:
            return set()


class SymbolTextTests(TempProjectTestCase):
    def test_parse_symbol_text(self):
        symbols, invalid = parse_symbol_text(" hd, JNJ;jpm\nPFE ,, hd , BRK/B, \"MRK\" ")
        self.assertEqual(symbols, ["HD", "JNJ", "JPM", "PFE", "MRK"])                      # upper case, no duplicates
        self.assertEqual(invalid, ["BRK/B"])
        self.assertEqual(parse_symbol_text(""), ([], []))
        self.assertEqual(parse_symbol_text(None), ([], []))


class ApiLoadManagerTests(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        load_list(self.settings)
        self.waits: list[float] = []
        self.manager = ApiLoadManager(self.settings, fetcher_factory=self.fetcher)

    def fetcher(self, settings, **kwargs) -> MarketFetcher:
        return MarketFetcher(settings, sleep=self.wait, **kwargs)

    def wait(self, seconds: float) -> None:
        self.waits.append(seconds)

    def test_selected_stocks_then_promotion(self):
        job = self.manager.start(KIND_SYMBOLS, "NYSE", "three stocks", "dummy_operator", symbols=["HD", "JNJ", "PFE"],
                                 promote=True, aggregates=True, fake=True, background=False)
        self.assertEqual((job.status, job.run_status), (JOB_FINISHED, RUN_COMPLETED))
        self.assertEqual((job.total, job.done, job.loaded, job.failed, job.api_calls), (3, 3, 3, 0, 3))
        self.assertEqual(set(staged(self.settings)), {"HD", "JNJ", "PFE"})
        self.assertEqual(daily(self.settings), {"HD", "JNJ", "PFE"})
        self.assertEqual(len(job.promotion), 3)                                  # eod, weekly, monthly
        self.assertTrue(all("failed" not in p for p in job.promotion), job.promotion)
        status = self.manager.status()
        self.assertFalse(status["busy"])
        self.assertEqual(status["job"]["percent"], 100)
        self.assertTrue(any("HD:" in line["text"] for line in status["job"]["log"]))
        self.assertEqual(self.manager.status(log_lines=0)["job"]["log"], [])

    def test_whole_market_with_types_and_limit(self):
        job = self.manager.start(KIND_MARKET, "NYSE", "market", fake=True, limit=2, background=False)
        self.assertEqual((job.total, job.loaded), (2, 2))                        # Common Stock: HD, JNJ
        self.assertEqual(set(staged(self.settings)), {"HD", "JNJ"})
        job = self.manager.start(KIND_MARKET, "NYSE", "ETFs", types=["ETF"], fake=True, background=False)
        self.assertEqual(job.total, 1)
        self.assertIn("SPY", staged(self.settings))
        self.assertEqual(daily(self.settings), set())                           # promote not asked for

    def test_failures_retries_and_resume(self):
        self.settings.eod_api_retries = 1
        job = self.manager.start(KIND_SYMBOLS, "NYSE", "with faults", symbols=["HD", "JNJ", "JPM"], fake=True,
                                 fake_faults="JNJ:503x5,JPM:404", background=False)
        self.assertEqual((job.run_status, job.loaded, job.failed, job.api_calls), (RUN_COMPLETED_ERRORS, 1, 2, 4))
        self.assertEqual(self.waits, [1.0])                                      # one retry of JNJ (404: none)
        resumed = self.manager.start(KIND_RESUME, "NYSE", "resume", resume=job.run_id, fake=True, background=False)
        self.assertEqual(resumed.run_id, job.run_id)
        self.assertEqual((resumed.run_status, resumed.loaded, resumed.done), (RUN_COMPLETED, 3, 3))
        items = {i["Symbol"]: i["Status"] for i in MarketFetcher(self.settings, fake=True).run_items(job.run_id)}
        self.assertEqual(set(items.values()), {ITEM_LOADED})

    def test_stop_after_the_current_stock(self):
        # JNJ needs a retry; the "sleep" before it asks the manager to stop -> JPM, MRK are not called
        def stop_on_wait(seconds):
            self.manager.stop()

        manager = ApiLoadManager(self.settings, fetcher_factory=lambda s, **kw: MarketFetcher(s, sleep=stop_on_wait,
                                                                                              **kw))
        self.manager = manager
        job = manager.start(KIND_SYMBOLS, "NYSE", "stop", symbols=["HD", "JNJ", "JPM", "MRK"], fake=True,
                            fake_faults="JNJ:503x1", promote=True, background=False)
        self.assertTrue(job.stop_requested)
        self.assertEqual((job.status, job.run_status, job.loaded, job.done), (JOB_FINISHED, RUN_INTERRUPTED, 2, 2))
        self.assertEqual(set(staged(self.settings)), {"HD", "JNJ"})
        self.assertEqual(daily(self.settings), {"HD", "JNJ"})                    # what was loaded is promoted
        self.assertIsNone(manager.stop())                                         # nothing running any more
        again = manager.start(KIND_RESUME, "NYSE", "resume", resume=job.run_id, fake=True, background=False)
        self.assertEqual((again.run_status, again.loaded, again.total), (RUN_COMPLETED, 4, 4))

    def test_one_load_at_a_time_in_the_background(self):
        gate_open = __import__("threading").Event()

        def slow(seconds):
            gate_open.wait(10)

        manager = ApiLoadManager(self.settings, fetcher_factory=lambda s, **kw: MarketFetcher(s, sleep=slow, **kw))
        job = manager.start(KIND_SYMBOLS, "NYSE", "slow", symbols=["HD", "JNJ"], fake=True, fake_faults="HD:503x1")
        try:
            with self.assertRaises(ApiLoadBusy):
                manager.start(KIND_SYMBOLS, "NYSE", "second", symbols=["PFE"], fake=True)
            self.assertTrue(manager.busy)
            self.assertIs(manager.current, job)
        finally:
            gate_open.set()
            manager.join(20)
        self.assertFalse(manager.busy)
        self.assertEqual((job.status, job.loaded), (JOB_FINISHED, 2))
        self.assertEqual(manager.start(KIND_SYMBOLS, "NYSE", "now", symbols=["PFE"], fake=True,
                                       background=False).loaded, 1)
        self.assertEqual([j.title for j in manager.jobs()], ["now", "slow"])
        self.assertIs(manager.get(job.job_id), job)

    def test_a_load_that_cannot_start_is_reported(self):
        job = self.manager.start(KIND_MARKET, "XETRA", "unknown market", fake=True, background=False)
        self.assertEqual(job.status, JOB_FAILED)
        self.assertIn("XETRA", job.message)
        self.assertEqual(job.log[-1]["level"], "error")
        # the fake next to real data is refused unless allowed
        real_data(self.settings)
        job = self.manager.start(KIND_SYMBOLS, "NYSE", "fake", symbols=["HD"], fake=True, background=False)
        self.assertEqual(job.status, JOB_FAILED)
        self.assertIn("real data", job.message)
        job = self.manager.start(KIND_SYMBOLS, "NYSE", "fake", symbols=["HD"], fake=True, allow_fake_data=True,
                                 background=False)
        self.assertEqual((job.status, job.loaded), (JOB_FINISHED, 1))
        with self.assertRaises(ValueError):
            self.manager.start("teleport", "NYSE", "nonsense")


class ApiLoadPageTests(DashboardTestCase):
    def setUp(self):
        super().setUp()
        load_list(self.state.settings)
        self.state.settings.eod_api_retry_backoff = 0.0
        self.loads: ApiLoadManager = self.app.extensions["api_loads"]

    def start(self, expect_flash: str | None = None, **form):
        data = {"market": "NYSE", "promote": "on", "fake": "on", **form}
        r = self.client.post("/api-load/start", data=data, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302)
        self.loads.join(60)
        if expect_flash is not None:
            self.assertIn(expect_flash, self.flashes(r))
        return self.loads.current

    def test_the_page_renders(self):
        self.state.settings.eod_api_token = "dummy-secret-token-4242"
        page = self.client.get("/api-load").get_data(as_text=True)
        for text in ("API loading", "Selected stocks", "Whole market", "From a text file", "Start the load",
                     "NYSE (6 listed)", "No API load has run yet", "api_token=***"):
            self.assertIn(text, page)
        self.assertNotIn("dummy-secret-token-4242", page)                                       # never the token
        self.assertIn("api_token=***", page)
        self.assertIn('href="/api-load"', self.client.get("/").get_data(as_text=True))          # in the menu

    def test_selected_stocks(self):
        job = self.start("Started: 3 selected stock(s) (NYSE)", mode="symbols", symbols="hd, jnj\nPFE")
        self.assertEqual((job.status, job.run_status, job.loaded, job.started_by), (JOB_FINISHED, RUN_COMPLETED, 3,
                                                                                      "dummy_admin"))
        self.assertEqual(daily(self.state.settings), {"HD", "JNJ", "PFE"})
        status = self.client.get("/api/api-load").get_json()
        self.assertEqual((status["busy"], status["job"]["loaded"], status["job"]["run_id"]), (False, 3, job.run_id))
        page = self.client.get("/api-load").get_data(as_text=True)
        self.assertIn(job.run_id, page)
        self.assertIn("selected stock", page)
        events = [l.event for l in self.state.audit.fetch_logs(limit=20, component="api_load")]
        self.assertIn("api_load_started", events)
        self.assertIn("api_load_finished", events)

    def test_whole_market(self):
        job = self.start("Started: NYSE: Common Stock (first 2)", mode="market", types="Common Stock", limit="2",
                         aggregates="on")
        self.assertEqual((job.total, job.loaded), (2, 2))
        self.assertEqual(len(job.promotion), 3)
        job = self.start("every type", mode="market", all_types="on", promote="")
        self.assertEqual(job.total, 6)                                            # incl. the ETF

    def test_text_file(self):
        data = {"market": "NYSE", "fake": "on", "mode": "file",
                "file": (BytesIO("\ufeffHD, JPM,\r\nMRK, BRK/B".encode("utf-8")), "my stocks.txt")}
        r = self.client.post("/api-load/start", data=data, content_type="multipart/form-data")
        self.loads.join(60)
        self.assertIn("3 stock(s) of my stocks.txt (NYSE) - not a symbol, left out: BRK/B", self.flashes(r))
        self.assertEqual(set(staged(self.state.settings)), {"HD", "JPM", "MRK"})
        self.assertEqual(daily(self.state.settings), set())                      # promote not ticked

    def test_invalid_forms_keep_the_values(self):
        cases = [({"mode": "symbols", "symbols": " , "}, "type one or more stock symbols"),
                 ({"mode": "symbols", "symbols": "HD", "market": "", "market_other": "NY SE!"}, "Market:"),
                 ({"mode": "symbols", "symbols": "HD", "date_from": "31.01.2026"}, "is not a date"),
                 ({"mode": "symbols", "symbols": "HD", "date_from": "2026-02-01", "date_to": "2026-01-01"},
                  "the first date is after the last date"),
                 ({"mode": "symbols", "symbols": "HD", "fake": "", "fake_faults": "HD:503"}, "need the built-in stub"),
                 ({"mode": "symbols", "symbols": "HD", "fake_faults": "HD:teapot"}, "teapot"),
                 ({"mode": "market", "limit": "many"}, "whole number"),
                 ({"mode": "file"}, "choose a text file")]
        for form, message in cases:
            with self.subTest(message):
                self.assertIn(message, self.flashes(self.client.post("/api-load/start",
                                                                     data={"market": "NYSE", "fake": "on", **form})))
        self.assertIsNone(self.loads.current)
        r = self.client.post("/api-load/start", data={"market": "NYSE", "mode": "symbols", "symbols": "XYZ, ",
                                                      "date_from": "bad"})
        page = self.client.get(r.headers["Location"]).get_data(as_text=True)
        self.assertIn("XYZ, </textarea>", page)                                   # the form is filled in again
        self.assertEqual(self.client.post("/api-load/start", data={"mode": "teleport"}).status_code, 400)
        with mock.patch("stock_dataload.dashboard.app.API_FILE_MAX_BYTES", 2048):     # (1 MB in real life)
            r = self.client.post("/api-load/start", data={"market": "NYSE", "fake": "on", "mode": "file",
                                                          "file": (BytesIO(b"HD," * 1000), "big.txt")},
                                 content_type="multipart/form-data")
        self.assertIn("the file is larger than 2 KB", self.flashes(r))

    def test_the_fake_is_refused_next_to_real_data(self):
        real_data(self.state.settings)
        r = self.client.post("/api-load/start", data={"market": "NYSE", "fake": "on", "mode": "symbols",
                                                      "symbols": "HD"})
        self.assertIn("real data", self.flashes(r))
        job = self.start("Started", mode="symbols", symbols="HD", allow_fake_data="on")
        self.assertEqual(job.loaded, 1)

    def test_live_loads_need_a_real_token(self):
        form = {"market": "NYSE", "mode": "symbols", "symbols": "HD"}
        self.assertIn("the EOD API token is the placeholder 'dummy' in config.ini - the live API refuses it",
                      self.flashes(self.client.post("/api-load/start", data=form)))
        self.state.settings.eod_api_token = ""
        self.assertIn("the EOD API token is not set", self.flashes(self.client.post("/api-load/start", data=form)))
        self.assertIsNone(self.loads.current)                                     # nothing was started
        self.state.settings.eod_api_token = "dummy-own-token-for-tests"
        run = MarketFetcher(self.state.settings, fake=True).fetch_market("NYSE", symbols=["HD"])
        with mock.patch.object(MarketFetcher, "fetch_market", autospec=True, return_value=run) as fetch:
            job = self.start("Started", mode="symbols", symbols="HD", fake="")    # no network call
        self.assertEqual(fetch.call_args.args[1], "NYSE")                        # the live fetcher was used
        self.assertFalse(fetch.call_args.args[0].fake)
        self.assertEqual(job.status, JOB_FINISHED)

    def test_run_page_and_resume(self):
        self.state.settings.eod_api_retries = 0
        job = self.start(None, mode="symbols", symbols="HD, JNJ, JPM", fake_faults="JNJ:503x1,JPM:404")
        self.assertEqual((job.run_status, job.failed), (RUN_COMPLETED_ERRORS, 2))
        page = self.client.get(f"/api-load/runs/{job.run_id}").get_data(as_text=True)
        for text in ("completed_with_errors", "failed (2)", "loaded (1)", "503", "404", "Resume"):
            self.assertIn(text, page)
        failed = self.client.get(f"/api-load/runs/{job.run_id[:6]}?status=failed").get_data(as_text=True)
        self.assertIn("JNJ", failed)
        self.assertNotIn(">HD<", failed)
        self.assertEqual(self.client.get("/api-load/runs/nonexistent").status_code, 404)
        runs = self.client.get("/api/api-load/runs").get_json()
        self.assertEqual((runs["count"], runs["records"][0]["Failed"]), (1, 2))
        self.assertEqual(self.client.get("/api/api-load/runs?market=NY SE!").status_code, 400)
        # resume: the fake gateway again (without the simulated failures): JNJ and JPM load now
        r = self.client.post(f"/api-load/runs/{job.run_id}/resume", data={"promote": "on"})
        self.loads.join(60)
        self.assertIn(f"Resuming run {job.run_id}", self.flashes(r))
        resumed = self.loads.current
        self.assertEqual((resumed.run_id, resumed.loaded, resumed.run_status), (job.run_id, 3, RUN_COMPLETED))
        items = {i["Symbol"]: i["Status"] for i in MarketFetcher(self.state.settings, fake=True).run_items(job.run_id)}
        self.assertEqual(items, {"HD": ITEM_LOADED, "JNJ": ITEM_LOADED, "JPM": ITEM_LOADED})
        self.assertNotIn("Resume</button>", self.client.get(f"/api-load/runs/{job.run_id}").get_data(as_text=True))
        self.assertEqual(self.client.post("/api-load/runs/nonexistent/resume").status_code, 404)

    def test_busy_and_stop(self):
        import threading
        gate = threading.Event()
        self.loads._fetcher_factory = lambda s, **kw: MarketFetcher(s, sleep=lambda _: gate.wait(10), **kw)
        self.state.settings.eod_api_retry_backoff = 1.0
        r = self.client.post("/api-load/start", data={"market": "NYSE", "fake": "on", "mode": "symbols",
                                                      "symbols": "HD, JNJ, JPM", "fake_faults": "HD:503x1"})
        try:
            self.assertEqual(r.status_code, 302)
            self.assertTrue(self.client.get("/api/api-load").get_json()["busy"])
            page = self.client.get("/api-load").get_data(as_text=True)
            self.assertIn('id="start-button" disabled', page)
            self.assertIn("an API load is running", self.flashes(self.client.post(
                "/api-load/start", data={"market": "NYSE", "fake": "on", "mode": "symbols", "symbols": "PFE"})))
            r = self.client.post("/api-load/stop")
            self.assertIn("Stopping after the current stock", self.flashes(r))
        finally:
            gate.set()
            self.loads.join(30)
        job = self.loads.current
        self.assertEqual((job.status, job.run_status, job.loaded), (JOB_FINISHED, RUN_INTERRUPTED, 1))
        self.assertIn("No API load is running", self.flashes(self.client.post("/api-load/stop")))

    def test_roles(self):
        self.logout()
        self.login(*VIEWER)
        self.assertEqual(self.client.get("/api-load").status_code, 200)              # viewers watch
        self.assertNotIn("Start the load", self.client.get("/api-load").get_data(as_text=True))
        self.assertEqual(self.client.get("/api/api-load").status_code, 200)
        for url in ("/api-load/start", "/api-load/stop", "/api-load/runs/x/resume"):
            self.assertEqual(self.client.post(url, data={"mode": "symbols", "symbols": "HD"}).status_code, 403, url)
        self.logout()
        self.login(*OPERATOR)
        job = self.start("Started", mode="symbols", symbols="HD")
        self.assertEqual((job.loaded, job.started_by), (1, OPERATOR[0]))
        self.logout()
        self.assertEqual(self.client.get("/api/api-load").status_code, 401)
