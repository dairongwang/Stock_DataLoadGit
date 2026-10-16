"""Unit tests for stock_dataload.correction (data correction of staging rows)."""

import sqlite3
import unittest
import unittest.mock as mock

from stock_dataload.audit import AuditLogger
from stock_dataload.correction import (DATA_CORRECTION_TABLE, CorrectionError, CorrectionResult,
                                       CorrectionService, clean_values, values_from_data_error)
from stock_dataload.db import MANUAL_LOAD_ID, StageDatabase
from stock_dataload.loader import StageLoader
from stock_dataload.quality import DATA_ERROR_TABLE, DataErrorRecord, DataErrorRepository, QualityIssue

from .helpers import DEFAULT_HEADER, TempProjectTestCase

GOOD = {"Market": "NYSE", "Ticker": "AAA", "TradeDate": "20250110", "Open": "1", "High": "2",
        "Low": "0.5", "Close": "1.5", "Vol": "100"}


class CorrectionTestCase(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.db = StageDatabase(self.settings)
        self.audit = AuditLogger(self.settings)
        self.repo = DataErrorRepository(self.settings)
        self.loader = StageLoader(self.settings, self.db, audit=self.audit, data_errors=self.repo)
        self.service = CorrectionService(self.settings, self.db, audit=self.audit, data_errors=self.repo)

    def load(self, name, body):
        self.write(f"loading/{name}", DEFAULT_HEADER + body)
        return self.loader.run()[0]

    def row_of(self, ticker, trade_date):
        rows = self.db.find_stage_rows(ticker=ticker, trade_date=trade_date)
        self.assertEqual(len(rows), 1)
        return rows[0]


class HelperTests(unittest.TestCase):
    def test_clean_values_keeps_editable_columns_only(self):
        out = clean_values({"Ticker": " AAA ", "Open": 1.5, "StageId": 9, "LoadId": "x", "Vol": None})
        self.assertEqual(out, {"Ticker": "AAA", "Open": "1.5"})

    def test_values_from_data_error_prefills_ticker_and_date(self):
        rec = DataErrorRecord(1, "t", "f.txt", "l", "NYSE", "BBB", "20250110", None, None,
                              "missing_market_day", "alert", 1, "msg", None)
        self.assertEqual(values_from_data_error(rec), {"Market": "NYSE", "Ticker": "BBB", "TradeDate": "20250110"})

    def test_values_from_data_error_prefills_rejected_row(self):
        rec = DataErrorRecord(1, "t", "f.txt", "l", "NYSE", "CCC", "20250110", None,
                              "CCC,20250110,x,2,0.5,1.5,100", "row_format", "error", 0, "open not numeric", 3)
        values = values_from_data_error(rec)
        self.assertEqual(values["Open"], "x")
        self.assertEqual(values["Vol"], "100")
        self.assertEqual(values["Ticker"], "CCC")

    def test_values_from_data_error_ignores_malformed_raw_row(self):
        rec = DataErrorRecord(1, "t", "f.txt", "l", "NYSE", "CCC", "20250110", None, "a,b", "row_format",
                              "error", 0, "m", 3)
        self.assertNotIn("Open", values_from_data_error(rec))

    def test_result_str(self):
        r = CorrectionResult("updated", 5, {"Ticker": "A"}, [("High", "1", "2")], resolved_errors=1)
        self.assertIn("High: '1' -> '2'", str(r))
        self.assertIn("1 data error(s) resolved", str(r))
        self.assertIn("nothing changed", str(CorrectionResult("unchanged", 5, {})))
        self.assertIn("inserted", str(CorrectionResult("inserted", 6, {"Ticker": "B", "TradeDate": "20250101"})))


class ValidateTests(CorrectionTestCase):
    def test_valid_row_has_no_issues(self):
        self.assertEqual(self.service.validate(GOOD), [])

    def test_required_fields(self):
        issues = self.service.validate(dict(GOOD, Ticker="", TradeDate=""))
        self.assertIn("ticker is required", [i.message for i in issues])
        self.assertIn("trade date is required", [i.message for i in issues])

    def test_business_rules_applied(self):
        self.assertTrue(any("calendar date" in i.message for i in self.service.validate(dict(GOOD, TradeDate="20251399"))))
        self.assertTrue(any("not a number" in i.message for i in self.service.validate(dict(GOOD, Close="abc"))))
        self.assertTrue(any("smaller than low" in i.message for i in self.service.validate(dict(GOOD, High="0.1"))))

    def test_issue_columns_use_file_names(self):
        issues = self.service.validate(dict(GOOD, Vol="x"))
        self.assertEqual(issues[0].column, "vol")


class CorrectTests(CorrectionTestCase):
    def test_update_changes_values_and_tracks_them(self):
        self.load("NYSE_20250110.txt", "AAA,20250110,1,0.4,0.5,1.5,100\n")
        row = self.row_of("AAA", "20250110")
        error_id = self.repo.fetch()[0].data_error_id
        result = self.service.correct(row["StageId"], dict(row, High="2"), note="swapped high/low",
                                      data_error_id=error_id)
        self.assertEqual(result.action, "updated")
        self.assertEqual(result.changes, [("High", "0.4", "2")])
        self.assertEqual(result.resolved_errors, 1)
        self.assertEqual(self.db.get_stage_row(row["StageId"])["High"], "2")
        # tracked in Data_Correction
        history = self.service.history(stage_id=row["StageId"])
        self.assertEqual(len(history), 1)
        h = history[0]
        self.assertEqual((h.action, h.column_name, h.old_value, h.new_value), ("updated", "High", "0.4", "2"))
        self.assertEqual(h.data_error_id, error_id)
        self.assertEqual(h.note, "swapped high/low")
        self.assertEqual(h.ticker, "AAA")
        self.assertEqual(self.service.count(), 1)
        # the data error is resolved
        rec = self.repo.get(error_id)
        self.assertEqual(rec.resolved, 1)
        self.assertTrue(rec.resolved_at)
        self.assertEqual(rec.resolution_note, "swapped high/low")
        # and the activity is logged
        logs = [r for r in self.audit.fetch_logs() if r.event == "row_updated"]
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].component, "correction")
        self.assertIn("High", logs[0].details)

    def test_update_resolves_every_open_finding_of_the_row(self):
        # high<low AND an impossible calendar date (passes the loader's shape check)
        self.load("NYSE_20250110.txt", "AAA,20251399,1,0.4,0.5,1.5,100\n")
        self.assertEqual(self.repo.count(open_only=True), 2)
        row = self.row_of("AAA", "20251399")
        result = self.service.correct(row["StageId"], dict(row, High="2", TradeDate="20250110"))
        self.assertEqual(result.resolved_errors, 2)                 # found under the original key
        self.assertEqual(self.repo.count(open_only=True), 0)
        self.assertEqual(self.repo.count(), 2)

    def test_multiple_columns_changed(self):
        self.load("NYSE_20250110.txt", "AAA,20250110,1,2,0.5,1.5,100\n")
        row = self.row_of("AAA", "20250110")
        result = self.service.correct(row["StageId"], dict(row, Open="1.1", Vol="200", Market="NASDAQ"))
        self.assertEqual({c for c, _, _ in result.changes}, {"Open", "Vol", "Market"})
        self.assertEqual(len(self.service.history(stage_id=row["StageId"])), 3)

    def test_unchanged(self):
        self.load("NYSE_20250110.txt", "AAA,20250110,1,2,0.5,1.5,100\n")
        row = self.row_of("AAA", "20250110")
        result = self.service.correct(row["StageId"], row)
        self.assertEqual(result.action, "unchanged")
        self.assertEqual(self.service.count(), 0)

    def test_invalid_values_rejected(self):
        self.load("NYSE_20250110.txt", "AAA,20250110,1,2,0.5,1.5,100\n")
        row = self.row_of("AAA", "20250110")
        with self.assertRaisesRegex(CorrectionError, "invalid values"):
            self.service.correct(row["StageId"], dict(row, High="0.1"))
        self.assertEqual(self.db.get_stage_row(row["StageId"])["High"], "2")   # untouched

    def test_unknown_row_rejected(self):
        with self.assertRaisesRegex(CorrectionError, "does not exist"):
            self.service.correct(99999, GOOD)

    def test_database_failure_is_captured(self):
        self.load("NYSE_20250110.txt", "AAA,20250110,1,2,0.5,1.5,100\n")
        row = self.row_of("AAA", "20250110")
        with mock.patch.object(self.db, "update_stage_row", side_effect=sqlite3.OperationalError("locked")):
            with self.assertRaisesRegex(CorrectionError, "could not correct"):
                self.service.correct(row["StageId"], dict(row, High="3"))
        errors = self.audit.fetch_errors()
        self.assertEqual(errors[0].component, "correction")
        self.assertEqual(errors[0].operation, "correct_row")


