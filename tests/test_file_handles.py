"""
Files must never stay open longer than needed, and a file that cannot be
moved or deleted (typical on Windows: it is open in another program) must be
reported, not crash the app.
"""

from __future__ import annotations

import gc
import shutil
import unittest
import unittest.mock as mock
import warnings

from stock_dataload.audit import AuditLogger
from stock_dataload.converter import ConversionError, FileConverter
from stock_dataload.db import StageDatabase
from stock_dataload.loader import StageLoader
from stock_dataload.readers import read_source

from .helpers import DEFAULT_HEADER, ORIGINAL, SAMPLES, TempProjectTestCase
from .test_dashboard import DashboardTestCase


class ReaderHandleTests(TempProjectTestCase):
    def assert_no_open_files(self, action):
        """Run *action*; garbage collection afterwards must not find an unclosed file."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            action()
            gc.collect()
        leaks = [w for w in caught if issubclass(w.category, ResourceWarning) and "unclosed file" in str(w.message)]
        self.assertEqual(leaks, [], [str(w.message) for w in leaks])

    def test_source_table_close_releases_the_file_without_reading_rows(self):
        from stock_dataload.file_formats import FileFormat
        for name in ("NASDAQ_20250123.csv", "NASDAQ_20250123.xlsx"):
            src = self.root / name
            shutil.copy(SAMPLES / name, src)

            def open_and_close():
                table = read_source(src, FileFormat(name="x"))
                table.close()          # no row was consumed: the generator never started
                table.close()          # idempotent

            self.assert_no_open_files(open_and_close)
            src.unlink()               # would raise on Windows if the handle were still open

    def test_converter_closes_the_source_when_the_mapping_fails(self):
        bad = self.write("conversion/mystery.csv", "a,b,c\n1,2,3\n")
        converter = FileConverter(self.settings)

        def convert():
            with self.assertRaises(ConversionError):
                converter.convert(bad)
            with self.assertRaises(ConversionError):
                converter.inspect(bad)
            self.assertFalse(converter.preview(bad)["ok"])

        self.assert_no_open_files(convert)
        bad.unlink()

    def test_converter_closes_the_source_on_a_name_conflict(self):
        src = self.write("conversion/NASDAQ_20250123.txt", ORIGINAL.read_text(encoding="utf-8"))
        (self.settings.loading_dir / "NASDAQ_20250123.txt").write_text("taken")
        converter = FileConverter(self.settings)

        def convert():
            with self.assertRaisesRegex(ConversionError, "already exists"):
                converter.convert(src, on_conflict="error")

        self.assert_no_open_files(convert)


class LockedFileTests(TempProjectTestCase):
    """shutil.move / unlink failing with a PermissionError (a file open elsewhere on Windows)."""

    def setUp(self):
        super().setUp()
        self.db = StageDatabase(self.settings)
        self.loader = StageLoader(self.settings, self.db)

    def test_loaded_file_that_cannot_be_archived_stays_loaded(self):
        path = self.settings.loading_dir / "NASDAQ_20250123.txt"
        shutil.copy(ORIGINAL, path)
        with mock.patch("stock_dataload.loader.shutil.move", side_effect=PermissionError("[WinError 32] in use")):
            r = self.loader.run()[0]
        self.assertTrue(r.ok, r.message)
        self.assertEqual(r.rows_loaded, 4092)
        self.assertEqual(r.final_path, path)
        self.assertTrue(path.exists())
        self.assertIn("could not be moved to the archive folder", r.message)
        self.assertIn("could not be moved", str(r))
        self.assertEqual(self.db.count_rows(), 4092)
        history = self.db.load_history(limit=5)
        self.assertEqual([h.status for h in history], ["loaded"])
        events = [l.event for l in AuditLogger(self.settings).fetch_logs(limit=20, component="loader")]
        self.assertIn("archive_failed", events)

    def test_failed_file_that_cannot_be_quarantined_does_not_stop_a_bulk_run(self):
        folder = self.root / "deliveries"
        folder.mkdir()
        (folder / "NASDAQ_20250106.txt").write_text("garbage\n")
        (folder / "NASDAQ_20250107.txt").write_text(DEFAULT_HEADER + "AAA,20250107,1,2,0.5,1.5,100\n")
        with mock.patch("stock_dataload.loader.shutil.move", side_effect=PermissionError("[WinError 32] in use")):
            summary = self.loader.load_folder(folder, move_files=True)
        self.assertEqual((summary.loaded, summary.failed), (1, 1))
        self.assertTrue((folder / "NASDAQ_20250106.txt").exists())
        self.assertTrue((self.settings.error_dir / "NASDAQ_20250106.txt.error.txt").exists())


class DashboardLockedFileTests(DashboardTestCase):
    def test_delete_of_a_locked_file_is_reported(self):
        self.drop("NASDAQ_20250123.csv")
        with mock.patch("pathlib.Path.unlink", side_effect=PermissionError("[WinError 32] in use")):
            r = self.client.post("/files/conversion_dir/NASDAQ_20250123.csv/delete")
        self.assertEqual(r.status_code, 302)
        self.assertIn("Could not delete NASDAQ_20250123.csv", self.flashes(r))
        self.assertTrue((self.state.settings.conversion_dir / "NASDAQ_20250123.csv").exists())

    def test_download_response_does_not_keep_the_file_open(self):
        self.drop("NASDAQ_20250123.csv")
        r = self.client.get("/files/conversion_dir/NASDAQ_20250123.csv")
        self.assertEqual(r.status_code, 200)
        r.close()
        (self.state.settings.conversion_dir / "NASDAQ_20250123.csv").unlink()


if __name__ == "__main__":
    unittest.main()
