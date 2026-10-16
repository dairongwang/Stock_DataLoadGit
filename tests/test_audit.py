"""Unit tests for stock_dataload.audit (App_Log / App_Error tables)."""

import json
import sqlite3
import unittest

from stock_dataload.audit import (APP_ERROR_TABLE, APP_LOG_TABLE, AuditLogger)

from .helpers import TempProjectTestCase


class EnsureTablesTests(TempProjectTestCase):
    def test_tables_and_indexes_created(self):
        audit = AuditLogger(self.settings)
        audit.ensure_tables()
        with sqlite3.connect(self.settings.sqlite_path) as conn:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn(APP_LOG_TABLE, names)
            self.assertIn(APP_ERROR_TABLE, names)
            indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn(f"IX_{APP_LOG_TABLE}_LoggedAt", indexes)
        self.assertIn(f"IX_{APP_ERROR_TABLE}_OccurredAt", indexes)

    def test_ensure_tables_is_idempotent(self):
        audit = AuditLogger(self.settings)
        audit.ensure_tables()
        audit.ensure_tables()
        self.assertEqual(audit.count_logs(), 0)
        self.assertEqual(audit.count_errors(), 0)


class LogTests(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.audit = AuditLogger(self.settings)

    def test_log_records_activity(self):
        self.audit.log("loader", "load_started", "picking up file", file_name="X_20250101.txt",
                       load_id="abc123", details={"rows": 7})
        records = self.audit.fetch_logs()
        self.assertEqual(len(records), 1)
        r = records[0]
        self.assertEqual(r.level, "INFO")
        self.assertEqual(r.component, "loader")
        self.assertEqual(r.event, "load_started")
        self.assertEqual(r.source_file, "X_20250101.txt")
        self.assertEqual(r.load_id, "abc123")
        self.assertEqual(r.message, "picking up file")
        self.assertEqual(json.loads(r.details), {"rows": 7})
        self.assertTrue(r.logged_at)

    def test_log_levels_normalised(self):
        self.audit.log("x", "e1", level="warning")
        self.audit.log("x", "e2", level="ALERT")
        self.audit.log("x", "e3", level="bogus")      # unknown level -> INFO
        levels = [r.level for r in self.audit.fetch_logs()]
        self.assertEqual(levels, ["INFO", "ALERT", "WARNING"])  # newest first

    def test_fetch_logs_filters_and_limit(self):
        for i in range(5):
            self.audit.log("loader", f"event{i}")
        self.audit.log("converter", "converted", level="WARNING")
        self.assertEqual(len(self.audit.fetch_logs(limit=3)), 3)
        self.assertEqual(len(self.audit.fetch_logs(component="converter")), 1)
        self.assertEqual(len(self.audit.fetch_logs(level="warning")), 1)
        self.assertEqual(len(self.audit.fetch_logs(component="loader", level="INFO")), 5)

    def test_count_logs(self):
        self.assertEqual(self.audit.count_logs(), 0)
        self.audit.log("a", "b")
        self.audit.log("a", "c")
        self.assertEqual(self.audit.count_logs(), 2)


class ErrorTests(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.audit = AuditLogger(self.settings)

    def test_error_from_exception_captures_type_and_stacktrace(self):
        try:
            raise ValueError("boom")
        except ValueError as exc:
            self.audit.error("loader", "insert_rows", exc, file_name="f.txt", load_id="id1",
                             details={"step": 3})
        records = self.audit.fetch_errors()
        self.assertEqual(len(records), 1)
        r = records[0]
        self.assertEqual(r.component, "loader")
        self.assertEqual(r.operation, "insert_rows")
        self.assertEqual(r.error_type, "ValueError")
        self.assertEqual(r.error_message, "boom")
        self.assertIn("Traceback", r.stacktrace)
        self.assertIn("ValueError: boom", r.stacktrace)
        self.assertEqual(r.source_file, "f.txt")
        self.assertEqual(r.load_id, "id1")
        self.assertEqual(json.loads(r.details), {"step": 3})

    def test_error_from_plain_message(self):
        self.audit.error("converter", "convert", "could not map columns")
        r = self.audit.fetch_errors()[0]
        self.assertIsNone(r.error_type)
        self.assertIsNone(r.stacktrace)
        self.assertEqual(r.error_message, "could not map columns")

    def test_error_with_empty_message_uses_type_name(self):
        self.audit.error("x", "y", KeyError())
        self.assertIn("KeyError", self.audit.fetch_errors()[0].error_message)

    def test_fetch_errors_filter_and_limit(self):
        for i in range(4):
            self.audit.error("loader", f"op{i}", "bad")
        self.audit.error("cli", "load", "worse")
        self.assertEqual(len(self.audit.fetch_errors(limit=2)), 2)
        self.assertEqual(len(self.audit.fetch_errors(component="cli")), 1)
        self.assertEqual(self.audit.count_errors(), 5)

    def test_audit_never_raises_when_database_is_unusable(self):
        # point the audit at a path that cannot be a database file
        self.settings.sqlite_path = self.root / "not_a_dir" / "db" / ".." / ".."  # a directory
        audit = AuditLogger(self.settings)
        audit.log("x", "y")              # must not raise
        audit.error("x", "y", "boom")    # must not raise


class StepTests(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.audit = AuditLogger(self.settings)

    def test_step_passes_through_on_success(self):
        with self.audit.step("loader", "read_file"):
            pass
        self.assertEqual(self.audit.count_errors(), 0)

    def test_step_records_and_reraises_exception(self):
        with self.assertRaises(RuntimeError):
            with self.audit.step("loader", "read_file", file_name="f.txt", load_id="id9"):
                raise RuntimeError("disk on fire")
        r = self.audit.fetch_errors()[0]
        self.assertEqual((r.component, r.operation), ("loader", "read_file"))
        self.assertEqual(r.error_message, "disk on fire")
        self.assertEqual(r.source_file, "f.txt")
        self.assertEqual(r.load_id, "id9")

    def test_step_can_swallow_exception(self):
        with self.audit.step("loader", "cleanup", reraise=False):
            raise OSError("ignorable")
        self.assertEqual(self.audit.count_errors(), 1)

    def test_nested_steps_record_the_error_only_once(self):
        with self.assertRaises(ValueError):
            with self.audit.step("outer", "op_outer"):
                with self.audit.step("inner", "op_inner"):
                    raise ValueError("only once")
        records = self.audit.fetch_errors()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].operation, "op_inner")   # innermost (most precise) step


if __name__ == "__main__":
    unittest.main()
