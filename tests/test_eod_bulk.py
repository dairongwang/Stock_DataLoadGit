"""
One trading day of every stock of a market (``eod_bulk.py``, EODHD ``eod-bulk-last-day``):
the request from the parameters (market, token, format, date), reading JSON and CSV answers, the
built-in stub, loading into ``Stock_EOD_Stage`` (``CODE.EXCHANGE``), rejected records, retries, the
daily limits (a bulk load is a whole-market load; 5 loads per stock), promotion, the background job,
the dashboard tab and the ``eod-bulk-fetch`` / ``eod-bulk-load`` commands.

No network: the stub or a mocked ``urlopen``.  Every token and account is a dummy.
"""

from __future__ import annotations

import io
import json
import sqlite3
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from unittest import mock

from stock_dataload import cli
from stock_dataload.api_jobs import JOB_FINISHED, KIND_BULK, ApiLoadManager
from stock_dataload.audit import AuditLogger
from stock_dataload.config import save_settings
from stock_dataload.eod_api import PROJECT_ROOT, SAMPLE_RESPONSE, SAMPLE_SYMBOL_LIST, EodApiError, FakeEodGateway, \
    UrllibGetter
from stock_dataload.eod_bulk import (BULK_LOADED, BULK_NO_DATA, BULK_SAVED, BulkDayLoader, EodBulkError,
                                     check_trade_day, clean_symbols, fetch_bulk_last_day, parse_bulk_response,
                                     promotion_market, validate_bulk)
from stock_dataload.eod_market import RUN_ABORTED, RUN_COMPLETED, EodMarketError, MarketFetcher, MarketLoadLimitError
from stock_dataload.load_limits import BulkCallLimitError, StockLoadLimitError

from .helpers import TempProjectTestCase, register_listed
from .test_dashboard import DashboardTestCase
from .test_eod_api import FakeHttpResponse
from .test_load_limits import seed_loads

SAMPLE_BULK = PROJECT_ROOT / "docs" / "SampleAPI_BulkLastDay_US.json"
DAY = "2026-10-12"                       # a Monday of the stub's sample year
TODAY = date(2026, 11, 17)
MSFT = {"code": "MSFT", "exchange_short_name": "US", "date": "2026-08-18", "open": 481.54, "high": 484.27,
        "low": 477.15, "close": 481.63, "adjusted_close": 481.63, "volume": 24059700}
SAMPLE = {r["date"]: r for r in json.loads(SAMPLE_RESPONSE.read_text(encoding="utf-8"))}
LISTED = [r["Code"] for r in json.loads(SAMPLE_SYMBOL_LIST.read_text(encoding="utf-8"))]


def staged(settings, where: str = "1 = 1", params=()) -> list[tuple]:
    with sqlite3.connect(settings.sqlite_path) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'Stock_EOD_Stage'").fetchone():
            return []
        return conn.execute(f'SELECT "Symbol", "Ticker", "Exchange", "TradeDate", "Open", "High", "Low", "Close", '
                            f'"AdjustedClose", "Volume", "SourceFile" FROM "Stock_EOD_Stage" WHERE {where} '
                            f'ORDER BY "Symbol"', params).fetchall()


