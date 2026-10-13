import unittest

from stock_dataload.converter import ConversionError, ConversionSpec, FileConverter, build_mapping, detect_delimiter

from .helpers import BROKER, DEFAULT_HEADER, ORIGINAL, REORDERED, TempProjectTestCase

ORIGINAL_TEXT = ORIGINAL.read_text(encoding="utf-8")


class DelimiterAndMappingTests(unittest.TestCase):
    def test_detect_delimiter(self):
        self.assertEqual(detect_delimiter(["a,b,c\n", "1,2,3\n"]), ",")
        self.assertEqual(detect_delimiter(["a\tb\tc\n", "1\t2\t3\n"]), "\t")
        self.assertEqual(detect_delimiter(["a;b;c\n", "1;2;3\n"]), ";")
        self.assertEqual(detect_delimiter(["a|b|c\n", "1|2|3\n"]), "|")
        # semicolon consistent on every line, comma only on some -> semicolon wins
        self.assertEqual(detect_delimiter(["a;b;c\n", "1,5;2;3\n"]), ";")
        self.assertEqual(detect_delimiter([]), ",")

    def test_build_mapping_from_reordered_header(self):
        m = build_mapping(["<date>", "<ticker>", "<vol>", "<close>", "<open>", "<high>", "<low>"])
        self.assertEqual(m, {"date": 0, "ticker": 1, "vol": 2, "close": 3, "open": 4, "high": 5, "low": 6})

    def test_build_mapping_ignores_extra_and_skip(self):
        m = build_mapping(["Symbol", "Date", "Open", "High", "Low", "Close", "Volume", "Adj Close", "skip"])
        self.assertEqual(m["ticker"], 0)
        self.assertEqual(m["vol"], 6)
        self.assertEqual(len(m), 7)

    def test_build_mapping_missing_column(self):
        with self.assertRaises(ConversionError) as ctx:
            build_mapping(["Date", "Open", "High", "Low", "Close", "Volume"])
        self.assertIn("ticker", str(ctx.exception))

    def test_build_mapping_duplicate_column(self):
        with self.assertRaises(ConversionError):
            build_mapping(["ticker", "symbol", "date", "open", "high", "low", "close", "vol"])


