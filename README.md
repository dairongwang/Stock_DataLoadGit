# stock_dataload – Python port of the `Stock_DataLoad` SSIS package

Loads daily stock trading data files (e.g. `NASDAQ_20250123.txt`) from a
**loading folder** into a SQLite **staging table**, ships a **conversion
module** that turns files delivered in other layouts / file types
(**CSV, delimited text, Excel, JSON, XML**) into the default layout first,
and a **web dashboard** where business users configure custom file
formats, the conversion folder and the loading folder.

```
  conversion folder              loading folder                     database
  csv / text / xlsx    convert   <ticker>,<date>,<open>,    load
  json / xml, any     ────────►  <high>,<low>,<close>,<vol> ────────►  Stock_Stage
  column sequence                always a .txt text file             (SQLite)
```


## Requirements

* Python 3.10+. Converting csv/text/json/xml and loading need the **standard library only**.
* `pip install flask` for the dashboard, `pip install openpyxl` to read Excel files
  (`pip install -r requirements.txt`).

## Folder layout

```
stock_dataload_py/
├── config.ini                 settings: folders, database, loader options (editable in the dashboard)
├── formats.json               custom file formats (editable in the dashboard)
├── stock_dataload/            the package
│   ├── formats.py             default format definition + header alias table
│   ├── file_formats.py        FileFormat model + FormatStore (formats.json)
│   ├── readers.py             csv / text / excel / json / xml readers
│   ├── converter.py           any source -> default format text file -> loading folder
│   ├── loader.py              loading folder -> staging table
│   ├── db.py                  staging table (SQLite)
│   ├── config.py              config.ini handling (load + save)
│   ├── cli.py                 python -m stock_dataload ...
│   └── dashboard/             Flask web dashboard (app.py, templates/, static/)
├── data/
│   ├── conversion/            <-- THE CONVERSION FOLDER: source files in any supported format
│   ├── converted/             source files after a successful conversion
│   ├── loading/               <-- THE LOADING FOLDER: default-format .txt files waiting to be loaded
│   ├── archive/               files that were loaded successfully
│   ├── error/                 files that failed + <file>.error.txt / <file>.rejects.txt reports
│   └── work/                  scratch space for the converter
├── samples/                   NASDAQ_20250123.txt + the same data as csv / tsv / txt / xlsx / json / xml
│   └── make_samples.py        regenerates the test files from the original
├── sql/                       DDL script for reference
├── tests/                     unittest suite (python -m unittest discover -s tests -t .)
└── logs/                      stock_dataload.log (rotating)
```

## Dashboard

```bash
pip install -r requirements.txt
python -m stock_dataload dashboard            # http://127.0.0.1:8050  (host/port in config.ini [dashboard])
```

| Page | What business users do there |
|------|------------------------------|
| **Overview** | see the pipeline *conversion folder → loading folder → staging table*; upload files; pick a format per file (a matching custom format is pre-selected) and **Convert** / **Convert all**; **Preview** a conversion before running it; **Load** / **Load all**; recent loads, error reports, archive. Files in the loading folder are checked and marked *default format* / *not loadable*. |
| **File formats** | create / edit / clone / delete custom formats: file type (auto, csv, text, excel, json, xml), file-name pattern for automatic selection, delimiter, header row, rows to skip, date format, encoding, Excel sheet, JSON record path, XML record tag, and the column mapping (auto by names, column sequence, or map each of the 7 target columns to a source field / `#position` / `=constant`). **Run preview** tests the format against a file in the conversion folder before saving. |
| **Settings** | configure the **conversion folder**, the **loading folder**, converted / archive / error / work / log folders, the SQLite file and staging table, the formats file, and loader options. Saved to `config.ini`; folders are created on the fly. |

The dashboard is a thin front end: it calls the same `FileConverter` /
`StageLoader` classes as the CLI, so everything it does can also be
scripted or scheduled.  A JSON status endpoint is available at `/api/status`.

![overview](docs/dashboard_overview.png)

Preview before converting, the format editor with "Run preview", and the settings page:

![preview](docs/dashboard_preview.png)
![format editor](docs/dashboard_format_editor.png)
![settings](docs/dashboard_settings.png)

## Supported source file types

| Type | Extensions | Notes |
|------|------------|-------|
| CSV / delimited text | `.csv .txt .tsv .dat .prn .psv` | delimiter auto-detected (`, ; \| tab`), optional header, quoted fields, title rows (`skip_rows`) |
| Excel | `.xlsx .xlsm` | sheet by name or number, title rows, numeric and date cells (needs `openpyxl`; legacy `.xls` → save as `.xlsx`) |
| JSON | `.json .jsonl .ndjson` | list of objects, nested objects (`prices.open`), object with the list under a path (`record_path`, auto-detected), list of arrays, JSON lines |
| XML | `.xml` | one element per record (`record_tag`, auto-detected); fields from attributes and child elements, `<field name="...">` style |

