"""
The daily limit of the **bulk load of all stocks of a market**: at most
``MAX_MARKET_LOADS_PER_DAY`` (3) whole-market loads per market and day.

Enforced in the back end (``MarketFetcher.fetch_market`` - checked before any API call and again,
atomically, when the run is registered; ``ApiLoadManager.start``; the CLI) and shown in the front end
(the API loading page: today's count, the blocked start button, the notice).  Loads of selected
stocks, text files and resumed runs do not count.

Everything runs against the built-in fake gateway; every token is a dummy.
"""

from __future__ import annotations

import io
import json
import sqlite3
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta

from stock_dataload import cli
from stock_dataload.api_jobs import KIND_MARKET, KIND_SYMBOLS, ApiLoadManager
from stock_dataload.audit import AuditLogger
from stock_dataload.config import save_settings
from stock_dataload.eod_market import (MAX_MARKET_LOADS_PER_DAY, RUN_COMPLETED, RUN_INTERRUPTED, RUN_TABLE,
                                       MarketFetcher, MarketLoadLimitError, MarketRun)

from .helpers import TempProjectTestCase
from .test_api_load import load_list
from .test_dashboard import OPERATOR, VIEWER, DashboardTestCase


def run_count(settings, market: str = "NYSE") -> int:
    with sqlite3.connect(settings.sqlite_path) as conn:
        return conn.execute(f'SELECT COUNT(*) FROM "{RUN_TABLE}" WHERE "Market" = ?', (market,)).fetchone()[0]


class LimitTestCase(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.settings.eod_api_token = "dummy-limit-token"
        self.settings.eod_api_retry_backoff = 0.0
        load_list(self.settings)
        self.audit = AuditLogger(self.settings)

    def fetcher(self, **kwargs) -> MarketFetcher:
        return MarketFetcher(self.settings, audit=self.audit, fake=True, sleep=lambda s: None, **kwargs)

    def whole_market(self, market: str = "NYSE"):
        return self.fetcher().fetch_market(market, limit=2)


class FetcherLimitTests(LimitTestCase):
    def test_the_limit_is_three(self):
        self.assertEqual(MAX_MARKET_LOADS_PER_DAY, 3)

    def test_fourth_whole_market_load_of_the_day_is_refused(self):
        for n in range(1, 4):
            run = self.whole_market()
            self.assertEqual(run.status, RUN_COMPLETED)
            quota = self.fetcher().market_load_quota("NYSE")
            self.assertEqual((quota.used, quota.remaining, quota.exhausted), (n, 3 - n, n == 3))
        fetcher = self.fetcher()
        with self.assertRaises(MarketLoadLimitError) as ctx:
            fetcher.fetch_market("NYSE")
        message = str(ctx.exception)
        self.assertIn("NYSE: the bulk load of all stocks of a market may run at most 3 times a day", message)
        self.assertIn("it already ran 3 time(s) today", message)
        self.assertEqual(ctx.exception.quota.used, 3)
        self.assertEqual(fetcher.client.transport.calls, [])                 # refused before any API call
        self.assertEqual(run_count(self.settings), 3)                        # nothing registered
        refused = [l for l in self.audit.fetch_logs(limit=20, component="eod_market") if l.event == "market_run_refused"]
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0].level, "WARNING")

    def test_what_does_not_count(self):
        for _ in range(3):
            self.whole_market()
        # selected stocks are no bulk load of the market
        run = self.fetcher().fetch_market("NYSE", symbols=["HD", "PFE"])
        self.assertEqual(run.status, RUN_COMPLETED)
        self.assertEqual(self.fetcher().market_load_quota("NYSE").used, 3)
        # another market has its own limit
        self.fetcher().market_load_quota("NASDAQ")
        self.assertFalse(self.fetcher().market_load_quota("NASDAQ").exhausted)
        # a run of yesterday does not count for today
        with sqlite3.connect(self.settings.sqlite_path) as conn:
            yesterday = (datetime.now() - timedelta(days=1)).replace(microsecond=0).isoformat(sep=" ")
            conn.execute(f'UPDATE "{RUN_TABLE}" SET "StartedAt" = ? WHERE "RunId" = '
                         f'(SELECT "RunId" FROM "{RUN_TABLE}" WHERE "Parameters" LIKE \'%"symbols_given": false%\' '
                         f'ORDER BY "StartedAt" LIMIT 1)', (yesterday,))
        self.assertEqual(self.fetcher().market_load_quota("NYSE").used, 2)
        self.assertEqual(self.whole_market().status, RUN_COMPLETED)

    def test_an_interrupted_run_can_be_resumed_after_the_limit(self):
        self.whole_market()
        self.whole_market()
        stopped = self.fetcher().fetch_market("NYSE", should_stop=lambda: True)
        self.assertEqual(stopped.status, RUN_INTERRUPTED)
        with self.assertRaises(MarketLoadLimitError):
            self.whole_market()
        resumed = self.fetcher().fetch_market("NYSE", resume=stopped.run_id)
        self.assertEqual((resumed.run_id, resumed.status), (stopped.run_id, RUN_COMPLETED))
        self.assertEqual(self.fetcher().market_load_quota("NYSE").used, 3)

    def test_the_registration_checks_again(self):
        """Two processes passed the first check at the same time: the second registration is refused."""
        for _ in range(3):
            self.whole_market()
        fetcher = self.fetcher()
        late = MarketRun("dummyrun0001", "NYSE", "fake", ["HD"], {"symbols_given": False})
        with self.assertRaises(MarketLoadLimitError):
            fetcher._register_market_run(late)
        self.assertIsNone(fetcher.find_run("dummyrun0001"))

    def test_quotas_for_the_dashboard(self):
        self.whole_market()
        quotas = self.fetcher().market_load_quotas(["LSE"])
        self.assertEqual(sorted(quotas), ["LSE", "NYSE"])
        self.assertEqual(quotas["NYSE"].as_dict()["used"], 1)
        self.assertEqual(quotas["LSE"].remaining, 3)


