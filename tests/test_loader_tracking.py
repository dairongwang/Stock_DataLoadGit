"""Loader integration tests: Load_History de-duplication, App_Log/App_Error
tracking and the Data_Error quality checks."""

import shutil
import unittest

from stock_dataload.audit import AuditLogger
from stock_dataload.db import StageDatabase, file_sha256
from stock_dataload.loader import StageLoader
from stock_dataload.quality import DataErrorRepository, DataQualityChecker

from .helpers import DEFAULT_HEADER, ORIGINAL, TempProjectTestCase, register_listed


class TrackingTestCase(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.db = StageDatabase(self.settings)
        self.audit = AuditLogger(self.settings)
        self.repo = DataErrorRepository(self.settings)
        self.loader = StageLoader(self.settings, self.db, audit=self.audit, data_errors=self.repo)

    def stage(self, src=ORIGINAL, name=None):
        dest = self.settings.loading_dir / (name or src.name)
        shutil.copy(src, dest)
        return dest

    def events(self, component=None):
        return [r.event for r in reversed(self.audit.fetch_logs(limit=1000, component=component))]


class HashAndHistoryTests(TrackingTestCase):
    def test_file_sha256_stable_and_content_sensitive(self):
        a = self.write("a.txt", "hello\n")
        b = self.write("b.txt", "hello\n")
        c = self.write("c.txt", "hello!\n")
        self.assertEqual(file_sha256(a), file_sha256(b))
        self.assertNotEqual(file_sha256(a), file_sha256(c))
        self.assertEqual(len(file_sha256(a)), 64)

    def test_history_records_successful_load(self):
        self.stage()
        r = self.loader.run()[0]
        history = self.db.load_history()
        self.assertEqual(len(history), 1)
        h = history[0]
        self.assertEqual(h.file_name, "NASDAQ_20250123.txt")
        self.assertEqual(h.file_hash, r.file_hash)
        self.assertEqual(h.load_id, r.load_id)
        self.assertEqual(h.status, "loaded")
        self.assertEqual(h.rows_loaded, 4092)
        self.assertEqual(h.market, "NASDAQ")
        self.assertEqual(h.file_size, ORIGINAL.stat().st_size)
        self.assertTrue(h.loaded_at)

    def test_history_records_failed_load(self):
        self.write("loading/BAD_20250107.txt", "wrong,header\n1,2\n")
        self.loader.run()
        h = self.db.load_history()[0]
        self.assertEqual(h.status, "failed")
        self.assertEqual(h.rows_loaded, 0)
        self.assertIn("not the default column sequence", h.message)

    def test_failed_load_is_not_a_duplicate_blocker(self):
        """A failed attempt must not prevent the fixed re-delivery from loading."""
        self.settings.skip_bad_rows = False
        path = self.write("loading/NYSE_20250110.txt",
                          DEFAULT_HEADER + "AAA,2025-01-10,1,2,0.5,1.5,100\n")
        bad_hash = file_sha256(path)
        self.assertEqual(self.loader.run()[0].status, "failed")
        # same name, fixed content -> loads
        self.write("loading/NYSE_20250110.txt", DEFAULT_HEADER + "AAA,20250110,1,2,0.5,1.5,100\n")
        r = self.loader.run()[0]
        self.assertTrue(r.ok, r.message)
        self.assertNotEqual(r.file_hash, bad_hash)

    def test_find_previous_load(self):
        self.assertIsNone(self.db.find_previous_load("NASDAQ_20250123.txt"))
        self.stage()
        r = self.loader.run()[0]
        hit = self.db.find_previous_load("NASDAQ_20250123.txt")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.load_id, r.load_id)
        self.assertEqual(hit.file_hash, r.file_hash)
        self.assertIsNone(self.db.find_previous_load("other.txt"))

    def test_mark_history_replaced(self):
        self.stage()
        self.loader.run()
        with self.db.connection() as conn:
            changed = self.db.mark_history_replaced(conn, "NASDAQ_20250123.txt")
        self.assertEqual(changed, 1)
        self.assertEqual(self.db.load_history()[0].status, "replaced")

    def test_truncate_reload_counts_no_replaced_rows(self):
        self.stage()
        self.loader.run()
        self.stage()
        r = self.loader.run(truncate=True)[0]
        self.assertTrue(r.ok, r.message)
        self.assertEqual(r.rows_replaced, 0)            # truncate emptied the table instead
        self.assertEqual(self.db.count_rows(), 4092)
        self.assertEqual([h.status for h in self.db.load_history()], ["loaded", "replaced"])

    def test_load_history_limit_and_filter(self):
        self.write("loading/A_20250107.txt", DEFAULT_HEADER + "AAA,20250107,1,2,0.5,1.5,100\n")
        self.write("loading/B_20250107.txt", DEFAULT_HEADER + "BBB,20250107,1,2,0.5,1.5,100\n")
        self.loader.run()
        self.assertEqual(len(self.db.load_history()), 2)
        self.assertEqual(len(self.db.load_history(limit=1)), 1)
        only_a = self.db.load_history(file_name="A_20250107.txt")
        self.assertEqual([h.file_name for h in only_a], ["A_20250107.txt"])