class BulkTestCase(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.settings.eod_api_token = "dummy-bulk-token"
        self.settings.eod_api_retry_backoff = 0.0
        self.audit = AuditLogger(self.settings)
        patcher = mock.patch("stock_dataload.eod_bulk._today", return_value=TODAY)   # the stub's sample year
        patcher.start()
        self.addCleanup(patcher.stop)

    def loader(self, faults: str | None = None, **kwargs) -> BulkDayLoader:
        return BulkDayLoader(self.settings, audit=self.audit, fake=True, fake_faults=faults, sleep=lambda s: None,
                             **kwargs)


class RequestTests(BulkTestCase):
    def test_the_url_from_the_parameters(self):
        loader = BulkDayLoader(self.settings, fake=False)
        self.assertEqual(loader.url_for("us", "json", "dummy-arg-token", DAY),
                         f"https://eodhd.com/api/eod-bulk-last-day/US?api_token=dummy-arg-token&fmt=json&date={DAY}")
        self.assertEqual(loader.url_for("US", "csv"), "https://eodhd.com/api/eod-bulk-last-day/US?api_token="
                                                      "dummy-bulk-token&fmt=csv")
        self.assertIn("&symbols=MSFT%2CHD", loader.url_for("US", "json", day=DAY, symbols=["MSFT", "HD"]))
        self.settings.eod_bulk_url = "https://mirror.example/bulk/{market}/day"
        self.assertTrue(loader.url_for("LSE").startswith("https://mirror.example/bulk/LSE/day?api_token="))
        with self.assertRaises(EodBulkError):
            loader.url_for("US", "xml")
        self.settings.eod_api_token = ""
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("STOCK_DATALOAD_EOD_API_TOKEN", None)
            with self.assertRaisesRegex(EodBulkError, "no API token"):
                loader.url_for("US")

    def test_the_trading_day(self):
        self.assertEqual(check_trade_day("US", "20261012"), DAY)
        self.assertIsNone(check_trade_day("US", None))
        for bad, message in (("2026-10-11", "a Sunday"), ("2025-12-25", "Christmas"), ("2026-11-30", "future"),
                             ("12.10.2026", "not a date")):
            with self.subTest(bad):
                with self.assertRaisesRegex(EodBulkError, message):
                    check_trade_day("US", bad)
        self.assertEqual(check_trade_day("US", "2026-10-11", force=True), "2026-10-11")
        with self.assertRaisesRegex(EodBulkError, "Thanksgiving"):
            check_trade_day("US", "2025-11-27")
        self.assertEqual(check_trade_day("LSE", "2025-11-27"), "2025-11-27")      # no US holiday in London
        with self.assertRaisesRegex(EodBulkError, "extra closure"):
            check_trade_day("US", DAY, extra_holidays=[DAY])
        self.assertEqual(clean_symbols(" msft, HD.us,msft "), ["MSFT", "HD.US"])
        with self.assertRaises(EodBulkError):
            clean_symbols("BRK/B")
        self.assertEqual((promotion_market("us"), promotion_market("lse")), (None, "LSE"))


class ParseTests(unittest.TestCase):
    def test_json_and_csv(self):
        raw, first = parse_bulk_response(json.dumps([MSFT]), "json")
        self.assertEqual((raw, first), ([MSFT], 1))
        self.assertEqual(parse_bulk_response(json.dumps({"data": [MSFT]}))[0], [MSFT])
        csv_text = "Code,Ex,Date,Open,High,Low,Close,Adjusted_close,Volume\nMSFT,US,2026-08-18,481.54,484.27,477.15,481.63,481.63,24059700\n"
        raw, first = parse_bulk_response(csv_text, "csv")
        self.assertEqual(first, 2)
        self.assertEqual(raw[0]["exchange_short_name"], "US")
        groups, rejected = validate_bulk(raw, "US", first)
        self.assertEqual(list(groups), ["MSFT.US"])
        self.assertEqual(groups["MSFT.US"][0].values(), (481.54, 484.27, 477.15, 481.63, 481.63, 24059700))
        with self.assertRaisesRegex(ValueError, "the API said: Unauthenticated"):
            parse_bulk_response('{"message": "Unauthenticated"}', "json")
        with self.assertRaisesRegex(ValueError, "lacks"):
            parse_bulk_response("Code,Date\nMSFT,2026-08-18\n", "csv")

    def test_validation(self):
        raw = [MSFT, dict(MSFT, code="HD", exchange_short_name=""), dict(MSFT, code=""), dict(MSFT, code="JNJ", volume="lots"),
               dict(MSFT), "garbage"]
        groups, rejected = validate_bulk(raw, "US")
        self.assertEqual(sorted(groups), ["HD.US", "MSFT.US"])                # no exchange: the market asked for
        reasons = {(s, r.number, r.column) for s, r in rejected}
        self.assertEqual(reasons, {("?", 3, "code"), ("JNJ.US", 4, "volume"), ("MSFT.US", 5, "code"), ("?", 6, "")})
        self.assertIn("appears more than once", [r.message for s, r in rejected if r.number == 5][0])


class StubTests(unittest.TestCase):
    def get(self, query: str):
        return FakeEodGateway(vary_by_symbol=True).get(
            f"https://eodhd.com/api/eod-bulk-last-day/{query}", 30)

    def test_every_listed_symbol_with_the_prices_of_its_single_calls(self):
        records = json.loads(self.get(f"US?api_token=dummy&fmt=json&date={DAY}").body)
        self.assertGreater(len(records), 5000)
        self.assertTrue({r["code"] for r in records} <= set(LISTED))
        by_code = {r["code"]: r for r in records}
        mrk = {k: v for k, v in by_code["MRK"].items() if k not in ("code", "exchange_short_name")}
        self.assertEqual(mrk, SAMPLE[DAY])                                      # MRK: the sample itself
        single = json.loads(FakeEodGateway(vary_by_symbol=True).get(
            "https://eodhd.com/api/eod/HD?api_token=dummy&fmt=json", 30).body)
        self.assertEqual({k: v for k, v in by_code["HD"].items() if k not in ("code", "exchange_short_name")},
                         next(r for r in single if r["date"] == DAY))
        self.assertEqual(by_code["HD"]["exchange_short_name"], "US")

    def test_csv_filters_and_empty_days(self):
        text = self.get(f"US?api_token=dummy&date={DAY}&symbols=MRK,HD.US").body.decode()
        self.assertTrue(text.startswith("Code,Ex,Date,Open,High,Low,Close,Adjusted_close,Volume\n"))
        self.assertEqual(len(text.strip().splitlines()), 3)
        self.assertEqual(json.loads(self.get("US?api_token=dummy&fmt=json&date=2026-10-11").body), [])    # Sunday
        self.assertEqual(json.loads(self.get(f"LSE?api_token=dummy&fmt=json&date={DAY}").body), [])
        last = json.loads(self.get("US?api_token=dummy&fmt=json&symbols=MRK").body)
        self.assertEqual(last[0]["date"], max(SAMPLE))                         # no date: the last day
        with self.assertRaises(EodApiError):
            self.get("US?fmt=json")                                              # no token


class LoadTests(BulkTestCase):
    def test_fetch_saves_and_loads_every_stock(self):
        result = fetch_bulk_last_day("US", fmt="json", date=DAY, settings=self.settings, fake=True, audit=self.audit)
        self.assertEqual(result.status, BULK_LOADED)
        self.assertEqual(result.received, len(result.loaded))
        self.assertEqual(result.inserted, result.received)
        self.assertIn(f"https://eodhd.com/api/eod-bulk-last-day/US?api_token=***&fmt=json&date={DAY}", result.url)
        self.assertTrue(result.path.is_file())
        self.assertTrue(result.path.name.startswith(f"US_bulk_{DAY}_"))
        mrk = staged(self.settings, '"Ticker" = ?', ("MRK",))
        self.assertEqual(mrk, [("MRK.US", "MRK", "US", DAY, *[SAMPLE[DAY][k] for k in
                                                               ("open", "high", "low", "close", "adjusted_close", "volume")],
                                "fake-api/" + result.path.name)])
        run = MarketFetcher(self.settings, fake=False).find_run(result.run_id)
        self.assertEqual((run["Status"], run["Loaded"], run["ApiCalls"], json.loads(run["Parameters"])["source"]),
                         (RUN_COMPLETED, result.received, 1, "bulk"))
        again = fetch_bulk_last_day("US", fmt="csv", date=DAY, settings=self.settings, fake=True)   # the same day as CSV
        self.assertEqual((again.inserted, again.updated, again.unchanged), (0, 0, again.received))
        self.assertTrue(again.path.name.endswith(".csv"))
        events = [l.event for l in self.audit.fetch_logs(limit=20, component="eod_bulk")]
        self.assertIn("bulk_loaded", events)

    def test_the_sample_answer_and_the_live_client(self):
        body = SAMPLE_BULK.read_bytes()
        loader = BulkDayLoader(self.settings, fake=False)
        with mock.patch("urllib.request.urlopen", return_value=FakeHttpResponse(body)) as urlopen:
            result = loader.fetch("US", api_token="dummy-live-token", fmt="json", date="2026-08-18", save=False)
        self.assertEqual(urlopen.call_args[0][0].full_url,
                         "https://eodhd.com/api/eod-bulk-last-day/US?api_token=dummy-live-token&fmt=json&date=2026-08-18")
        self.assertEqual((result.received, result.inserted), (2, 2))
        self.assertEqual(result.source_file, "api/bulk/US/2026-08-18")
        msft = staged(self.settings, '"Symbol" = ?', ("MSFT.US",))[0]
        self.assertEqual(msft[:10], ("MSFT.US", "MSFT", "US", "2026-08-18", 481.54, 484.27, 477.15, 481.63, 481.63,
                                     24059700))
        self.assertNotIn("dummy-live-token", result.url)

    def test_save_only_and_no_save(self):
        saved = self.loader().fetch("US", date=DAY, symbols="MRK,HD", load=False)
        self.assertEqual((saved.status, saved.received), (BULK_SAVED, 2))
        self.assertEqual(staged(self.settings), [])
        loaded = self.loader().fetch("US", date=DAY, symbols="MRK", save=False)
        self.assertIsNone(loaded.path)
        self.assertEqual(loaded.source_file, f"fake-api/bulk/US/{DAY}")

    def test_rejected_records(self):
        bad = self.root / "bad.json"
        bad.write_text(json.dumps([MSFT, dict(MSFT, code="HD", close="n/a"), dict(MSFT, code="")]), encoding="utf-8")
        result = self.loader().load_file(bad, "US")
        self.assertEqual((result.status, len(result.loaded), result.rejected_records), (BULK_LOADED, 1, 2))
        self.assertTrue(any("2 invalid record(s) rejected" in w for w in result.warnings()))
        from stock_dataload.quality import DataErrorRepository
        self.assertEqual(len(DataErrorRepository(self.settings).fetch(limit=10)), 2)
        strict = self.loader().load_file(bad, "US", skip_bad_rows=False)
        self.assertFalse(strict.ok)
        self.assertIn("nothing was loaded", strict.message)
        broken = self.root / "broken.json"
        broken.write_text('{"error": "quota exceeded"}', encoding="utf-8")
        self.assertIn("quota exceeded", self.loader().load_file(broken, "US").message)

    def test_an_empty_answer(self):
        result = self.loader().fetch("US", date="2026-10-11", force=True, symbols="MRK")
        self.assertEqual(result.status, BULK_NO_DATA)
        self.assertTrue(result.ok)
        self.assertIn("no data", result.warnings()[0])


class ApiProblemTests(BulkTestCase):
    def test_retries_and_failures(self):
        result = self.loader("US:503x2").fetch("US", date=DAY, symbols="MRK")
        self.assertEqual((result.status, result.api_calls), (BULK_LOADED, 3))
        loader = self.loader("US:401")
        with self.assertRaises(EodApiError) as ctx:
            loader.fetch("US", date=DAY, symbols="HD")
        self.assertEqual(ctx.exception.status, 401)
        runs = MarketFetcher(self.settings, fake=False).runs("US")
        self.assertEqual((runs[0]["Status"], runs[0]["ApiCalls"]), (RUN_ABORTED, 1))     # 401 is not repeated
        self.assertIn("the API call failed", runs[0]["Message"])
        with self.assertRaisesRegex(EodApiError, "valid JSON"):
            self.loader("US:badjson").fetch("US", date=DAY, symbols="HD", retries=0)

    def test_fake_data_is_kept_away_from_real_data(self):
        BulkDayLoader(self.settings, fake=False).load_file(SAMPLE_BULK, "US")            # real (saved) data
        with self.assertRaisesRegex(EodMarketError, "invents prices"):
            self.loader().fetch("US", date=DAY, symbols="MRK")
        self.assertEqual(self.loader().fetch("US", date=DAY, symbols="MRK", allow_fake_data=True).status, BULK_LOADED)


class LimitTests(BulkTestCase):
    def test_a_bulk_load_is_a_whole_market_load(self):
        fetcher = MarketFetcher(self.settings, fake=False)
        for n, day in enumerate(("2026-10-12", "2026-10-13", "2026-10-14"), 1):
            self.loader().fetch("US", date=day)
            self.assertEqual(fetcher.market_load_quota("US").used, n)
        loader = self.loader()
        with self.assertRaisesRegex(MarketLoadLimitError, "US: the bulk load of all stocks of a market may run at most 3"):
            loader.fetch("US", date="2026-10-15")
        self.assertEqual(loader.client.transport.calls, [])                    # refused before the call
        self.assertEqual(self.loader().fetch("US", date="2026-10-15", symbols="PFE").status, BULK_LOADED)  # no market load
        with self.assertRaises(MarketLoadLimitError):
            self.loader().load_file(SAMPLE_BULK, "US")                           # a saved answer counts too
        with self.assertRaisesRegex(EodMarketError, "one-day bulk load"):
            fetcher.fetch_market("US", resume=fetcher.runs("US")[0]["RunId"])

    def test_at_most_3_calls_for_the_same_market_and_trade_date(self):
        loader = self.loader()
        self.assertEqual(loader.bulk_call_quota("US", DAY).as_dict()["remaining"], 3)
        loader.fetch("US", date=DAY, symbols="MRK")                             # calls with symbols count ...
        loader.fetch("US", date=DAY, symbols="HD", load=False)                  # ... save-only calls too
        with self.assertRaises(EodApiError):
            self.loader("US:401").fetch("US", date=DAY, symbols="PFE")          # ... and failed ones
        quota = loader.bulk_call_quota("US", DAY)
        self.assertEqual((quota.used, quota.exhausted), (3, True))
        refused = self.loader()
        with self.assertRaises(BulkCallLimitError) as ctx:
            refused.fetch("US", date=DAY, symbols="JNJ")
        self.assertIn(f"US {DAY}: the bulk endpoint may be called at most 3 times for the same market and day", str(ctx.exception))
        self.assertEqual(refused.client.transport.calls, [])                    # nothing requested
        self.assertEqual([l.event for l in self.audit.fetch_logs(limit=5)][0], "bulk_call_refused")
        self.assertEqual(self.loader().fetch("US", date="2026-10-13", symbols="JNJ").status, BULK_LOADED)  # another day
        self.assertEqual(self.loader().fetch("LSE", date=DAY, symbols="JNJ", allow_fake_data=True).status,
                         BULK_NO_DATA)                                          # another market
        self.loader().load_file(SAMPLE_BULK, "US")                              # a saved answer is no call
        self.assertEqual(loader.bulk_call_quota("US", "2026-08-18").used, 0)

    def test_a_call_without_a_date_counts_for_the_day_of_the_answer(self):
        loader = self.loader()
        self.assertEqual(loader.trade_date_of("US", None), "2026-11-17")       # the latest trading day (a Tuesday)
        result = loader.fetch("US", symbols="MRK")                              # the stub answers its last day
        self.assertEqual(result.dates, ["2026-11-13"])
        self.assertEqual(loader.bulk_call_quota("US", "2026-11-13").used, 1)
        self.assertEqual(loader.bulk_call_quota("US", "2026-11-17").used, 0)

    def test_the_registration_checks_again(self):
        from stock_dataload.eod_bulk import BulkLoadResult
        for _ in range(3):
            self.loader().fetch("US", date=DAY, symbols="MRK")
        loader = self.loader()
        with self.assertRaises(BulkCallLimitError):
            loader._register(BulkLoadResult("US", DAY), "json", ["MRK"], "fake", trade_date=DAY)

    def test_stocks_at_their_daily_limit(self):
        seed_loads(self.settings, "MRK", 5)
        result = self.loader().fetch("US", date=DAY, symbols="MRK,HD,PFE")
        self.assertEqual(sorted(result.limited), ["MRK"])
        self.assertEqual(sorted(result.tickers), ["HD", "PFE"])
        self.assertTrue(any("1 stock(s) skipped" in w for w in result.warnings()))
        self.assertEqual(staged(self.settings, '"Ticker" = ?', ("MRK",)), [])
        skipped = [l for l in self.audit.fetch_logs(limit=50) if l.event == "stock_load_skipped"]
        self.assertEqual(len(skipped), 1)
        loader = self.loader()
        with self.assertRaises(StockLoadLimitError):                           # only stocks at their limit
            loader.fetch("US", date=DAY, symbols="MRK")
        self.assertEqual(loader.client.transport.calls, [])
        from stock_dataload.load_limits import StockLoadLedger
        from stock_dataload.db import StageDatabase
        self.assertEqual(StockLoadLedger(StageDatabase(self.settings)).count("HD"), 1)   # a bulk load counts per stock


class PromotionTests(BulkTestCase):
    def test_promote_exactly_the_load(self):
        register_listed(self.settings, "NYSE", ["MRK"])
        loader = BulkDayLoader(self.settings, fake=False)
        result = loader.load_file(SAMPLE_BULK, "US")
        promoted = loader.promote(result)[0]
        self.assertEqual((promoted.rows_inserted, promoted.rows_rejected), (1, 1))     # MSFT: market unknown
        self.settings.eod_default_market = "NASDAQ"
        self.assertEqual(loader.promote(result)[0].rows_inserted, 1)
        with sqlite3.connect(self.settings.sqlite_path) as conn:
            daily = dict(conn.execute('SELECT "Ticker", "Close" FROM "Stock_Daily"').fetchall())
        self.assertEqual(daily["MSFT"], 481.63)


class JobTests(BulkTestCase):
    def test_the_background_job(self):
        manager = ApiLoadManager(self.settings, audit_factory=AuditLogger)
        job = manager.start(KIND_BULK, "US", "US bulk", "dummy_operator", date=DAY, fake=True, background=False,
                            symbols=["MRK", "HD"], promote=False)
        self.assertEqual((job.status, job.loaded, job.run_status), (JOB_FINISHED, 2, RUN_COMPLETED))
        self.assertTrue(any("GET https://eodhd.com/api/eod-bulk-last-day/US?api_token=***" in l["text"] for l in job.log))
        with self.assertRaisesRegex(EodBulkError, "a Saturday"):
            manager.start(KIND_BULK, "US", "US bulk", "dummy_operator", date="2026-10-10", fake=True, background=False)
        self.assertEqual(len(manager.jobs()), 1)


class DashboardBulkTests(DashboardTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch("stock_dataload.eod_bulk._today", return_value=TODAY)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.loads: ApiLoadManager = self.app.extensions["api_loads"]

    def start(self, **form):
        r = self.client.post("/api-load/start", data={"mode": "bulk", "fake": "on", "promote": "", **form})
        self.loads.join(60)
        return r

    def test_the_tab_the_start_and_the_run(self):
        page = self.client.get("/api-load").get_data(as_text=True)
        self.assertIn('value="bulk"', page)
        self.assertIn("One day, all stocks", page)
        self.assertIn('name="bulk_date"', page)
        r = self.start(bulk_market="us", bulk_date=DAY)
        self.assertIn(f"Started: US {DAY}: every stock (bulk) - whole-market load 1 of 3 today", self.flashes(r))
        job = self.loads.current
        self.assertEqual(job.status, JOB_FINISHED, job.message)
        self.assertGreater(job.loaded, 5000)
        page = self.client.get("/api-load").get_data(as_text=True)
        self.assertIn("one day (bulk)", page)
        self.assertIn('<span class="badge warn">US 1/3</span>', page)
        run_page = self.client.get(f"/api-load/runs/{job.run_id}").get_data(as_text=True)
        self.assertIn("One day of every stock (bulk)", run_page)
        self.assertNotIn('id="resume-button"', run_page)

    def test_refusals_and_a_few_stocks(self):
        r = self.start(bulk_market="US", bulk_date="2026-10-11")
        self.assertIn("Not started - US does not trade on 2026-10-11 (a Sunday)", self.flashes(r))
        r = self.start(bulk_market="US", bulk_date="2026-10-11", bulk_force="on", bulk_symbols="MRK")
        self.assertIn("Started: US 2026-10-11: 1 stock(s) (bulk)", self.flashes(r))
        self.assertIn("no data", self.loads.current.message)
        r = self.start(bulk_market="US", bulk_date=DAY, bulk_symbols="BRK/B")
        self.assertIn("no symbols", self.flashes(r))
        r = self.start(bulk_market="NY SE", bulk_date=DAY)
        self.assertIn("Market:", self.flashes(r))


class CliBulkTests(BulkTestCase):
    def setUp(self):
        super().setUp()
        self.settings.config_file = self.root / "config.ini"
        save_settings(self.settings, self.settings.config_file)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["--config", str(self.settings.config_file), "-q", *args])
        return rc, out.getvalue(), err.getvalue()

    def test_eod_bulk_fetch_and_load(self):
        rc, out, err = self.run_cli("eod-bulk-fetch", "US", "--date", DAY, "--fake", "--symbols", "MRK,HD",
                                    "--fake-faults", "US:503x1")
        self.assertEqual(rc, 0, err)
        self.assertIn(f"US {DAY}: 2 record(s), 2 stock(s) loaded into Stock_EOD_Stage", out)
        self.assertIn("answer received", out)
        rc, _, err = self.run_cli("eod-bulk-fetch", "US", "--date", "2026-10-10", "--fake")
        self.assertEqual(rc, 1)
        self.assertIn("ERROR: US does not trade on 2026-10-10 (a Saturday)", err)
        rc, _, err = self.run_cli("eod-bulk-fetch", "US", "--fake-faults", "US:503")
        self.assertEqual(rc, 2)
        rc, out, err = self.run_cli("eod-bulk-load", str(SAMPLE_BULK), "--market", "US")
        self.assertEqual(rc, 0, err)
        self.assertIn("US 2026-08-18: 2 record(s), 2 stock(s) loaded", out)
        rc, _, err = self.run_cli("eod-bulk-load", str(self.root / "missing.json"))
        self.assertEqual(rc, 1)
        self.assertIn("file not found", err)
        for _ in range(2):
            self.run_cli("eod-bulk-fetch", "US", "--date", DAY, "--fake", "--allow-fake-data")
        rc, _, err = self.run_cli("eod-bulk-fetch", "US", "--date", DAY, "--fake", "--allow-fake-data")
        self.assertEqual(rc, 1)
        self.assertIn(f"REFUSED: US {DAY}: the bulk endpoint may be called at most 3 times for the same market and day", err)
        rc, _, err = self.run_cli("eod-bulk-fetch", "US", "--date", "2026-10-13", "--fake", "--allow-fake-data")
        self.assertEqual(rc, 1)                                                  # 3 whole-market loads today
        self.assertIn("REFUSED: US: the bulk load of all stocks of a market may run at most 3 times a day", err)


if __name__ == "__main__":
    unittest.main()
