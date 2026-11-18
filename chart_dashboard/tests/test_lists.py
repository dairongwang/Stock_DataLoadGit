"""The stock lists saved in the database: parsing screen-list files, the ListStore (watchlist,
Selected list, screen lists) and the /api/lists endpoints.  The page itself: test_browser."""

from __future__ import annotations

import base64
import io
import os
import sqlite3
import stat
import unittest

from stock_chart.app import WRITE_HEADER, create_app
from stock_chart.config import APP_ROOT
from stock_chart.lists import (LIST_MAX, SCREEN_FILE_MAX_BYTES, SCREEN_LISTS_MAX, ListError, ListNotFound,
                               ListsUnavailable, ListStore, decode_file, list_name_from_file, parse_symbols,
                               render_ddl)

from .helpers import ChartTestCase

W = {WRITE_HEADER: "1"}


class ParsingTests(unittest.TestCase):
    def test_symbols_separated_by_commas(self):
        self.assertEqual(parse_symbols("HD, jnj,PFE"), (["HD", "JNJ", "PFE"], []))
        self.assertEqual(parse_symbols(' "hd" , JNJ,,\n pfe;MRK\tJPM, hd '), (["HD", "JNJ", "PFE", "MRK", "JPM"], []))
        self.assertEqual(parse_symbols("\ufeffHD,BRK-B.US,^GSPC"), (["HD", "BRK-B.US", "^GSPC"], []))
        self.assertEqual(parse_symbols("HD, bad/one, a+b, HD"), (["HD"], ["bad/one", "a+b"]))
        self.assertEqual(parse_symbols(""), ([], []))

    def test_files(self):
        self.assertEqual(decode_file("\ufeffHD,JNJ".encode("utf-8")), "HD,JNJ")
        self.assertEqual(decode_file("HD,\xe9".encode("cp1252")), "HD,\xe9")
        self.assertEqual(list_name_from_file("C:\\screens\\tech picks.txt"), "tech picks")
        self.assertEqual(list_name_from_file("momentum<2026>.csv"), "momentum 2026")
        self.assertEqual(list_name_from_file(".txt"), "Screen list")

    def test_reference_script(self):
        script = (APP_ROOT / "sql" / "create_chart_lists_sqlite.sql").read_text(encoding="utf-8")
        self.assertTrue(script.endswith(render_ddl()))


class StoreTests(ChartTestCase):
    def setUp(self):
        super().setUp()
        self.calls = 0

        def seed():
            self.calls += 1
            return ["aaa", "AAB", "bad one"]

        self.store = ListStore(self.db, seed)

    def test_default_lists(self):
        lists = self.store.lists()
        self.assertEqual([(l.kind, l.name, l.count) for l in lists], [("watch", "Watchlist", 2), ("selected", "Selected", 0)])
        self.assertEqual((lists[0].tickers, lists[0].as_dict()["edited"]), (["AAA", "AAB"], False))
        self.store.lists()
        self.assertEqual(self.calls, 1)                                   # seeded once
        self.assertEqual(len(self.store.lists(owner="dummy_other")), 2)    # every owner has its own
        with sqlite3.connect(self.db) as conn:                              # in the same database as the stock data
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM "Chart_List"').fetchone()[0], 4)

    def test_add_remove_replace(self):
        sel = self.store.of_kind("selected")
        lst, added, already = self.store.add(sel.list_id, ["zzz", "AAA", "zzz", "not valid"])
        self.assertEqual((lst.tickers, added, already), (["ZZZ", "AAA"], ["ZZZ", "AAA"], []))
        lst, added, already = self.store.add(sel.list_id, ["AAA", "AAB"])
        self.assertEqual((lst.tickers, added, already, lst.revision), (["ZZZ", "AAA", "AAB"], ["AAB"], ["AAA"], 2))
        lst, removed = self.store.remove(sel.list_id, "aaa")
        self.assertEqual((lst.tickers, removed), (["ZZZ", "AAB"], True))
        self.assertFalse(self.store.remove(sel.list_id, "AAA")[1])
        lst, _, _ = self.store.add(sel.list_id, ["AAA"])
        self.assertEqual(lst.tickers, ["ZZZ", "AAB", "AAA"])               # appended at the end
        self.assertEqual(self.store.replace(sel.list_id, ["aab", "ZZZ"]).tickers, ["AAB", "ZZZ"])
        with self.assertRaises(ListError):
            self.store.replace(sel.list_id, [f"T{i}" for i in range(LIST_MAX + 1)])
        with self.assertRaises(ListNotFound):
            self.store.add(999, ["AAA"])
        with self.assertRaises(ListNotFound):                               # another owner's list
            self.store.get(sel.list_id, owner="dummy_other")
        watch = self.store.of_kind("watch")
        self.store.replace(watch.list_id, ["ZZZ"])
        self.assertEqual(self.store.reset_watchlist().tickers, ["AAA", "AAB"])

    def test_screen_lists(self):
        lst, replaced = self.store.save_screen("tech picks", ["AAB", "ZZZ", "NOPE"], "tech picks.txt")
        self.assertEqual((lst.kind, lst.name, lst.tickers, lst.source_file, replaced),
                         ("screen", "tech picks", ["AAB", "ZZZ", "NOPE"], "tech picks.txt", False))
        again, replaced = self.store.save_screen("tech picks", ["AAA"], "tech picks (2).txt")
        self.assertEqual((again.list_id, again.tickers, again.source_file, replaced),
                         (lst.list_id, ["AAA"], "tech picks (2).txt", True))
        self.store.save_screen("a first", ["AAA"])
        self.assertEqual([l.name for l in self.store.lists()], ["Watchlist", "Selected", "a first", "tech picks"])
        with self.assertRaisesRegex(ListError, "no stock symbols"):
            self.store.save_screen("empty", [])
        with self.assertRaises(ListError):
            self.store.save_screen("  <>  ", ["AAA"])
        with self.assertRaisesRegex(ListError, "cannot be deleted"):
            self.store.delete(self.store.of_kind("selected").list_id)
        self.assertEqual(self.store.delete(lst.list_id).name, "tech picks")
        with self.assertRaises(ListNotFound):
            self.store.get(lst.list_id)
        with sqlite3.connect(self.db) as conn:                              # the items went with the list
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM "Chart_List_Item" WHERE "ListId" = ?',
                                          (lst.list_id,)).fetchone()[0], 0)
        for i in range(SCREEN_LISTS_MAX - 1):
            self.store.save_screen(f"list {i}", ["AAA"])
        with self.assertRaisesRegex(ListError, "at most"):
            self.store.save_screen("one too many", ["AAA"])

    def test_unavailable_storage(self):
        with self.assertRaises(ListsUnavailable):
            ListStore(self.root / "no such folder" / "lists.db").lists()
        if os.name != "nt" and os.geteuid() != 0:
            read_only = self.root / "read_only.db"
            ListStore(read_only).lists()
            read_only.chmod(stat.S_IRUSR)
            with self.assertRaisesRegex(ListsUnavailable, "sqlite_path"):
                ListStore(read_only).save_screen("x", ["AAA"])


