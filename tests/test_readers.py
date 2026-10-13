"""
Multi-format conversion: csv, delimited text, Excel, JSON and XML sources
must all convert into the default TEXT file with the default column sequence.

Every sample in samples/ was generated from NASDAQ_20250123.txt
(samples/make_samples.py), so the converted output must be byte-identical
to that original file.
"""

import unittest
from datetime import datetime
from pathlib import Path

from stock_dataload.converter import ConversionError, FileConverter
from stock_dataload.file_formats import FileFormat
from stock_dataload.readers import ReadError, detect_file_type, read_source, to_text

from .helpers import DEFAULT_HEADER, ORIGINAL, SAMPLES, TempProjectTestCase

ORIGINAL_TEXT = ORIGINAL.read_text(encoding="utf-8")

SAMPLE_FILES = {
    "NASDAQ_20250123_reordered.txt": "csv",
    "NASDAQ_20250123_broker.csv": "csv",
    "NASDAQ_20250123.csv": "csv",
    "NASDAQ_20250123.tsv": "text",
    "NASDAQ_20250123_noheader.txt": "text",
    "NASDAQ_20250123.xlsx": "excel",
    "NASDAQ_20250123.json": "json",
    "NASDAQ_20250123.xml": "xml",
}


class SampleFilesExistTests(unittest.TestCase):
    def test_all_sample_formats_are_present(self):
        for name in SAMPLE_FILES:
            self.assertTrue((SAMPLES / name).is_file(), f"missing sample {name} - run samples/make_samples.py")


class EveryFormatConvertsToDefaultTextFileTests(TempProjectTestCase):
    """One test per file type: convert -> byte-identical to the original default-format file."""

    def setUp(self):
        super().setUp()
        self.converter = FileConverter(self.settings)

    def _check(self, name: str, spec=None):
        res = self.converter.convert(SAMPLES / name, spec)
        self.assertEqual(res.rows_written, 4092)
        self.assertEqual(res.output_file.suffix, ".txt")                     # always a text file
        self.assertEqual(res.output_file.parent, self.settings.loading_dir)  # always in the loading folder
        text = res.output_file.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(DEFAULT_HEADER))                     # always the default sequence
        self.assertEqual(text, ORIGINAL_TEXT)
        return res

    def test_csv_different_sequence_and_names(self):
        res = self._check("NASDAQ_20250123.csv")
        self.assertEqual(res.file_type, "csv")
        self.assertEqual(res.source_header, ["Symbol", "Open", "High", "Low", "Close", "Volume", "Date"])

    def test_csv_semicolon_broker_export_via_saved_format(self):
        res = self._check("NASDAQ_20250123_broker.csv")        # matched by pattern *_broker.csv
        self.assertEqual(res.format_name, "broker_csv")
        self.assertEqual(res.delimiter, ";")

    def test_text_tab_delimited(self):
        res = self._check("NASDAQ_20250123.tsv")
        self.assertEqual(res.file_type, "text")
        self.assertEqual(res.delimiter, "\t")

    def test_text_pipe_delimited_without_header_via_saved_format(self):
        res = self._check("NASDAQ_20250123_noheader.txt")      # matched by pattern *_noheader.txt
        self.assertEqual(res.format_name, "pipe_noheader")
        self.assertIsNone(res.source_header)

    def test_text_reordered_columns(self):
        self._check("NASDAQ_20250123_reordered.txt")

    def test_excel_workbook_with_title_row_and_numeric_cells(self):
        res = self._check("NASDAQ_20250123.xlsx")              # matched by pattern *.xlsx (sheet Prices, skip 1)
        self.assertEqual(res.file_type, "excel")
        self.assertEqual(res.format_name, "excel_prices")

    def test_excel_explicit_format_by_sheet_index(self):
        fmt = FileFormat(name="x", file_type="excel", sheet="1", skip_rows=1)
        self._check("NASDAQ_20250123.xlsx", fmt)

    def test_json_nested_records(self):
        res = self._check("NASDAQ_20250123.json")
        self.assertEqual(res.file_type, "json")
        self.assertIn("prices.open", res.source_header)

    def test_json_with_explicit_record_path(self):
        fmt = FileFormat(name="j", file_type="json", record_path="data")
        self._check("NASDAQ_20250123.json", fmt)

    def test_xml_attributes_and_child_elements(self):
        res = self._check("NASDAQ_20250123.xml")
        self.assertEqual(res.file_type, "xml")
        self.assertEqual(res.source_header, ["symbol", "date", "open", "high", "low", "close", "volume"])

    def test_xml_with_explicit_record_tag(self):
        fmt = FileFormat(name="x", file_type="xml", record_tag="quote")
        self._check("NASDAQ_20250123.xml", fmt)

    def test_all_formats_in_one_folder_run(self):
        """Drop every sample into the conversion folder -> convert_folder -> 8 default text files."""
        import shutil
        for name in SAMPLE_FILES:
            shutil.copy(SAMPLES / name, self.settings.conversion_dir / name)
        outcomes = self.converter.convert_folder()
        self.assertEqual(len(outcomes), 8)
        self.assertTrue(all(res is not None for _, res, _ in outcomes), [e for _, r, e in outcomes if r is None])
        outputs = sorted(p.name for p in self.settings.loading_dir.iterdir())
        self.assertEqual(len(outputs), 8)
        self.assertTrue(all(n.endswith(".txt") for n in outputs))
        for p in self.settings.loading_dir.iterdir():
            self.assertEqual(p.read_text(encoding="utf-8"), ORIGINAL_TEXT, p.name)
        # sources moved out of the conversion folder, nothing left behind
        self.assertEqual(self.converter.pending_sources(), [])
        self.assertEqual(len(list(self.settings.converted_dir.iterdir())), 8)
        # name clashes were resolved by renaming, not by overwriting
        self.assertIn("NASDAQ_20250123_json.txt", outputs)
        self.assertIn("NASDAQ_20250123_xlsx.txt", outputs)


