import json
import unittest

from stock_dataload.file_formats import BUILTIN_FORMATS, FileFormat, FormatError, FormatStore

from .helpers import TempProjectTestCase


class FileFormatTests(unittest.TestCase):
    def test_mapping_mode(self):
        self.assertEqual(FileFormat(name="a").mapping_mode, "auto")
        self.assertEqual(FileFormat(name="a", columns=["ticker"]).mapping_mode, "sequence")
        self.assertEqual(FileFormat(name="a", column_map={"ticker": "Symbol"}).mapping_mode, "map")

    def test_pattern_matching(self):
        f = FileFormat(name="a", file_pattern="NYSE_*.csv; *.json")
        self.assertTrue(f.matches("NYSE_20250123.csv"))
        self.assertTrue(f.matches("nyse_20250123.CSV"))
        self.assertTrue(f.matches("anything.json"))
        self.assertFalse(f.matches("NASDAQ_20250123.csv"))
        self.assertFalse(FileFormat(name="a").matches("x.csv"))

    def test_validate(self):
        FileFormat(name="ok").validate()
        with self.assertRaises(FormatError):
            FileFormat(name="").validate()
        with self.assertRaises(FormatError):
            FileFormat(name="bad/name").validate()
        with self.assertRaises(FormatError):
            FileFormat(name="a", file_type="pdf").validate()
        with self.assertRaises(FormatError):
            FileFormat(name="a", delimiter=";;").validate()
        with self.assertRaises(FormatError):
            FileFormat(name="a", columns=["ticker", "nonsense"]).validate()
        with self.assertRaises(FormatError):
            FileFormat(name="a", column_map={"ticker": "Symbol"}).validate()          # incomplete map
        with self.assertRaises(FormatError):
            FileFormat(name="a", column_map={"ticker": "Symbol", "date": "#x", "open": "O", "high": "H",
                                             "low": "L", "close": "C", "vol": "V"}).validate()
        with self.assertRaises(FormatError):
            FileFormat(name="a", has_header=False).validate()                         # no header & no mapping
        FileFormat(name="a", has_header=False, columns=["ticker", "date", "open", "high", "low", "close", "vol"]).validate()

    def test_round_trip_dict(self):
        f = FileFormat(name="x", file_type="excel", sheet="Prices", skip_rows=1, column_map={"ticker": "A"})
        g = FileFormat.from_dict(json.loads(json.dumps(f.to_dict())))
        self.assertEqual(f, g)

    def test_from_dict_coercions(self):
        f = FileFormat.from_dict({"name": "x", "columns": "date, ticker ,vol", "delimiter": "", "date_format": "",
                                  "has_header": "false", "skip_rows": "2", "unknown_key": 1})
        self.assertEqual(f.columns, ["date", "ticker", "vol"])
        self.assertIsNone(f.delimiter)
        self.assertIsNone(f.date_format)
        self.assertFalse(f.has_header)
        self.assertEqual(f.skip_rows, 2)

    def test_from_form_modes(self):
        base = {"name": "f", "file_type": "csv", "has_header": "on", "delimiter": "tab"}
        f = FileFormat.from_form({**base, "mapping_mode": "auto"})
        self.assertEqual(f.mapping_mode, "auto")
        self.assertEqual(f.delimiter, "\t")
        f = FileFormat.from_form({**base, "mapping_mode": "sequence", "columns": "date, ticker, vol, close, open, high, low"})
        self.assertEqual(f.columns, ["date", "ticker", "vol", "close", "open", "high", "low"])
        form = {**base, "mapping_mode": "map", "map_ticker": "Symbol", "map_date": "#2", "map_open": "Open",
                "map_high": "High", "map_low": "Low", "map_close": "Close", "map_vol": "Volume"}
        f = FileFormat.from_form(form)
        self.assertEqual(f.column_map["date"], "#2")
        self.assertIsNone(f.columns)
        f = FileFormat.from_form({"name": "n", "file_type": "json", "record_path": "data.rows", "skip_rows": ""})
        self.assertFalse(f.has_header)
        self.assertEqual(f.record_path, "data.rows")


class FormatStoreTests(TempProjectTestCase):
    def test_new_store_starts_with_builtins_and_persists(self):
        path = self.root / "cfg" / "formats.json"
        store = FormatStore(path)
        self.assertEqual(set(store.names()), {f.name for f in BUILTIN_FORMATS})
        self.assertFalse(path.exists())                       # nothing written until a change
        store.put(FileFormat(name="mine", file_type="json", file_pattern="*.json"))
        self.assertTrue(path.exists())
        again = FormatStore(path)
        self.assertIn("mine", again.names())
        self.assertEqual(again.get("mine").file_pattern, "*.json")

    def test_put_validates_and_renames(self):
        store = FormatStore(self.root / "f.json")
        with self.assertRaises(FormatError):
            store.put(FileFormat(name=""))
        store.put(FileFormat(name="a", columns=["ticker", "date", "open", "high", "low", "close", "vol"]))
        store.put(FileFormat(name="b", columns=["ticker", "date", "open", "high", "low", "close", "vol"]), old_name="a")
        self.assertFalse(store.exists("a"))
        self.assertTrue(store.exists("b"))

    def test_delete_and_unknown(self):
        store = FormatStore(self.root / "f.json")
        store.put(FileFormat(name="tmp"))
        store.delete("tmp")
        self.assertFalse(store.exists("tmp"))
        with self.assertRaises(FormatError):
            store.delete("tmp")
        with self.assertRaises(FormatError):
            store.get("tmp")

    def test_match_prefers_user_formats_over_builtins(self):
        store = FormatStore(self.root / "f.json")
        store.put(FileFormat(name="user_json", file_pattern="*.json"))
        store.put(FileFormat(name="shipped_json", file_pattern="*.json", builtin=True))
        self.assertEqual(store.match("x.json").name, "user_json")
        self.assertIsNone(store.match("x.csv"))
        self.assertEqual(store.suggest("x.csv").name, "(auto-detect)")

    def test_legacy_ini_formats_are_imported(self):
        store = FormatStore(self.root / "f.json", legacy={"old": ["date", "ticker", "vol", "close", "open", "high", "low"]})
        self.assertEqual(store.get("old").columns[0], "date")

    def test_invalid_json_file(self):
        p = self.root / "broken.json"
        p.write_text("{not json")
        with self.assertRaises(FormatError):
            FormatStore(p)

    def test_shipped_formats_json_is_valid(self):
        from .helpers import PROJECT_ROOT
        store = FormatStore(PROJECT_ROOT / "formats.json")
        for f in store.all():
            f.validate()
        self.assertTrue({"yahoo", "pipe_noheader", "excel_prices", "broker_csv"} <= set(store.names()))


if __name__ == "__main__":
    unittest.main()
