"""
The daily limits of ``load_limits.py``:

* a stock is loaded into ``Stock_EOD_Stage`` at most 5 times a day (``eod-fetch``, ``eod-load``,
  ``eod-fetch-market``, the dashboard) - refused in the load transaction, not called by runs
  (skipped with a warning), refused before the API call by ``eod-fetch``;
* a text file lists at most 100 stock symbols;
* loads from a text file run at most 5 times a day;

with the warnings and errors of the back end, the dashboard and the command line.  Everything runs
against the built-in fake gateway; every token and account is a dummy.
"""

from __future__ import annotations

import io
import json
import sqlite3
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from io import BytesIO

from stock_dataload import cli
from stock_dataload.api_jobs import KIND_FILE, KIND_SYMBOLS, ApiLoadManager
from stock_dataload.audit import AuditLogger
from stock_dataload.config import save_settings
from stock_dataload.db import StageDatabase
from stock_dataload.eod_api import SAMPLE_RESPONSE, EodApiClient, FakeEodGateway
from stock_dataload.eod_market import ITEM_LOADED, ITEM_SKIPPED, RUN_COMPLETED, RUN_TABLE, MarketFetcher
from stock_dataload.eod_stage import EodStageLoader
from stock_dataload.load_limits import (MAX_FILE_LOADS_PER_DAY, MAX_FILE_SYMBOLS, MAX_LOADS_PER_STOCK_PER_DAY,
                                        STOCK_LOAD_TABLE, FileLoadLimitError, StockLoadLedger, StockLoadLimitError,
                                        TooManySymbolsError, render_ddl, ticker_of)

from .helpers import PROJECT_ROOT, TempProjectTestCase
from .test_api_load import load_list
from .test_dashboard import DashboardTestCase

RECORDS = json.loads(SAMPLE_RESPONSE.read_text(encoding="utf-8"))[:5]


def seed_loads(settings, ticker: str, n: int, when: datetime | None = None) -> None:
    """*n* loads of *ticker* in the ledger (as if it had been loaded n times)."""
    db = StageDatabase(settings)
    moment = (when or datetime.now()).replace(microsecond=0).isoformat(sep=" ")
    with db.connection() as conn:
        for i in range(n):
            StockLoadLedger(db).record(conn, ticker, f"dummy-load-{ticker}-{i}", "test/seed", 1, moment)


def events(settings, name: str) -> list:
    return [l for l in AuditLogger(settings).fetch_logs(limit=200) if l.event == name]


def app_errors(settings) -> int:
    with sqlite3.connect(settings.sqlite_path) as conn:
        try:
            return conn.execute('SELECT COUNT(*) FROM "App_Error"').fetchone()[0]
        except sqlite3.OperationalError:
            return 0