Whatever the source, the **output is always the same**: a comma separated
`.txt` file with the default header `<ticker>,<date>,<open>,<high>,<low>,<close>,<vol>`
and dates as `YYYYMMDD` – the only thing the loader accepts.

### Test files

`samples/make_samples.py` derives these from `NASDAQ_20250123.txt`; converting
any of them yields a file **byte-identical** to the original (verified by the tests):

| File | Layout |
|------|--------|
| `NASDAQ_20250123_reordered.txt` | text, `<date>,<ticker>,<vol>,<close>,<open>,<high>,<low>` |
| `NASDAQ_20250123.csv` | CSV, `Symbol,Open,High,Low,Close,Volume,Date` |
| `NASDAQ_20250123_broker.csv` | `;` delimited, `Symbol;Trade Date;Volume;Close;Open;High;Low;Currency`, ISO dates |
| `NASDAQ_20250123.tsv` | tab delimited, `ticker close vol date open high low` |
| `NASDAQ_20250123_noheader.txt` | `\|` delimited, no header (format `pipe_noheader`) |
| `NASDAQ_20250123.xlsx` | Excel, sheet *Prices*, title row, numeric cells (format `excel_prices`) |
| `NASDAQ_20250123.json` | nested JSON: `{"data": [{"symbol", "date", "prices": {"open", ...}, "volume"}]}` |
| `NASDAQ_20250123.xml` | `<quote symbol=".." date=".."><open>..</open>...<volume>..</volume></quote>` |

## Quick start

```bash
cd stock_dataload_py

# 1. create the staging table (SQLite file data/stock_trade.db by default)
python -m stock_dataload init-db

# 2. drop a default-format file into the loading folder and load it
cp samples/NASDAQ_20250123.txt data/loading/
python -m stock_dataload load
#   NASDAQ_20250123.txt: loaded (4092 rows, market=NASDAQ, load_id=...)

# 3. a file in a different column sequence / file type?  convert it first - the
#    converted .txt is written to data/loading/ automatically, then load as usual
python -m stock_dataload convert samples/NASDAQ_20250123_reordered.txt
python -m stock_dataload convert samples/NASDAQ_20250123.xlsx samples/NASDAQ_20250123.json --rename
python -m stock_dataload load

#    or drop files into data/conversion/ and convert the whole folder
cp samples/NASDAQ_20250123.xml data/conversion/ && python -m stock_dataload convert-folder

#    or use the dashboard for all of the above
python -m stock_dataload dashboard

# 4. what is in the staging table?
python -m stock_dataload status
```

All commands accept `--config FILE`, `-v` (debug) and `-q` (quiet).

## The default file format

```
<ticker>,<date>,<open>,<high>,<low>,<close>,<vol>
AACG,20250123,0.9,0.9229,0.89,0.9128,2800
```

* comma delimited, one header row (angle brackets optional, case-insensitive)
* `date` as `YYYYMMDD`
* defined in one place: `stock_dataload/formats.py` (`DEFAULT_COLUMNS`)

The loader is strict: a file whose header is not exactly this sequence is
moved to `data/error/` with the message *"run the converter first"*.

## Loading (`load`)

```
python -m stock_dataload load                      # every *.txt / *.csv in data/loading/
python -m stock_dataload load path/to/file.txt     # specific file(s)
    --market NYSE        Market value for all rows (default: from file name, see config)
    --truncate           empty the staging table before loading
    --skip-bad-rows      skip invalid rows (written to data/error/<file>.rejects.txt)
    --keep               leave files where they are instead of moving them
```

Per file: header check → row validation → batched insert in **one
transaction** → move to `archive/` (success) or `error/` (failure, with a
`.error.txt` report and a full rollback).  Loading the same file name again
replaces its previous rows (`replace_existing_file_rows = true`), so re-runs
are idempotent.  Exit code is 1 if any file failed – handy for schedulers.

## Converting (`convert`, `convert-folder`, `inspect`, `formats`)

```
python -m stock_dataload convert SOURCE [SOURCE ...]
    --format NAME         use a saved format (formats.json / dashboard)
    --type excel          source type: auto (default) csv text excel json xml
    --columns date,ticker,vol,close,open,high,low   source column sequence ('skip' ignores a column)
    --map ticker=Symbol,date=#7,open=Open,...       map each target column to a field, #position or =constant
    --delimiter ';'       csv/text delimiter (default: auto-detect)
    --no-header           no header line (needs --columns or --map with #positions)
    --skip-rows 1         title rows above the header
    --date-format %d/%m/%Y   source date pattern (default: auto)
    --sheet Prices  --record-path data  --record-tag quote     excel / json / xml options
    --output-name X.txt   name of the converted file (default: <source stem>.txt)
    --overwrite | --rename   if the converted file already exists
    --move-source         move the source to data/converted/ afterwards
    --no-move             keep the result in data/work/ instead of data/loading/
    --skip-bad-rows       skip rows that cannot be converted
python -m stock_dataload convert-folder [--format NAME] [--on-conflict rename|overwrite|error] [--keep-source]
python -m stock_dataload inspect SOURCE [same options]   dry run: detected type, mapping, first converted rows
python -m stock_dataload formats                          list the saved formats
```

