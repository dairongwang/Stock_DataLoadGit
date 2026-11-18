"""
The staging table of the EOD API data (``Stock_EOD_Stage``, ``eod_stage.py``):
loading the sample response ``docs/SampleAPI_JasonData.txt`` (251 MRK
records), upserts of overlapping responses, rejected records, CSV answers,
loading right after an API call (fake gateway / mocked HTTP), and the
``eod-load`` / ``eod-stage`` / ``eod-fetch`` commands.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from stock_dataload import cli
from stock_dataload.audit import AuditLogger
from stock_dataload.config import ConfigError, load_settings, save_settings
from stock_dataload.db import StageDatabase
from stock_dataload.eod_api import SAMPLE_RESPONSE, EodApiClient, FakeEodGateway, UrllibGetter
from stock_dataload.eod_stage import (EodStageError, EodStageLoader, parse_response, render_ddl, split_symbol,
                                      symbol_from_filename, validate_records)
from stock_dataload.quality import DataErrorRepository

from .helpers import PROJECT_ROOT, TempProjectTestCase
from .test_eod_api import FakeHttpResponse

SAMPLE = json.loads(SAMPLE_RESPONSE.read_text(encoding="utf-8"))
EXPECTED = [(r["date"], r["open"], r["high"], r["low"], r["close"], r["adjusted_close"], r["volume"]) for r in SAMPLE]


class StageTestCase(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.loader = EodStageLoader(self.settings, audit=AuditLogger(self.settings),
                                     data_errors=DataErrorRepository(self.settings))

    def query(self, sql: str, params=()):
        with sqlite3.connect(self.settings.sqlite_path) as conn:
            return conn.execute(sql, params).fetchall()

    def staged(self, symbol: str = "MRK"):
        return self.query('SELECT "TradeDate", "Open", "High", "Low", "Close", "AdjustedClose", "Volume" '
                          'FROM "Stock_EOD_Stage" WHERE "Symbol" = ? ORDER BY "TradeDate"', (symbol,))

    def write_json(self, name: str, records) -> os.PathLike:
        return self.write(name, json.dumps(records))


class HelperTests(unittest.TestCase):
    def test_reference_script_matches_the_module(self):
        script = (PROJECT_ROOT / "sql" / "create_eod_stage_sqlite.sql").read_text(encoding="utf-8")
        self.assertTrue(script.endswith(render_ddl()))

    def test_symbols(self):
        self.assertEqual(split_symbol("MRK"), ("MRK", None))
        self.assertEqual(split_symbol("MRK.US"), ("MRK", "US"))
        self.assertEqual(split_symbol("BRK-B.US"), ("BRK-B", "US"))
        self.assertEqual(split_symbol("^GSPC.INDX"), ("^GSPC", "INDX"))
        self.assertEqual(symbol_from_filename("MRK.US_20251114_101500.json"), "MRK.US")
        self.assertEqual(symbol_from_filename("data/api_downloads/mrk_20251114_101500.csv"), "MRK")
        self.assertIsNone(symbol_from_filename("SampleAPI_JasonData.txt"))
        # a file named after the stock only (e.g. data/api_downloads/PFE.json)
        self.assertEqual(symbol_from_filename("data/api_downloads/PFE.json"), "PFE")
        self.assertEqual(symbol_from_filename("brk-b.us.csv"), "BRK-B.US")
        self.assertIsNone(symbol_from_filename("notes.md"))

    def test_validation(self):
        good, bad = validate_records(SAMPLE)
        self.assertEqual((len(good), bad), (251, []))
        self.assertEqual(good[0].trade_date, "2025-11-14")
        self.assertEqual(good[0].values(), (92.2, 95.08, 91.49, 92.92, 90.2693, 19632000))
        records = [{"date": "2025-11-14", "open": 96, "high": "97.5", "low": 95, "close": 96.5,
                    "adjusted_close": 96.5, "volume": 1000.0},                                   # ints / strings ok
                   {"date": "2025-11-14", "open": 1, "high": 1, "low": 1, "close": 1, "adjusted_close": 1,
                    "volume": 1},                                                                # same date again
                   {"date": "14.11.2025", "open": True, "high": "x", "low": -1, "close": float("nan"),
                    "volume": 1.5},
                   "not an object"]
        good, bad = validate_records(records)
        self.assertEqual([g.values() for g in good], [(96.0, 97.5, 95.0, 96.5, 96.5, 1000)])
        messages = [r.message for r in bad]
        self.assertIn("2025-11-14 appears more than once in the response", messages)
        for text in ("date must look like YYYY-MM-DD", "open is not a number", "high is not a number",
                     "low must not be negative", "close is not a finite number", "adjusted_close is missing",
                     "volume must be a whole number", "the record is not an object"):
            self.assertIn(text, messages)
        self.assertEqual({r.number for r in bad}, {2, 3, 4})

    def test_parsing(self):
        rows, first = parse_response(json.dumps({"data": SAMPLE[:2]}), "json")
        self.assertEqual((len(rows), first), (2, 1))
        rows, first = parse_response("Date,Open,High,Low,Close,Adjusted_close,Volume\n"
                                     "2025-11-14,92.2,95.08,91.49,92.92,90.2693,19632000\n\n", "csv")
        self.assertEqual((rows[0]["adjusted_close"], first), ("90.2693", 2))
        with self.assertRaisesRegex(ValueError, "Invalid API token"):
            parse_response('{"message": "Invalid API token"}', "json")
        with self.assertRaisesRegex(ValueError, "lacks"):
            parse_response("Date,Close\n2025-11-14,1\n", "csv")


class LoadTests(StageTestCase):
    def test_the_sample_response(self):
        result = self.loader.load_file(SAMPLE_RESPONSE, symbol="mrk")
        self.assertTrue(result.ok, result.message)
        self.assertEqual((result.received, result.inserted, result.updated, result.unchanged), (251, 251, 0, 0))
        self.assertEqual((result.first_date, result.last_date), ("2025-11-14", "2026-11-13"))
        self.assertEqual(self.staged(), EXPECTED)
        types = self.query('SELECT DISTINCT typeof("Open"), typeof("AdjustedClose"), typeof("Volume") '
                           'FROM "Stock_EOD_Stage"')
        self.assertEqual(types, [("real", "real", "integer")])
        row = self.loader.rows("MRK", limit=1)[0]
        self.assertEqual((row["Symbol"], row["Ticker"], row["Exchange"], row["SourceFile"]),
                         ("MRK", "MRK", None, "SampleAPI_JasonData.txt"))
        self.assertEqual(row["LoadedAt"], row["UpdatedAt"])
        [history] = StageDatabase(self.settings).load_history()
        self.assertEqual((history.file_name, history.status, history.rows_loaded), ("SampleAPI_JasonData.txt", "loaded", 251))
        self.assertIn("251 inserted", history.message)
        events = [l.event for l in AuditLogger(self.settings).fetch_logs(component="eod_stage")]
        self.assertEqual(events, ["eod_loaded"])

    def test_overlapping_responses_keep_one_row_per_day(self):
        self.loader.load_file(SAMPLE_RESPONSE, symbol="MRK")
        again = self.loader.load_file(SAMPLE_RESPONSE, symbol="MRK")
        self.assertEqual((again.inserted, again.updated, again.unchanged), (0, 0, 251))
        changed = [dict(r) for r in SAMPLE[-3:]]
        changed[0]["adjusted_close"] = 150.0                                    # e.g. after a dividend
        changed.append({"date": "2026-11-16", "open": 148.0, "high": 149.0, "low": 147.0, "close": 148.5,
                        "adjusted_close": 148.5, "volume": 5000000})
        result = self.loader.load_file(self.write_json("MRK_20261116_220000.json", changed))
        self.assertEqual((result.symbol, result.inserted, result.updated, result.unchanged), ("MRK", 1, 1, 2))
        self.assertEqual(self.loader.count("MRK"), 252)
        row = self.loader.rows("MRK", date_from=SAMPLE[-3]["date"], date_to=SAMPLE[-3]["date"])[0]
        self.assertEqual((row["AdjustedClose"], row["SourceFile"]), (150.0, "MRK_20261116_220000.json"))
        self.assertGreaterEqual(row["UpdatedAt"], row["LoadedAt"])
        # another symbol of the same data is a different series
        self.loader.load_file(SAMPLE_RESPONSE, symbol="MRK.US")
        self.assertEqual([(s.symbol, s.exchange, s.rows) for s in self.loader.summary()],
                         [("MRK", None, 252), ("MRK.US", "US", 251)])
        statuses = [h.status for h in StageDatabase(self.settings).load_history()]
        self.assertEqual(statuses, ["loaded"] * 4)                  # upserted per day: no load is 'replaced'
        self.assertEqual(self.loader.count("MRK"), 252)             # MRK.US did not touch the MRK rows

    def test_invalid_records(self):
        records = [dict(r) for r in SAMPLE[:4]]
        records[1]["volume"] = -5
        records[2]["close"] = None
        path = self.write_json("MRK_20251120_000000.json", records)
        result = self.loader.load_file(path)
        self.assertFalse(result.ok)
        self.assertIn("2 of 4 record(s) are invalid", result.message)
        self.assertEqual(self.loader.count(), 0)                                # nothing was loaded
        self.assertEqual(StageDatabase(self.settings).load_history()[0].status, "failed")
        errors = DataErrorRepository(self.settings).fetch(source_file=path.name)
        self.assertEqual({(e.rule, e.trade_date, e.column_name, e.line_no) for e in errors},
                         {("eod_record", "2025-11-17", "volume", 2), ("eod_record", "2025-11-18", "close", 3)})
        result = self.loader.load_file(path, skip_bad_rows=True)
        self.assertTrue(result.ok)
        self.assertEqual((result.inserted, result.rejected_records), (2, 2))
        self.assertIn("2 rejected", str(result))
        self.assertEqual([r[0] for r in self.staged()], ["2025-11-14", "2025-11-19"])

    def test_unreadable_responses_and_usage_errors(self):
        result = self.loader.load_file(self.write("MRK_20251120_000000.json", '{"message": "Invalid API token"}'))
        self.assertFalse(result.ok)
        self.assertIn("the API said: Invalid API token", result.message)
        self.assertFalse(self.loader.load_file(self.write("MRK_20251120_000001.json", "<html>")).ok)
        with self.assertRaisesRegex(EodStageError, "--symbol"):
            self.loader.load_file(SAMPLE_RESPONSE)
        with self.assertRaisesRegex(EodStageError, "not found"):
            self.loader.load_file(self.root / "missing.json", "MRK")
        with self.assertRaisesRegex(EodStageError, "not a valid symbol"):
            self.loader.load_file(SAMPLE_RESPONSE, "MRK US")
        empty = self.loader.load_file(self.write_json("MRK_20251120_000002.json", []))
        self.assertTrue(empty.ok)
        self.assertEqual(empty.received, 0)

    def test_csv_answers_load_the_same_values(self):
        result = EodApiClient(self.settings, transport=FakeEodGateway()).fetch("MRK", fmt="csv")
        self.assertTrue(result.stage.ok, result.stage.message)
        self.assertEqual(self.staged(), EXPECTED)

    def test_delete_symbol(self):
        self.loader.load_file(SAMPLE_RESPONSE, symbol="MRK")
        self.assertEqual(self.loader.delete_symbol("mrk"), 251)
        self.assertEqual(self.loader.summary(), [])


class FetchAndLoadTests(StageTestCase):
    def test_an_api_call_is_loaded(self):
        client = EodApiClient(self.settings, transport=FakeEodGateway())
        result = client.fetch("MRK.US")
        self.assertTrue(result.ok)
        self.assertEqual((result.stage.symbol, result.stage.inserted), ("MRK.US", 251))
        self.assertEqual(result.stage.source_file, result.path.name)
        self.assertEqual(self.staged("MRK.US"), EXPECTED)
        self.assertIsNone(client.fetch("AAPL", load=False).stage)
        self.settings.eod_load_to_stage = False
        self.assertIsNone(client.fetch("MSFT").stage)
        self.assertEqual([s.symbol for s in self.loader.summary()], ["MRK.US"])

    def test_the_live_client_loads_the_answer(self):
        with mock.patch("urllib.request.urlopen", return_value=FakeHttpResponse(SAMPLE_RESPONSE.read_bytes())):
            result = EodApiClient(self.settings, transport=UrllibGetter()).fetch("MRK")
        self.assertEqual(result.stage.inserted, 251)
        self.assertEqual(self.staged(), EXPECTED)

    def test_other_formats_are_saved_but_not_loaded(self):
        gateway = mock.Mock()
        gateway.get.return_value = mock.Mock(status=200, body=b"<eod/>", content_type="text/xml")
        result = EodApiClient(self.settings, transport=gateway).fetch("MRK", fmt="xml")
        self.assertTrue(result.path.is_file())
        self.assertIsNone(result.stage)


class ConfigAndCliTests(StageTestCase):
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
        self.assertIn("stage_table = Stock_EOD_Stage", text)
        self.assertIn("load_to_stage = true", text)
        self.settings.config_file.write_text(text.replace("stage_table = Stock_EOD_Stage", "stage_table = Eod_Raw")
                                             .replace("load_to_stage = true", "load_to_stage = false"), encoding="utf-8")
        s = load_settings(self.settings.config_file)
        self.assertEqual((s.eod_stage_table, s.eod_load_to_stage), ("Eod_Raw", False))
        result = EodStageLoader(s).load_file(SAMPLE_RESPONSE, "MRK")
        self.assertEqual((result.table, result.inserted), ("Eod_Raw", 251))
        self.settings.config_file.write_text(text.replace("stage_table = Stock_EOD_Stage", "stage_table = Stock_Stage"),
                                             encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_settings(self.settings.config_file)

    def test_commands(self):
        rc, out, err = self.run_cli("init-db")
        self.assertEqual(rc, 0, err)
        self.assertIn("EOD API staging tables ready: Stock_EOD_Stage, Stock_Symbol_Stage", out)
        self.assertIn("Stock_EOD_Stage is empty", self.run_cli("eod-stage")[1])
        rc, out, err = self.run_cli("eod-load", str(SAMPLE_RESPONSE))
        self.assertEqual(rc, 1)
        self.assertIn("--symbol", err)
        rc, out, err = self.run_cli("eod-load", str(SAMPLE_RESPONSE), "--symbol", "MRK")
        self.assertEqual(rc, 0, err)
        self.assertIn("MRK: 251 record(s) 2025-11-14 .. 2026-11-13, loaded into Stock_EOD_Stage - 251 inserted", out)
        rc, out, err = self.run_cli("eod-fetch", "AAPL.US", "--fake", "--fmt", "csv", "--from", "2026-11-01")
        self.assertEqual(rc, 0, err)
        self.assertIn("AAPL.US: 10 record(s) 2026-11-02 .. 2026-11-13, loaded into Stock_EOD_Stage - 10 inserted", out)
        rc, out, _ = self.run_cli("eod-fetch", "MSFT", "--fake", "--no-load")
        self.assertNotIn("loaded into", out)
        rc, out, _ = self.run_cli("eod-stage")
        self.assertIn("AAPL.US        AAPL       US", out)
        self.assertIn("261 row(s) of 2 symbol(s) in Stock_EOD_Stage", out)
        rc, out, _ = self.run_cli("eod-stage", "--symbol", "MRK", "--limit", "2")
        self.assertIn("2026-11-13", out)
        self.assertIn("(2 of 251 row(s), newest first)", out)
        self.assertIn("Stock_EOD_Stage: 261 rows of 2 symbol(s)", self.run_cli("status")[1])
        bad = self.write_json("MRK_20251120_000000.json", [{**SAMPLE[0], "volume": "lots"}, SAMPLE[1]])
        rc, out, err = self.run_cli("eod-load", str(bad))
        self.assertEqual(rc, 1)
        self.assertIn("rejected record 1 (2025-11-14): volume is not a number", err)
        rc, out, err = self.run_cli("eod-load", str(bad), "--skip-bad-rows")
        self.assertEqual(rc, 0, err)
        self.assertIn("1 unchanged, 1 rejected", out)
        self.assertIn("251 row(s) of MRK deleted", self.run_cli("eod-stage", "--delete", "MRK")[1])
        self.assertIn("Stock_EOD_Stage (10 rows)", self.run_cli("db", "reset-data")[1])

    def test_files_named_after_the_stock(self):
        """api_downloads/PFE.json: the symbol is the file name - no --symbol needed."""
        shutil.copy(SAMPLE_RESPONSE, self.settings.eod_download_dir / "PFE.json")
        rc, out, err = self.run_cli("eod-load", str(self.settings.eod_download_dir / "PFE.json"))
        self.assertEqual(rc, 0, err)
        self.assertIn("PFE: 251 record(s) 2025-11-14 .. 2026-11-13, loaded into Stock_EOD_Stage - 251 inserted", out)


if __name__ == "__main__":
    unittest.main()
