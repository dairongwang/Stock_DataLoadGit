"""
Requirement-traceability tests.

These tests exist specifically to demonstrate/validate the three
requirements called out for this project, each in its own TestCase:

  R1. A data file is loaded into the staging table successfully with the
      RIGHT COLUMN MAPPING (every source column lands in the correct
      staging column, for every row - not just "row counts match").

  R2. A data file with a different column sequence is CONVERTED into a
      file with the default column sequence, dropped into the loading
      folder, and loads correctly from there (same path a native
      default-format file would take).

  R3. Conversion and loading are INDEPENDENT/DECOUPLED: each can be run
      on its own - as separate CLI processes, separate Python calls, on
      different machines/schedules - without the other module being
      present/invoked.  Converting never touches the database; loading
      never invokes the converter.

  R4. Whatever the source file type (csv, text, excel, json, xml) and
      column sequence, the file placed in the loading folder is a TEXT
      file with the DEFAULT column sequence - and the loader accepts it.
      (detailed per-format tests: tests/test_readers.py)

  R5. Business users can configure custom file formats, the conversion
      folder and the loading folder through the dashboard.
      (detailed tests: tests/test_dashboard.py)
"""

from __future__ import annotations

import ast
import subprocess
import sys
import unittest
from pathlib import Path

from stock_dataload.config import load_settings
from stock_dataload.converter import ConversionSpec, FileConverter
from stock_dataload.db import STAGE_COLUMNS, StageDatabase
from stock_dataload.file_formats import FormatStore
from stock_dataload.loader import StageLoader

from .helpers import BROKER, ORIGINAL, REORDERED, TempProjectTestCase

PACKAGE_DIR = Path(__file__).resolve().parent.parent / "stock_dataload"


