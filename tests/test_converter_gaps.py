"""Targeted tests for converter branches not covered by the main suite."""

import unittest

from stock_dataload.converter import (ConversionError, ConversionSpec, FileConverter,
                                      _convert_date, resolve_mapping)
from stock_dataload.file_formats import FileFormat

from .helpers import ORIGINAL, REORDERED, TempProjectTestCase


class ResolveMappingTests(unittest.TestCase):
    def test_column_map_must_define_every_column(self):
        fmt = FileFormat(name="x", column_map={"ticker": "Symbol"})
        with self.assertRaisesRegex(ConversionError, "does not define 'date'"):
            resolve_mapping(fmt, ["Symbol"])

    def test_column_map_field_without_header_needs_positions(self):
        fmt = FileFormat(name="x", column_map={c: c.title() for c in
                                               ("ticker", "date", "open", "high", "low", "close", "vol")})
        with self.assertRaisesRegex(ConversionError, "no header - use #positions"):
            resolve_mapping(fmt, None)

    def test_column_map_unknown_field_reported(self):
        fmt = FileFormat(name="x", column_map={"ticker": "Nope", "date": "#1", "open": "#2",
                                               "high": "#3", "low": "#4", "close": "#5", "vol": "#6"})
        with self.assertRaisesRegex(ConversionError, "source field 'Nope'"):
            resolve_mapping(fmt, ["a", "b", "c", "d", "e", "f"])


class ConvertDateTests(unittest.TestCase):
    def test_unparseable_date_raises(self):
        with self.assertRaisesRegex(ConversionError, "cannot parse date"):
            _convert_date("not-a-date", None, 3)

    def test_explicit_format_mismatch_raises(self):
        with self.assertRaisesRegex(ConversionError, "does not match format"):
            _convert_date("2025-01-23", "%d/%m/%Y", 4)

    def test_explicit_format_ok(self):
        self.assertEqual(_convert_date("23/01/2025", "%d/%m/%Y", 1), "20250123")


class ConverterBranchTests(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.converter = FileConverter(self.settings)

    def test_resolve_named_format_returns_columns(self):
        self.assertEqual(self.converter.resolve_named_format("pipe_noheader")[0], "date")

    def test_resolve_named_format_without_columns_raises(self):
        with self.assertRaisesRegex(ConversionError, "no column sequence"):
            self.converter.resolve_named_format("yahoo")

    def test_preview_reports_read_error(self):
        missing = self.root / "nope.csv"
        out = self.converter.preview(missing)
        self.assertFalse(out["ok"])
        self.assertTrue(out["errors"])

    def test_preview_skips_blank_rows_and_collects_row_errors(self):
        src = self.write("conversion/x.csv",
                         "Symbol,Date,Open,High,Low,Close,Volume\n"
                         "\n"
                         "AAA,garbage,1,2,0.5,1.5,100\n")
        out = self.converter.preview(src)
        self.assertEqual(out["total_rows"], 1)
        self.assertTrue(any("cannot parse date" in e for e in out["errors"]))

    def test_map_row_rejects_invalid_converted_row(self):
        src = self.write("conversion/x.csv",
                         "Symbol,Date,Open,High,Low,Close,Volume\n"
                         "AAA,20250123,bad,2,0.5,1.5,100\n")
        with self.assertRaisesRegex(ConversionError, "not numeric"):
            self.converter.convert(src)

    def test_unique_name_numbers_additional_conflicts(self):
        self.write("loading/X.txt", "taken\n")
        self.write("loading/X_csv.txt", "taken\n")
        name = self.converter._unique_name(self.settings.loading_dir, self.root / "X.csv", "X.txt")
        self.assertEqual(name, "X_csv_2.txt")

    def test_invalid_on_conflict_rejected(self):
        with self.assertRaisesRegex(ConversionError, "on_conflict"):
            self.converter.convert(REORDERED, on_conflict="bogus")

    def test_source_without_data_rows_rejected(self):
        src = self.write("conversion/empty.csv", "Symbol,Date,Open,High,Low,Close,Volume\n")
        with self.assertRaisesRegex(ConversionError, "no data rows"):
            self.converter.convert(src)

    def test_archive_source_timestamps_name_clash(self):
        src1 = self.write("conversion/same.csv", "a\n")
        (self.settings.converted_dir / "same.csv").write_text("already here\n")
        dest = self.converter._archive_source(src1)
        self.assertTrue(dest.name.startswith("same."))
        self.assertNotEqual(dest.name, "same.csv")
        self.assertTrue(dest.exists())

    def test_convert_many(self):
        results = self.converter.convert_many([REORDERED, ORIGINAL],
                                              spec=None, on_conflict="rename")
        self.assertEqual(len(results), 2)
        self.assertEqual({r.rows_written for r in results}, {4092})

    def test_pending_sources_with_missing_folder(self):
        import shutil
        shutil.rmtree(self.settings.conversion_dir)
        self.assertEqual(self.converter.pending_sources(), [])

    def test_convert_folder_clears_stale_error_note(self):
        import shutil
        shutil.copy(REORDERED, self.settings.conversion_dir / REORDERED.name)
        note = self.settings.conversion_dir / (REORDERED.name + ".error.txt")
        note.write_text("old failure\n")
        outcomes = self.converter.convert_folder()
        self.assertEqual(len(outcomes), 1)
        self.assertIsNotNone(outcomes[0][1])
        self.assertFalse(note.exists())

    def test_convert_with_conversionspec_alias(self):
        res = self.converter.convert(REORDERED, ConversionSpec(name="", date_format="auto"))
        self.assertEqual(res.rows_written, 4092)


if __name__ == "__main__":
    unittest.main()