class ManagerLimitTests(LimitTestCase):
    def test_the_manager_refuses_at_once(self):
        manager = ApiLoadManager(self.settings, audit_factory=AuditLogger,
                                 fetcher_factory=lambda s, **kw: MarketFetcher(s, sleep=lambda x: None, **kw))
        for _ in range(3):
            manager.start(KIND_MARKET, "NYSE", "market", "dummy_operator", limit=1, fake=True, background=False)
        with self.assertRaises(MarketLoadLimitError):
            manager.start(KIND_MARKET, "NYSE", "market", "dummy_operator", fake=True, background=False)
        self.assertEqual(len(manager.jobs()), 3)                               # no job was created
        refused = [l for l in self.audit.fetch_logs(limit=20, component="eod_market") if l.event == "market_run_refused"]
        self.assertEqual(json.loads(refused[0].details)["requested_by"], "dummy_operator")
        job = manager.start(KIND_SYMBOLS, "NYSE", "two stocks", "dummy_operator", symbols=["HD", "JNJ"], fake=True,
                            background=False)
        self.assertEqual(job.run_status, RUN_COMPLETED)
        self.assertFalse(manager.busy)


class DashboardLimitTests(DashboardTestCase):
    def setUp(self):
        super().setUp()
        load_list(self.state.settings)
        self.state.settings.eod_api_retry_backoff = 0.0
        self.loads: ApiLoadManager = self.app.extensions["api_loads"]

    def start_market(self):
        r = self.client.post("/api-load/start", data={"mode": "market", "market": "NYSE", "limit": "1", "fake": "on",
                                                      "promote": ""}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302)
        self.loads.join(60)
        return r

    def test_the_page_shows_the_count_and_the_fourth_load_is_refused(self):
        page = self.client.get("/api-load").get_data(as_text=True)
        self.assertIn("Whole-market loads today", page)
        self.assertIn("at most <b>3</b> whole-market loads per market and day", page)
        self.assertIn('id="quota-block" class="flash error" role="alert" hidden', page)
        for n in range(1, 4):
            r = self.start_market()
            self.assertIn("Started: NYSE", self.flashes(r))
            quota = self.client.get("/api/api-load/quota?market=nyse").get_json()
            self.assertEqual((quota["market"], quota["used"], quota["remaining"]), ("NYSE", n, 3 - n))
        page = self.client.get("/api-load").get_data(as_text=True)
        self.assertIn('<span class="badge err">NYSE 3/3</span>', page)
        self.assertIn("data-quotas=", page)
        self.assertIn('data-limit="3"', page)
        r = self.start_market()
        page = self.client.get(r.headers["Location"]).get_data(as_text=True)
        notice = "\n".join(self.flash_messages(page))
        self.assertIn("Not started - NYSE: the bulk load of all stocks of a market may run at most 3 times a day", notice)
        self.assertEqual(run_count(self.state.settings), 3)
        # the form is kept (whole market), and with it the notice next to the start button
        self.assertIn('id="quota-block" class="flash error" role="alert">', page)
        self.assertIn("Daily limit reached.", page)
        # selected stocks still load
        r = self.client.post("/api-load/start", data={"mode": "symbols", "market": "NYSE", "symbols": "HD, PFE",
                                                      "fake": "on", "promote": ""})
        self.loads.join(60)
        self.assertIn("Started: 2 selected stock(s) (NYSE)", self.flashes(r))
        self.assertEqual(self.client.get("/api/api-load/quota?market=NY SE").status_code, 400)

    def test_viewers_see_the_quota_but_cannot_start(self):
        self.logout()
        self.login(*VIEWER)
        self.assertEqual(self.client.get("/api/api-load/quota?market=NYSE").status_code, 200)
        self.assertEqual(self.client.post("/api-load/start", data={"mode": "market"}).status_code, 403)
        self.logout()
        self.login(*OPERATOR)
        self.assertIn("Started: NYSE", self.flashes(self.start_market()))


class CliLimitTests(LimitTestCase):
    def setUp(self):
        super().setUp()
        self.settings.config_file = self.root / "config.ini"
        save_settings(self.settings, self.settings.config_file)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["--config", str(self.settings.config_file), "-q", *args])
        return rc, out.getvalue(), err.getvalue()

    def test_eod_fetch_market_refuses_the_fourth_load(self):
        for _ in range(3):
            rc, out, err = self.run_cli("eod-fetch-market", "NYSE", "--fake", "--limit", "1", "--summary-only")
            self.assertEqual(rc, 0, err)
        rc, out, err = self.run_cli("eod-fetch-market", "NYSE", "--fake", "--summary-only")
        self.assertEqual(rc, 1)
        self.assertIn("REFUSED: NYSE: the bulk load of all stocks of a market may run at most 3 times a day", err)
        rc, out, err = self.run_cli("eod-fetch-market", "NYSE", "--fake", "--symbols", "HD", "--summary-only")
        self.assertEqual(rc, 0, err)
        rc, out, _ = self.run_cli("eod-market-runs", "NYSE")
        self.assertIn("whole-market loads today (max per market and day): NYSE 3/3", out)


if __name__ == "__main__":
    unittest.main()