class DistinctTickersTests(TrackingTestCase):
    def test_distinct_tickers_per_market_and_exclusion(self):
        self.write("loading/NYSE_20250109.txt",
                   DEFAULT_HEADER + "AAA,20250109,1,2,0.5,1.5,100\nBBB,20250109,1,2,0.5,1.5,100\n")
        self.write("loading/OTC_20250109.txt", DEFAULT_HEADER + "CCC,20250109,1,2,0.5,1.5,100\n")
        self.loader.run()
        self.assertEqual(self.db.distinct_tickers(), {"AAA", "BBB", "CCC"})
        self.assertEqual(self.db.distinct_tickers(market="NYSE"), {"AAA", "BBB"})
        self.assertEqual(self.db.distinct_tickers(market="NYSE",
                                                  exclude_source_file="NYSE_20250109.txt"),
                         frozenset())


class AppLogTrackingTests(TrackingTestCase):
    def test_successful_load_is_tracked_in_app_log(self):
        self.stage()
        self.loader.run()
        events = self.events("loader")
        self.assertIn("load_started", events)
        self.assertIn("file_loaded", events)
        self.assertIn("run_complete", events)
        loaded = [r for r in self.audit.fetch_logs() if r.event == "file_loaded"][0]
        self.assertEqual(loaded.source_file, "NASDAQ_20250123.txt")
        self.assertIn("4092 rows", loaded.message)

    def test_reload_is_tracked(self):
        self.stage()
        first = self.loader.run()[0]
        self.stage()
        second = self.loader.run()[0]
        events = self.events("loader")
        self.assertIn("reload_detected", events)
        self.assertIn("previous_load_replaced", events)
        detected = [r for r in self.audit.fetch_logs() if r.event == "reload_detected"][0]
        self.assertIn("identical content", detected.message)
        self.assertEqual(detected.load_id, second.load_id)
        replaced = [r for r in self.audit.fetch_logs() if r.event == "previous_load_replaced"][0]
        self.assertIn("4092 rows of the previous load deleted", replaced.message)
        self.assertIn(first.load_id, replaced.details)

    def test_reload_with_changed_content_is_tracked_as_such(self):
        self.write("loading/NYSE_20250110.txt", DEFAULT_HEADER + "AAA,20250110,1,2,0.5,1.5,100\n")
        self.loader.run()
        self.write("loading/NYSE_20250110.txt", DEFAULT_HEADER + "AAA,20250110,1,2,0.5,1.6,100\n")
        self.loader.run()
        detected = [r for r in self.audit.fetch_logs() if r.event == "reload_detected"][0]
        self.assertIn("changed content", detected.message)

    def test_failed_load_is_tracked_in_app_error_with_step(self):
        self.write("loading/BAD_20250107.txt", "wrong,header\n1,2\n")
        self.loader.run()
        self.assertIn("load_failed", self.events("loader"))
        errors = self.audit.fetch_errors()
        self.assertEqual(len(errors), 1)
        e = errors[0]
        self.assertEqual(e.component, "loader")
        self.assertEqual(e.operation, "insert_rows")          # the step that failed
        self.assertEqual(e.error_type, "FileFormatError")
        self.assertEqual(e.source_file, "BAD_20250107.txt")
        self.assertIn("not the default column sequence", e.error_message)

    def test_missing_file_fails_in_check_file_step(self):
        r = self.loader.load_file(self.root / "ghost.txt")
        self.assertEqual(r.status, "failed")
        self.assertEqual(self.audit.fetch_errors()[0].operation, "check_file")