class AddRowTests(CorrectionTestCase):
    def test_add_missing_row_resolves_alert(self):
        self.load("NYSE_20250109.txt", "AAA,20250109,1,2,0.5,1.5,100\nBBB,20250109,1,2,0.5,1.5,100\n")
        self.load("NYSE_20250110.txt", "AAA,20250110,1,2,0.5,1.5,100\n")
        alert = self.repo.fetch(alerts_only=True)[0]
        self.assertEqual(alert.ticker, "BBB")
        values = dict(values_from_data_error(alert), Open="1", High="2", Low="0.5", Close="1.5", Vol="100")
        result = self.service.add_row(values, source_file=alert.source_file, note="late delivery",
                                      data_error_id=alert.data_error_id)
        self.assertEqual(result.action, "inserted")
        self.assertEqual(result.resolved_errors, 1)
        row = self.db.get_stage_row(result.stage_id)
        self.assertEqual((row["Ticker"], row["TradeDate"], row["Market"]), ("BBB", "20250110", "NYSE"))
        self.assertEqual(row["SourceFile"], "NYSE_20250110.txt")
        self.assertEqual(row["LoadId"], MANUAL_LOAD_ID)
        self.assertEqual(self.repo.get(alert.data_error_id).resolved, 1)
        history = self.service.history(stage_id=result.stage_id)
        self.assertEqual({h.action for h in history}, {"inserted"})
        self.assertTrue(all(h.old_value is None for h in history))
        self.assertIn("row_inserted", [r.event for r in self.audit.fetch_logs()])

    def test_add_row_without_source_file(self):
        result = self.service.add_row(GOOD)
        self.assertEqual(self.db.get_stage_row(result.stage_id)["SourceFile"], MANUAL_LOAD_ID)

    def test_add_row_invalid_rejected(self):
        with self.assertRaisesRegex(CorrectionError, "invalid values"):
            self.service.add_row(dict(GOOD, TradeDate="2025-01-10"))
        self.db.ensure_stage_table()
        self.assertEqual(self.db.count_rows(), 0)

    def test_add_row_database_failure_is_captured(self):
        with mock.patch.object(self.db, "insert_stage_row", side_effect=sqlite3.OperationalError("full")):
            with self.assertRaisesRegex(CorrectionError, "could not add"):
                self.service.add_row(GOOD)
        self.assertEqual(self.audit.fetch_errors()[0].operation, "add_row")


