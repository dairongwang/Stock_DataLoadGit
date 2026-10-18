"""
Dashboard: bulk folder load, the active tables page (/promote, /active/<kind>),
the promotion JSON api and the new settings (Flask test client).
"""

from __future__ import annotations

import re
import unittest

from stock_dataload.db import StageDatabase, StageRow, now_iso

from .helpers import DEFAULT_HEADER
from .test_dashboard import OPERATOR, VIEWER, DashboardTestCase


def flashes(page: str) -> list[str]:
    return [re.sub(r"<[^>]+>", "", m).strip() for m in re.findall(r'class="flash[^"]*">(.*?)</div>', page, re.S)]


class PromotionDashboardTests(DashboardTestCase):
    def seed_staging(self) -> None:
        db = StageDatabase(self.state.settings)
        db.ensure_stage_table()
        with db.connection() as conn:
            for ticker, day, close in (("AAA", "20250106", "1.5"), ("AAA", "20250107", "1.6"), ("BBB", "20250106", "5.5"),
                                       ("BBB", "20250105", "5.5")):      # Sunday -> rejected
                db.insert_stage_row(conn, StageRow("NASDAQ", ticker, day, "1", "6", "0.5", close, "100",
                                                   "NASDAQ_x.txt", "L1", now_iso()))

    # -- pages render -------------------------------------------------------------
    def test_pages_render_empty(self):
        for url in ("/promote", "/active/daily", "/active/weekly", "/active/monthly", "/api/promotions",
                    "/api/active/daily", "/promote?procedure=promote_daily&limit=5"):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
        page = self.client.get("/promote").get_data(as_text=True)
        self.assertIn("never promoted", page)
        self.assertIn("Validation rules", page)
        self.assertIn("No promotion runs yet", page)
        self.assertIn("the table is empty", self.client.get("/active/weekly").get_data(as_text=True))
        self.assertEqual(self.client.get("/active/yearly").status_code, 404)
        self.assertEqual(self.client.get("/api/active/yearly").status_code, 404)
        # overview shows the active tables and the bulk load form
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("Bulk load a folder", page)
        self.assertIn("Stock_Daily", page)
        self.assertIn("never", page)

    def test_promote_from_the_dashboard(self):
        self.seed_staging()
        r = self.client.post("/promote", data={"what": "all"})
        self.assertEqual(r.status_code, 302)
        page = self.client.get("/promote").get_data(as_text=True)
        msgs = flashes(page)
        self.assertTrue(any("promote_daily: completed (full) - 4 rows read: 3 inserted" in m for m in msgs), msgs)
        self.assertTrue(any("1 staging row(s) were rejected" in m for m in msgs), msgs)
        self.assertTrue(any("promote_weekly: completed" in m for m in msgs), msgs)
        self.assertTrue(any("promote_monthly: completed" in m for m in msgs), msgs)
        self.assertIn("promote_monthly", page)                      # history table
        self.assertIn('<span class="badge ok">completed</span>', page)
        # counts on the page
        self.assertIn("3</div>", page)
        # browse the tables
        page = self.client.get("/active/daily?ticker=AAA&order=asc").get_data(as_text=True)
        self.assertIn("2025-01-06", page)
        self.assertIn("2025-01-07", page)
        self.assertNotIn("BBB", page)
        page = self.client.get("/active/weekly").get_data(as_text=True)
        self.assertIn("2025-01-06", page)          # WeekStart
        self.assertIn("2025-01-12", page)          # WeekEnd
        data = self.client.get("/api/active/monthly?ticker=BBB").get_json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["records"][0]["MonthStart"], "2025-01-01")
        self.assertEqual(data["table"], "Stock_Monthly")
        data = self.client.get("/active/daily?json=1&ticker=AAA").get_json()
        self.assertEqual(data["count"], 2)
        data = self.client.get("/api/promotions").get_json()
        self.assertEqual(data["count"], 3)
        self.assertEqual(data["records"][0]["procedure"], "promote_monthly")
        # monitoring strip / counts
        counts = self.client.get("/api/monitor").get_json()
        self.assertEqual((counts["promotions"], counts["failed_promotions"]), (3, 0))
        # the rejected Sunday row + BBB's week and month are incomplete (no row for 2025-01-07)
        self.assertEqual(counts["data_errors"], 3)
        page = self.client.get("/monitor/data-errors").get_data(as_text=True)
        self.assertIn("market_closed", page)
        self.assertIn("Promotions", page)
        self.assertIn("incomplete_week", page)
        self.assertIn("BBB week 2025-W02 (2025-01-06..2025-01-12) is incomplete: 1 of 2 trading days loaded - missing 2025-01-07", page)
        page = self.client.get("/active/weekly?incomplete=1").get_data(as_text=True)
        self.assertIn("BBB", page)
        self.assertNotIn(">AAA<", page)
        self.assertEqual(self.client.get("/api/active/monthly?incomplete=1").get_json()["records"][0]["MissingDates"], "2025-01-07")

    def test_promote_with_filters_and_bad_input(self):
        self.seed_staging()
        self.client.post("/promote", data={"what": "daily", "ticker": "AAA", "date_from": "20250107", "full": "on"})
        msgs = flashes(self.client.get("/promote").get_data(as_text=True))
        self.assertTrue(any("1 rows read: 1 inserted" in m for m in msgs), msgs)
        self.client.post("/promote", data={"what": "daily", "date_from": "not-a-date"})
        msgs = flashes(self.client.get("/promote").get_data(as_text=True))
        self.assertTrue(any("not a date" in m for m in msgs), msgs)
        self.assertEqual(self.client.post("/promote", data={"what": "yearly"}).status_code, 400)
        r = self.client.get("/active/daily?from=garbage")
        self.assertEqual(r.status_code, 200)
        self.assertIn("not a date", r.get_data(as_text=True))
        self.assertEqual(self.client.get("/api/active/daily?from=garbage").status_code, 400)

    def test_roles(self):
        self.logout()
        self.login(*VIEWER)
        self.assertEqual(self.client.get("/promote").status_code, 200)
        self.assertEqual(self.client.get("/active/daily").status_code, 200)
        self.assertEqual(self.client.post("/promote", data={"what": "all"}).status_code, 403)
        self.assertEqual(self.client.post("/load-folder", data={"folder": "x"}).status_code, 403)
        page = self.client.get("/promote").get_data(as_text=True)
        self.assertNotIn("Run promotion", page)
        self.logout()
        self.login(*OPERATOR)
        self.assertEqual(self.client.post("/promote", data={"what": "all"}).status_code, 302)
        self.assertEqual(self.client.post("/load-folder", data={"folder": str(self.root / "nothing")}).status_code, 302)

    # -- bulk load ------------------------------------------------------------------
    def test_load_folder_from_the_dashboard(self):
        folder = self.root / "deliveries"
        folder.mkdir()
        for day in ("20250106", "20250107", "20250108"):
            (folder / f"NASDAQ_{day}.txt").write_text(DEFAULT_HEADER + f"AAA,{day},1,2,0.5,1.5,100\n")
        (folder / "NASDAQ_20250109.txt").write_text("broken\n")
        r = self.client.post("/load-folder", data={"folder": str(folder), "patterns": "*.txt"})
        self.assertEqual(r.status_code, 302)
        msgs = flashes(self.client.get("/").get_data(as_text=True))
        self.assertTrue(any("3 of 4 file(s) loaded (3 rows), 1 failed" in m for m in msgs), msgs)
        self.assertTrue(any("NASDAQ_20250109.txt: failed" in m for m in msgs), msgs)
        self.assertEqual(StageDatabase(self.state.settings).count_rows(), 3)
        self.assertEqual(len(list(folder.iterdir())), 4, "files stay in place")
        # move + stop on error + market
        (folder / "NASDAQ_20250109.txt").unlink()
        r = self.client.post("/load-folder", data={"folder": str(folder), "move": "on", "stop_on_error": "on",
                                                   "market": "NYSE", "truncate": "on"})
        msgs = flashes(self.client.get("/").get_data(as_text=True))
        self.assertTrue(any("3 of 3 file(s) loaded" in m for m in msgs), msgs)
        self.assertEqual(len(list(folder.iterdir())), 0)
        self.assertEqual(len(list(self.state.settings.archive_dir.iterdir())), 3)
        rows = StageDatabase(self.state.settings).find_stage_rows(ticker="AAA")
        self.assertEqual({r["Market"] for r in rows}, {"NYSE"})

    def test_load_folder_errors(self):
        msgs = flashes(self.client.get(self.client.post("/load-folder", data={"folder": ""}).headers["Location"]).get_data(as_text=True))
        self.assertTrue(any("Please enter the folder" in m for m in msgs), msgs)
        self.client.post("/load-folder", data={"folder": str(self.root / "missing")})
        msgs = flashes(self.client.get("/").get_data(as_text=True))
        self.assertTrue(any("folder not found" in m for m in msgs), msgs)
        empty = self.root / "empty"
        empty.mkdir()
        self.client.post("/load-folder", data={"folder": str(empty)})
        msgs = flashes(self.client.get("/").get_data(as_text=True))
        self.assertTrue(any("no files matching" in m for m in msgs), msgs)

    def test_database_problems_are_reported_not_crashes(self):
        import sqlite3
        import unittest.mock as mock
        from stock_dataload.db import DatabaseError
        with mock.patch.object(self.state.promotions, "summaries", side_effect=sqlite3.OperationalError("locked")):
            r = self.client.get("/promote")
            self.assertEqual(r.status_code, 200)
            self.assertIn("database problem: locked", r.get_data(as_text=True))
            self.assertEqual(self.client.get("/").status_code, 200)       # overview survives too
        with mock.patch.object(self.state.promotions, "fetch", side_effect=DatabaseError("gone")):
            r = self.client.get("/active/daily")
            self.assertEqual(r.status_code, 200)
            self.assertIn("database problem: gone", r.get_data(as_text=True))
            r = self.client.get("/api/active/daily")
            self.assertEqual(r.status_code, 503)
            self.assertEqual(r.get_json()["error"], "gone")
        with mock.patch.object(self.state.loader, "load_folder", side_effect=DatabaseError("no disk")):
            self.client.post("/load-folder", data={"folder": str(self.root)})
            msgs = flashes(self.client.get("/").get_data(as_text=True))
            self.assertTrue(any("database problem: no disk" in m for m in msgs), msgs)
        counts = self.state.monitor_counts()
        self.assertIn("promotions", counts)

    # -- settings ---------------------------------------------------------------------
    def test_settings_page_has_the_new_options(self):
        page = self.client.get("/settings").get_data(as_text=True)
        for field in ("daily_table", "weekly_table", "monthly_table", "reject_market_closed_days", "check_price_range"):
            self.assertIn(f'name="{field}"', page)
        form = self.settings_form(daily_table="Active_Daily", weekly_table="Active_Weekly",
                                  monthly_table="Active_Monthly", check_price_range="on")
        r = self.client.post("/settings", data=form)
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True))
        s = self.state.settings
        self.assertEqual((s.daily_table, s.weekly_table, s.monthly_table), ("Active_Daily", "Active_Weekly", "Active_Monthly"))
        self.assertFalse(s.reject_market_closed_days)        # checkbox not sent
        self.assertTrue(s.check_price_range)
        self.assertIn("daily_table = Active_Daily", s.config_file.read_text())
        self.assertEqual(self.state.promotions.tables["daily"], "Active_Daily")
        r = self.client.post("/settings", data=self.settings_form(daily_table="Stock_Stage"))
        self.assertEqual(r.status_code, 400)
        self.assertIn("must differ", r.get_data(as_text=True))

    def settings_form(self, **overrides):
        s = self.state.settings
        form = {
            "conversion_dir": str(s.conversion_dir), "converted_dir": str(s.converted_dir),
            "loading_dir": str(s.loading_dir), "archive_dir": str(s.archive_dir), "error_dir": str(s.error_dir),
            "work_dir": str(s.work_dir), "log_dir": str(s.log_dir), "formats_file": str(s.formats_file),
            "sqlite_path": str(s.sqlite_path), "stage_table": s.stage_table, "batch_size": str(s.batch_size),
            "file_patterns": ";".join(s.file_patterns),
            "market_from_filename_regex": s.market_from_filename_regex, "default_market": s.default_market,
            "encoding": s.encoding, "daily_table": s.daily_table, "weekly_table": s.weekly_table,
            "monthly_table": s.monthly_table,
        }
        form.update(overrides)
        return form


if __name__ == "__main__":
    unittest.main()
