"""
Exchange symbol lists: the API call (``EodApiClient.fetch_symbol_list``, mocked
HTTP / fake gateway), the staging table ``Stock_Symbol_Stage`` (``eod_symbols.py``)
and the ``eod-symbols-*`` commands.  Sample data: docs/SampleAPI_NYSE_List.json
(the NYSE list as delivered: 5,641 symbols).  Tokens are dummies.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from stock_dataload import cli
from stock_dataload.audit import AuditLogger
from stock_dataload.config import ConfigError, load_settings, save_settings
from stock_dataload.db import StageDatabase
from stock_dataload.eod_api import (SAMPLE_SYMBOL_LIST, EodApiClient, EodApiError, FakeEodGateway, UrllibGetter,
                                    fetch_symbol_list, validate_market)
from stock_dataload.eod_symbols import (SymbolListLoader, SymbolStageError, market_from_filename, parse_list,
                                        render_ddl, validate_symbols)
from stock_dataload.quality import DataErrorRepository

from .helpers import PROJECT_ROOT, TempProjectTestCase
from .test_eod_api import FakeHttpResponse

SAMPLE = json.loads(SAMPLE_SYMBOL_LIST.read_text(encoding="utf-8"))
ATTRS = ("Name", "Country", "Exchange", "Currency", "Type", "Isin")


def expected(record: dict) -> tuple:
    """A record as staged: values as delivered, leading / trailing blanks trimmed."""
    return tuple(record[k].strip() if isinstance(record[k], str) else record[k] for k in ATTRS)


class SymbolTestCase(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.loader = SymbolListLoader(self.settings, audit=AuditLogger(self.settings),
                                       data_errors=DataErrorRepository(self.settings))

    def staged(self, market: str = "NYSE") -> dict:
        with sqlite3.connect(self.settings.sqlite_path) as conn:
            return {r[0]: tuple(r[1:]) for r in conn.execute(
                'SELECT "Code", "Name", "Country", "Exchange", "Currency", "Type", "Isin" FROM "Stock_Symbol_Stage" '
                'WHERE "Market" = ?', (market,))}

    def write_list(self, name: str, records) -> os.PathLike:
        return self.write(name, json.dumps(records))


class HelperTests(unittest.TestCase):
    def test_reference_script_matches_the_module(self):
        script = (PROJECT_ROOT / "sql" / "create_symbol_stage_sqlite.sql").read_text(encoding="utf-8")
        self.assertTrue(script.endswith(render_ddl()))

    def test_sample_is_the_delivered_file(self):
        self.assertEqual(len(SAMPLE), 5641)
        self.assertEqual(set(SAMPLE[0]), {"Code", "Name", "Country", "Exchange", "Currency", "Type", "Isin"})

    def test_markets_and_file_names(self):
        self.assertEqual(validate_market(" nyse "), "NYSE")
        for bad in ("", "NY SE", "NYSE/../x", "A" * 30):
            with self.assertRaises(EodApiError, msg=bad):
                validate_market(bad)
        for name, market in (("NYSE_List.json", "NYSE"), ("SampleAPI_NYSE_List.json", "NYSE"),
                             ("NYSE_symbols_20251114_101500.json", "NYSE"), ("lse-list.csv", "LSE"),
                             ("MRK_20251114_101500.json", None), ("prices.json", None)):
            self.assertEqual(market_from_filename(name), market, name)

    def test_validation_and_parsing(self):
        records = [{"Code": " MRK ", "Name": "Merck & Company Inc ", "Isin": "us58933y1055", "Type": ""},
                   {"Code": "mrk", "Name": "again"},                           # the same code (case-insensitive)
                   {"Code": "", "Name": "no code"}, {"Name": "no code at all"}, {"Code": "A B"}, "text"]
        good, bad = validate_symbols(records)
        self.assertEqual([(g.code, g.name, g.isin, g.type) for g in good],
                         [("MRK", "Merck & Company Inc", "US58933Y1055", None)])
        self.assertEqual([b.number for b in bad], [2, 3, 4, 5, 6])
        self.assertIn("appears more than once", bad[0].message)
        rows, first = parse_list(json.dumps({"data": SAMPLE[:2]}), "json")
        self.assertEqual((len(rows), first), (2, 1))
        rows, first = parse_list("Code,Name,Country,Exchange,Currency,Type,Isin\nMRK,Merck,USA,NYSE,USD,Common Stock,\n", "csv")
        self.assertEqual((rows[0]["Code"], rows[0]["Isin"], first), ("MRK", "", 2))
        with self.assertRaisesRegex(ValueError, "Invalid API token"):
            parse_list('{"message": "Invalid API token"}', "json")
        with self.assertRaisesRegex(ValueError, "no Code column"):
            parse_list("Ticker,Name\nMRK,Merck\n", "csv")


class LoadTests(SymbolTestCase):
    def test_the_sample_list(self):
        result = self.loader.load_file(SAMPLE_SYMBOL_LIST, "nyse")
        self.assertTrue(result.ok, result.message)
        self.assertEqual((result.received, result.inserted, result.updated, result.unchanged), (5641, 5641, 0, 0))
        self.assertEqual(result.types["ETF"], 2710)
        self.assertEqual(result.types["Common Stock"], 2150)
        staged = self.staged()
        self.assertEqual(staged, {r["Code"]: expected(r) for r in SAMPLE})             # every value, exactly
        self.assertEqual(staged["MRK"], ("Merck & Company Inc", "USA", "NYSE", "USD", "Common Stock", "US58933Y1055"))
        self.assertTrue(any(v[0] and not v[0].isascii() for v in staged.values()))     # UTF-8 names survive
        self.assertEqual(sum(1 for v in staged.values() if v[5] is None), 439)          # ISIN may be empty
        [history] = StageDatabase(self.settings).load_history()
        self.assertEqual((history.file_name, history.status, history.rows_loaded, history.market),
                         ("SampleAPI_NYSE_List.json", "loaded", 5641, "NYSE"))
        [summary] = self.loader.summary()
        self.assertEqual((summary.market, summary.codes, summary.exchanges),
                         ("NYSE", 5641, {"NYSE": 3101, "NYSE ARCA": 2501, "NYSE MKT": 39}))
        self.assertEqual([e.event for e in AuditLogger(self.settings).fetch_logs(component="eod_symbols")],
                         ["symbols_loaded"])

    def test_newer_lists_upsert(self):
        first = SAMPLE[:100]
        self.loader.load_file(self.write_list("NYSE_List.json", first))
        again = self.loader.load_file(self.write_list("NYSE_List.json", first))
        self.assertEqual((again.inserted, again.updated, again.unchanged, again.not_in_list), (0, 0, 100, 0))
        newer = [dict(r) for r in SAMPLE[1:100]] + [dict(SAMPLE[100])]
        newer[0]["Name"] = "Renamed Fund"
        result = self.loader.load_file(self.write_list("NYSE_symbols_20260101_000000.json", newer))
        self.assertEqual((result.inserted, result.updated, result.unchanged, result.not_in_list), (1, 1, 98, 1))
        self.assertIn("1 code(s) of earlier lists are not in this one", str(result))
        rows = {r["Code"]: r for r in self.loader.search(market="NYSE", limit=500)}
        self.assertEqual(len(rows), 101)                                            # nothing is deleted
        gone = rows[SAMPLE[0]["Code"]]
        renamed = rows[SAMPLE[1]["Code"]]
        self.assertEqual(renamed["Name"], "Renamed Fund")
        self.assertLessEqual(gone["LastSeenAt"], renamed["LastSeenAt"])          # (timestamps have 1 s resolution)
        self.assertEqual(gone["FirstSeenAt"], renamed["FirstSeenAt"])
        self.assertEqual(renamed["SourceFile"], "NYSE_symbols_20260101_000000.json")
        self.assertEqual(gone["SourceFile"], "NYSE_List.json")

    def test_markets_are_separate(self):
        self.loader.load_file(self.write_list("NYSE_List.json", SAMPLE[:10]))
        self.loader.load_file(self.write_list("LSE_List.json", [dict(r, Exchange="LSE") for r in SAMPLE[:5]]))
        self.assertEqual((self.loader.count("NYSE"), self.loader.count("LSE"), self.loader.count()), (10, 5, 15))
        code = SAMPLE[0]["Code"]
        self.assertEqual(sorted(r["Market"] for r in self.loader.get(code)), ["LSE", "NYSE"])
        self.assertEqual(self.loader.delete_market("lse"), 5)
        self.assertEqual([m.market for m in self.loader.summary()], ["NYSE"])

    def test_invalid_records(self):
        records = [dict(r) for r in SAMPLE[:5]]
        records[1]["Code"] = ""
        records[3]["Code"] = records[0]["Code"]
        path = self.write_list("NYSE_List.json", records)
        result = self.loader.load_file(path)
        self.assertFalse(result.ok)
        self.assertIn("2 of 5 record(s) are invalid", result.message)
        self.assertEqual(self.loader.count(), 0)
        errors = DataErrorRepository(self.settings).fetch(source_file="NYSE_List.json")
        self.assertEqual(sorted((e.rule, e.line_no) for e in errors), [("symbol_record", 2), ("symbol_record", 4)])
        self.assertEqual(StageDatabase(self.settings).load_history()[0].status, "failed")
        result = self.loader.load_file(path, skip_bad_rows=True)
        self.assertEqual((result.ok, result.inserted, result.rejected_records), (True, 3, 2))

    def test_csv_lists_and_usage_errors(self):
        header = "Code,Name,Country,Exchange,Currency,Type,Isin\n"
        body = "".join(f'{r["Code"]},"{r["Name"]}",USA,{r["Exchange"]},USD,{r["Type"]},{r["Isin"] or ""}\n'
                       for r in SAMPLE[:20] if '"' not in r["Name"])
        result = self.loader.load_file(self.write("NYSE_symbols_20260101_000000.csv", header + body))
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.inserted, body.count("\n"))
        first = self.staged()[SAMPLE[0]["Code"]]
        self.assertEqual(first, expected(SAMPLE[0]))
        self.assertFalse(self.loader.load_file(self.write("NYSE_List.json", '{"message": "Invalid API token"}')).ok)
        with self.assertRaisesRegex(SymbolStageError, "--market"):
            self.loader.load_file(self.write("symbols.json", "[]"))
        with self.assertRaisesRegex(SymbolStageError, "not found"):
            self.loader.load_file(self.root / "missing.json", "NYSE")
        with self.assertRaisesRegex(SymbolStageError, "not a valid market"):
            self.loader.load_file(SAMPLE_SYMBOL_LIST, "NY SE")

    def test_search(self):
        self.loader.load_file(SAMPLE_SYMBOL_LIST, "NYSE")
        hits = self.loader.search("merck")
        self.assertIn("MRK", [h["Code"] for h in hits])
        self.assertEqual(self.loader.search("mrk")[0]["Code"], "MRK")                   # exact code first
        etfs = self.loader.search(market="NYSE", type_="etf", limit=5)
        self.assertTrue(etfs and all(h["Type"] == "ETF" for h in etfs))
        self.assertEqual(self.loader.search("zzzzzz"), [])


class ApiTests(SymbolTestCase):
    def setUp(self):
        super().setUp()
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("STOCK_DATALOAD_EOD_API_TOKEN", None)

    def tearDown(self):
        self.env.stop()
        super().tearDown()

    def test_the_request(self):
        client = EodApiClient(self.settings)
        self.assertEqual(client.symbol_list_url_for("NYSE"),
                         "https://eodhd.com/api/exchange-symbol-list/NYSE?api_token=dummy&fmt=json")
        self.settings.eod_api_token = "Dummy"
        self.settings.eod_symbol_list_url = "https://mirror.test/v1/{market}/symbols"
        self.assertEqual(client.symbol_list_url_for("lse", fmt="csv", params={"type": "etf", "x": None}),
                         "https://mirror.test/v1/LSE/symbols?api_token=Dummy&fmt=csv&type=etf")
        os.environ["STOCK_DATALOAD_EOD_API_TOKEN"] = "dummy-from-env"
        self.assertIn("api_token=dummy-from-env", client.symbol_list_url_for("NYSE"))
        self.assertIn("api_token=dummy-arg", client.symbol_list_url_for("NYSE", api_token="dummy-arg"))
        with self.assertRaises(EodApiError):
            client.symbol_list_url_for("NYSE", fmt="js on")
        self.settings.eod_api_token = ""
        del os.environ["STOCK_DATALOAD_EOD_API_TOKEN"]
        with self.assertRaisesRegex(EodApiError, "no API token"):
            client.symbol_list_url_for("NYSE")

    def test_live_call_is_saved_and_loaded(self):
        body = SAMPLE_SYMBOL_LIST.read_bytes()
        with mock.patch("urllib.request.urlopen", return_value=FakeHttpResponse(body)) as urlopen:
            result = EodApiClient(self.settings, transport=UrllibGetter()).fetch_symbol_list("NYSE")
        self.assertEqual(urlopen.call_args[0][0].full_url,
                         "https://eodhd.com/api/exchange-symbol-list/NYSE?api_token=dummy&fmt=json")
        self.assertEqual((result.symbol, result.records, result.url),
                         ("NYSE", 5641, "https://eodhd.com/api/exchange-symbol-list/NYSE?api_token=***&fmt=json"))
        self.assertRegex(result.path.name, r"^NYSE_symbols_\d{8}_\d{6}\.json$")
        self.assertEqual(result.path.read_bytes(), body)
        self.assertEqual((result.stage.inserted, result.stage.source_file), (5641, result.path.name))
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError("u", 401, "Unauthorized", {},
                                                                                     io.BytesIO(b""))):
            with self.assertRaisesRegex(EodApiError, "check the API token"):
                EodApiClient(self.settings, transport=UrllibGetter()).fetch_symbol_list("NASDAQ")
        self.assertEqual(list(self.settings.eod_download_dir.glob("NASDAQ*")), [])

    def test_fake_gateway_and_options(self):
        gateway = FakeEodGateway()
        client = EodApiClient(self.settings, transport=gateway)
        result = client.fetch_symbol_list("NYSE", params={"type": "common_stock"})
        self.assertEqual(result.stage.inserted, 2150)
        self.assertEqual(set(r["Type"] for r in self.loader.search(market="NYSE", limit=5000)), {"Common Stock"})
        csv_result = client.fetch_symbol_list("NYSE", fmt="csv", load=False)
        self.assertIsNone(csv_result.stage)
        self.assertTrue(csv_result.path.read_text(encoding="utf-8").startswith("Code,Name,Country,Exchange"))
        loaded = self.loader.load_file(csv_result.path)                               # market from the file name
        self.assertEqual((loaded.market, loaded.inserted, loaded.unchanged), ("NYSE", 3491, 2150))
        self.assertEqual(self.staged(), {r["Code"]: expected(r) for r in SAMPLE})      # CSV gives the same values
        empty = client.fetch_symbol_list("LSE")
        self.assertEqual((empty.records, empty.stage.received), (0, 0))
        self.assertEqual(len(gateway.calls), 3)
        self.assertTrue(all("api_token=***" in c for c in gateway.calls))
        self.settings.eod_api_gateway = "fake"
        self.assertEqual(fetch_symbol_list("NYSE", self.settings, load=False).records, 5641)


class ConfigAndCliTests(SymbolTestCase):
    def setUp(self):
        super().setUp()
        self.settings.config_file = self.root / "config.ini"
        save_settings(self.settings, self.settings.config_file)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["--config", str(self.settings.config_file), "-q", *args])
        return rc, out.getvalue(), err.getvalue()

    def test_configuration(self):
        text = self.settings.config_file.read_text(encoding="utf-8")
        for line in ("symbol_list_url = https://eodhd.com/api/exchange-symbol-list/", "symbol_list_fmt = json",
                     "symbol_list_file_name = {market}_symbols_{timestamp}.{ext}",
                     "symbol_stage_table = Stock_Symbol_Stage"):
            self.assertIn(line, text)
        for old, new in (("symbol_list_url = https://", "symbol_list_url = ftp://"),
                         ("symbol_stage_table = Stock_Symbol_Stage", "symbol_stage_table = Stock_Symbol"),
                         ("symbol_stage_table = Stock_Symbol_Stage", "symbol_stage_table = Stock_EOD_Stage"),
                         ("symbol_list_fmt = json", "symbol_list_fmt = js on")):
            self.settings.config_file.write_text(text.replace(old, new), encoding="utf-8")
            with self.assertRaises(ConfigError, msg=new):
                load_settings(self.settings.config_file)

    def test_commands(self):
        rc, out, _ = self.run_cli("init-db")
        self.assertIn("Stock_Symbol_Stage", out)
        self.assertIn("Stock_Symbol_Stage is empty", self.run_cli("eod-symbols")[1])
        rc, out, err = self.run_cli("eod-symbols-load", str(self.write("symbols.json", "[]")))
        self.assertEqual(rc, 1)
        self.assertIn("--market", err)
        rc, out, err = self.run_cli("eod-symbols-load", str(SAMPLE_SYMBOL_LIST))
        self.assertEqual(rc, 0, err)
        self.assertIn("NYSE: 5,641 symbol(s) loaded into Stock_Symbol_Stage - 5,641 inserted", out)
        self.assertIn("types: ETF 2,710, Common Stock 2,150", out)
        rc, out, _ = self.run_cli("eod-symbols")
        self.assertIn("NYSE: 5,641 symbol(s)", out)
        self.assertIn("exchanges: NYSE 3,101, NYSE ARCA 2,501, NYSE MKT 39", out)
        rc, out, _ = self.run_cli("eod-symbols", "--q", "merck")
        self.assertIn("Merck & Company Inc", out)
        rc, out, _ = self.run_cli("eod-symbols", "--market", "NYSE", "--type", "Warrant", "--limit", "100")
        self.assertIn("(61 shown", out)
        self.assertIn("Stock_Symbol_Stage: NYSE 5,641 symbols", self.run_cli("status")[1])
        rc, out, err = self.run_cli("eod-symbols-fetch", "NYSE", "LSE", "BAD CODE", "--fake", "--type", "etf")
        self.assertEqual(rc, 1)
        self.assertIn("NYSE: 2,710 symbol(s) loaded", out)
        self.assertIn("LSE: 0 symbol(s) loaded", out)
        self.assertIn("ERROR BAD CODE", err)
        rc, out, _ = self.run_cli("eod-symbols-fetch", "NYSE", "--fake", "--no-load")
        self.assertNotIn("loaded into", out)
        self.assertIn("5,641 symbol(s) of NYSE deleted", self.run_cli("eod-symbols", "--delete", "NYSE")[1])


if __name__ == "__main__":
    unittest.main()
