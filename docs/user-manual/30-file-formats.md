# File formats

A **format** describes how a source file is laid out so the converter can
turn it into the default format. Formats are stored in `formats.json` and
edited on the *File formats* page.

## Fields of a format

| Field | Meaning |
|-------|---------|
| Name | how the format is referred to (`--format NAME` on the command line) |
| File pattern | files in the conversion folder matching this glob (`NASDAQ_*.xlsx`) get the format suggested automatically |
| File type | `auto`, `csv`, `text`, `excel`, `json`, `xml` |
| Delimiter, header, rows to skip | for csv / text: separator (auto-detected if empty), whether the first row is a header, title rows to skip |
| Date format | `strptime` pattern of the source dates, e.g. `%d/%m/%Y` (auto-detected if empty) |
| Encoding | e.g. `utf-8-sig`, `cp1252` |
| Sheet | Excel: sheet name or number |
| Record path / record tag | JSON: dotted path to the list of records; XML: the element that is one record |
| Column mapping | how the seven default columns are found in the source |

## Column mapping

Three ways, tried in this order:

1. an explicit **map** - each default column gets a source field name, a
   `#position` or a `=constant` (`ticker==YHOO` for files without a ticker column);
2. an explicit **column sequence** - the source columns in order, `skip` for
   columns to ignore (`date,ticker,vol,close,open,high,low`);
3. **automatic** - header names / JSON keys / XML tags are matched against an
   alias table (`Symbol` → ticker, `Volume` → vol, `Trade Date` → date …).

All seven default columns must be found; unknown columns are ignored.

## Testing a format

*Run preview* on the format page converts the first rows of a file from the
conversion folder with the format as it is on screen - fix the mapping until
the preview looks right, then save. *Clone* copies a format as a starting
point; built-in example formats cannot be deleted.

## From the command line

```bash
python -m stock_dataload convert samples/NASDAQ_20250123.xlsx --rename
python -m stock_dataload convert broker.csv --columns date,ticker,vol,close,open,high,low --date-format %d/%m/%Y
python -m stock_dataload inspect broker.csv          # dry run: type, mapping, first converted rows
python -m stock_dataload convert-folder              # everything in the conversion folder
```