class ApiTests(ChartTestCase):
    def setUp(self):
        super().setUp()
        self.settings.watchlist = ("AAB", "AAA")
        self.app = create_app(self.settings)
        self.client = self.app.test_client()
        self.lists = {l["kind"]: l for l in self.client.get("/api/lists").get_json()["lists"]}

    def upload(self, text: str | bytes, filename: str = "screen.txt", **extra):
        data = text.encode("utf-8") if isinstance(text, str) else text
        return self.client.post("/api/lists/screen", headers=W, content_type="multipart/form-data",
                                data={"file": (io.BytesIO(data), filename), **extra})

    def test_reading(self):
        body = self.client.get("/api/lists").get_json()
        self.assertEqual(body["max_items"], LIST_MAX)
        watch = self.lists["watch"]
        self.assertEqual((watch["name"], watch["tickers"], watch["count"], watch["edited"], watch["deletable"]),
                         ("Watchlist", ["AAB", "AAA"], 2, False, False))
        self.assertEqual(self.lists["selected"]["tickers"], [])
        one = self.client.get(f"/api/lists/{watch['id']}").get_json()
        self.assertEqual(one["tickers"], ["AAB", "AAA"])
        self.assertEqual(self.client.get("/api/lists/999").status_code, 404)

    def test_writes_need_the_header(self):
        sel = self.lists["selected"]["id"]
        r = self.client.post(f"/api/lists/{sel}/items", json={"tickers": ["AAA"]})
        self.assertEqual(r.status_code, 403)
        self.assertIn(WRITE_HEADER, r.get_json()["error"])
        self.assertEqual(self.client.delete(f"/api/lists/{sel}").status_code, 403)
        self.assertEqual(self.client.post("/api/lists/screen", data={}).status_code, 403)
        self.assertEqual(self.client.post("/api/daily/AAA").status_code, 405)      # other APIs: unchanged

    def test_selected_list(self):
        sel = self.lists["selected"]["id"]
        body = self.client.post(f"/api/lists/{sel}/items", headers=W, json={"tickers": ["aab", "NOPE", "x/y"]}).get_json()
        self.assertEqual((body["added"], body["unknown"], body["invalid"], body["list"]["tickers"]),
                         (["AAB"], ["NOPE"], ["x/y"], ["AAB"]))
        body = self.client.post(f"/api/lists/{sel}/items", headers=W, json={"text": "AAB, ZZZ"}).get_json()
        self.assertEqual((body["added"], body["already"], body["list"]["tickers"]), (["ZZZ"], ["AAB"], ["AAB", "ZZZ"]))
        self.assertEqual(self.client.post(f"/api/lists/{sel}/items", headers=W, json={"tickers": []}).status_code, 400)
        r = self.client.delete(f"/api/lists/{sel}/items/aab", headers=W)
        self.assertEqual((r.status_code, r.get_json()["list"]["tickers"]), (200, ["ZZZ"]))
        self.assertEqual(self.client.delete(f"/api/lists/{sel}/items/AAB", headers=W).status_code, 404)
        self.assertEqual(self.client.delete(f"/api/lists/{sel}", headers=W).status_code, 400)   # cannot be deleted
        self.assertEqual(self.client.post(f"/api/lists/{sel}/reset", headers=W).status_code, 400)
        # saved in the database - a new app (restart) sees it
        fresh = create_app(self.settings).test_client().get("/api/lists").get_json()["lists"]
        self.assertEqual([l["tickers"] for l in fresh if l["kind"] == "selected"], [["ZZZ"]])

    def test_watchlist_replace_and_reset(self):
        watch = self.lists["watch"]["id"]
        body = self.client.put(f"/api/lists/{watch}/items", headers=W, json={"tickers": ["ZZZ", "NOPE", "AAA"]}).get_json()
        self.assertEqual((body["list"]["tickers"], body["unknown"], body["list"]["edited"]), (["ZZZ", "AAA"], ["NOPE"], True))
        body = self.client.post(f"/api/lists/{watch}/reset", headers=W).get_json()
        self.assertEqual(body["list"]["tickers"], ["AAB", "AAA"])                  # [chart] watchlist

    def test_screen_list_from_a_text_file(self):
        r = self.upload("aaa, AAB ,zzz,NOPE, bad/one, aaa", "momentum.txt")
        self.assertEqual(r.status_code, 201)
        body = r.get_json()
        self.assertEqual((body["list"]["name"], body["list"]["kind"], body["symbols"], body["without_data"], body["invalid"],
                          body["replaced"]), ("momentum", "screen", 4, ["NOPE"], ["bad/one"], False))
        self.assertEqual(body["list"]["tickers"], ["AAA", "AAB", "ZZZ", "NOPE"])      # the file's order
        r = self.upload("ZZZ", "momentum.txt")                                         # the same name: replaced
        self.assertEqual((r.status_code, r.get_json()["list"]["tickers"], r.get_json()["replaced"]), (200, ["ZZZ"], True))
        r = self.upload("AAA,AAB", "whatever.csv", name="My picks")
        self.assertEqual(r.get_json()["list"]["name"], "My picks")
        kinds = [(l["kind"], l["name"]) for l in self.client.get("/api/lists").get_json()["lists"]]
        self.assertEqual(kinds, [("watch", "Watchlist"), ("selected", "Selected"), ("screen", "momentum"),
                                 ("screen", "My picks")])
        screen = r.get_json()["list"]["id"]
        self.assertNotIn("tickers", [l for l in self.client.get("/api/lists").get_json()["lists"] if l["id"] == screen][0])
        self.assertEqual(self.client.get(f"/api/lists/{screen}").get_json()["tickers"], ["AAA", "AAB"])
        self.assertEqual(self.client.delete(f"/api/lists/{screen}", headers=W).status_code, 200)
        self.assertEqual(self.client.get(f"/api/lists/{screen}").status_code, 404)

    def test_bad_uploads(self):
        for r, status, message in (
                (self.upload(" , ;; ", "empty.txt"), 400, "no stock symbols"),
                (self.client.post("/api/lists/screen", headers=W, data={}), 400, "choose a text file"),
                (self.upload(b"A," * (SCREEN_FILE_MAX_BYTES // 2 + 10), "big.txt"), 400, "larger than"),
                (self.upload(b"A," * SCREEN_FILE_MAX_BYTES, "huge.txt"), 413, "too large")):
            self.assertEqual(r.status_code, status, message)
            self.assertIn(message, r.get_json()["error"])

    def test_unavailable_storage_is_503(self):
        self.settings.lists_path = self.root / "missing folder" / "lists.db"
        client = create_app(self.settings).test_client()
        r = client.get("/api/lists")
        self.assertEqual(r.status_code, 503)
        self.assertIn("does not exist", r.get_json()["error"])
        self.assertEqual(client.get("/api/daily/AAA").status_code, 200)              # the charts still work

    def test_separate_list_database_and_owners(self):
        self.settings.lists_path = self.root / "lists.db"
        client = create_app(self.settings).test_client()
        client.get("/api/lists")
        self.assertTrue((self.root / "lists.db").is_file())
        self.settings.auth_mode, self.settings.username, self.settings.password = "basic", "dummy_viewer", "dummy-pass-1"
        client = create_app(self.settings.validate()).test_client()
        auth = {"Authorization": "Basic " + base64.b64encode(b"dummy_viewer:dummy-pass-1").decode()}
        self.assertEqual(client.get("/api/lists").status_code, 401)
        sel = [l for l in client.get("/api/lists", headers=auth).get_json()["lists"] if l["kind"] == "selected"][0]
        client.post(f"/api/lists/{sel['id']}/items", headers={**auth, **W}, json={"tickers": ["AAA"]})
        with sqlite3.connect(self.root / "lists.db") as conn:
            self.assertEqual(conn.execute('SELECT "Owner", COUNT(*) FROM "Chart_List" GROUP BY 1 ORDER BY 1').fetchall(),
                             [("", 2), ("dummy_viewer", 2)])


if __name__ == "__main__":
    unittest.main()