How the column mapping is chosen (the same rules apply in the dashboard):

1. an explicit **column map** (`--map` / "map each column") – source field
   name, `#n` position or `=VALUE` constant (e.g. `ticker==YHOO` for Yahoo
   files that carry no ticker column);
2. else an explicit **column sequence** (`--columns` / "column sequence");
3. else the header / JSON keys / XML tags are matched against the alias
   table in `formats.COLUMN_ALIASES` (`Symbol`→ticker, `Volume`→vol,
   `Trade Date`→date, `prices.open`→open, ...).  Unknown columns such as
   `Adj Close` or `Currency` are ignored; all seven default columns must be
   found or the conversion fails with a hint.

Files in the conversion folder are matched against the **file pattern** of
each saved format (`NASDAQ_*.xlsx` → `excel_prices`); otherwise auto-detection
is used.  The output is written to `data/work/` and only *moved* into
`data/loading/` once complete, so the loader never picks up a half-written
file.  If a file with the same name already exists the folder workflow
*renames* (`NASDAQ_20250123.txt` → `NASDAQ_20250123_json.txt`) rather than
silently overwriting.

## Using it from Python

```python
from stock_dataload import load_settings, FileConverter, ConversionSpec, StageLoader

settings = load_settings()                     # finds config.ini
FileConverter(settings).convert("inbox/NASDAQ_20250123_reordered.txt")
FileConverter(settings).convert("inbox/broker.csv", ConversionSpec(date_format="%Y-%m-%d"))
for result in StageLoader(settings).run():
    print(result)                              # NASDAQ_20250123_reordered.txt: loaded (4092 rows, ...)
```

## Configuration (`config.ini`)

Edit in the dashboard (*Settings*) or by hand.

| Section / key                       | Meaning                                                          |
|-------------------------------------|------------------------------------------------------------------|
| `[paths] conversion_dir`            | the conversion folder (default `data/conversion`)                |
| `loading_dir`                       | the loading folder (default `data/loading`)                      |
| `converted_dir`, `archive_dir`, `error_dir`, `work_dir`, `log_dir` | see folder layout                 |
| `[database] sqlite_path`            | SQLite database file (created on first use)                      |
| `stage_table`                       | target table (`Stock_Stage`)                                     |
| `batch_size`                        | rows per `executemany`                                           |
| `truncate_before_load`              | truncate-and-load style staging                                  |
| `replace_existing_file_rows`        | delete rows of a previously loaded file with the same name first |
| `[loader] file_patterns`            | which files in the loading folder are loaded (`*.txt`)           |
| `market_from_filename_regex`        | how `Market` is derived from the file name                       |
| `default_market`, `skip_bad_rows`, `encoding` |                                                        |
| `[formats] file`                    | JSON file holding the custom formats (default `formats.json`)    |
| `[dashboard] host`, `port`          | where the dashboard listens (default `127.0.0.1:8050`)           |

Relative paths are resolved against the folder that contains `config.ini`.
The file is located via `--config`, `$STOCK_DATALOAD_CONFIG`, `./config.ini`
or the project root, in that order.

## Staging table

`Stock_Stage` in the SQLite file `data/stock_trade.db` (DDL: `sql/create_stock_stage_sqlite.sql`):

| Column     | Type    | Content                                  |
|------------|---------|------------------------------------------|
| StageId    | INTEGER PRIMARY KEY AUTOINCREMENT |                |
| Market     | TEXT    | from file name / `--market`              |
| Ticker     | TEXT    | `<ticker>`                               |
| TradeDate  | TEXT    | `<date>` (YYYYMMDD, as delivered)        |
| Open/High/Low/Close | TEXT | as delivered                       |
| Vol        | TEXT    | `<vol>`                                  |
| SourceFile | TEXT    | file name                                |
| LoadId     | TEXT    | one id per file load                     |
| LoadedAt   | TEXT    | ISO timestamp                            |

Query it with any SQLite tool, e.g. `sqlite3 data/stock_trade.db "select * from Stock_Stage limit 5"`.

## Tests

```bash
python -m unittest discover -s tests -t . -v      # no external services needed
```

`tests/test_requirements.py` maps tests to requirements (R1 column mapping,
R2 conversion → same loading folder, R3 conversion/loading independent,
R4 every file type → default text file, R5 dashboard configuration);
`tests/test_readers.py` round-trips every sample file type,
`tests/test_dashboard.py` drives the web UI with Flask's test client and
`tests/test_dashboard_browser.py` clicks through it in a real headless
Chromium via Playwright (`pip install playwright && playwright install chromium`;
skipped automatically when no browser is available).

## Scheduling

`python -m stock_dataload load` returns 0 on success, 1 if any file failed,
2 on a configuration error – run it from cron / Windows Task Scheduler after
the files have been dropped (or converted) into `data/loading/`.
