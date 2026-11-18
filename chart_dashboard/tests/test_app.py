"""The HTTP side: pages, JSON / CSV API, /health, basic auth, headers, the command line."""

from __future__ import annotations

import base64
import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from stock_chart.app import create_app, main

from .helpers import ChartTestCase


class AppTestCase(ChartTestCase):
    def setUp(self):
        super().setUp()
        self.app = create_app(self.settings)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()


class PageTests(AppTestCase):
    def test_chart_page(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        page = r.get_data(as_text=True)
        self.assertIn('data-ticker="AAA"', page)                     # default: the ticker with most rows
        self.assertIn('data-range="6M"', page)
        self.assertIn("candlestick.js", page)
        self.assertNotIn("cdn", page.lower())                        # no external resources
        page = self.client.get("/?ticker=aab&range=1y").get_data(as_text=True)
        self.assertIn('data-ticker="AAB"', page)
        self.assertIn('data-range="1Y"', page)
        self.assertIn('data-range="6M"', self.client.get("/?range=bogus").get_data(as_text=True))
        page = self.client.get("/?ticker=%3Cscript%3E").get_data(as_text=True)
        self.assertIn("is not a valid ticker", page)
        self.assertNotIn("<script>", page.replace('<script src=', ''))
        r = self.client.get("/chart/AAB?range=3M")
        self.assertEqual(r.status_code, 302)
        self.assertIn("ticker=AAB", r.headers["Location"])
        self.assertEqual(self.client.get("/nope").status_code, 404)
        for name in ("candlestick.js", "app.js", "chart.css"):
            self.assertEqual(self.client.get(f"/static/{name}").status_code, 200, name)

    def test_security_headers(self):
        r = self.client.get("/")
        self.assertIn("script-src 'self'", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertEqual(self.client.get("/api/tickers").headers["Cache-Control"], "no-store")

    def test_empty_or_missing_data(self):
        self.settings.daily_table = "Missing_Table"
        client = create_app(self.settings).test_client()
        page = client.get("/").get_data(as_text=True)
        self.assertIn("promote data", page)
        self.assertEqual(client.get("/api/daily/AAA").status_code, 503)
        self.assertEqual(client.get("/api/tickers").status_code, 503)
        r = client.get("/health")
        self.assertEqual((r.status_code, r.get_json()["status"]), (503, "degraded"))


class ApiTests(AppTestCase):
    def test_tickers(self):
        data = self.client.get("/api/tickers?q=aa").get_json()
        self.assertEqual([t["ticker"] for t in data["tickers"]], ["AAA", "AAB"])
        self.assertEqual(data["tickers"][0]["rows"], 300)
        self.assertEqual(self.client.get("/api/tickers?limit=x").status_code, 400)

    def test_daily(self):
        data = self.client.get("/api/daily/AAA").get_json()
        self.assertEqual(data["columns"], ["date", "open", "high", "low", "close", "volume"])
        self.assertEqual((data["count"], data["total"], data["truncated"], data["market"]), (300, 300, False, "NASDAQ"))
        self.assertEqual(data["rows"][0], list(self.data["AAA"][0][1:]))
        data = self.client.get("/api/daily/AAA?limit=10").get_json()
        self.assertEqual((data["count"], data["truncated"], data["last"]), (10, True, self.data["AAA"][-1][1]))
        first = self.data["AAA"][5][1]
        data = self.client.get(f"/api/daily/AAA?from={first}&to={first}").get_json()
        self.assertEqual(data["rows"], [list(self.data["AAA"][5][1:])])
        for url, status in (("/api/daily/NOPE", 404), ("/api/daily/A%20A", 400), ("/api/daily/AAA?from=yesterday", 400),
                            ("/api/daily/AAA?limit=many", 400), ("/api/nothing", 404)):
            r = self.client.get(url)
            self.assertEqual(r.status_code, status, url)
            self.assertIn("error", r.get_json())
        self.assertEqual(self.client.post("/api/daily/AAA").status_code, 405)

    def test_csv(self):
        r = self.client.get("/api/daily/AAB?format=csv")
        self.assertEqual(r.mimetype, "text/csv")
        lines = r.get_data(as_text=True).splitlines()
        self.assertEqual(lines[0], "date,open,high,low,close,volume")
        self.assertEqual(len(lines), 41)
        self.assertIn('filename="AAB_', r.headers["Content-Disposition"])

    def test_health(self):
        body = self.client.get("/health").get_json()
        self.assertEqual((body["status"], body["rows"], body["table"]), ("ok", 345, "Stock_Daily"))


class AuthTests(ChartTestCase):
    def setUp(self):
        super().setUp()
        self.settings.auth_mode, self.settings.username, self.settings.password = "basic", "dummy_viewer", "dummy-pass-1"
        self.client = create_app(self.settings.validate()).test_client()

    def header(self, user, password):
        return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}

    def test_basic_auth(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 401)
        self.assertIn("Basic", r.headers["WWW-Authenticate"])
        self.assertEqual(self.client.get("/api/daily/AAA", headers=self.header("dummy_viewer", "wrong")).status_code, 401)
        self.assertEqual(self.client.get("/api/daily/AAA", headers={"Authorization": "Basic !!!"}).status_code, 401)
        self.assertEqual(self.client.get("/", headers=self.header("dummy_viewer", "dummy-pass-1")).status_code, 200)
        self.assertEqual(self.client.get("/health").status_code, 200)          # monitoring needs no password


class CommandLineTests(ChartTestCase):
    def run_main(self, *args, env=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env or {}), redirect_stdout(out), redirect_stderr(err):
            rc = main(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_check(self):
        ini = self.root / "chart.ini"
        ini.write_text("[database]\nsqlite_path = stock_trade.db\n", encoding="utf-8")
        rc, out, _ = self.run_main("--config", str(ini), "--check")
        self.assertEqual(rc, 0)
        self.assertIn("345 rows in Stock_Daily", out)
        ini.write_text("[database]\nsqlite_path = nothing.db\n", encoding="utf-8")
        rc, _, err = self.run_main("--config", str(ini), "--check")
        self.assertEqual(rc, 1)
        self.assertIn("DATA NOT AVAILABLE", err)
        ini.write_text("[server]\nport = -1\n", encoding="utf-8")
        rc, _, err = self.run_main("--config", str(ini), "--check")
        self.assertEqual(rc, 2)
        self.assertIn("configuration error", err)

    def test_run_uses_the_settings(self):
        ini = self.root / "chart.ini"
        ini.write_text("[database]\nsqlite_path = stock_trade.db\n[server]\nport = 9123\n", encoding="utf-8")
        with mock.patch("flask.Flask.run") as run:
            rc, out, _ = self.run_main("--config", str(ini), "--host", "0.0.0.0")
        self.assertEqual(rc, 0)
        self.assertEqual(run.call_args.kwargs["host"], "0.0.0.0")
        self.assertEqual(run.call_args.kwargs["port"], 9123)
        self.assertIn("http://0.0.0.0:9123/", out)


if __name__ == "__main__":
    unittest.main()
