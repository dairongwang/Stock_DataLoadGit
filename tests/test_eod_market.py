"""
Whole markets from the EOD API (``eod_market.py``, ``eod-fetch-market``): the symbol list, one
call per stock loaded straight into the staging table, retries, skipped / failed / empty stocks,
stopping a run, the run registry and resume, promotion - all against the fake gateway with
injected faults, and the live transport against a mocked ``urlopen``.

No network; every token is a dummy.
"""

from __future__ import annotations

import io
import json
import sqlite3
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from stock_dataload import cli
from stock_dataload.config import save_settings
from stock_dataload.eod_api import (SAMPLE_RESPONSE, EodApiClient, EodApiError, FakeEodGateway, FaultPlan,
                                    UrllibGetter)
from stock_dataload.eod_market import (ITEM_FAILED, ITEM_LOADED, ITEM_NO_DATA, ITEM_PENDING, ITEM_SKIPPED, RUN_ABORTED,
                                       RUN_COMPLETED, RUN_COMPLETED_ERRORS, RUN_INTERRUPTED, EodMarketError,
                                       MarketFetcher, render_ddl)
from stock_dataload.eod_symbols import SymbolListLoader

from .helpers import PROJECT_ROOT, TempProjectTestCase

SAMPLE = json.loads(SAMPLE_RESPONSE.read_text(encoding="utf-8"))


class FakeGatewayTests(unittest.TestCase):
    def get(self, gateway, symbol):
        return gateway.get(f"https://eodhd.com/api/eod/{symbol}?api_token=dummy&fmt=json", 5)

    def test_fault_plan(self):
        plan = FaultPlan.parse("mrk:503x2, PFE:404, HD:timeout, *:429x1, rate=0.25, seed=3, retry_after=2")
        self.assertEqual(plan.rules, {"MRK": ["503", 2], "PFE": ["404", None], "HD": ["timeout", 1], "*": ["429", 1]})
        self.assertEqual((plan.rate, plan.seed, plan.retry_after), (0.25, 3, 2.0))
        self.assertEqual([plan.next_fault("MRK") for _ in range(3)][:2], ["503", "503"])
        self.assertEqual([plan.next_fault("PFE") for _ in range(3)], ["404"] * 3)        # permanent
        for bad in ("MRK", "MRK:teapot", "rate=2", "speed=3", ":503"):
            with self.assertRaises(EodApiError, msg=bad):
                FaultPlan.parse(bad)
        # the random faults are reproducible
        a, b = FaultPlan.parse("rate=0.5,seed=1"), FaultPlan.parse("rate=0.5,seed=1")
        self.assertEqual([a.next_fault("X") for _ in range(20)], [b.next_fault("X") for _ in range(20)])

    def test_prices_per_symbol(self):
        gateway = FakeEodGateway(vary_by_symbol=True)
        self.assertEqual(json.loads(self.get(gateway, "MRK").body), SAMPLE)                # MRK: the sample itself
        hd, again = json.loads(self.get(gateway, "HD").body), json.loads(self.get(gateway, "HD").body)
        self.assertEqual(hd, again)                                                           # deterministic
        self.assertNotEqual(hd[-1]["close"], SAMPLE[-1]["close"])
        for r in hd:
            self.assertTrue(r["low"] <= min(r["open"], r["close"]) <= max(r["open"], r["close"]) <= r["high"], r)
        self.assertEqual(json.loads(self.get(FakeEodGateway(), "HD").body), SAMPLE)         # default: unchanged

    def test_faults(self):
        gateway = FakeEodGateway(faults="A:503,B:429,C:timeout,D:badjson,E:truncated,F:empty,G:badrecord,retry_after=7")
        with self.assertRaises(EodApiError) as ctx:
            self.get(gateway, "A")
        self.assertEqual(ctx.exception.status, 503)
        with self.assertRaises(EodApiError) as ctx:
            self.get(gateway, "B")
        self.assertEqual((ctx.exception.status, ctx.exception.retry_after), (429, 7.0))
        with self.assertRaises(EodApiError) as ctx:
            self.get(gateway, "C")
        self.assertIsNone(ctx.exception.status)
        self.assertIn("timed out", str(ctx.exception))
        for symbol in ("D", "E"):
            with self.assertRaises(ValueError):
                json.loads(self.get(gateway, symbol).body)
        self.assertEqual(self.get(gateway, "F").body, b"[]")
        bad = json.loads(self.get(gateway, "G").body)[1]
        self.assertEqual(bad["volume"], "lots")
        self.assertEqual(len(json.loads(self.get(gateway, "A").body)), len(SAMPLE))         # transient: once