class HistoryAndTableTests(CorrectionTestCase):
    def test_ensure_table_and_empty_history(self):
        self.service.ensure_table()
        with sqlite3.connect(self.settings.sqlite_path) as conn:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn(DATA_CORRECTION_TABLE, names)
        self.assertEqual(self.service.history(), [])
        self.assertEqual(self.service.count(), 0)

    def test_history_limit_and_order(self):
        self.load("NYSE_20250110.txt", "AAA,20250110,1,2,0.5,1.5,100\n")
        row = self.row_of("AAA", "20250110")
        self.service.correct(row["StageId"], dict(row, Open="1.1"))
        self.service.correct(row["StageId"], dict(row, Open="1.2"))
        history = self.service.history(limit=1)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].new_value, "1.2")

    def test_default_construction(self):
        service = CorrectionService(self.settings)
        self.assertEqual(service.checker.strategy_names(), ["date_is_valid", "numeric_columns", "high_above_low"])


class StageRowAccessTests(CorrectionTestCase):
    def test_get_and_find_rows(self):
        self.load("NYSE_20250110.txt", "AAA,20250110,1,2,0.5,1.5,100\nBBB,20250110,1,2,0.5,1.5,100\n")
        self.load("NASDAQ_20250110.txt", "AAA,20250110,3,4,2.5,3.5,100\n")
        self.assertIsNone(self.db.get_stage_row(12345))
        rows = self.db.find_stage_rows(ticker="AAA")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["StageId"] > rows[1]["StageId"], True)      # newest first
        self.assertEqual(len(self.db.find_stage_rows(ticker="AAA", market="NYSE")), 1)
        self.assertEqual(len(self.db.find_stage_rows(source_file="NYSE_20250110.txt")), 2)
        self.assertEqual(len(self.db.find_stage_rows(trade_date="20250111")), 0)
        self.assertEqual(len(self.db.find_stage_rows(limit=1)), 1)
        self.assertEqual(len(self.db.find_stage_rows()), 3)
        row = self.db.get_stage_row(rows[0]["StageId"])
        self.assertEqual(set(row), {"StageId", "Market", "Ticker", "TradeDate", "Open", "High", "Low", "Close",
                                    "Vol", "SourceFile", "LoadId", "LoadedAt"})

    def test_update_without_editable_values_is_noop(self):
        self.load("NYSE_20250110.txt", "AAA,20250110,1,2,0.5,1.5,100\n")
        row = self.row_of("AAA", "20250110")
        with self.db.connection() as conn:
            self.assertEqual(self.db.update_stage_row(conn, row["StageId"], {"LoadId": "x"}), 0)
            self.assertEqual(self.db.update_stage_row(conn, row["StageId"], {"Vol": "5"}), 1)
            self.assertEqual(self.db.update_stage_row(conn, 999999, {"Vol": "5"}), 0)


