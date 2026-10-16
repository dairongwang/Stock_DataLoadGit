"""Targeted unit tests for branches not exercised by the feature tests
(CLI option handling, defensive error paths, small helpers)."""

import contextlib
import io
import os
import shutil
import sqlite3
import unittest
import unittest.mock as mock

from stock_dataload import cli
from stock_dataload.audit import AuditLogger
from stock_dataload.config import ConfigError, find_config_file, load_settings
from stock_dataload.db import DatabaseError, StageDatabase
from stock_dataload.loader import StageLoader
from stock_dataload.quality import DataErrorRepository, DataQualityChecker

from .helpers import DEFAULT_HEADER, ORIGINAL, REORDERED, TempProjectTestCase


class CliOptionTests(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.cfg = self.write("config.ini", """
[paths]
loading_dir = loading
archive_dir = archive
error_dir = error
work_dir = work
log_dir = logs
conversion_dir = conversion
converted_dir = converted
[database]
sqlite_path = cli.db
""")

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["--config", str(self.cfg), "-q", *args])
        return rc, out.getvalue(), err.getvalue()

    def test_formats_command_lists_formats(self):
        rc, out, _ = self.run_cli("formats")
        self.assertEqual(rc, 0)
        self.assertIn("formats file:", out)
        self.assertIn("yahoo", out)

    def test_convert_folder_success_failure_and_empty(self):
        rc, out, _ = self.run_cli("convert-folder")
        self.assertEqual(rc, 0)
        self.assertIn("Nothing to convert", out)
        shutil.copy(REORDERED, self.root / "conversion" / REORDERED.name)
        self.write("conversion/broken.csv", "no,stock,columns\n1,2,3\n")
        rc, out, err = self.run_cli("convert-folder")
        self.assertEqual(rc, 1)
        self.assertIn("NASDAQ_20250123_reordered.txt", out)
        self.assertIn("ERROR broken.csv", err)
        rc, out, _ = self.run_cli("errors")
        self.assertIn("converter/convert_folder", out)

    def test_convert_with_named_format(self):
        src = self.write("raw/x.txt", "20250123|AAA|1|2|0.5|1.5|100\n")
        rc, out, err = self.run_cli("convert", str(src), "--format", "pipe_noheader")
        self.assertEqual(rc, 0, err)

    def test_convert_unknown_named_format(self):
        src = self.write("raw/x.txt", "a\n")
        rc, _, err = self.run_cli("convert", str(src), "--format", "nope")
        self.assertEqual(rc, 1)

    def test_bad_delimiter_rejected(self):
        rc, _, err = self.run_cli("convert", str(REORDERED), "--delimiter", "ab")
        self.assertEqual(rc, 1)
        self.assertIn("single character", err)

    def test_bad_map_entry_rejected(self):
        rc, _, err = self.run_cli("convert", str(REORDERED), "--map", "tickerSymbol")
        self.assertEqual(rc, 1)
        self.assertIn("default=source", err)

    def test_output_name_with_multiple_sources_rejected(self):
        rc, _, err = self.run_cli("convert", str(REORDERED), str(ORIGINAL), "--output-name", "x.txt")
        self.assertEqual(rc, 1)

    def test_inspect_with_constant_map(self):
        src = self.write("raw/y.csv", "Date,Open,High,Low,Close,Volume\n"
                                      "2025-01-23,1,2,0.5,1.5,100\n")
        rc, out, err = self.run_cli("inspect", str(src), "--map",
                                    "ticker==YHOO,date=Date,open=Open,high=High,low=Low,"
                                    "close=Close,vol=Volume")
        self.assertEqual(rc, 0, err)
        self.assertIn("constant 'YHOO'", out)

    def test_inspect_unmappable_file_returns_1(self):
        src = self.write("raw/weird.csv", "a,b,c\n1,2,3\n")
        rc, out, err = self.run_cli("inspect", str(src))
        self.assertEqual(rc, 1)
        self.assertIn("ERROR", err)

    def test_load_keep_and_skip_bad_rows_flags(self):
        self.write("loading/NYSE_20250110.txt",
                   DEFAULT_HEADER + "AAA,20250110,1,2,0.5,1.5,100\nBBB,2025,1,2,0.5,1.5,100\n")
        rc, out, _ = self.run_cli("load", "--keep", "--skip-bad-rows")
        self.assertEqual(rc, 0, out)
        self.assertTrue((self.root / "loading" / "NYSE_20250110.txt").exists())
        self.assertIn("1 rows skipped", out)

    def test_load_truncate_flag(self):
        self.write("loading/NYSE_20250109.txt", DEFAULT_HEADER + "AAA,20250109,1,2,0.5,1.5,100\n")
        self.run_cli("load")
        self.write("loading/NYSE_20250110.txt", DEFAULT_HEADER + "BBB,20250110,1,2,0.5,1.5,100\n")
        rc, out, _ = self.run_cli("load", "--truncate")
        self.assertEqual(rc, 0, out)
        s = load_settings(self.cfg)
        self.assertEqual(StageDatabase(s).count_rows(), 1)