class ConverterTests(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.converter = FileConverter(self.settings)

    def test_reordered_sample_converts_back_to_original_exactly(self):
        """The key test: the re-sequenced NASDAQ file converts to a file identical to the original."""
        res = self.converter.convert(REORDERED)
        self.assertTrue(res.moved_to_loading)
        self.assertEqual(res.output_file.parent, self.settings.loading_dir)
        self.assertEqual(res.output_file.name, "NASDAQ_20250123_reordered.txt")
        self.assertEqual(res.rows_written, 4092)
        self.assertEqual(res.output_file.read_text(encoding="utf-8"), ORIGINAL_TEXT)
        # source untouched, nothing left in work dir
        self.assertTrue(REORDERED.exists())
        self.assertEqual(list(self.settings.work_dir.iterdir()), [])

    def test_broker_sample_semicolon_names_isodate_extra_column(self):
        res = self.converter.convert(BROKER, ConversionSpec(date_format="auto"))
        self.assertEqual(res.delimiter, ";")
        self.assertEqual(res.output_file.read_text(encoding="utf-8"), ORIGINAL_TEXT)

    def test_explicit_date_format(self):
        res = self.converter.convert(BROKER, ConversionSpec(date_format="%Y-%m-%d"))
        self.assertEqual(res.output_file.read_text(encoding="utf-8"), ORIGINAL_TEXT)

    def test_wrong_date_format_fails(self):
        with self.assertRaises(ConversionError) as ctx:
            self.converter.convert(BROKER, ConversionSpec(date_format="%d/%m/%Y"))
        self.assertIn("does not match", str(ctx.exception))
        self.assertEqual(list(self.settings.loading_dir.iterdir()), [])
        self.assertEqual(list(self.settings.work_dir.iterdir()), [])

    def test_explicit_columns_override_header(self):
        src = self.write("in/x.csv", "c1,c2,c3,c4,c5,c6,c7\n20250123,AAA,100,1.5,1,2,0.5\n")
        spec = ConversionSpec(columns=["date", "ticker", "vol", "close", "open", "high", "low"])
        res = self.converter.convert(src, spec)
        self.assertEqual(res.output_file.read_text(), DEFAULT_HEADER + "AAA,20250123,1,2,0.5,1.5,100\n")

    def test_headerless_file(self):
        src = self.write("in/nohead.txt", "AAA\t20250123\t1\t2\t0.5\t1.5\t100\nBBB\t20250123\t3\t4\t2.5\t3.5\t200\n")
        spec = ConversionSpec(columns=["ticker", "date", "open", "high", "low", "close", "vol"], has_header=False)
        res = self.converter.convert(src, spec)
        self.assertEqual(res.delimiter, "\t")
        self.assertEqual(res.rows_written, 2)
        self.assertEqual(res.output_file.read_text(),
                         DEFAULT_HEADER + "AAA,20250123,1,2,0.5,1.5,100\nBBB,20250123,3,4,2.5,3.5,200\n")

    def test_headerless_without_columns_is_an_error(self):
        src = self.write("in/nohead.txt", "AAA,20250123,1,2,0.5,1.5,100\n")
        with self.assertRaises(ConversionError):
            self.converter.convert(src, ConversionSpec(has_header=False))

    def test_named_format_yahoo_layout_with_constant_ticker(self):
        """Yahoo files have no ticker column: the format supplies it as a constant (=YHOO)."""
        src = self.write("in/YHOO.csv", "Date,Open,High,Low,Close,Volume,Adj Close\n2014-10-24,44.1,44.5,43.9,44.2,1000,44.0\n")
        # without a ticker source the positional sequence cannot work
        with self.assertRaises(ConversionError):
            self.converter.convert(src, ConversionSpec(columns=["date", "open", "high", "low", "close", "volume", "skip"]))
        res = self.converter.convert(src, "yahoo")
        self.assertEqual(res.output_file.read_text(), DEFAULT_HEADER + "YHOO,20141024,44.1,44.5,43.9,44.2,1000\n")
        with self.assertRaises(ConversionError):
            self.converter.convert(src, "nope")

    def test_quoted_fields_and_blank_lines(self):
        src = self.write("in/q.csv", '"Symbol","Date","Open","High","Low","Close","Volume"\n'
                                     '"AAA","20250123","1","2","0.5","1.5","100"\n\n')
        res = self.converter.convert(src)
        self.assertEqual(res.output_file.read_text(), DEFAULT_HEADER + "AAA,20250123,1,2,0.5,1.5,100\n")

    def test_unknown_header_gives_helpful_error(self):
        src = self.write("in/weird.csv", "a,b,c,d,e,f,g\n1,2,3,4,5,6,7\n")
        with self.assertRaises(ConversionError) as ctx:
            self.converter.convert(src)
        self.assertIn("--columns", str(ctx.exception))

    def test_short_row_fails_unless_skip(self):
        text = "ticker,date,open,high,low,close,vol\nAAA,20250123,1,2,0.5,1.5,100\nBBB,20250123\n"
        src = self.write("in/short.csv", text)
        with self.assertRaises(ConversionError) as ctx:
            self.converter.convert(src)
        self.assertIn("line 3", str(ctx.exception))
        res = self.converter.convert(src, ConversionSpec(skip_bad_rows=True))
        self.assertEqual(res.rows_written, 1)
        self.assertEqual(res.rows_skipped, 1)

    def test_existing_target_requires_overwrite(self):
        self.converter.convert(REORDERED)
        with self.assertRaises(ConversionError):
            self.converter.convert(REORDERED)
        res = self.converter.convert(REORDERED, overwrite=True)
        self.assertEqual(res.rows_written, 4092)

    def test_no_move_keeps_file_in_work_dir(self):
        res = self.converter.convert(REORDERED, move_to_loading=False, output_name="out.txt")
        self.assertFalse(res.moved_to_loading)
        self.assertEqual(res.output_file, self.settings.work_dir / "out.txt")
        self.assertEqual(list(self.settings.loading_dir.iterdir()), [])

    def test_inspect(self):
        delimiter, header, mapping = self.converter.inspect(BROKER)
        self.assertEqual(delimiter, ";")
        self.assertEqual(header[0], "Symbol")
        self.assertEqual(mapping["ticker"], 0)
        self.assertEqual(mapping["date"], 1)

    def test_missing_source(self):
        with self.assertRaises(ConversionError):
            self.converter.convert(self.root / "nope.csv")


if __name__ == "__main__":
    unittest.main()
