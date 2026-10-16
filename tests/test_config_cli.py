import contextlib
import io
import unittest
from pathlib import Path

from stock_dataload import cli
from stock_dataload.config import ConfigError, load_settings

from .helpers import ORIGINAL, REORDERED, TempProjectTestCase


class ConfigTests(TempProjectTestCase):
    def test_load_settings_resolves_relative_paths_and_formats(self):
        cfg = self.write("cfg/config.ini", """
[paths]
loading_dir = in
archive_dir = done
[database]
sqlite_path = db/x.db
stage_table = Stage
batch_size = 10
truncate_before_load = true
[loader]
file_patterns = *.dat, *.txt
default_market = NYSE
[formats]
MyFmt = date, ticker, vol, close, open, high, low
""")
        s = load_settings(cfg)
        base = cfg.parent
        self.assertEqual(s.loading_dir, base / "in")
        self.assertEqual(s.archive_dir, base / "done")
        self.assertEqual(s.error_dir, base / "data" / "error")
        self.assertEqual(s.sqlite_path, base / "db" / "x.db")
        self.assertEqual(s.stage_table, "Stage")
        self.assertEqual(s.batch_size, 10)
        self.assertTrue(s.truncate_before_load)
        self.assertEqual(s.file_patterns, ("*.dat", "*.txt"))
        self.assertEqual(s.default_market, "NYSE")
        self.assertEqual(s.named_formats["MyFmt"], ["date", "ticker", "vol", "close", "open", "high", "low"])

    def test_defaults_for_dedupe_and_quality(self):
        from stock_dataload.config import Settings
        s = Settings()
        self.assertTrue(s.quality_checks_enabled)
        self.assertTrue(s.alert_missing_days)
        self.assertEqual(s.extra_market_holidays, ())

    def test_dedupe_and_quality_settings_parsed(self):
        cfg = self.write("cfg/config.ini", """
[quality]
enabled = false
alert_missing_days = false
extra_holidays = 2025-01-09, 2025-06-05
""")
        s = load_settings(cfg)
        self.assertFalse(s.quality_checks_enabled)
        self.assertFalse(s.alert_missing_days)
        self.assertEqual(s.extra_market_holidays, ("2025-01-09", "2025-06-05"))

    def test_settings_round_trip_keeps_new_options(self):
        from stock_dataload.config import save_settings
        cfg = self.write("cfg/config.ini", """
[quality]
enabled = false
alert_missing_days = false
extra_holidays = 2025-01-09
""")
        s = load_settings(cfg)
        target = self.root / "cfg" / "saved.ini"
        save_settings(s, target)
        s2 = load_settings(target)
        self.assertFalse(s2.quality_checks_enabled)
        self.assertFalse(s2.alert_missing_days)
        self.assertEqual(s2.extra_market_holidays, ("2025-01-09",))

    def test_shipped_config_has_quality_section(self):
        s = load_settings(Path(__file__).resolve().parent.parent / "config.ini")
        self.assertTrue(s.quality_checks_enabled)
        self.assertTrue(s.alert_missing_days)

    def test_invalid_batch_size(self):
        cfg = self.write("cfg/config.ini", "[database]\nbatch_size = 0\n")
        with self.assertRaises(ConfigError):
            load_settings(cfg)
        cfg = self.write("cfg/config2.ini", "[database]\nbatch_size = many\n")
        with self.assertRaises(ConfigError):
            load_settings(cfg)

    def test_empty_stage_table_name_rejected(self):
        cfg = self.write("cfg/config.ini", "[database]\nstage_table =\n")
        with self.assertRaises(ConfigError):
            load_settings(cfg)

    def test_missing_explicit_config(self):
        with self.assertRaises(ConfigError):
            load_settings(self.root / "nope.ini")

    def test_shipped_config_loads(self):
        s = load_settings(Path(__file__).resolve().parent.parent / "config.ini")
        self.assertEqual(s.sqlite_path.name, "stock_trade.db")
        self.assertEqual(s.loading_dir.name, "loading")
        self.assertEqual(s.formats_file.name, "formats.json")
        self.assertEqual(s.conversion_dir.name, "conversion")
        from stock_dataload.file_formats import FormatStore
        self.assertIn("yahoo", FormatStore(s.formats_file).names())