class LimitsTestCase(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.settings.eod_api_token = "dummy-limits-token"
        self.settings.eod_api_retry_backoff = 0.0
        self.audit = AuditLogger(self.settings)

    def fetcher(self, **kwargs) -> MarketFetcher:
        return MarketFetcher(self.settings, audit=self.audit, fake=True, sleep=lambda s: None, **kwargs)

    def runs(self) -> int:
        with sqlite3.connect(self.settings.sqlite_path) as conn:
            try:
                return conn.execute(f'SELECT COUNT(*) FROM "{RUN_TABLE}"').fetchone()[0]
            except sqlite3.OperationalError:
                return 0


class RuleTests(unittest.TestCase):
    def test_the_limits(self):
        self.assertEqual((MAX_LOADS_PER_STOCK_PER_DAY, MAX_FILE_SYMBOLS, MAX_FILE_LOADS_PER_DAY), (5, 100, 5))
        self.assertEqual((ticker_of("mrk.us"), ticker_of("BRK-B.US"), ticker_of("^GSPC"), ticker_of("HD")),
                         ("MRK", "BRK-B", "^GSPC", "HD"))

    def test_reference_script(self):
        script = (PROJECT_ROOT / "sql" / "create_eod_stock_load_sqlite.sql").read_text(encoding="utf-8")
        self.assertTrue(script.endswith(render_ddl()))


class StageLimitTests(LimitsTestCase):
    def load(self, symbol: str = "MRK", records=RECORDS, loader=None):
        loader = loader or EodStageLoader(self.settings, audit=self.audit)
        return loader.load_text(symbol, json.dumps(records), source_file=f"test/{symbol}")

    def test_the_sixth_load_of_a_stock_is_refused(self):
        for n in range(MAX_LOADS_PER_STOCK_PER_DAY):
            self.assertTrue(self.load("MRK.US" if n == 2 else "MRK").ok)              # MRK.US is the same stock
        changed = [dict(r, close=r["close"] + 1) for r in RECORDS]
        result = self.load("MRK", changed)
        self.assertTrue(result.refused)
        self.assertFalse(result.ok)
        self.assertIn("MRK was already loaded 5 times today - a stock may be loaded at most 5 times a day", result.message)
        self.assertIn("(refused)", str(result))
        with sqlite3.connect(self.settings.sqlite_path) as conn:                      # nothing written
            closes = {r[0] for r in conn.execute('SELECT "Close" FROM "Stock_EOD_Stage" WHERE "Symbol" = ?', ("MRK",))}
        self.assertEqual(closes, {r["close"] for r in RECORDS})
        refused = events(self.settings, "stock_load_refused")
        self.assertEqual((len(refused), refused[0].level), (1, "WARNING"))
        self.assertEqual(app_errors(self.settings), 0)                               # a refusal is no error
        self.assertTrue(self.load("PFE").ok)                                          # other stocks are fine

    def test_failed_loads_and_yesterday_do_not_count(self):
        seed_loads(self.settings, "MRK", 5, datetime.now() - timedelta(days=1))
        bad = [dict(RECORDS[0], volume="lots")]
        for _ in range(7):
            self.assertFalse(self.load("MRK", bad).ok)
        self.assertEqual(StockLoadLedger(StageDatabase(self.settings)).count("MRK"), 0)
        self.assertTrue(self.load("MRK").ok)


class FetchLimitTests(LimitsTestCase):
    def test_eod_fetch_is_refused_before_the_api_call(self):
        seed_loads(self.settings, "MRK", 5)
        gateway = FakeEodGateway()
        client = EodApiClient(self.settings, transport=gateway, audit=self.audit)
        with self.assertRaises(StockLoadLimitError) as ctx:
            client.fetch("MRK")
        self.assertEqual(gateway.calls, [])
        self.assertEqual(ctx.exception.tickers, ["MRK"])
        self.assertIsNotNone(client.fetch("MRK", load=False).path)                     # only saving is no load
        outcomes = client.fetch_many(["MRK", "PFE"])
        self.assertIsInstance(outcomes[0][2], StockLoadLimitError)
        self.assertTrue(outcomes[1][1].stage.ok)


class RunLimitTests(LimitsTestCase):
    def setUp(self):
        super().setUp()
        load_list(self.settings)

    def test_stocks_at_their_limit_are_skipped_with_a_warning(self):
        seed_loads(self.settings, "HD", 5)
        fetcher = self.fetcher()
        run = fetcher.fetch_market("NYSE", symbols=["HD", "JNJ", "PFE"])
        self.assertEqual(run.status, RUN_COMPLETED)
        hd = run.outcomes["HD"]
        self.assertEqual((hd.status, hd.limited, hd.attempts), (ITEM_SKIPPED, True, 0))
        self.assertIn("daily limit: HD was already loaded 5 times today", hd.error)
        self.assertEqual(run.outcomes["JNJ"].status, ITEM_LOADED)
        self.assertNotIn("/HD?", " ".join(fetcher.client.transport.calls))           # HD was not called
        self.assertIn("1 at the daily limit of 5 loads per stock", run.summary())
        skipped = events(self.settings, "stock_load_skipped")
        self.assertEqual(len(skipped), 1)
        self.assertIn("HD", skipped[0].message)

    def test_a_stock_reaches_its_limit_during_the_day(self):
        seed_loads(self.settings, "JNJ", 4)
        self.assertEqual(self.fetcher().fetch_market("NYSE", symbols=["JNJ"]).outcomes["JNJ"].status, ITEM_LOADED)
        with self.assertRaisesRegex(StockLoadLimitError, "nothing to load: JNJ was already loaded 5 times today"):
            self.fetcher().fetch_market("NYSE", symbols=["JNJ"])

    def test_a_run_whose_stocks_are_all_at_their_limit_is_refused(self):
        for t in ("HD", "JNJ", "JPM", "MRK", "PFE"):
            seed_loads(self.settings, t, 5)
        with self.assertRaisesRegex(StockLoadLimitError, "nothing to load: HD, JNJ were already loaded 5 times"):
            self.fetcher().fetch_market("NYSE", symbols=["HD", "JNJ"])
        with self.assertRaises(StockLoadLimitError):                                 # the whole market too ...
            self.fetcher().fetch_market("NYSE")
        self.assertEqual(self.runs(), 0)                                              # ... nothing registered,
        self.assertEqual(self.fetcher().market_load_quota("NYSE").used, 0)            # no market load used

    def test_resume_tries_the_limited_stocks_again(self):
        seed_loads(self.settings, "HD", 5)
        run = self.fetcher().fetch_market("NYSE", symbols=["HD", "PFE"])
        self.assertEqual(run.outcomes["HD"].status, ITEM_SKIPPED)
        with sqlite3.connect(self.settings.sqlite_path) as conn:                      # "the next day"
            conn.execute(f'DELETE FROM "{STOCK_LOAD_TABLE}" WHERE "Ticker" = ?', ("HD",))
        resumed = self.fetcher().fetch_market("NYSE", resume=run.run_id)
        self.assertEqual(resumed.outcomes["HD"].status, ITEM_LOADED)


class FileLimitTests(LimitsTestCase):
    def setUp(self):
        super().setUp()
        load_list(self.settings)

    def test_at_most_100_symbols(self):
        symbols = [f"S{i:03d}" for i in range(101)]
        with self.assertRaisesRegex(TooManySymbolsError, "lists 101 stock symbols - a text file may list at most 100"):
            self.fetcher().fetch_market("NYSE", symbols=symbols, source="file")
        self.assertEqual(self.runs(), 0)
        self.assertEqual(len(events(self.settings, "file_too_many_symbols")), 1)
        run = self.fetcher().fetch_market("NYSE", symbols=symbols, source="symbols", limit=1)   # typed in: no such rule
        self.assertEqual(run.status, RUN_COMPLETED)

    def test_at_most_5_file_loads_a_day(self):
        for n in range(1, 6):
            self.fetcher().fetch_market("NYSE", symbols=["HD"] if n % 2 else ["PFE"], source="file")
            self.assertEqual(self.fetcher().file_load_quota().used, n)
        with self.assertRaises(FileLoadLimitError) as ctx:
            self.fetcher().fetch_market("NYSE", symbols=["JNJ"], source="file")
        self.assertIn("loading from a text file may run at most 5 times a day - it already ran 5 time(s) today",
                      str(ctx.exception))
        self.assertEqual(self.runs(), 5)
        self.assertEqual(self.fetcher().fetch_market("NYSE", symbols=["JNJ"]).status, RUN_COMPLETED)   # typed in: ok
        self.assertEqual(self.fetcher().market_load_quota("NYSE").used, 0)            # file loads are no market loads
        self.assertEqual(len(events(self.settings, "file_load_refused")), 1)

    def test_bad_source_arguments(self):
        from stock_dataload.eod_market import EodMarketError
        with self.assertRaises(EodMarketError):
            self.fetcher().fetch_market("NYSE", source="telepathy")
        with self.assertRaises(EodMarketError):
            self.fetcher().fetch_market("NYSE", source="file")                        # a file needs its symbols


class ManagerLimitTests(LimitsTestCase):
    def setUp(self):
        super().setUp()
        load_list(self.settings)
        self.manager = ApiLoadManager(self.settings, audit_factory=AuditLogger,
                                      fetcher_factory=lambda s, **kw: MarketFetcher(s, sleep=lambda x: None, **kw))

    def start(self, kind, symbols, **kw):
        return self.manager.start(kind, "NYSE", "dummy list.txt", "dummy_operator", symbols=symbols, fake=True,
                                  background=False, **kw)

    def test_file_rules_before_a_job_exists(self):
        with self.assertRaises(TooManySymbolsError):
            self.start(KIND_FILE, [f"S{i:03d}" for i in range(101)])
        for _ in range(5):
            self.start(KIND_FILE, ["HD"])
        with self.assertRaises(FileLoadLimitError):
            self.start(KIND_FILE, ["HD"])
        self.assertEqual(len(self.manager.jobs()), 5)

    def test_stocks_at_their_limit(self):
        seed_loads(self.settings, "HD", 5)
        job = self.start(KIND_SYMBOLS, ["HD", "PFE"])
        self.assertEqual(job.at_limit, ["HD"])
        self.assertEqual(job.as_dict()["at_limit"], ["HD"])
        lines = [(l["level"], l["text"]) for l in job.log]
        self.assertTrue(any(level == "warning" and "will be skipped (daily limit per stock): HD" in text
                            for level, text in lines), lines)
        self.assertTrue(any(level == "warning" and text.startswith("HD: SKIPPED") for level, text in lines), lines)
        seed_loads(self.settings, "PFE", 5)
        with self.assertRaises(StockLoadLimitError):
            self.start(KIND_SYMBOLS, ["HD", "PFE"])


class DashboardLimitTests(DashboardTestCase):
    def setUp(self):
        super().setUp()
        load_list(self.state.settings)
        self.state.settings.eod_api_retry_backoff = 0.0
        self.loads: ApiLoadManager = self.app.extensions["api_loads"]

    def post_file(self, text: str, name: str = "dummy stocks.txt"):
        r = self.client.post("/api-load/start", data={"market": "NYSE", "fake": "on", "mode": "file",
                                                      "file": (BytesIO(text.encode("utf-8")), name)},
                             content_type="multipart/form-data")
        self.loads.join(60)
        return r

    def test_the_page_shows_the_limits(self):
        seed_loads(self.state.settings, "HD", 5)
        page = self.client.get("/api-load").get_data(as_text=True)
        self.assertIn("Text-file loads today:", page)
        self.assertIn("Stocks at their daily limit (5 loads):", page)
        self.assertIn("at most <b>100</b> stock symbols per file and", page)
        self.assertIn('data-stocks-at-limit=\'{"HD": 5}\'', page)
        self.assertIn('data-max-file-symbols="100"', page)
        self.assertIn('id="file-block" class="flash error" role="alert" hidden', page)
        limits = self.client.get("/api/api-load/limits?symbols=hd,PFE&market=NYSE").get_json()
        self.assertEqual(limits["stocks"]["HD"], {"used": 5, "remaining": 0, "exhausted": True})
        self.assertEqual(limits["stocks"]["PFE"]["used"], 0)
        self.assertEqual((limits["stock_limit"], limits["max_file_symbols"], limits["file_loads"]["limit"]), (5, 100, 5))
        self.assertEqual(limits["market"]["market"], "NYSE")
        self.assertEqual(list(self.client.get("/api/api-load/limits").get_json()["stocks"]), ["HD"])
        self.assertEqual(self.client.get("/api/api-load/limits?market=NY SE").status_code, 400)

    def test_a_file_with_too_many_symbols_is_refused(self):
        r = self.post_file(", ".join(f"S{i:03d}" for i in range(120)), "big list.txt")
        self.assertIn("Not started - big list.txt lists 120 stock symbols - a text file may list at most 100",
                      self.flashes(r))
        self.assertIsNone(self.loads.current)
        self.assertEqual(len(events(self.state.settings, "file_too_many_symbols")), 1)

    def test_the_sixth_file_load_of_the_day_is_refused(self):
        for n in range(1, 6):
            r = self.post_file("HD" if n % 2 else "PFE")
            self.assertIn(f"text-file load {n} of 5 today", self.flashes(r))
        r = self.post_file("JNJ")
        self.assertIn("Not started - loading from a text file may run at most 5 times a day - it already ran 5 time(s)",
                      self.flashes(r))
        page = self.client.get("/api-load").get_data(as_text=True)
        self.assertIn('<span class="badge err">5/5</span>', page)

    def test_stocks_at_their_limit_warn_or_refuse(self):
        seed_loads(self.state.settings, "HD", 5)
        r = self.client.post("/api-load/start", data={"market": "NYSE", "fake": "on", "mode": "symbols",
                                                      "symbols": "HD, PFE", "promote": ""})
        self.loads.join(60)
        page = self.client.get(r.headers["Location"]).get_data(as_text=True)
        self.assertIn('class="flash warning">Already loaded 5 times today - 1 stock(s) will be skipped '
                      '(daily limit per stock): HD</div>', page)
        self.assertIn("Started: 2 selected stock(s) (NYSE)", page)
        seed_loads(self.state.settings, "PFE", 5)
        r = self.client.post("/api-load/start", data={"market": "NYSE", "fake": "on", "mode": "symbols",
                                                      "symbols": "HD, PFE"})
        self.assertIn("Not started - nothing to load: HD, PFE were already loaded 5 times today", self.flashes(r))


class CliLimitTests(LimitsTestCase):
    def setUp(self):
        super().setUp()
        load_list(self.settings)
        self.settings.config_file = self.root / "config.ini"
        save_settings(self.settings, self.settings.config_file)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["--config", str(self.settings.config_file), "-q", *args])
        return rc, out.getvalue(), err.getvalue()

    def test_eod_fetch_and_eod_load(self):
        seed_loads(self.settings, "MRK", 5)
        rc, _, err = self.run_cli("eod-fetch", "MRK", "--fake")
        self.assertEqual(rc, 1)
        self.assertIn("REFUSED MRK: MRK was already loaded 5 times today", err)
        rc, _, err = self.run_cli("eod-load", str(SAMPLE_RESPONSE), "--symbol", "MRK")
        self.assertEqual(rc, 1)
        self.assertIn("REFUSED: MRK:", err)
        self.assertEqual(app_errors(self.settings), 0)

    def test_eod_fetch_market_with_a_symbols_file(self):
        big = self.root / "big.txt"
        big.write_text(", ".join(f"S{i:03d}" for i in range(101)), encoding="utf-8")
        rc, _, err = self.run_cli("eod-fetch-market", "NYSE", "--fake", "--symbols-file", str(big))
        self.assertEqual(rc, 1)
        self.assertIn("REFUSED: big.txt lists 101 stock symbols", err)
        small = self.root / "small.txt"
        small.write_text("HD, PFE, BRK/B", encoding="utf-8")
        seed_loads(self.settings, "PFE", 5)
        for _ in range(5):
            rc, out, err = self.run_cli("eod-fetch-market", "NYSE", "--fake", "--symbols-file", str(small),
                                        "--summary-only")
            self.assertEqual(rc, 0, err)
        self.assertIn("WARNING: not a symbol, left out: BRK/B", err)
        self.assertIn("WARNING PFE: SKIPPED", err)
        self.assertIn("WARNING: 1 stock(s) skipped - already loaded the maximum number of times today", err)
        rc, _, err = self.run_cli("eod-fetch-market", "NYSE", "--fake", "--symbols-file", str(small))
        self.assertEqual(rc, 1)
        self.assertIn("REFUSED: loading from a text file may run at most 5 times a day", err)
        rc, _, err = self.run_cli("eod-fetch-market", "NYSE", "--symbols", "HD", "--symbols-file", str(small))
        self.assertEqual(rc, 2)
        rc, out, _ = self.run_cli("eod-limits", "HD", "PFE", "--market", "NYSE")
        self.assertEqual(rc, 0)
        self.assertIn("text-file loads today: 5/5", out)
        self.assertIn("whole-market loads today: NYSE 0/3", out)
        self.assertIn("HD           5/5  LIMIT REACHED", out)                      # 5 file runs loaded HD
        self.assertIn("PFE          5/5  LIMIT REACHED", out)
        rc, out, _ = self.run_cli("eod-market-runs", "NYSE")
        self.assertIn("text-file loads today (max per day): 5/5", out)


if __name__ == "__main__":
    unittest.main()
