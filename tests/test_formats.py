import unittest

from stock_dataload import formats


class NormaliseAndResolveTests(unittest.TestCase):
    def test_normalise_strips_brackets_case_and_spaces(self):
        self.assertEqual(formats.normalise_name("<Ticker>"), "ticker")
        self.assertEqual(formats.normalise_name("  TICKER "), "ticker")
        self.assertEqual(formats.normalise_name("Trade Date"), "tradedate")
        self.assertEqual(formats.normalise_name('"Adj Close"'), "adjclose")

    def test_resolve_known_aliases(self):
        self.assertEqual(formats.resolve_column("Symbol"), "ticker")
        self.assertEqual(formats.resolve_column("<vol>"), "vol")
        self.assertEqual(formats.resolve_column("Volume"), "vol")
        self.assertEqual(formats.resolve_column("Trade Date"), "date")
        self.assertEqual(formats.resolve_column("Close"), "close")
        self.assertEqual(formats.resolve_column("Last"), "close")

    def test_resolve_unknown_and_skip(self):
        self.assertIsNone(formats.resolve_column("Adj Close"))
        self.assertIsNone(formats.resolve_column("Currency"))
        self.assertEqual(formats.resolve_column("skip"), formats.SKIP_COLUMN)
        self.assertEqual(formats.resolve_column("_"), formats.SKIP_COLUMN)

    def test_default_header_detection(self):
        self.assertTrue(formats.is_default_header(formats.DEFAULT_HEADER_FIELDS))
        self.assertTrue(formats.is_default_header(["ticker", "date", "open", "high", "low", "close", "vol"]))
        self.assertTrue(formats.is_default_header(["<TICKER>", "<Date>", "<open>", "<high>", "<low>", "<close>", "<vol>"]))
        self.assertFalse(formats.is_default_header(["<date>", "<ticker>", "<open>", "<high>", "<low>", "<close>", "<vol>"]))
        self.assertFalse(formats.is_default_header(["<ticker>", "<date>", "<open>", "<high>", "<low>", "<close>"]))
        # alias names are NOT the default header - the loader must be strict
        self.assertFalse(formats.is_default_header(["symbol", "date", "open", "high", "low", "close", "volume"]))

    def test_default_header_line(self):
        self.assertEqual(formats.DEFAULT_HEADER_LINE, "<ticker>,<date>,<open>,<high>,<low>,<close>,<vol>")


class RowValidationTests(unittest.TestCase):
    def test_valid_row(self):
        self.assertIsNone(formats.validate_default_row(["AACG", "20250123", "0.9", "0.9229", "0.89", "0.9128", "2800"]))
        self.assertIsNone(formats.validate_default_row(["X", "20250123", "", "", "", "1.5", ""]))

    def test_wrong_column_count(self):
        self.assertIn("expected 7 columns", formats.validate_default_row(["A", "20250123", "1"]))

    def test_bad_values(self):
        self.assertIn("ticker is empty", formats.validate_default_row(["", "20250123", "1", "1", "1", "1", "1"]))
        self.assertIn("YYYYMMDD", formats.validate_default_row(["A", "2025-01-23", "1", "1", "1", "1", "1"]))
        self.assertIn("open", formats.validate_default_row(["A", "20250123", "abc", "1", "1", "1", "1"]))
        self.assertIn("vol", formats.validate_default_row(["A", "20250123", "1", "1", "1", "1", "1,000"]))


if __name__ == "__main__":
    unittest.main()
