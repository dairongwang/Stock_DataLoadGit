"""chart.ini + environment variables."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from stock_chart.config import APP_ROOT, ChartSettings, ConfigError, load_settings


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for key in [k for k in os.environ if k.startswith("STOCK_CHART_")]:
            del os.environ[key]

    def tearDown(self):
        self.env.stop()

    def write(self, text: str) -> Path:
        path = self.dir / "chart.ini"
        path.write_text(text, encoding="utf-8")
        return path

    def test_shipped_file(self):
        s = load_settings(APP_ROOT / "chart.ini")
        self.assertEqual(s.sqlite_path, (APP_ROOT.parent / "data" / "stock_trade.db").resolve())
        self.assertEqual((s.daily_table, s.host, s.port, s.default_range, s.auth_mode), ("Stock_Daily", "127.0.0.1",
                                                                                         8070, "6M", "none"))
        self.assertIn("dummy", s.username)
        self.assertEqual(s.password, "")

    def test_weekly_and_monthly_settings(self):
        s = load_settings(APP_ROOT / "chart.ini")
        self.assertEqual((s.weekly_table, s.monthly_table, s.default_interval, s.period_source),
                         ("Stock_Weekly", "Stock_Monthly", "daily", "auto"))
        s = load_settings(self.write("[database]\nweekly_table = Weeks\nmonthly_table = Months\n"
                                     "[chart]\ndefault_interval = Monthly\nperiod_source = TABLES\n"))
        self.assertEqual((s.weekly_table, s.monthly_table, s.default_interval, s.period_source),
                         ("Weeks", "Months", "monthly", "tables"))
        for text in ("[chart]\ndefault_interval = hourly\n", "[chart]\nperiod_source = cache\n",
                     "[database]\nweekly_table = Stock_Daily\n", "[database]\nmonthly_table = bad name\n"):
            with self.assertRaises(ConfigError, msg=text):
                load_settings(self.write(text))

    def test_file_values_and_relative_paths(self):
        path = self.write("[database]\nsqlite_path = data/x.db\ndaily_table = Daily_Copy\n[server]\nport = 9000\n"
                          "[chart]\ndefault_ticker = mrk\ndefault_range = 1y\nmax_points = 300\n"
                          "[logging]\nlevel = debug\nfile = logs/chart.log\n")
        s = load_settings(path)
        self.assertEqual(s.sqlite_path, (self.dir / "data" / "x.db").resolve())
        self.assertEqual((s.daily_table, s.port, s.default_ticker, s.default_range, s.max_points, s.log_level),
                         ("Daily_Copy", 9000, "MRK", "1Y", 300, "DEBUG"))
        self.assertEqual(s.log_file, (self.dir / "logs" / "chart.log").resolve())
        self.assertEqual(s.config_file, path.resolve())

    def test_environment_wins(self):
        path = self.write("[server]\nport = 9000\n[auth]\nmode = basic\nusername = dummy_viewer\n")
        env = {"STOCK_CHART_DB": str(self.dir / "other.db"), "STOCK_CHART_PORT": "9100", "STOCK_CHART_HOST": "0.0.0.0",
               "STOCK_CHART_PASSWORD": "dummy-password"}
        with mock.patch.dict(os.environ, env):
            s = load_settings(path)
        self.assertEqual((s.sqlite_path, s.port, s.host, s.password), ((self.dir / "other.db").resolve(), 9100,
                                                                        "0.0.0.0", "dummy-password"))
        self.assertNotIn("dummy-password", repr(s))                     # never printed
        with mock.patch.dict(os.environ, {"STOCK_CHART_CONFIG": str(path), "STOCK_CHART_PASSWORD": "dummy-pw"}):
            self.assertEqual(load_settings().port, 9000)

    def test_validation(self):
        for text in ("[database]\ndaily_table = Stock Daily; DROP\n", "[server]\nport = 0\n", "[server]\nport = x\n",
                     "[chart]\ndefault_range = 2W\n", "[chart]\nmax_points = 5\n", "[auth]\nmode = token\n",
                     "[auth]\nmode = basic\n", "[logging]\nlevel = LOUD\n"):
            with self.assertRaises(ConfigError, msg=text):
                load_settings(self.write(text))
        with self.assertRaises(ConfigError):
            load_settings(self.dir / "missing.ini")
        with mock.patch.dict(os.environ, {"STOCK_CHART_PORT": "eighty"}):
            with self.assertRaises(ConfigError):
                load_settings(self.write(""))
        self.assertEqual(ChartSettings().validate().port, 8070)


if __name__ == "__main__":
    unittest.main()