class ReaderEdgeCaseTests(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.converter = FileConverter(self.settings)

    def test_detect_file_type(self):
        self.assertEqual(detect_file_type(SAMPLES / "NASDAQ_20250123.csv"), "csv")
        self.assertEqual(detect_file_type(SAMPLES / "NASDAQ_20250123.tsv"), "text")
        self.assertEqual(detect_file_type(SAMPLES / "NASDAQ_20250123.xlsx"), "excel")
        self.assertEqual(detect_file_type(SAMPLES / "NASDAQ_20250123.json"), "json")
        self.assertEqual(detect_file_type(SAMPLES / "NASDAQ_20250123.xml"), "xml")
        # the "<ticker>,<date>,..." header must NOT be mistaken for XML
        self.assertEqual(detect_file_type(ORIGINAL), "text")
        # content sniffing when the extension says nothing
        j = self.write("in/data.dat", '[{"ticker":"A"}]')
        self.assertEqual(detect_file_type(j), "json")
        x = self.write("in/data.txt", '<?xml version="1.0"?><r><q symbol="A"/></r>')
        self.assertEqual(detect_file_type(x), "xml")
        self.assertEqual(detect_file_type(x, "text"), "text")      # declared type wins
        with self.assertRaises(ReadError):
            detect_file_type(self.write("in/old.xls", "x"))

    def test_to_text(self):
        self.assertEqual(to_text(None), "")
        self.assertEqual(to_text(2800), "2800")
        self.assertEqual(to_text(20250123.0), "20250123")
        self.assertEqual(to_text(0.9229), "0.9229")
        self.assertEqual(to_text(True), "true")
        self.assertEqual(to_text(datetime(2025, 1, 23)), "20250123")
        self.assertEqual(to_text(" AACG "), "AACG")

    def test_json_list_of_lists_with_header(self):
        src = self.write("in/rows.json", '[["ticker","date","open","high","low","close","vol"],'
                                          '["AAA","20250123",1,2,0.5,1.5,100]]')
        res = self.converter.convert(src)
        self.assertEqual(res.output_file.read_text(), DEFAULT_HEADER + "AAA,20250123,1,2,0.5,1.5,100\n")

    def test_json_lines(self):
        src = self.write("in/rows.jsonl", '{"symbol":"AAA","date":"2025-01-23","open":1,"high":2,"low":0.5,"close":1.5,"volume":100}\n'
                                           '{"symbol":"BBB","date":"2025-01-23","open":1,"high":2,"low":0.5,"close":1.5,"volume":200}\n')
        res = self.converter.convert(src)
        self.assertEqual(res.rows_written, 2)
        self.assertIn("BBB,20250123,1,2,0.5,1.5,200", res.output_file.read_text())

    def test_json_bad_record_path_and_invalid_json(self):
        src = self.write("in/x.json", '{"data": [{"symbol":"A"}]}')
        with self.assertRaises(ConversionError) as ctx:
            self.converter.convert(src, FileFormat(name="j", record_path="nope"))
        self.assertIn("record_path", str(ctx.exception))
        bad = self.write("in/bad.json", '{"data": [')
        with self.assertRaises(ConversionError) as ctx:
            self.converter.convert(bad)
        self.assertIn("not valid JSON", str(ctx.exception))

    def test_xml_field_name_style(self):
        src = self.write("in/q.xml", """<?xml version="1.0"?>
<rows><row><field name="Symbol">AAA</field><field name="Date">20250123</field><field name="Open">1</field>
<field name="High">2</field><field name="Low">0.5</field><field name="Close">1.5</field><field name="Volume">100</field></row></rows>""")
        res = self.converter.convert(src)
        self.assertEqual(res.output_file.read_text(), DEFAULT_HEADER + "AAA,20250123,1,2,0.5,1.5,100\n")

    def test_xml_not_well_formed(self):
        src = self.write("in/bad.xml", "<rows><row>")
        with self.assertRaises(ConversionError) as ctx:
            self.converter.convert(src)
        self.assertIn("well-formed", str(ctx.exception))

    def test_excel_unknown_sheet(self):
        with self.assertRaises(ConversionError) as ctx:
            self.converter.convert(SAMPLES / "NASDAQ_20250123.xlsx", FileFormat(name="x", file_type="excel", sheet="Nope"))
        self.assertIn("not found", str(ctx.exception))

    def test_excel_date_cells_become_yyyymmdd(self):
        import openpyxl
        path = self.root / "in" / "dates.xlsx"
        path.parent.mkdir(parents=True, exist_ok=True)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Ticker", "Date", "Open", "High", "Low", "Close", "Volume"])
        ws.append(["AAA", datetime(2025, 1, 23), 1, 2, 0.5, 1.5, 100])
        wb.save(path)
        res = self.converter.convert(path)
        self.assertEqual(res.output_file.read_text(), DEFAULT_HEADER + "AAA,20250123,1,2,0.5,1.5,100\n")

    def test_delimited_skip_rows(self):
        src = self.write("in/t.csv", "report title\ngenerated 2025\nticker,date,open,high,low,close,vol\nAAA,20250123,1,2,0.5,1.5,100\n")
        res = self.converter.convert(src, FileFormat(name="t", skip_rows=2))
        self.assertEqual(res.rows_written, 1)

    def test_read_source_generic_shape(self):
        table = read_source(SAMPLES / "NASDAQ_20250123.xml", FileFormat(name="x"))
        rows = list(table.rows)
        self.assertEqual(table.file_type, "xml")
        self.assertEqual(len(rows), 4092)
        self.assertEqual(rows[0][0], "AACG")

    def test_constant_column(self):
        src = self.write("in/one.csv", "Date,Open,High,Low,Close,Volume\n2025-01-23,1,2,0.5,1.5,100\n")
        fmt = FileFormat(name="c", column_map={"ticker": "=MSFT", "date": "Date", "open": "Open", "high": "High",
                                                "low": "Low", "close": "Close", "vol": "#6"})
        res = self.converter.convert(src, fmt)
        self.assertEqual(res.output_file.read_text(), DEFAULT_HEADER + "MSFT,20250123,1,2,0.5,1.5,100\n")

    def test_conflict_policies(self):
        self.converter.convert(SAMPLES / "NASDAQ_20250123.csv")
        with self.assertRaises(ConversionError):
            self.converter.convert(SAMPLES / "NASDAQ_20250123.json")                       # default: error
        res = self.converter.convert(SAMPLES / "NASDAQ_20250123.json", on_conflict="rename")
        self.assertEqual(res.output_file.name, "NASDAQ_20250123_json.txt")
        res = self.converter.convert(SAMPLES / "NASDAQ_20250123.xml", on_conflict="overwrite")
        self.assertEqual(res.output_file.name, "NASDAQ_20250123.txt")
        self.assertEqual(len(list(self.settings.loading_dir.iterdir())), 2)


if __name__ == "__main__":
    unittest.main()