class CliEntryPointTests(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.cfg = self.write("config.ini", "[database]\nsqlite_path = cli.db\n")

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["--config", str(self.cfg), "-q", *args])
        return rc, out.getvalue(), err.getvalue()

    def test_load_with_empty_loading_folder(self):
        rc, out, _ = self.run_cli("load")
        self.assertEqual(rc, 0)
        self.assertIn("No files to load", out)

    def test_convert_skip_bad_rows_flag(self):
        src = self.write("raw/x.csv", "Symbol,Date,Open,High,Low,Close,Volume\n"
                                      "AAA,20250123,1,2,0.5,1.5,100\n"
                                      "BBB,garbage,1,2,0.5,1.5,100\n")
        rc, out, err = self.run_cli("convert", str(src), "--skip-bad-rows")
        self.assertEqual(rc, 0, err)
        self.assertIn("1 rows", out)

    def test_named_format_with_cli_overrides_validated(self):
        rc, _, err = self.run_cli("convert", str(REORDERED), "--format", "pipe_noheader",
                                  "--skip-rows", "-1")
        self.assertEqual(rc, 1)
        self.assertIn("ERROR", err)

    def test_dashboard_command_invokes_run(self):
        with mock.patch("stock_dataload.dashboard.run") as run:
            rc, _, _ = self.run_cli("dashboard", "--host", "127.0.0.1", "--port", "1234")
        self.assertEqual(rc, 0)
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs["port"], 1234)

    def test_keyboard_interrupt_returns_130(self):
        from stock_dataload.loader import StageLoader
        with mock.patch.object(StageLoader, "run", side_effect=KeyboardInterrupt):
            rc, _, err = self.run_cli("load")
        self.assertEqual(rc, 130)
        self.assertIn("interrupted", err)

    def test_python_dash_m_entry_point(self):
        import runpy
        import sys
        argv = ["stock_dataload", "--config", str(self.cfg), "-q", "init-db"]
        with mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            runpy.run_module("stock_dataload", run_name="__main__")
        self.assertEqual(ctx.exception.code, 0)


class DashboardRunTests(TempProjectTestCase):
    def test_run_builds_app_and_serves(self):
        try:
            import flask  # noqa: F401
        except ImportError:  # pragma: no cover
            self.skipTest("flask not installed")
        from stock_dataload.dashboard import app as dashboard_app
        self.settings.config_file = self.write("config.ini", "[database]\nsqlite_path = d.db\n")
        with mock.patch("flask.Flask.run") as served:
            dashboard_app.run(self.settings, host="127.0.0.1", port=9999)
        served.assert_called_once()
        self.assertEqual(served.call_args.kwargs["port"], 9999)

    def test_run_uses_config_defaults(self):
        try:
            import flask  # noqa: F401
        except ImportError:  # pragma: no cover
            self.skipTest("flask not installed")
        from stock_dataload.dashboard import app as dashboard_app
        self.settings.dashboard_port = 8123
        with mock.patch("flask.Flask.run") as served:
            dashboard_app.run(self.settings)
        self.assertEqual(served.call_args.kwargs["port"], 8123)


class ConfigEdgeTests(TempProjectTestCase):
    def test_find_config_file_via_environment(self):
        cfg = self.write("env.ini", "[database]\nbatch_size = 7\n")
        with mock.patch.dict(os.environ, {"STOCK_DATALOAD_CONFIG": str(cfg)}):
            self.assertEqual(find_config_file(), cfg)
            self.assertEqual(load_settings().batch_size, 7)

    def test_find_config_file_falls_back_to_cwd(self):
        cfg = self.write("config.ini", "[database]\nbatch_size = 9\n")
        old = os.getcwd()
        os.chdir(self.root)
        try:
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("STOCK_DATALOAD_CONFIG", None)
                self.assertEqual(find_config_file(), cfg)
        finally:
            os.chdir(old)

    def test_no_config_anywhere_returns_defaults(self):
        empty = self.root / "empty"
        empty.mkdir()
        old = os.getcwd()
        os.chdir(empty)
        try:
            os.environ.pop("STOCK_DATALOAD_CONFIG", None)
            with mock.patch("stock_dataload.config.PROJECT_ROOT", empty):
                self.assertIsNone(find_config_file())
                s = load_settings()
                self.assertIsNone(s.config_file)
                self.assertEqual(s.stage_table, "Stock_Stage")
        finally:
            os.chdir(old)

    def test_invalid_boolean_raises_config_error(self):
        cfg = self.write("bad.ini", "[database]\ntruncate_before_load = maybe\n")
        with self.assertRaises(ConfigError):
            load_settings(cfg)
        cfg = self.write("bad2.ini", "[quality]\nenabled = maybe\n")
        with self.assertRaises(ConfigError):
            load_settings(cfg)

    def test_save_settings_writes_absolute_path_outside_base(self):
        from stock_dataload.config import save_settings
        s = load_settings(self.write("cfg/config.ini", "[database]\nsqlite_path = x.db\n"))
        s.archive_dir = self.root.parent / "elsewhere_archive"
        target = self.root / "cfg" / "out.ini"
        save_settings(s, target)
        self.assertIn(str(s.archive_dir.resolve()), target.read_text())


