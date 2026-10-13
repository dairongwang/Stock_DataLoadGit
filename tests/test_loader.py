import shutil
import unittest

from stock_dataload.converter import ConversionSpec, FileConverter
from stock_dataload.db import STAGE_COLUMNS, StageDatabase
from stock_dataload.loader import StageLoader

from .helpers import BROKER, DEFAULT_HEADER, ORIGINAL, REORDERED, TempProjectTestCase


class LoaderTests(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.db = StageDatabase(self.settings)
        self.loader = StageLoader(self.settings, self.db)

    def stage(self, src=ORIGINAL, name=None):
        dest = self.settings.loading_dir / (name or src.name)
        shutil.copy(src, dest)
        return dest

    def rows(self, source_file=None):
        with self.db.connection() as conn:
            return self.db.fetch_all(conn, source_file)

    # ------------------------------------------------------------------
    def test_load_sample_file(self):
        self.stage()
        results = self.loader.run()
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertTrue(r.ok, r.message)
        self.assertEqual(r.rows_loaded, 4092)
        self.assertEqual(r.market, "NASDAQ")
        self.assertEqual(r.rows_rejected, 0)
        # file moved to archive, loading folder empty
        self.assertEqual(r.final_path, self.settings.archive_dir / "NASDAQ_20250123.txt")
        self.assertTrue(r.final_path.exists())
        self.assertEqual(self.loader.pending_files(), [])
        # database content
        self.assertEqual(self.db.count_rows(), 4092)
        rows = self.rows()
        first = dict(zip(STAGE_COLUMNS, rows[0]))
        self.assertEqual(first["Market"], "NASDAQ")
        self.assertEqual(first["Ticker"], "AACG")
        self.assertEqual(first["TradeDate"], "20250123")
        self.assertEqual((first["Open"], first["High"], first["Low"], first["Close"], first["Vol"]),
                         ("0.9", "0.9229", "0.89", "0.9128", "2800"))
        self.assertEqual(first["SourceFile"], "NASDAQ_20250123.txt")
        self.assertEqual(first["LoadId"], r.load_id)
        last = dict(zip(STAGE_COLUMNS, rows[-1]))
        self.assertEqual(last["Ticker"], "ZYXI")
        self.assertEqual(len({row[9] for row in rows}), 1)  # single LoadId

    def test_nothing_to_load(self):
        self.assertEqual(self.loader.run(), [])

    def test_file_patterns_respected(self):
        self.stage(name="NASDAQ_20250123.dat")
        self.assertEqual(self.loader.pending_files(), [])

    def test_explicit_market_and_default_market(self):
        self.stage(name="prices.txt")
        r = self.loader.run(market="nyse")[0]
        self.assertEqual(r.market, "nyse")
        self.settings.default_market = "OTHER"
        self.stage(name="prices2.txt")
        r = self.loader.run()[0]
        self.assertEqual(r.market, "OTHER")

    def test_wrong_column_sequence_is_quarantined(self):
        self.stage(REORDERED)
        r = self.loader.run()[0]
        self.assertEqual(r.status, "failed")
        self.assertIn("not the default column sequence", r.message)
        self.assertEqual(r.final_path, self.settings.error_dir / REORDERED.name)
        self.assertTrue((self.settings.error_dir / (REORDERED.name + ".error.txt")).exists())
        self.assertEqual(self.db.count_rows(), 0)

    def test_bad_row_rolls_back_whole_file(self):
        self.write("loading/NYSE_20250123.txt",
                   DEFAULT_HEADER + "AAA,20250123,1,2,0.5,1.5,100\nBBB,2025-01-23,1,2,0.5,1.5,100\n")
        r = self.loader.run()[0]
        self.assertEqual(r.status, "failed")
        self.assertIn("line 3", r.message)
        self.assertEqual(self.db.count_rows(), 0)
        self.assertTrue((self.settings.error_dir / "NYSE_20250123.txt").exists())

    def test_skip_bad_rows(self):
        self.settings.skip_bad_rows = True
        self.write("loading/NYSE_20250123.txt",
                   DEFAULT_HEADER + "AAA,20250123,1,2,0.5,1.5,100\nBBB,2025-01-23,1,2,0.5,1.5,100\n"
                                    "CCC,20250123,x,2,0.5,1.5,100\nDDD,20250123,1,2,0.5,1.5,100\n")
        r = self.loader.run()[0]
        self.assertTrue(r.ok)
        self.assertEqual(r.rows_loaded, 2)
        self.assertEqual(r.rows_rejected, 2)
        self.assertEqual([row[1] for row in self.rows()], ["AAA", "DDD"])
        report = self.settings.error_dir / "NYSE_20250123.txt.rejects.txt"
        self.assertIn("line,reason,data", report.read_text())

    def test_empty_and_header_only_files_fail(self):
        self.write("loading/A_20250101.txt", "")
        self.write("loading/B_20250101.txt", DEFAULT_HEADER)
        results = self.loader.run()
        self.assertEqual([r.status for r in results], ["failed", "failed"])
        self.assertIn("empty", results[0].message)
        self.assertIn("no data rows", results[1].message)

    def test_reload_replaces_rows_of_same_file(self):
        self.stage()
        first = self.loader.run()[0]
        self.stage()
        second = self.loader.run()[0]
        self.assertTrue(second.ok)
        self.assertEqual(second.rows_replaced, 4092)
        self.assertEqual(self.db.count_rows(), 4092)
        self.assertEqual(self.db.count_rows(source_file="NASDAQ_20250123.txt"), 4092)
        self.assertNotEqual(first.load_id, second.load_id)
        # archive keeps both copies (second gets a timestamp suffix)
        self.assertEqual(len(list(self.settings.archive_dir.iterdir())), 2)

    def test_reload_appends_when_replace_disabled(self):
        self.settings.replace_existing_file_rows = False
        self.stage()
        self.loader.run()
        self.stage()
        self.loader.run()
        self.assertEqual(self.db.count_rows(), 2 * 4092)

    def test_truncate_before_load(self):
        self.stage(name="NASDAQ_20250122.txt")
        self.loader.run()
        self.stage(name="NASDAQ_20250123.txt")
        self.stage(name="NASDAQ_20250124.txt")
        results = self.loader.run(truncate=True)
        self.assertTrue(all(r.ok for r in results))
        # truncated once, then both files of this run loaded
        self.assertEqual(self.db.count_rows(), 2 * 4092)
        self.assertEqual(self.db.count_rows(source_file="NASDAQ_20250122.txt"), 0)

    def test_keep_files_in_place(self):
        path = self.stage()
        r = self.loader.run(move_files=False)[0]
        self.assertTrue(r.ok)
        self.assertTrue(path.exists())
        self.assertEqual(r.final_path, path)

    def test_load_specific_file_outside_loading_folder(self):
        r = self.loader.run(files=[ORIGINAL], move_files=False)[0]
        self.assertTrue(r.ok)
        self.assertTrue(ORIGINAL.exists())
        self.assertEqual(self.db.count_rows(), 4092)

    def test_header_without_angle_brackets_is_accepted(self):
        self.write("loading/NASDAQ_20250123.txt",
                   "ticker,date,open,high,low,close,vol\nAAA,20250123,1,2,0.5,1.5,100\n")
        r = self.loader.run()[0]
        self.assertTrue(r.ok, r.message)
        self.assertEqual(r.rows_loaded, 1)

    def test_summary(self):
        self.stage()
        self.loader.run()
        rows = self.db.summary()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "NASDAQ_20250123.txt")
        self.assertEqual(rows[0][2], 4092)

    # ------------------------------------------------------------------
    def test_end_to_end_convert_then_load(self):
        """Convert both 'foreign' samples, then load everything in the loading folder."""
        converter = FileConverter(self.settings)
        converter.convert(REORDERED)
        converter.convert(BROKER, ConversionSpec(date_format="auto"))
        self.assertEqual(len(self.loader.pending_files()), 2)
        results = self.loader.run()
        self.assertEqual([r.status for r in results], ["loaded", "loaded"])
        self.assertEqual(self.db.count_rows(), 2 * 4092)
        # data from the converted files equals the data of the original file
        self.stage()
        self.loader.run()
        orig = [r[1:8] for r in self.rows("NASDAQ_20250123.txt")]
        conv1 = [r[1:8] for r in self.rows("NASDAQ_20250123_reordered.txt")]
        conv2 = [r[1:8] for r in self.rows("NASDAQ_20250123_broker.txt")]
        self.assertEqual(orig, conv1)
        self.assertEqual(orig, conv2)




class SqlRenderingTests(unittest.TestCase):
    def test_statements_use_quoted_identifiers(self):
        from stock_dataload.config import Settings
        db = StageDatabase(Settings())
        self.assertEqual(db.table, '"Stock_Stage"')
        self.assertTrue(db._insert_sql.startswith('INSERT INTO "Stock_Stage" ("Market", "Ticker", "TradeDate", "Open"'))
        self.assertEqual(db._insert_sql.count("?"), len(STAGE_COLUMNS))
        self.assertEqual(db.q("SourceFile"), '"SourceFile"')


if __name__ == "__main__":
    unittest.main()