class CliTests(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.cfg = self.write("config.ini", f"""
[paths]
loading_dir = loading
archive_dir = archive
error_dir = error
work_dir = work
log_dir = logs
[database]
sqlite_path = cli.db
""")

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["--config", str(self.cfg), "-q", *args])
        return rc, out.getvalue(), err.getvalue()

    def test_full_cycle(self):
        rc, out, _ = self.run_cli("init-db")
        self.assertEqual(rc, 0)
        self.assertIn("Stock_Stage", out)

        rc, out, _ = self.run_cli("inspect", str(REORDERED))
        self.assertEqual(rc, 0)
        self.assertIn("ticker  <- #2", out)

        rc, out, _ = self.run_cli("convert", str(REORDERED))
        self.assertEqual(rc, 0, out)
        self.assertTrue((self.root / "loading" / "NASDAQ_20250123_reordered.txt").exists())

        rc, out, _ = self.run_cli("status")
        self.assertIn("files waiting to be loaded: 1", out)
        self.assertIn("staging table is empty", out)

        rc, out, _ = self.run_cli("load")
        self.assertEqual(rc, 0, out)
        self.assertIn("loaded (4092 rows, market=NASDAQ", out)

        rc, out, _ = self.run_cli("status")
        self.assertIn("NASDAQ_20250123_reordered.txt", out)
        self.assertIn("4092", out)

    def test_convert_with_options(self):
        src = self.write("raw/x.txt", "20250123|AAA|100|1.5|1|2|0.5\n")
        rc, out, err = self.run_cli("convert", str(src), "--no-header", "--columns",
                                    "date,ticker,vol,close,open,high,low", "--delimiter", "pipe",
                                    "--output-name", "X_20250123.txt")
        self.assertEqual(rc, 0, err)
        self.assertEqual((self.root / "loading" / "X_20250123.txt").read_text(),
                         "<ticker>,<date>,<open>,<high>,<low>,<close>,<vol>\nAAA,20250123,1,2,0.5,1.5,100\n")

    def test_convert_failure_returns_nonzero(self):
        rc, out, err = self.run_cli("convert", str(self.root / "missing.csv"))
        self.assertEqual(rc, 1)
        self.assertIn("ERROR", err)
        rc, out, err = self.run_cli("convert", str(ORIGINAL), "--no-header")
        self.assertEqual(rc, 1)
        self.assertIn("--columns", err)

    def test_load_failure_returns_nonzero(self):
        (self.root / "loading").mkdir(exist_ok=True)
        self.write("loading/bad.txt", "a,b\n1,2\n")
        rc, out, _ = self.run_cli("load")
        self.assertEqual(rc, 1)
        self.assertIn("failed", out)
        self.assertTrue((self.root / "error" / "bad.txt").exists())

    def test_init_db_creates_all_tables(self):
        rc, out, _ = self.run_cli("init-db")
        self.assertEqual(rc, 0)
        self.assertIn("Load_History", out)
        import sqlite3
        with sqlite3.connect(self.root / "cli.db") as conn:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("Stock_Stage", "Load_History", "App_Log", "App_Error", "Data_Error", "Data_Correction",
                      "App_User"):
            self.assertIn(table, names)

    def test_reloading_same_file_via_cli_keeps_one_copy(self):
        import shutil
        shutil.copy(ORIGINAL, self.root / "loading" / ORIGINAL.name)
        rc, out, _ = self.run_cli("load")
        self.assertEqual(rc, 0, out)
        shutil.copy(ORIGINAL, self.root / "loading" / ORIGINAL.name)
        rc, out, _ = self.run_cli("load")
        self.assertEqual(rc, 0, out)
        self.assertIn("re-load: 4092 rows of the previous load replaced", out)
        rc, out, _ = self.run_cli("status")
        self.assertIn("4092", out)
        self.assertNotIn("8184", out)

    def test_logs_errors_history_commands(self):
        rc, out, _ = self.run_cli("logs")
        self.assertEqual(rc, 0)
        self.assertIn("no log entries", out)
        rc, out, _ = self.run_cli("errors")
        self.assertIn("no errors recorded", out)
        rc, out, _ = self.run_cli("data-errors")
        self.assertIn("no data errors recorded", out)
        rc, out, _ = self.run_cli("history")
        self.assertIn("no load history", out)

        import shutil
        shutil.copy(ORIGINAL, self.root / "loading" / ORIGINAL.name)
        self.run_cli("load")
        rc, out, _ = self.run_cli("logs")
        self.assertEqual(rc, 0)
        self.assertIn("file_loaded", out)
        rc, out, _ = self.run_cli("logs", "--component", "loader", "--level", "INFO", "--limit", "5")
        self.assertIn("load_started", out)
        rc, out, _ = self.run_cli("history")
        self.assertIn("NASDAQ_20250123.txt", out)
        self.assertIn("loaded", out)
        rc, out, _ = self.run_cli("history", "--file", "NASDAQ_20250123.txt", "--limit", "1")
        self.assertIn("loaded", out)

    def test_failed_load_lands_in_errors_command(self):
        (self.root / "loading").mkdir(exist_ok=True)
        self.write("loading/bad.txt", "a,b\n1,2\n")
        self.run_cli("load")
        rc, out, _ = self.run_cli("errors")
        self.assertEqual(rc, 0)
        self.assertIn("loader/insert_rows", out)
        self.assertIn("bad.txt", out)
        rc, out, _ = self.run_cli("errors", "--component", "loader", "--limit", "3")
        self.assertIn("FileFormatError", out)

    def test_data_errors_command_shows_findings_and_alerts(self):
        self.write("loading/NYSE_20250109.txt",
                   "<ticker>,<date>,<open>,<high>,<low>,<close>,<vol>\n"
                   "AAA,20250109,1,2,0.5,1.5,100\nBBB,20250109,1,2,0.5,1.5,100\n")
        self.run_cli("load")
        self.write("loading/NYSE_20250110.txt",
                   "<ticker>,<date>,<open>,<high>,<low>,<close>,<vol>\n"
                   "AAA,20250110,1,0.4,0.5,1.5,100\n")
        self.run_cli("load")
        rc, out, _ = self.run_cli("data-errors")
        self.assertEqual(rc, 0)
        self.assertIn("high_above_low", out)
        self.assertIn("missing_market_day", out)
        self.assertIn("ALERT", out)
        rc, out, _ = self.run_cli("data-errors", "--alerts")
        self.assertIn("missing_market_day", out)
        self.assertNotIn("high_above_low", out)
        rc, out, _ = self.run_cli("data-errors", "--file", "NYSE_20250110.txt", "--limit", "10")
        # AAA's only row failed a rule, so AAA is also alerted as missing valid data
        self.assertIn("total: 3", out)

    def test_failed_conversion_recorded_in_app_error(self):
        self.run_cli("convert", str(self.root / "missing.csv"))
        rc, out, _ = self.run_cli("errors")
        self.assertIn("converter/convert", out)

    def test_unexpected_error_captured_and_returns_1(self):
        import unittest.mock as mock
        from stock_dataload.loader import StageLoader
        with mock.patch.object(StageLoader, "run", side_effect=RuntimeError("kaboom")):
            rc, out, err = self.run_cli("load")
        self.assertEqual(rc, 1)
        self.assertIn("UNEXPECTED ERROR", err)
        rc, out, _ = self.run_cli("errors")
        self.assertIn("cli/load", out)
        self.assertIn("kaboom", out)

    def test_user_commands(self):
        from stock_dataload.auth import UserStore
        rc, out, _ = self.run_cli("user", "list")
        self.assertEqual(rc, 0)
        self.assertIn("no users", out)
        rc, out, err = self.run_cli("user", "add", "admin", "--role", "admin", "--password", "Admin-Pass-1",
                                    "--name", "Root")
        self.assertEqual(rc, 0, err)
        self.assertIn("created with role admin", out)
        rc, out, err = self.run_cli("user", "add", "admin", "--role", "admin", "--password", "Admin-Pass-1")
        self.assertEqual(rc, 1)
        self.assertIn("already exists", err)
        import unittest.mock as mock
        with mock.patch.dict("os.environ", {"STOCK_DATALOAD_PASSWORD": "Jane-Pass-1"}):
            rc, out, err = self.run_cli("user", "add", "jane", "--must-change")
        self.assertEqual(rc, 0, err)
        rc, out, _ = self.run_cli("user", "list")
        self.assertIn("admin", out)
        self.assertIn("jane", out)
        self.assertIn("active*", out)                             # must change the password
        store = UserStore(load_settings(self.cfg))
        self.assertIsNotNone(store.authenticate("jane", "Jane-Pass-1"))
        rc, out, _ = self.run_cli("user", "passwd", "jane", "--password", "New-Pass-22")
        self.assertEqual(rc, 0)
        self.assertIsNotNone(store.authenticate("jane", "New-Pass-22"))
        rc, out, _ = self.run_cli("user", "role", "jane", "operator")
        self.assertIn("now operator", out)
        rc, out, _ = self.run_cli("user", "disable", "jane")
        self.assertIn("disabled", out)
        self.assertIsNone(store.authenticate("jane", "New-Pass-22"))
        rc, out, _ = self.run_cli("user", "enable", "jane")
        self.assertIsNotNone(store.authenticate("jane", "New-Pass-22"))
        rc, out, _ = self.run_cli("user", "delete", "jane")
        self.assertIn("deleted", out)
        self.assertIsNone(store.get_by_username("jane"))
        rc, _, err = self.run_cli("user", "delete", "jane")
        self.assertEqual(rc, 1)
        self.assertIn("does not exist", err)
        rc, _, err = self.run_cli("user", "delete", "admin")
        self.assertEqual(rc, 1)
        self.assertIn("last active administrator", err)
        rc, out, _ = self.run_cli("logs", "--component", "auth")
        self.assertIn("user_created", out)

    def test_user_password_prompt(self):
        import unittest.mock as mock
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("STOCK_DATALOAD_PASSWORD", None)
            with mock.patch("sys.stdin.isatty", return_value=False):
                rc, _, err = self.run_cli("user", "add", "jane")
            self.assertEqual(rc, 1)
            self.assertIn("no password given", err)
            with mock.patch("sys.stdin.isatty", return_value=True), \
                    mock.patch("getpass.getpass", side_effect=["Jane-Pass-1", "Jane-Pass-1"]):
                rc, out, err = self.run_cli("user", "add", "jane")
            self.assertEqual(rc, 0, err)
            with mock.patch("sys.stdin.isatty", return_value=True), \
                    mock.patch("getpass.getpass", side_effect=["Jane-Pass-1", "other"]):
                rc, _, err = self.run_cli("user", "passwd", "jane")
            self.assertEqual(rc, 1)
            self.assertIn("do not match", err)

    def test_bad_config_returns_2(self):
        cfg = self.write("bad.ini", "[database]\nbatch_size = -1\n")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["--config", str(cfg), "status"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