class DatabaseEdgeTests(TempProjectTestCase):
    def test_unusable_sqlite_path_raises_database_error(self):
        self.settings.sqlite_path = self.root   # a directory, not a file
        db = StageDatabase(self.settings)
        with self.assertRaises(DatabaseError):
            with db.connection():
                pass  # pragma: no cover

    def test_connection_rolls_back_on_error(self):
        db = StageDatabase(self.settings)
        db.ensure_stage_table()
        with self.assertRaises(RuntimeError):
            with db.connection() as conn:
                conn.execute(f"INSERT INTO {db.table} "
                             "(Market,Ticker,TradeDate,SourceFile,LoadId,LoadedAt) "
                             "VALUES ('M','T','20250101','f','l','t')")
                raise RuntimeError("abort")
        self.assertEqual(db.count_rows(), 0)


class AuditEdgeTests(TempProjectTestCase):
    def test_connection_rolls_back_on_error(self):
        audit = AuditLogger(self.settings)
        audit.ensure_tables()
        with self.assertRaises(RuntimeError):
            with audit.connection() as conn:
                conn.execute('INSERT INTO "App_Log" ("LoggedAt","Level","Component","Event") '
                             "VALUES ('t','INFO','c','e')")
                raise RuntimeError("abort")
        self.assertEqual(audit.count_logs(), 0)


class QualityRepoEdgeTests(TempProjectTestCase):
    def test_connection_rolls_back_on_error(self):
        repo = DataErrorRepository(self.settings)
        repo.ensure_table()
        with self.assertRaises(RuntimeError):
            with repo.connection() as conn:
                conn.execute('INSERT INTO "Data_Error" '
                             '("DetectedAt","Rule","Severity","ErrorMessage") '
                             "VALUES ('t','r','error','m')")
                raise RuntimeError("abort")
        self.assertEqual(repo.count(), 0)

    def test_strategies_property_is_a_copy(self):
        checker = DataQualityChecker()
        snapshot = checker.strategies
        self.assertEqual(len(snapshot), 4)
        checker.remove_strategy("high_above_low")
        self.assertEqual(len(snapshot), 4)          # tuple snapshot unchanged
        self.assertEqual(len(checker.strategies), 3)


class LoaderEdgeTests(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.loader = StageLoader(self.settings)

    def test_pending_files_with_missing_folder(self):
        shutil.rmtree(self.settings.loading_dir)
        self.assertEqual(self.loader.pending_files(), [])

    def test_market_regex_without_named_group_uses_group_1(self):
        self.settings.market_from_filename_regex = r"^([A-Za-z]+)_\d{8}"
        loader = StageLoader(self.settings)
        self.assertEqual(loader.derive_market(self.root / "nyse_20250110.txt"), "NYSE")

    def test_blank_lines_are_ignored(self):
        self.write("loading/NYSE_20250110.txt",
                   DEFAULT_HEADER + "AAA,20250110,1,2,0.5,1.5,100\n\n , , \n"
                                    "BBB,20250110,1,2,0.5,1.5,100\n")
        r = self.loader.run()[0]
        self.assertTrue(r.ok, r.message)
        self.assertEqual(r.rows_loaded, 2)

    def test_reload_with_keep_files_leaves_file_in_place(self):
        path = self.write("loading/NYSE_20250110.txt",
                          DEFAULT_HEADER + "AAA,20250110,1,2,0.5,1.5,100\n")
        self.loader.run(move_files=False)
        r = self.loader.run(move_files=False)[0]
        self.assertTrue(r.ok)
        self.assertEqual(r.rows_replaced, 1)
        self.assertEqual(r.final_path, path)
        self.assertTrue(path.exists())

    def test_load_result_str_for_reload_and_failed(self):
        self.write("loading/NYSE_20250110.txt", DEFAULT_HEADER + "AAA,20250110,1,2,0.5,1.5,100\n")
        self.loader.run()
        self.write("loading/NYSE_20250110.txt", DEFAULT_HEADER + "AAA,20250110,1,2,0.5,1.5,100\n")
        again = self.loader.run()[0]
        self.assertIn("re-load: 1 rows of the previous load replaced", str(again))
        self.write("loading/BAD_20250110.txt", "x,y\n")
        failed = self.loader.run()[0]
        self.assertIn("failed", str(failed))


if __name__ == "__main__":
    unittest.main()
