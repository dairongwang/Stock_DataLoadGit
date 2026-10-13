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

    def test_bad_config_returns_2(self):
        cfg = self.write("bad.ini", "[database]\nbatch_size = -1\n")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["--config", str(cfg), "status"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
