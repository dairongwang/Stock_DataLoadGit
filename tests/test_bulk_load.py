"""
Bulk loading: every file of a folder -> staging table (StageLoader.load_folder,
`python -m stock_dataload load-folder`), including the handling of everything
that can go wrong along the way.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import sqlite3
import sys
import unittest
import unittest.mock as mock
from pathlib import Path

from stock_dataload import cli
from stock_dataload.audit import AuditLogger
from stock_dataload.db import StageDatabase
from stock_dataload.loader import FolderLoadResult, KnownTickers, LoadError, StageLoader

from .helpers import DEFAULT_HEADER, ORIGINAL, TempProjectTestCase, register_listed


def day_file(folder: Path, date: str, rows: dict[str, str], market: str = "NASDAQ") -> Path:
    """Write <market>_<date>.txt with one row per ticker (values 'o,h,l,c,v')."""
    lines = [DEFAULT_HEADER.strip()] + [f"{t},{date},{v}" for t, v in rows.items()]
    path = folder / f"{market}_{date}.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class BulkLoadTests(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.db = StageDatabase(self.settings)
        self.loader = StageLoader(self.settings, self.db)
        self.folder = self.root / "deliveries"
        self.folder.mkdir()

    def good_week(self, folder: Path | None = None) -> list[Path]:
        folder = folder or self.folder
        # Mon 6 .. Fri 10 January 2025 (market open every day)
        return [day_file(folder, f"202501{d:02d}", {"AAA": "1,2,0.5,1.5,100", "BBB": "5,6,4,5.5,200"})
                for d in range(6, 11)]

    # -- discovery -----------------------------------------------------------
    def test_find_files_sorted_by_name_and_patterns(self):
        files = self.good_week()
        (self.folder / "notes.csv").write_text("x")
        (self.folder / ".hidden.txt").write_text("x")
        self.assertEqual(self.loader.find_files(self.folder), sorted(files))
        self.assertEqual([p.name for p in self.loader.find_files(self.folder, patterns=["*.csv"])], ["notes.csv"])
        self.assertEqual(len(self.loader.find_files(self.folder, patterns=["*.csv", "*.txt"])), 6)

    def test_find_files_recursive(self):
        self.good_week()
        sub = self.folder / "2024"
        sub.mkdir()
        day_file(sub, "20241230", {"AAA": "1,2,0.5,1.5,100"})
        self.assertEqual(len(self.loader.find_files(self.folder)), 5)
        found = self.loader.find_files(self.folder, recursive=True)
        self.assertEqual(len(found), 6)
        self.assertIn(sub / "NASDAQ_20241230.txt", found)

    def test_find_files_problems_raise_load_error(self):
        with self.assertRaisesRegex(LoadError, "folder not found"):
            self.loader.find_files(self.root / "nope")
        with self.assertRaisesRegex(LoadError, "not a folder"):
            self.loader.find_files(ORIGINAL)
        with self.assertRaisesRegex(LoadError, "no file pattern"):
            self.loader.find_files(self.folder, patterns=["", " "])

    # -- loading -------------------------------------------------------------
    def test_load_folder_loads_every_file_and_keeps_them_in_place(self):
        files = self.good_week()
        summary = self.loader.load_folder(self.folder)
        self.assertIsInstance(summary, FolderLoadResult)
        self.assertEqual(summary.status, "completed")
        self.assertTrue(summary.ok)
        self.assertEqual((summary.loaded, summary.failed, summary.not_attempted), (5, 0, 0))
        self.assertEqual(summary.rows_loaded, 10)
        self.assertEqual([r.source_file.name for r in summary.results], [p.name for p in files])
        self.assertTrue(all(p.exists() for p in files), "files stay where they are by default")
        self.assertEqual(self.db.count_rows(), 10)
        self.assertEqual(len(self.db.load_history(limit=100)), 5)
        self.assertIn("5 of 5 file(s) loaded (10 rows)", str(summary))
        self.assertEqual(summary.as_dict()["loaded"], 5)
        self.assertEqual(list(self.settings.archive_dir.iterdir()), [])
        # audit trail: started + complete events with the summary
        events = [r.event for r in AuditLogger(self.settings).fetch_logs(limit=100, component="loader")]
        self.assertIn("bulk_load_started", events)
        self.assertIn("bulk_load_complete", events)

    def test_load_folder_can_move_files_to_archive(self):
        files = self.good_week()
        summary = self.loader.load_folder(self.folder, move_files=True)
        self.assertTrue(summary.ok)
        self.assertFalse(any(p.exists() for p in files))
        self.assertEqual(sorted(p.name for p in self.settings.archive_dir.iterdir()), sorted(p.name for p in files))

    def test_summary_text_mentions_skipped_rows(self):
        self.settings.skip_bad_rows = True
        self.good_week()
        (self.folder / "NASDAQ_20250108.txt").write_text(
            DEFAULT_HEADER + "AAA,20250108,1,2,0.5,1.5,100\nBBB,20250108,x,y,z\n", encoding="utf-8")
        summary = self.loader.load_folder(self.folder)
        self.assertTrue(summary.ok)
        self.assertEqual(summary.rows_rejected, 1)
        self.assertIn("1 row(s) skipped", str(summary))
        self.assertIn("data error(s)", str(summary))

    def test_load_folder_reports_empty_folder(self):
        summary = self.loader.load_folder(self.folder)
        self.assertEqual(summary.status, "completed")
        self.assertEqual(summary.files, [])
        self.assertIn("no files matching", summary.message)
        self.assertTrue(summary.ok)

    def test_load_folder_explicit_market_and_truncate(self):
        self.good_week()
        self.loader.load_folder(self.folder, market="NYSE")
        with self.db.connection() as conn:
            markets = {r[0] for r in self.db.fetch_all(conn)}
        self.assertEqual(markets, {"NYSE"})
        # a second run with truncate: the table holds only this run's rows
        other = self.root / "other"
        other.mkdir()
        day_file(other, "20250113", {"AAA": "1,2,0.5,1.5,100"})
        self.loader.load_folder(other, truncate=True)
        self.assertEqual(self.db.count_rows(), 1)

    def test_progress_callback_is_called_per_file(self):
        self.good_week()
        seen = []
        self.loader.load_folder(self.folder, progress=seen.append)
        self.assertEqual(len(seen), 5)
        self.assertTrue(all(r.ok for r in seen))

    # -- exceptions ----------------------------------------------------------
    def test_broken_file_does_not_stop_the_others(self):
        files = self.good_week()
        bad = self.folder / "NASDAQ_20250108.txt"          # overwrite Wednesday with garbage
        bad.write_text("ticker;date\nfoo;bar\n", encoding="utf-8")
        empty = self.folder / "NASDAQ_20250109.txt"
        empty.write_text("", encoding="utf-8")
        summary = self.loader.load_folder(self.folder)
        self.assertEqual(summary.status, "completed")
        self.assertFalse(summary.ok)
        self.assertEqual((summary.loaded, summary.failed), (3, 2))
        self.assertEqual({r.source_file.name for r in summary.failures}, {bad.name, empty.name})
        self.assertIn("2 failed", str(summary))
        self.assertEqual(self.db.count_rows(), 6)
        self.assertTrue(all(p.exists() for p in files))
        # every failure is in App_Error and Load_History
        errors = AuditLogger(self.settings).fetch_errors(limit=10, component="loader")
        self.assertEqual({e.source_file for e in errors}, {bad.name, empty.name})
        failed = [h for h in self.db.load_history(limit=100) if h.status == "failed"]
        self.assertEqual(len(failed), 2)

    def test_stop_on_error(self):
        self.good_week()
        (self.folder / "NASDAQ_20250107.txt").write_text("garbage\n", encoding="utf-8")
        summary = self.loader.load_folder(self.folder, stop_on_error=True)
        self.assertEqual(summary.status, "stopped")
        self.assertEqual((summary.loaded, summary.failed, summary.not_attempted), (1, 1, 3))
        self.assertIn("stopped after the first failure (NASDAQ_20250107.txt)", summary.message)
        self.assertIn("3 not attempted", str(summary))
        self.assertFalse(summary.ok)

    def test_unreadable_file_is_a_failed_file_not_a_crash(self):
        if sys.platform == "win32" or getattr(os, "geteuid", lambda: -1)() == 0:  # pragma: no cover
            self.skipTest("file permissions cannot be used for this test here (Windows / root)")
        files = self.good_week()
        files[2].chmod(0o000)
        try:
            summary = self.loader.load_folder(self.folder)
        finally:
            files[2].chmod(0o644)
        self.assertEqual((summary.loaded, summary.failed), (4, 1))
        self.assertIn("Permission denied", summary.failures[0].message)

    def test_bad_encoding_is_a_failed_file(self):
        self.good_week()
        (self.folder / "NASDAQ_20250108.txt").write_bytes(b"\xff\xfe\x00garbage")
        summary = self.loader.load_folder(self.folder)
        self.assertEqual((summary.loaded, summary.failed), (4, 1))
        self.assertEqual(summary.failures[0].source_file.name, "NASDAQ_20250108.txt")

    def test_missing_folder_is_recorded_and_raised(self):
        with self.assertRaisesRegex(LoadError, "folder not found"):
            self.loader.load_folder(self.root / "missing")
        audit = AuditLogger(self.settings)
        errors = audit.fetch_errors(limit=5, component="loader")
        self.assertEqual(errors[0].operation, "bulk_load_folder")
        self.assertIn("folder not found", errors[0].error_message)
        self.assertIn("bulk_load_failed", [r.event for r in audit.fetch_logs(limit=5)])

    def test_unusable_database_is_recorded_and_raised(self):
        self.good_week()
        from stock_dataload.db import DatabaseError
        with mock.patch.object(self.db, "ensure_stage_table", side_effect=DatabaseError("disk gone")):
            with self.assertRaisesRegex(DatabaseError, "disk gone"):
                self.loader.load_folder(self.folder)
        errors = AuditLogger(self.settings).fetch_errors(limit=5, component="loader")
        self.assertEqual(errors[0].operation, "bulk_load_folder")
        self.assertEqual(errors[0].error_type, "DatabaseError")

    def test_keyboard_interrupt_ends_the_run_cleanly(self):
        self.good_week()
        real = self.loader.load_file
        calls = []

        def interrupting(path, **kw):
            calls.append(path)
            if len(calls) == 3:
                raise KeyboardInterrupt
            return real(path, **kw)

        with mock.patch.object(self.loader, "load_file", side_effect=interrupting):
            summary = self.loader.load_folder(self.folder)
        self.assertEqual(summary.status, "interrupted")
        self.assertEqual(len(summary.results), 2)
        self.assertIn("interrupted after 2 of 5 file(s)", summary.message)
        self.assertEqual(self.db.count_rows(), 4)
        self.assertIsNone(self.loader._known, "the per-run ticker registry is released")

    def test_duplicate_content_under_another_name_is_loaded_with_a_warning(self):
        self.good_week()
        shutil.copy(self.folder / "NASDAQ_20250106.txt", self.folder / "NASDAQ_20250106_copy.txt")
        summary = self.loader.load_folder(self.folder)
        self.assertEqual(summary.loaded, 6)
        self.assertEqual(self.db.count_rows(), 12)
        logs = AuditLogger(self.settings).fetch_logs(limit=100, component="loader")
        dup = [r for r in logs if r.event == "duplicate_content"]
        self.assertEqual(len(dup), 1)
        self.assertEqual(dup[0].source_file, "NASDAQ_20250106_copy.txt")
        self.assertIn("NASDAQ_20250106.txt", dup[0].message)

    # -- the missing-market-day alerts behave exactly like single loads -------
    def test_known_tickers_registry_matches_database_answer(self):
        reg = KnownTickers({"a.txt": frozenset({"AAA", "BBB"}), "b.txt": frozenset({"BBB", "CCC"})})
        self.assertEqual(reg.expected_for("a.txt"), {"BBB", "CCC"})
        self.assertEqual(reg.expected_for("b.txt"), {"AAA", "BBB"})
        self.assertEqual(reg.expected_for("new.txt"), {"AAA", "BBB", "CCC"})
        reg.replace_file("a.txt", ["AAA", "DDD"])          # a re-load of a.txt without BBB
        self.assertEqual(reg.expected_for("b.txt"), {"AAA", "DDD"})
        self.assertEqual(reg.expected_for("a.txt"), {"BBB", "CCC"})

    def test_bulk_alerts_equal_single_file_alerts(self):
        """Loading a folder (registry) raises the same alerts as loading the files one by one (database)."""
        register_listed(self.settings, "NASDAQ", ["AAA", "BBB", "CCC"])
        self.good_week()
        # Thursday: BBB is missing; Friday: a new stock CCC appears, AAA missing
        day_file(self.folder, "20250109", {"AAA": "1,2,0.5,1.5,100"})
        day_file(self.folder, "20250110", {"BBB": "5,6,4,5.5,200", "CCC": "1,1,1,1,1"})
        bulk = self.loader.load_folder(self.folder)
        bulk_alerts = [(r.source_file.name, sorted(i.ticker for i in r.quality_issues if i.alert)) for r in bulk.results]

        other = TempProjectTestCase()
        other.setUp()
        try:
            folder2 = other.root / "deliveries"
            shutil.copytree(self.folder, folder2)
            register_listed(other.settings, "NASDAQ", ["AAA", "BBB", "CCC"])
            loader2 = StageLoader(other.settings)
            single = [loader2.load_file(p, move_file_after=False) for p in sorted(folder2.iterdir())]
            single_alerts = [(r.source_file.name, sorted(i.ticker for i in r.quality_issues if i.alert)) for r in single]
        finally:
            other.tearDown()
        self.assertEqual(bulk_alerts, single_alerts)
        self.assertEqual(bulk_alerts[3], ("NASDAQ_20250109.txt", ["BBB"]))
        self.assertEqual(bulk_alerts[4], ("NASDAQ_20250110.txt", ["AAA"]))

    def test_reload_within_a_bulk_run_replaces_rows_and_updates_registry(self):
        register_listed(self.settings, "NASDAQ", ["AAA", "BBB"])
        self.good_week()
        self.loader.load_folder(self.folder)
        # deliver Tuesday again without BBB, then Wednesday again: BBB must be alerted on Wednesday? no -
        # BBB is still known from the other days; the re-loaded Tuesday has only one copy in staging
        day_file(self.folder, "20250107", {"AAA": "1,2,0.5,1.5,100"})
        summary = self.loader.load_folder(self.folder)
        self.assertEqual(summary.loaded, 5)
        self.assertEqual(self.db.count_rows(source_file="NASDAQ_20250107.txt"), 1)
        self.assertEqual(self.db.count_rows(), 9)
        tue = [r for r in summary.results if r.source_file.name == "NASDAQ_20250107.txt"][0]
        self.assertEqual(tue.rows_replaced, 2)
        self.assertEqual([i.ticker for i in tue.quality_issues if i.alert], ["BBB"])

    def test_tickers_by_source_query(self):
        self.good_week()
        self.loader.load_folder(self.folder)
        by_file = self.db.tickers_by_source("NASDAQ")
        self.assertEqual(len(by_file), 5)
        self.assertEqual(by_file["NASDAQ_20250106.txt"], {"AAA", "BBB"})
        self.assertEqual(self.db.tickers_by_source("NYSE"), {})
        self.assertEqual(len(self.db.tickers_by_source()), 5)


class BulkLoadCliTests(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        from stock_dataload.config import save_settings
        self.settings.config_file = self.root / "config.ini"
        save_settings(self.settings, self.settings.config_file)
        self.folder = self.root / "deliveries"
        self.folder.mkdir()
        for d in range(6, 9):
            day_file(self.folder, f"2025010{d}", {"AAA": "1,2,0.5,1.5,100"})

    def run_cli(self, *args) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["--config", str(self.settings.config_file), "-q", *args])
        return rc, out.getvalue(), err.getvalue()

    def test_load_folder_command(self):
        rc, out, err = self.run_cli("load-folder", str(self.folder))
        self.assertEqual(rc, 0, err)
        self.assertIn("NASDAQ_20250106.txt: loaded (1 rows", out)
        self.assertIn("3 of 3 file(s) loaded (3 rows)", out)
        self.assertEqual(StageDatabase(self.settings).count_rows(), 3)
        self.assertEqual(len(list(self.folder.iterdir())), 3, "files stay in place")

    def test_load_folder_command_failure_codes(self):
        rc, out, err = self.run_cli("load-folder", str(self.root / "missing"))
        self.assertEqual(rc, 1)
        self.assertIn("folder not found", err)
        (self.folder / "NASDAQ_20250107.txt").write_text("broken\n")
        rc, out, err = self.run_cli("load-folder", str(self.folder))
        self.assertEqual(rc, 1)
        self.assertIn("1 failed", out)
        rc, out, err = self.run_cli("load-folder", str(self.folder), "--stop-on-error")
        self.assertEqual(rc, 1)
        self.assertIn("stopped", out)

    def test_load_folder_command_options(self):
        (self.folder / "extra.dat").write_text(DEFAULT_HEADER + "ZZZ,20250109,1,2,0.5,1.5,1\n")
        rc, out, _ = self.run_cli("load-folder", str(self.folder), "--pattern", "*.dat", "--market", "NYSE", "--move")
        self.assertEqual(rc, 0)
        self.assertIn("extra.dat: loaded", out)
        self.assertFalse((self.folder / "extra.dat").exists())
        self.assertTrue((self.settings.archive_dir / "extra.dat").exists())
        rows = StageDatabase(self.settings).find_stage_rows(ticker="ZZZ")
        self.assertEqual(rows[0]["Market"], "NYSE")

    def test_load_folder_then_promote(self):
        rc, out, _ = self.run_cli("load-folder", str(self.folder), "--promote")
        self.assertEqual(rc, 0)
        self.assertIn("promote_daily: completed", out)
        self.assertIn("promote_weekly: completed", out)
        self.assertIn("promote_monthly: completed", out)
        with sqlite3.connect(self.settings.sqlite_path) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM "Stock_Daily"').fetchone()[0], 3)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM "Stock_Weekly"').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM "Stock_Monthly"').fetchone()[0], 1)

    def test_keyboard_interrupt_exit_code(self):
        with mock.patch.object(StageLoader, "load_file", side_effect=KeyboardInterrupt):
            rc, out, _ = self.run_cli("load-folder", str(self.folder))
        self.assertEqual(rc, 130)
        self.assertIn("interrupted", out)


if __name__ == "__main__":
    unittest.main()