class QualityIntegrationTests(TrackingTestCase):
    def test_clean_file_has_no_data_errors(self):
        self.stage()
        r = self.loader.run()[0]
        self.assertTrue(r.ok)
        self.assertEqual(r.data_errors, 0)
        self.assertEqual(r.alerts, 0)
        self.assertEqual(self.repo.count(), 0)

    def test_high_below_low_recorded_in_data_error(self):
        # loads fine (staging lands data as delivered) but is flagged
        self.write("loading/NYSE_20250110.txt",
                   DEFAULT_HEADER + "AAA,20250110,1,0.4,0.5,1.5,100\n")
        r = self.loader.run()[0]
        self.assertTrue(r.ok, r.message)
        self.assertEqual(r.data_errors, 1)
        rec = self.repo.fetch()[0]
        self.assertEqual(rec.rule, "high_above_low")
        self.assertEqual(rec.ticker, "AAA")
        self.assertEqual(rec.trade_date, "20250110")
        self.assertEqual(rec.source_file, "NYSE_20250110.txt")
        self.assertEqual(rec.load_id, r.load_id)
        self.assertEqual(rec.market, "NYSE")
        self.assertEqual(rec.line_no, 2)

    def test_invalid_calendar_date_recorded(self):
        self.write("loading/NYSE_20250110.txt",
                   DEFAULT_HEADER + "AAA,20251399,1,2,0.5,1.5,100\n")
        r = self.loader.run()[0]
        self.assertTrue(r.ok, r.message)   # YYYYMMDD shape passes the loader...
        self.assertEqual([x.rule for x in self.repo.fetch()], ["date_is_valid"])

    def test_missing_market_day_alert(self):
        register_listed(self.settings, "NYSE", ["AAA", "BBB"])
        # day 1 establishes AAA and BBB as known NYSE stocks
        self.write("loading/NYSE_20250109.txt",
                   DEFAULT_HEADER + "AAA,20250109,1,2,0.5,1.5,100\nBBB,20250109,1,2,0.5,1.5,100\n")
        self.loader.run()
        # day 2 (Friday, market open): BBB is missing
        self.write("loading/NYSE_20250110.txt", DEFAULT_HEADER + "AAA,20250110,1,2,0.5,1.5,100\n")
        r = self.loader.run()[0]
        self.assertTrue(r.ok)
        self.assertEqual(r.alerts, 1)
        alerts = self.repo.fetch(alerts_only=True)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].ticker, "BBB")
        self.assertEqual(alerts[0].trade_date, "20250110")
        self.assertEqual(alerts[0].rule, "missing_market_day")
        self.assertEqual(alerts[0].alert, 1)
        # the alert is raised loudly in the activity log too
        alert_logs = [x for x in self.audit.fetch_logs(level="ALERT")]
        self.assertEqual(len(alert_logs), 1)
        self.assertIn("BBB", alert_logs[0].message)

    def test_no_alert_for_weekend_file(self):
        self.write("loading/NYSE_20250110.txt", DEFAULT_HEADER + "AAA,20250110,1,2,0.5,1.5,100\n")
        self.loader.run()
        # Saturday: nobody expects data - the delivery is not even loaded
        self.write("loading/NYSE_20250111.txt", DEFAULT_HEADER + "ZZZ,20250111,1,2,0.5,1.5,100\n")
        r = self.loader.run()[0]
        self.assertTrue(r.skipped)
        self.assertEqual(r.alerts, 0)
        # ... unless asked for: then the missing-day check knows it is a closed day
        self.write("loading/NYSE_20250111.txt", DEFAULT_HEADER + "ZZZ,20250111,1,2,0.5,1.5,100\n")
        r = self.loader.run(include_no_trading_days=True)[0]
        self.assertTrue(r.ok)
        self.assertEqual(r.alerts, 0)

    def test_extra_holiday_suppresses_alert(self):
        self.settings.extra_market_holidays = ("2025-01-10",)
        self.write("loading/NYSE_20250109.txt", DEFAULT_HEADER + "AAA,20250109,1,2,0.5,1.5,100\n")
        self.loader.run()
        self.write("loading/NYSE_20250110.txt", DEFAULT_HEADER + "BBB,20250110,1,2,0.5,1.5,100\n")
        r = self.loader.run()[0]
        self.assertEqual(r.alerts, 0)

    def test_stock_with_broken_row_counts_as_missing(self):
        register_listed(self.settings, "NYSE", ["AAA", "BBB"])
        self.settings.skip_bad_rows = True
        self.write("loading/NYSE_20250109.txt",
                   DEFAULT_HEADER + "AAA,20250109,1,2,0.5,1.5,100\nBBB,20250109,1,2,0.5,1.5,100\n")
        self.loader.run()
        # BBB delivers, but its only row is rejected -> alert
        self.write("loading/NYSE_20250110.txt",
                   DEFAULT_HEADER + "AAA,20250110,1,2,0.5,1.5,100\nBBB,20250110,x,2,0.5,1.5,100\n")
        r = self.loader.run()[0]
        self.assertTrue(r.ok)
        self.assertEqual(r.alerts, 1)
        rules = {x.rule for x in self.repo.fetch()}
        self.assertIn("row_format", rules)             # the rejected row is tracked
        self.assertIn("missing_market_day", rules)
        alert = self.repo.fetch(alerts_only=True)[0]
        self.assertEqual(alert.ticker, "BBB")

    def test_rejected_rows_recorded_even_when_file_fails(self):
        self.settings.skip_bad_rows = False
        self.write("loading/NYSE_20250110.txt",
                   DEFAULT_HEADER + "AAA,20250110,x,2,0.5,1.5,100\n")
        r = self.loader.run()[0]
        self.assertEqual(r.status, "failed")
        recs = self.repo.fetch()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].rule, "row_format")
        self.assertEqual(recs[0].ticker, "AAA")
        self.assertIn("not numeric", recs[0].error_message)

    def test_quality_checks_can_be_disabled(self):
        self.settings.quality_checks_enabled = False
        loader = StageLoader(self.settings, self.db, audit=self.audit, data_errors=self.repo)
        self.write("loading/NYSE_20250110.txt",
                   DEFAULT_HEADER + "AAA,20250110,1,0.4,0.5,1.5,100\n")
        r = loader.run()[0]
        self.assertTrue(r.ok)
        self.assertEqual(r.data_errors, 0)
        self.assertEqual(self.repo.count(), 0)

    def test_alert_missing_days_can_be_disabled(self):
        self.settings.alert_missing_days = False
        loader = StageLoader(self.settings, self.db, audit=self.audit, data_errors=self.repo)
        self.assertNotIn("missing_market_day", loader.checker.strategy_names())
        self.write("loading/NYSE_20250109.txt", DEFAULT_HEADER + "AAA,20250109,1,2,0.5,1.5,100\n")
        loader.run()
        self.write("loading/NYSE_20250110.txt", DEFAULT_HEADER + "BBB,20250110,1,2,0.5,1.5,100\n")
        r = loader.run()[0]
        self.assertEqual(r.alerts, 0)

    def test_custom_checker_is_honoured(self):
        checker = DataQualityChecker(strategies=[])      # business rules switched off
        loader = StageLoader(self.settings, self.db, audit=self.audit, checker=checker,
                             data_errors=self.repo)
        self.write("loading/NYSE_20250110.txt",
                   DEFAULT_HEADER + "AAA,20250110,1,0.4,0.5,1.5,100\n")
        r = loader.run()[0]
        self.assertEqual(r.data_errors, 0)

    def test_quality_issue_details_on_result(self):
        self.write("loading/NYSE_20250110.txt",
                   DEFAULT_HEADER + "AAA,20250110,1,0.4,0.5,1.5,100\n")
        r = self.loader.run()[0]
        self.assertEqual(len(r.quality_issues), 1)
        self.assertIn("data error", str(r))


if __name__ == "__main__":
    unittest.main()