class DataErrorResolutionTests(CorrectionTestCase):
    def test_resolve_by_id_and_by_row_key(self):
        self.repo.record([QualityIssue("r", "m", ticker="AAA", trade_date="20250110"),
                          QualityIssue("r", "m2", ticker="AAA", trade_date="20250110"),
                          QualityIssue("r", "m3", ticker="BBB", trade_date="20250110")], source_file="f.txt")
        ids = [r.data_error_id for r in self.repo.fetch()]
        self.assertEqual(self.repo.resolve([ids[0]], "fixed"), 1)
        self.assertEqual(self.repo.resolve([ids[0]], "fixed"), 0)              # already resolved
        self.assertEqual(self.repo.resolve(source_file="f.txt", ticker="AAA", trade_date="20250110"), 2)
        self.assertEqual(self.repo.count(open_only=True), 0)
        self.assertEqual(self.repo.resolve(ticker="ZZZ", trade_date="20250110"), 0)
        self.assertEqual(self.repo.resolve(), 0)
        self.assertEqual(len(self.repo.fetch(open_only=True)), 0)
        self.assertEqual(len(self.repo.fetch()), 3)
        self.assertIsNone(self.repo.get(987654))

    def test_existing_database_is_migrated(self):
        """A Data_Error table from before the 'Resolved' columns gets them added."""
        with sqlite3.connect(self.settings.sqlite_path) as conn:
            conn.execute(f'CREATE TABLE "{DATA_ERROR_TABLE}" ("DataErrorId" INTEGER PRIMARY KEY AUTOINCREMENT, '
                         '"DetectedAt" TEXT NOT NULL, "SourceFile" TEXT, "LoadId" TEXT, "Market" TEXT, '
                         '"Ticker" TEXT, "TradeDate" TEXT, "ColumnName" TEXT, "Value" TEXT, "Rule" TEXT NOT NULL, '
                         '"Severity" TEXT NOT NULL, "Alert" INTEGER NOT NULL DEFAULT 0, '
                         '"ErrorMessage" TEXT NOT NULL, "LineNo" INTEGER)')
            conn.execute(f'INSERT INTO "{DATA_ERROR_TABLE}" ("DetectedAt","Rule","Severity","ErrorMessage") '
                         "VALUES ('t','r','error','old finding')")
        repo = DataErrorRepository(self.settings)
        records = repo.fetch()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].resolved, 0)
        self.assertEqual(repo.resolve([records[0].data_error_id]), 1)


if __name__ == "__main__":
    unittest.main()
