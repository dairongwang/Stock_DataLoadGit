"""
Configuration (``config.ini`` <-> :class:`Settings`) and the command line basics: the shipped
configuration, a save / load round trip, invalid values, secrets from environment variables,
and ``--version`` / ``init-db`` / ``status`` / ``formats``.

Every secret here is a dummy.
"""

from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from stock_dataload import __version__, cli
from stock_dataload.config import ConfigError, Settings, load_settings, parse_token_packs, save_settings

from .helpers import PROJECT_ROOT, TempProjectTestCase


class ShippedConfigTests(unittest.TestCase):
    def test_the_shipped_config_loads(self):
        s = load_settings(PROJECT_ROOT / "config.ini")
        self.assertEqual(s.sqlite_path, PROJECT_ROOT / "data" / "stock_trade.db")
        self.assertEqual(s.payment_gateway, "fake")                       # nothing is charged out of the box
        self.assertEqual(s.mail_provider, "console")
        self.assertEqual(s.eod_api_token.lower(), "dummy")                 # a placeholder, never a real token
        for secret in (s.stripe_secret_key, s.paypal_client_secret, s.smtp_password, s.twilio_auth_token,
                       s.google_client_secret, s.facebook_app_secret, s.apple_client_secret):
            self.assertTrue(secret == "" or "dummy" in secret.lower(), "a real secret in config.ini")


class RoundTripTests(TempProjectTestCase):
    def test_save_and_load(self):
        self.settings.config_file = self.root / "config.ini"
        self.settings.default_market = "NYSE"
        self.settings.token_packs = parse_token_packs("Mini:10:1.00, Maxi:1000:50")
        self.settings.eod_market_types = ("Common Stock", "ETF")
        save_settings(self.settings, self.settings.config_file)
        loaded = load_settings(self.settings.config_file)
        self.assertEqual(loaded.default_market, "NYSE")
        self.assertEqual(loaded.token_packs, (("Mini", 10, 100), ("Maxi", 1000, 5000)))
        self.assertEqual(loaded.eod_market_types, ("Common Stock", "ETF"))
        self.assertEqual(loaded.loading_dir, self.settings.loading_dir)
        self.assertIn("loading_dir = loading", self.settings.config_file.read_text(encoding="utf-8"))   # relative

    def test_invalid_values(self):
        path = self.root / "bad.ini"
        for section, text in (("database", "batch_size = 0"), ("portal", "payment_gateway = paid"),
                              ("mail", "provider = pigeon"), ("eod_api", "base_url = ftp://x"),
                              ("accounts", "dashboard_registration_role = admin"), ("portal", "packs = Broken")):
            with self.subTest(section=section, text=text):
                path.write_text(f"[{section}]\n{text}\n", encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load_settings(path)

    def test_secrets_from_the_environment(self):
        path = self.root / "config.ini"
        save_settings(Settings(), path)
        env = {"STOCK_DATALOAD_STRIPE_SECRET_KEY": "sk_test_dummy", "STOCK_DATALOAD_SMTP_PASSWORD": "dummy-smtp",
               "STOCK_DATALOAD_TWILIO_AUTH_TOKEN": "dummy-twilio", "STOCK_DATALOAD_GOOGLE_CLIENT_SECRET": "dummy-google"}
        with mock.patch.dict(os.environ, env):
            s = load_settings(path)
        self.assertEqual((s.stripe_secret_key, s.smtp_password, s.twilio_auth_token, s.google_client_secret),
                         ("sk_test_dummy", "dummy-smtp", "dummy-twilio", "dummy-google"))


class CliBasicsTests(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.settings.config_file = self.root / "config.ini"
        save_settings(self.settings, self.settings.config_file)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = cli.main(["--config", str(self.settings.config_file), "-q", *args])
            except SystemExit as exc:                                    # argparse: --version / --help
                rc = exc.code
        return rc, out.getvalue(), err.getvalue()

    def test_version_init_db_status(self):
        rc, out, _ = self.run_cli("--version")
        self.assertEqual(rc, 0)
        self.assertIn(__version__, out)
        rc, out, err = self.run_cli("init-db")
        self.assertEqual(rc, 0, err)
        import sqlite3
        with sqlite3.connect(self.settings.sqlite_path) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        for table in ("Stock_Stage", "Stock_Daily", "App_User", "Api_Customer", "Auth_Code", "Eod_Market_Run"):
            self.assertIn(table, tables)
        self.assertEqual(self.run_cli("status")[0], 0)
        rc, out, _ = self.run_cli("formats")
        self.assertEqual(rc, 0)

    def test_a_broken_config_is_reported(self):
        self.settings.config_file.write_text("[database]\nbatch_size = many\n", encoding="utf-8")
        rc, _, err = self.run_cli("status")
        self.assertEqual(rc, 2)
        self.assertIn("configuration error", err)


if __name__ == "__main__":
    unittest.main()