# =====================================================================
# R1 - column mapping correctness on load
# =====================================================================
class R1_ColumnMappingOnLoadTests(TempProjectTestCase):
    """A loaded file must map EVERY column to the right staging column."""

    def setUp(self):
        super().setUp()
        self.db = StageDatabase(self.settings)
        self.loader = StageLoader(self.settings, self.db)

    def test_every_field_lands_in_the_correct_staging_column(self):
        # Deliberately distinct values per column so a mapping bug (e.g.
        # high/low swapped, or vol landing in close) cannot hide.
        content = (
            "<ticker>,<date>,<open>,<high>,<low>,<close>,<vol>\n"
            "AAPL,20250123,111.11,222.22,33.33,44.44,555555\n"
            "MSFT,20250124,10.1,20.2,3.3,4.4,5000\n"
        )
        self.write("loading/NASDAQ_20250123.txt", content)
        result = self.loader.run()[0]
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.rows_loaded, 2)

        with self.db.connection() as conn:
            rows = self.db.fetch_all(conn)
        self.assertEqual(len(rows), 2)

        row = dict(zip(STAGE_COLUMNS, rows[0]))
        self.assertEqual(row["Ticker"], "AAPL")
        self.assertEqual(row["TradeDate"], "20250123")
        self.assertEqual(row["Open"], "111.11")
        self.assertEqual(row["High"], "222.22")
        self.assertEqual(row["Low"], "33.33")
        self.assertEqual(row["Close"], "44.44")
        self.assertEqual(row["Vol"], "555555")
        self.assertEqual(row["Market"], "NASDAQ")          # derived from file name
        self.assertEqual(row["SourceFile"], "NASDAQ_20250123.txt")

        row2 = dict(zip(STAGE_COLUMNS, rows[1]))
        self.assertEqual((row2["Ticker"], row2["TradeDate"], row2["Open"], row2["High"],
                          row2["Low"], row2["Close"], row2["Vol"]),
                         ("MSFT", "20250124", "10.1", "20.2", "3.3", "4.4", "5000"))

    def test_full_sample_file_maps_column_by_column_for_every_row(self):
        """Load the full 4092-row sample and verify column-level fidelity, not just counts."""
        import csv
        with open(ORIGINAL, newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            next(reader)  # header
            expected = [tuple(r) for r in reader]

        self.write("loading/NASDAQ_20250123.txt", ORIGINAL.read_text(encoding="utf-8"))
        result = self.loader.run()[0]
        self.assertTrue(result.ok, result.message)

        with self.db.connection() as conn:
            rows = self.db.fetch_all(conn)
        self.assertEqual(len(rows), len(expected))

        # STAGE_COLUMNS = Market, Ticker, TradeDate, Open, High, Low, Close, Vol, SourceFile, LoadId, LoadedAt
        actual = [(r[1], r[2], r[3], r[4], r[5], r[6], r[7]) for r in rows]
        self.assertEqual(actual, expected, "every row must round-trip column-for-column through the load")
        # and the derived column is correct for 100% of rows
        self.assertTrue(all(r[0] == "NASDAQ" for r in rows))

    def test_column_order_in_staging_table_matches_insert_statement(self):
        """The INSERT statement column list must match STAGE_COLUMNS exactly (regression guard)."""
        db = StageDatabase(self.settings)
        for col in STAGE_COLUMNS:
            self.assertIn(db.q(col), db._insert_sql)
        # order: build the column list the same way db.py does and compare
        expected_cols = ", ".join(db.q(c) for c in STAGE_COLUMNS)
        self.assertIn(expected_cols, db._insert_sql)


# =====================================================================
# R2 - conversion of a different column sequence into the default format,
#      then successful load via the SAME loading folder
# =====================================================================
class R2_ConversionThenLoadTests(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.converter = FileConverter(self.settings)
        self.db = StageDatabase(self.settings)
        self.loader = StageLoader(self.settings, self.db)

    def test_reordered_file_is_converted_to_default_sequence(self):
        """<date>,<ticker>,<vol>,<close>,<open>,<high>,<low>  ->  default sequence."""
        res = self.converter.convert(REORDERED)
        out_lines = res.output_file.read_text(encoding="utf-8").splitlines()
        self.assertEqual(out_lines[0], "<ticker>,<date>,<open>,<high>,<low>,<close>,<vol>")
        # spot check a data row was re-sequenced correctly
        first_data_row = out_lines[1].split(",")
        self.assertEqual(first_data_row, ["AACG", "20250123", "0.9", "0.9229", "0.89", "0.9128", "2800"])
        self.assertEqual(res.output_file.parent, self.settings.loading_dir)

    def test_converted_file_lands_in_the_loading_folder_and_only_there(self):
        self.converter.convert(REORDERED)
        self.assertEqual([p.name for p in self.settings.loading_dir.iterdir()], ["NASDAQ_20250123_reordered.txt"])
        self.assertEqual(list(self.settings.work_dir.iterdir()), [])  # no leftovers in scratch space
        self.assertEqual(list(self.settings.archive_dir.iterdir()), [])
        self.assertEqual(list(self.settings.error_dir.iterdir()), [])

    def test_converted_file_loads_successfully_through_the_normal_loading_path(self):
        """Convert, then load via the SAME loading-folder mechanism a native file would use."""
        self.converter.convert(REORDERED)
        self.converter.convert(BROKER, ConversionSpec(date_format="auto"))

        results = self.loader.run()       # loader only scans the loading folder, like for any other file
        self.assertEqual(len(results), 2)
        for r in results:
            self.assertTrue(r.ok, r.message)
            self.assertEqual(r.rows_loaded, 4092)
            self.assertEqual(r.market, "NASDAQ")

        # compare the loaded rows against a native load of the untouched original file
        self.write("loading/NASDAQ_20250123.txt", ORIGINAL.read_text(encoding="utf-8"))
        self.loader.run()

        with self.db.connection() as conn:
            native = [r[1:8] for r in self.db.fetch_all(conn, "NASDAQ_20250123.txt")]
            from_reordered = [r[1:8] for r in self.db.fetch_all(conn, "NASDAQ_20250123_reordered.txt")]
            from_broker = [r[1:8] for r in self.db.fetch_all(conn, "NASDAQ_20250123_broker.txt")]
        self.assertEqual(native, from_reordered)
        self.assertEqual(native, from_broker)

    def test_conversion_of_unmapped_columns_is_rejected_before_it_ever_reaches_loading_folder(self):
        """A file the converter cannot map must never reach the loading folder."""
        from stock_dataload.converter import ConversionError
        src = self.write("inbox/mystery.csv", "a,b,c,d,e,f,g\n1,2,3,4,5,6,7\n")
        with self.assertRaises(ConversionError):
            self.converter.convert(src)
        self.assertEqual(list(self.settings.loading_dir.iterdir()), [])


# =====================================================================
# R3 - conversion and loading can be operated independently
# =====================================================================
class R3_ConversionAndLoadingAreIndependentTests(TempProjectTestCase):
    """Conversion and loading must be separately deployable/operable."""

    def test_converter_module_has_no_static_dependency_on_db_or_loader(self):
        """Static check: converter.py must not import db.py / loader.py.

        This guarantees the conversion step can be deployed/run (e.g. on a
        file-drop server) without a database driver or loader code present.
        """
        tree = ast.parse((PACKAGE_DIR / "converter.py").read_text(encoding="utf-8"))
        imported_modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules.add(alias.name)
        self.assertNotIn("db", imported_modules)
        self.assertNotIn("loader", imported_modules)
        self.assertNotIn("stock_dataload.db", imported_modules)
        self.assertNotIn("stock_dataload.loader", imported_modules)

    def test_loader_module_has_no_static_dependency_on_converter(self):
        """Static check: loader.py must not import converter.py.

        This guarantees the loading step can be deployed/run on its own
        (e.g. a scheduled job next to the database) without the converter.
        """
        tree = ast.parse((PACKAGE_DIR / "loader.py").read_text(encoding="utf-8"))
        imported_modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules.add(alias.name)
        self.assertNotIn("converter", imported_modules)
        self.assertNotIn("stock_dataload.converter", imported_modules)

    def test_conversion_runs_without_any_database_being_created_or_touched(self):
        """Running the converter alone must not create/open a database at all."""
        converter = FileConverter(self.settings)
        self.assertFalse(self.settings.sqlite_path.exists())
        converter.convert(REORDERED)
        converter.convert(BROKER, ConversionSpec(date_format="auto"))
        # still no database file - conversion is a pure file-to-file operation
        self.assertFalse(self.settings.sqlite_path.exists())
        # but the files are ready and waiting in the loading folder for a
        # (possibly much later, possibly on another host) load step
        self.assertEqual(len(list(self.settings.loading_dir.glob("*.txt"))), 2)

    def test_loading_runs_without_ever_invoking_the_converter(self):
        """A file already in the default format loads with the converter never imported/used."""
        # Build the default-format file "by hand" - i.e. exactly as if an
        # upstream system (or an operator on a different day) had already
        # produced it; the converter plays no role in this test at all.
        self.write("loading/NASDAQ_20250123.txt", ORIGINAL.read_text(encoding="utf-8"))
        loader = StageLoader(self.settings)
        results = loader.run()
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].ok)
        self.assertEqual(results[0].rows_loaded, 4092)

    def test_convert_and_load_as_two_separate_os_processes(self):
        """
        The strongest proof of decoupling: run 'convert' and 'load' as two
        independent CLI invocations (separate Python subprocesses, as they
        would be on two different servers/schedules in production),
        communicating only through the loading folder on disk.
        """
        cfg = self.write("config.ini", f"""
[paths]
loading_dir = loading
archive_dir = archive
error_dir = error
work_dir = work
log_dir = logs
[database]
sqlite_path = stage.db
""")
        env_args = ["--config", str(cfg), "-q"]

        # --- process 1: conversion only -----------------------------------
        proc = subprocess.run(
            [sys.executable, "-m", "stock_dataload", *env_args, "convert", str(REORDERED)],
            cwd=PACKAGE_DIR.parent, capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse((self.root / "stage.db").exists(), "conversion step must not touch the database")
        self.assertTrue((self.root / "loading" / "NASDAQ_20250123_reordered.txt").exists())

        # --- process 2: loading only (different invocation/time) ----------
        proc = subprocess.run(
            [sys.executable, "-m", "stock_dataload", *env_args, "load"],
            cwd=PACKAGE_DIR.parent, capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("loaded (4092 rows", proc.stdout)
        self.assertTrue((self.root / "stage.db").exists())
        self.assertTrue((self.root / "archive" / "NASDAQ_20250123_reordered.txt").exists())

    def test_settings_allow_independent_folder_and_schedule_configuration(self):
        """Converter and loader each only need the bits of Settings relevant to them."""
        settings = load_settings(Path(__file__).resolve().parent.parent / "config.ini")
        # Converter only needs work_dir / loading_dir / named_formats - no db settings required to construct it
        converter = FileConverter(settings)
        self.assertIsNotNone(converter)
        # Loader can be constructed and introspected without a converter instance anywhere in scope
        loader = StageLoader(settings)
        self.assertIsNotNone(loader)


# =====================================================================
# R4 - every supported source type ends up as a default-format TEXT file
# =====================================================================
class R4_LoadingFileAlwaysDefaultTextFormatTests(TempProjectTestCase):
    SOURCES = ("NASDAQ_20250123.csv", "NASDAQ_20250123.tsv", "NASDAQ_20250123_noheader.txt",
               "NASDAQ_20250123.xlsx", "NASDAQ_20250123.json", "NASDAQ_20250123.xml")

    def test_csv_text_excel_json_xml_all_become_default_text_files_that_load(self):
        from .helpers import SAMPLES
        converter = FileConverter(self.settings)
        db = StageDatabase(self.settings)
        loader = StageLoader(self.settings, db)
        expected = ORIGINAL.read_text(encoding="utf-8")
        for name in self.SOURCES:
            res = converter.convert(SAMPLES / name, on_conflict="rename")
            out = res.output_file
            self.assertEqual(out.parent, self.settings.loading_dir, name)
            self.assertEqual(out.suffix, ".txt", name)                                  # text file
            first_line = out.read_text(encoding="utf-8").splitlines()[0]
            self.assertEqual(first_line, "<ticker>,<date>,<open>,<high>,<low>,<close>,<vol>", name)  # default sequence
            self.assertEqual(out.read_text(encoding="utf-8"), expected, name)            # same data
        results = loader.run()
        self.assertEqual(len(results), len(self.SOURCES))
        self.assertTrue(all(r.ok for r in results), [r.message for r in results if not r.ok])
        self.assertEqual(db.count_rows(), len(self.SOURCES) * 4092)


# =====================================================================
# R5 - dashboard configuration of formats / folders
# =====================================================================
class R5_DashboardConfigurationTests(TempProjectTestCase):
    def test_dashboard_persists_custom_format_and_folders(self):
        from stock_dataload.config import save_settings
        from stock_dataload.dashboard import create_app
        self.settings.config_file = self.root / "config.ini"
        save_settings(self.settings, self.settings.config_file)
        from stock_dataload.auth import UserStore
        UserStore(self.settings).create_user("admin", "Admin-Pass-1", role="admin")
        app = create_app(load_settings(self.settings.config_file))
        app.config["TESTING"] = True
        client = app.test_client()
        state = app.extensions["state"]
        self.assertEqual(client.post("/login", data={"username": "admin", "password": "Admin-Pass-1"}).status_code, 302)

        # custom format via the Formats page
        r = client.post("/formats/new", data={"name": "vendor_json", "file_type": "json", "file_pattern": "vendor_*.json",
                                              "record_path": "data", "mapping_mode": "auto", "action": "save"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(FormatStore(state.settings.formats_file).get("vendor_json").record_path, "data")

        # folders via the Settings page
        s = state.settings
        form = {"conversion_dir": "drop_zone", "converted_dir": str(s.converted_dir), "loading_dir": "to_load",
                "archive_dir": str(s.archive_dir), "error_dir": str(s.error_dir), "work_dir": str(s.work_dir),
                "log_dir": str(s.log_dir), "formats_file": str(s.formats_file), "sqlite_path": str(s.sqlite_path),
                "stage_table": s.stage_table, "batch_size": "5000",
                "file_patterns": "*.txt", "market_from_filename_regex": s.market_from_filename_regex,
                "default_market": "", "encoding": "utf-8-sig"}
        r = client.post("/settings", data=form)
        self.assertEqual(r.status_code, 302)
        reloaded = load_settings(self.root / "config.ini")
        self.assertEqual(reloaded.conversion_dir, self.root / "drop_zone")
        self.assertEqual(reloaded.loading_dir, self.root / "to_load")
        self.assertTrue((self.root / "drop_zone").is_dir())
        self.assertTrue((self.root / "to_load").is_dir())

        # and the configured format + folders are used by the conversion
        import shutil
        from .helpers import SAMPLES
        shutil.copy(SAMPLES / "NASDAQ_20250123.json", self.root / "drop_zone" / "vendor_20250123.json")
        status = client.get("/api/status").get_json()
        self.assertEqual(status["conversion_files"], ["vendor_20250123.json"])
        r = client.post("/convert", data={"file": "vendor_20250123.json"})
        self.assertEqual(r.status_code, 302)
        out = self.root / "to_load" / "vendor_20250123.txt"
        self.assertTrue(out.exists())
        self.assertEqual(out.read_text(encoding="utf-8"), ORIGINAL.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