class MarketTestCase(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.settings.eod_api_retry_backoff = 1.0
        self.waits: list[float] = []
        loaded = SymbolListLoader(self.settings).load_text("NYSE", json.dumps([
            {"Code": c, "Name": n, "Country": "USA", "Exchange": "NYSE", "Currency": "USD", "Type": t, "Isin": None}
            for c, n, t in (("HD", "Home Depot", "Common Stock"), ("JNJ", "Johnson & Johnson", "Common Stock"),
                            ("JPM", "JPMorgan", "Common Stock"), ("MRK", "Merck", "Common Stock"),
                            ("PFE", "Pfizer", "Common Stock"), ("SPY", "SPDR ETF", "ETF"),
                            ("BRK/B", "Not usable in a URL", "Common Stock"))]), "json", source_file="test-list.json")
        self.assertTrue(loaded.ok, loaded.message)

    def fetcher(self, faults: str | None = None, **kwargs) -> MarketFetcher:
        return MarketFetcher(self.settings, sleep=self.waits.append, fake=True, fake_faults=faults, **kwargs)

    def staged(self) -> dict[str, int]:
        with sqlite3.connect(self.settings.sqlite_path) as conn:
            return dict(conn.execute('SELECT "Symbol", COUNT(*) FROM "Stock_EOD_Stage" GROUP BY 1').fetchall())


class MarketFetchTests(MarketTestCase):
    def test_every_common_stock_loaded_straight_into_the_stage_table(self):
        run = self.fetcher().fetch_market("nyse")
        self.assertEqual(run.symbols, ["BRK/B", "HD", "JNJ", "JPM", "MRK", "PFE"])           # no ETF
        self.assertEqual((run.status, run.loaded, run.count(ITEM_SKIPPED)), (RUN_COMPLETED, 5, 1))
        self.assertEqual(run.outcomes["BRK/B"].status, ITEM_SKIPPED)                        # no valid URL symbol
        self.assertIn("not a valid symbol", run.outcomes["BRK/B"].error)
        self.assertEqual(set(self.staged()), {"HD", "JNJ", "JPM", "MRK", "PFE"})
        self.assertEqual(self.staged()["MRK"], len(SAMPLE))
        self.assertEqual(list(self.settings.eod_download_dir.iterdir()), [])                # no files
        with sqlite3.connect(self.settings.sqlite_path) as conn:
            sources = {r[0] for r in conn.execute('SELECT DISTINCT "SourceFile" FROM "Stock_EOD_Stage"')}
            history = conn.execute('SELECT COUNT(*) FROM "Load_History" WHERE "FileName" LIKE ?',
                                   ("fake-api/NYSE/%",)).fetchone()[0]
        self.assertEqual(sources, {f"fake-api/NYSE/{s}" for s in ("HD", "JNJ", "JPM", "MRK", "PFE")})
        self.assertEqual(history, 5)
        # a second run: the same data is recognised as unchanged
        again = self.fetcher().fetch_market("NYSE", types=["Common Stock", "ETF"])
        self.assertIn("SPY", again.symbols)
        self.assertEqual(again.outcomes["MRK"].unchanged, len(SAMPLE))

    def test_retries_and_what_is_not_retried(self):
        fetcher = self.fetcher("HD:503x2,JNJ:timeoutx3,JPM:404,MRK:503x4,PFE:badjsonx1,retry_after=0")
        run = fetcher.fetch_market("NYSE", symbols=["HD", "JNJ", "JPM", "MRK", "PFE"])
        o = run.outcomes
        self.assertEqual((o["HD"].status, o["HD"].attempts), (ITEM_LOADED, 3))
        self.assertEqual((o["JNJ"].status, o["JNJ"].attempts), (ITEM_LOADED, 4))               # 1 + 3 retries
        self.assertEqual((o["JPM"].status, o["JPM"].attempts, o["JPM"].http_status), (ITEM_FAILED, 1, 404))
        self.assertEqual((o["MRK"].status, o["MRK"].attempts, o["MRK"].http_status), (ITEM_FAILED, 4, 503))
        self.assertEqual((o["PFE"].status, o["PFE"].attempts), (ITEM_LOADED, 2))
        self.assertEqual(run.api_calls, 3 + 4 + 1 + 4 + 2)
        self.assertEqual(run.status, RUN_COMPLETED_ERRORS)
        # exponential backoff 1, 2, 4 s (the 429's Retry-After would win)
        self.assertEqual(self.waits[:2], [1.0, 2.0])
        self.assertEqual(self.waits[2:5], [1.0, 2.0, 4.0])

    def test_retry_after_and_no_retries(self):
        run = self.fetcher("HD:429x1,retry_after=9").fetch_market("NYSE", symbols=["HD"])
        self.assertEqual((run.outcomes["HD"].attempts, self.waits), (2, [9.0]))
        run = self.fetcher("JNJ:503x1").fetch_market("NYSE", symbols=["JNJ"], retries=0)
        self.assertEqual((run.outcomes["JNJ"].status, run.outcomes["JNJ"].attempts), (ITEM_FAILED, 1))

    def test_empty_answers_and_invalid_records(self):
        run = self.fetcher("HD:empty,JNJ:badrecord").fetch_market("NYSE", symbols=["HD", "JNJ"])
        self.assertEqual(run.outcomes["HD"].status, ITEM_NO_DATA)
        self.assertEqual((run.outcomes["JNJ"].status, run.outcomes["JNJ"].attempts), (ITEM_FAILED, 1))   # no retry
        self.assertIn("invalid", run.outcomes["JNJ"].error)
        run = self.fetcher("JNJ:badrecord").fetch_market("NYSE", symbols=["JNJ"], skip_bad_rows=True)
        self.assertEqual((run.outcomes["JNJ"].status, run.outcomes["JNJ"].received, run.outcomes["JNJ"].rejected),
                         (ITEM_LOADED, 3, 1))

    def test_a_refused_token_stops_the_run(self):
        run = self.fetcher("*:401").fetch_market("NYSE", symbols=["HD", "JNJ", "JPM"])
        self.assertEqual((run.status, run.api_calls, run.count(ITEM_PENDING)), (RUN_ABORTED, 1, 3))
        self.assertIn("401", run.message)
        run = self.fetcher("*:402").fetch_market("NYSE", symbols=["HD", "JNJ"])
        self.assertEqual((run.status, run.api_calls), (RUN_ABORTED, 1))                       # quota used up

    def test_too_many_failures_in_a_row_stop_the_run(self):
        self.settings.eod_max_consecutive_failures = 2
        run = self.fetcher("*:503x100").fetch_market("NYSE", symbols=["HD", "JNJ", "JPM", "MRK"], retries=1)
        self.assertEqual((run.status, run.failed, run.count(ITEM_PENDING), run.api_calls), (RUN_ABORTED, 2, 2, 4))
        self.assertIn("2 stocks failed in a row", run.message)

    def test_resume_after_an_interruption(self):
        fetcher = self.fetcher("JPM:503x10")
        seen = []

        def progress(n, total, outcome):
            seen.append(outcome.symbol)
            if outcome.symbol == "JNJ":
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            fetcher.fetch_market("NYSE", symbols=["HD", "JNJ", "JPM", "MRK"], progress=progress)
        first = fetcher.runs("NYSE")[0]
        self.assertEqual((first["Status"], first["Loaded"]), (RUN_INTERRUPTED, 2))
        run = self.fetcher("JPM:503x10").fetch_market("NYSE", resume=True)      # continues: JPM fails, MRK loads
        self.assertEqual(run.run_id, first["RunId"])
        self.assertEqual((run.resumed_from, run.loaded, run.failed), (2, 3, 1))
        run = self.fetcher().fetch_market("NYSE", resume=run.run_id[:6])        # the API is fine again: JPM only
        self.assertEqual((run.status, run.loaded, run.api_calls), (RUN_COMPLETED, 4, 2 + 4 + 1 + 1))
        items = {i["Symbol"]: i for i in self.fetcher().run_items(run.run_id)}
        self.assertEqual((items["JPM"]["Status"], items["JPM"]["Attempts"]), (ITEM_LOADED, 5))
        with self.assertRaises(EodMarketError):
            self.fetcher().fetch_market("NYSE", resume=True)                     # nothing unfinished left
        with self.assertRaises(EodMarketError):
            self.fetcher().fetch_market("LSE", resume=run.run_id)                # a run of another market

    def test_the_symbol_list_is_fetched_when_missing(self):
        SymbolListLoader(self.settings).delete_market("NYSE")
        run = self.fetcher().fetch_market("NYSE", limit=3)            # the fake serves the NYSE sample list
        self.assertEqual((len(run.symbols), run.loaded), (3, 3))
        self.assertEqual(list(self.settings.eod_download_dir.iterdir()), [])
        self.assertGreater(SymbolListLoader(self.settings).count("NYSE"), 5000)
        with self.assertRaisesRegex(EodMarketError, "no symbols"):
            self.fetcher().fetch_market("XETRA")                        # the fake knows no other market
        with self.assertRaisesRegex(EodMarketError, "could not be fetched after 4"):
            self.fetcher("*:503x10").fetch_market("NYSE", refresh_list=True)

    def test_fake_data_never_mixes_into_real_data(self):
        from stock_dataload.eod_stage import EodStageLoader
        EodStageLoader(self.settings).load_file(SAMPLE_RESPONSE, "MRK")          # real data
        with self.assertRaisesRegex(EodMarketError, "real data"):
            self.fetcher().fetch_market("NYSE", symbols=["HD"])
        run = self.fetcher().fetch_market("NYSE", symbols=["HD"], allow_fake_data=True)
        self.assertEqual(run.loaded, 1)

    def test_promotion_of_exactly_the_run(self):
        from stock_dataload.eod_stage import EodStageLoader
        from stock_dataload.promotion import PromotionService
        run = self.fetcher().fetch_market("NYSE", symbols=["HD", "JNJ"])
        EodStageLoader(self.settings).load_file(SAMPLE_RESPONSE, "VOD.LSE")      # another market, staged
        result = PromotionService(self.settings).promote_eod_daily(market="NYSE", symbols=[o.symbol for o in
                                                                                              run.outcomes.values()])
        self.assertEqual(result.rows_inserted, self.staged()["HD"] + self.staged()["JNJ"])
        with sqlite3.connect(self.settings.sqlite_path) as conn:
            self.assertEqual({r[0] for r in conn.execute('SELECT DISTINCT "Ticker" FROM "Stock_Daily"')}, {"HD", "JNJ"})

    def test_reference_script(self):
        script = (PROJECT_ROOT / "sql" / "create_eod_market_run_sqlite.sql").read_text(encoding="utf-8")
        self.assertTrue(script.endswith(render_ddl()))


class LiveTransportTests(MarketTestCase):
    def test_retry_after_a_503_from_the_real_client(self):
        body = json.dumps(SAMPLE[:5]).encode()

        class Answer(io.BytesIO):
            status = 200
            headers = {"Content-Type": "application/json"}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        busy = urllib.error.HTTPError("https://eodhd.com/api/eod/HD", 503, "Service Unavailable",
                                      {"Retry-After": "3"}, io.BytesIO(b"busy"))
        fetcher = MarketFetcher(self.settings, client=EodApiClient(self.settings, transport=UrllibGetter()),
                                sleep=self.waits.append, api_token="dummy-live-token")
        with mock.patch("urllib.request.urlopen", side_effect=[busy, Answer(body)]) as urlopen:
            run = fetcher.fetch_market("NYSE", symbols=["HD"])
        self.assertEqual((run.outcomes["HD"].status, run.outcomes["HD"].attempts, self.waits), (ITEM_LOADED, 2, [3.0]))
        self.assertIn("api_token=dummy-live-token", urlopen.call_args[0][0].full_url)
        with sqlite3.connect(self.settings.sqlite_path) as conn:
            self.assertEqual(conn.execute('SELECT DISTINCT "SourceFile" FROM "Stock_EOD_Stage"').fetchone()[0], "api/NYSE/HD")
        refused = urllib.error.HTTPError("https://eodhd.com/api/eod/HD", 401, "Unauthorized", {}, io.BytesIO(b""))
        with mock.patch("urllib.request.urlopen", side_effect=refused):
            run = fetcher.fetch_market("NYSE", symbols=["HD", "JNJ"])
        self.assertEqual((run.status, run.api_calls), (RUN_ABORTED, 1))
        self.assertNotIn("dummy-live-token", run.message)                        # the token is never shown


class CliTests(MarketTestCase):
    def setUp(self):
        super().setUp()
        self.settings.config_file = self.root / "config.ini"
        self.settings.eod_api_retry_backoff = 0.0
        save_settings(self.settings, self.settings.config_file)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["--config", str(self.settings.config_file), "-q", *args])
        return rc, out.getvalue(), err.getvalue()

    def test_fetch_market_runs_and_resume(self):
        rc, out, err = self.run_cli("eod-fetch-market", "NYSE", "--fake", "--fake-faults", "JPM:503x9,HD:503x1")
        self.assertEqual(rc, 1)
        self.assertIn("HD: 251 record(s) 2025-11-14 .. 2026-11-13, 251 inserted, 0 updated, 0 unchanged after 2 attempt(s)",
                      out)
        self.assertIn("JPM: FAILED after 4 attempt(s) - the API answered 503 Service Unavailable", err)
        self.assertIn("NYSE (fake): 6 symbol(s) - 4 loaded", out)
        self.assertIn("eod-fetch-market NYSE --resume", err)
        rc, out, _ = self.run_cli("eod-market-runs", "NYSE")
        self.assertIn("completed_with_errors", out)
        run_id = out.splitlines()[1].split()[0]
        rc, out, _ = self.run_cli("eod-market-runs", "--run", run_id)
        self.assertIn("JPM          failed       4  503", out)
        rc, out, err = self.run_cli("eod-fetch-market", "NYSE", "--fake", "--resume", "--promote")
        self.assertIn(f"run {run_id} completed", out)
        total = sum(self.staged().values())                                      # every stock of the run
        self.assertIn(f"promote_eod_daily: completed (full) - {total:,} rows read: {total:,} inserted", out)
        rc, out, _ = self.run_cli("eod-fetch-market", "NYSE", "--fake", "--symbols", "hd,jnj", "--summary-only")
        self.assertEqual(rc, 0)
        self.assertNotIn("HD:", out)
        self.assertIn("2 symbol(s) - 2 loaded", out)

    def test_errors(self):
        rc, _, err = self.run_cli("eod-fetch-market", "NYSE", "--fake-faults", "HD:503")
        self.assertEqual(rc, 2)
        rc, _, err = self.run_cli("eod-fetch-market", "NYSE", "--fake", "--types", "Warrant")
        self.assertEqual(rc, 1)
        self.assertIn("no symbols to fetch", err)
        rc, _, err = self.run_cli("eod-fetch-market", "NYSE", "--fake", "--fake-faults", "HD:teapot")
        self.assertEqual(rc, 1)
        self.assertIn("SYMBOL:KIND", err)
        rc, out, _ = self.run_cli("eod-market-runs")
        self.assertIn("no eod-fetch-market runs yet", out)


if __name__ == "__main__":
    unittest.main()
