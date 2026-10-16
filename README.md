# stock_dataload

Loads daily stock trading files (e.g. `NASDAQ_20250123.txt`) from a **loading
folder** into a SQLite **staging table**. Files delivered in other layouts or
file types (**CSV, delimited text, Excel, JSON, XML**) are first **converted**
to the default layout. A **web dashboard** lets business users manage file
formats, folders and loads, and **monitor** the activity log, errors and data
quality findings.

```
  conversion folder              loading folder                     database
  csv / text / xlsx    convert   <ticker>,<date>,<open>,    load
  json / xml, any     ────────►  <high>,<low>,<close>,<vol> ────────►  Stock_Stage
  column sequence                always a .txt text file             (SQLite)
```

Python port of the `Stock_DataLoad` SSIS package.

---

## 1. Installation

Requires **Python 3.10+**. Everything is installed into the project's own
virtual environment (`.venv`) – nothing is installed globally.

### Windows (Command Prompt or PowerShell)

```bat
cd Stock_DataLoadGit
py -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python -m stock_dataload --help
```

* `py` is the Python launcher that comes with the python.org installer.
  If `py` is not found, use `python -m venv .venv`.
  (`python3` is **not** a Windows command – it only opens the Microsoft
  Store alias, which is why `python3 -m venv .venv` fails.)
* Paths on Windows use `.venv\Scripts\...`, not `.venv/bin/...`.
* PowerShell may refuse to run `activate` ("running scripts is disabled").
  Run once: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, then
  `.venv\Scripts\Activate.ps1`.
* Without activating the venv, call the tools by their full path instead:
  `.venv\Scripts\python -m pip install -r requirements.txt` and
  `.venv\Scripts\python -m stock_dataload --help`.

### macOS / Linux

```bash
cd Stock_DataLoadGit
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m stock_dataload --help
```

(Without activating: `.venv/bin/python -m pip install -r requirements.txt`,
`.venv/bin/python -m stock_dataload --help`.)

All commands below assume the venv is **activated** (`(.venv)` shows in the
prompt), so `python` is the venv's Python on every platform.

What gets installed: `flask` (dashboard), `openpyxl` (Excel files),
`coverage` and `playwright` (tests). Converting csv/text/json/xml and
loading need the standard library only.

---

## 2. Quick start

```bash
python -m stock_dataload init-db                        # create the tables (data/stock_trade.db)

# load a default-format file
copy samples\NASDAQ_20250123.txt data\loading\          # Windows   (cp ... on macOS/Linux)
python -m stock_dataload load
#   NASDAQ_20250123.txt: loaded (4092 rows, market=NASDAQ, load_id=...)

# a file in another layout / file type: convert it first (result lands in data/loading/)
python -m stock_dataload convert samples/NASDAQ_20250123.xlsx samples/NASDAQ_20250123.json --rename
python -m stock_dataload load

# or put files into data/conversion/ and convert the whole folder
python -m stock_dataload convert-folder

# what happened?
python -m stock_dataload status
python -m stock_dataload logs
python -m stock_dataload data-errors --alerts

# or do all of this in the browser
python -m stock_dataload dashboard                      # http://127.0.0.1:8050
```

All commands accept `--config FILE`, `-v` (debug) and `-q` (quiet).

---

## 3. Dashboard

```bash
python -m stock_dataload dashboard            # host/port in config.ini [dashboard]
```

### Signing in – the initial administrator

Every page requires a signed-in user. **No account or secret is shipped with
the repository**; create the first administrator in one of two ways:

```bash
# a) before the first start - prompts for the password (nothing is echoed or stored in clear text)
python -m stock_dataload user add admin --role admin

# b) just start the dashboard: with an empty user table it creates 'admin' with a random
#    ONE-TIME password that is printed to the console (or set STOCK_DATALOAD_ADMIN_PASSWORD)
python -m stock_dataload dashboard
#   ========================================================================
#     Initial administrator created: username 'admin'
#     One-time password: Xk7pQ2...            <- change it at the first sign-in
#   ========================================================================
```

Sign in at `http://127.0.0.1:8050/login`; the initial administrator has to
choose a new password first, then adds further users on the **Users** page.

| Role | May |
|------|-----|
| `viewer` | read the overview, formats and every monitoring page / JSON api |
| `operator` | + upload, convert, load, delete files, edit formats, data correction |
| `admin` | + settings, user management (add / edit / disable / delete users, reset passwords) |

Users can change their own password under their name in the header. All
sign-ins, failed attempts, denied actions and user changes are written to
`App_Log` (component `auth`).

Passwords are stored only as salted **PBKDF2-HMAC-SHA256** hashes in the
`App_User` table. The session-signing secret is read from
`STOCK_DATALOAD_SECRET` or generated once into the git-ignored
`data/.secret_key` file – nothing secret ever lives in the repository.
The same accounts can be managed from the command line:

```
python -m stock_dataload user list
python -m stock_dataload user add NAME [--role viewer|operator|admin] [--name "Full Name"] [--must-change]
python -m stock_dataload user passwd NAME          # password: prompt, --password, or $STOCK_DATALOAD_PASSWORD
python -m stock_dataload user role NAME ROLE | enable NAME | disable NAME | delete NAME
```

| Page | What you do there |
|------|-------------------|
| **Overview** | the pipeline *conversion folder → loading folder → staging table*: upload files, pick a format per file (a matching custom format is pre-selected), **Convert** / **Preview** / **Load**, recent loads, error reports, and a **monitoring strip** with the current error / alert counts. |
| **File formats** | create / edit / clone / delete custom formats: file type, file-name pattern, delimiter, header, rows to skip, date format, encoding, Excel sheet, JSON record path, XML record tag and the column mapping. **Run preview** tests a format before saving. |
| **Monitoring** | summary with totals; **activity log** (`App_Log`, filter by level / component); **errors** (`App_Error`, failing step + expandable stack trace); **data errors** and **alerts** (`Data_Error`, filter by file / alerts only / open only, each with a **Correct** link); **load history** (`Load_History`). JSON: `/api/logs`, `/api/errors`, `/api/data-errors`, `/api/history`, `/api/corrections`, `/api/monitor`, `/api/status`. |
| **Data correction** | fix a row of the staging table: find it by stage id, ticker / trade date / market / source file – or click **Correct** on a data error, which opens the page with that row populated. Values are validated with the same business rules as a load; a row that is missing (e.g. a missing-market-day alert or a rejected line) can be added. Every change is tracked in `Data_Correction` (old / new value per column, note) and the related data errors are marked **resolved**. |
| **Settings** (admin) | conversion / loading / other folders, SQLite file, staging table, formats file, loader options. Saved to `config.ini`. |
| **Users** (admin) | add users with a role and an initial password (optionally to be changed at the first sign-in), edit name / role / active status, reset passwords, delete accounts. The last active administrator cannot be demoted, disabled or deleted. |

The dashboard calls the same `FileConverter` / `StageLoader` classes as the
CLI – everything it does can also be scripted or scheduled.

![overview](docs/dashboard_overview.png)
![monitoring](docs/dashboard_monitoring.png)
![data correction](docs/dashboard_correction.png)
![users](docs/dashboard_users.png)

<details>
<summary>More screenshots: preview, format editor, settings</summary>

![preview](docs/dashboard_preview.png)
![format editor](docs/dashboard_format_editor.png)
![settings](docs/dashboard_settings.png)
</details>

---

## 4. Loading (`load`)

```
python -m stock_dataload load                      # every *.txt in data/loading/
python -m stock_dataload load path/to/file.txt     # specific file(s)
    --market NYSE        Market value for all rows (default: from the file name)
    --truncate           empty the staging table before loading
    --skip-bad-rows      skip invalid rows (written to data/error/<file>.rejects.txt)
    --keep               leave files where they are instead of moving them
```

The loader only accepts the **default format** – comma separated, one
header row, dates as `YYYYMMDD`:

```
<ticker>,<date>,<open>,<high>,<low>,<close>,<vol>
AACG,20250123,0.9,0.9229,0.89,0.9128,2800
```

Per file: sha-256 fingerprint → header check → row validation → **delete the
file's previous rows + insert the new rows in one transaction** → data
quality checks → move to `data/archive/` (success) or `data/error/`
(failure, with a `.error.txt` report and a full rollback). Exit code is 1 if
any file failed – handy for schedulers.

**Re-loading a file.** If a file with the same name was loaded before, its
rows are deleted from the staging table and the file is loaded again – in
the same transaction, so a broken re-delivery rolls back and the previous
copy stays. No matter how often a file is loaded, exactly **one copy** (the
latest delivery) is in the staging table. Every attempt is recorded in
`Load_History` (`loaded` / `replaced` / `failed`).

`Market` is derived from the file name (`NASDAQ_20250123.txt` → `NASDAQ`,
regex in `config.ini`) unless `--market` is given.

---

## 5. Converting (`convert`, `convert-folder`, `inspect`, `formats`)

| Source | Extensions | Notes |
|--------|------------|-------|
| CSV / delimited text | `.csv .txt .tsv .dat .prn .psv` | delimiter auto-detected (`, ; \| tab`), optional header, title rows |
| Excel | `.xlsx .xlsm` | sheet by name or number, title rows, numeric / date cells |
| JSON | `.json .jsonl .ndjson` | list of objects, nested objects (`prices.open`), `record_path`, JSON lines |
| XML | `.xml` | one element per record (`record_tag`), attributes and child elements |

Whatever the source, the output is always a default-format `.txt` file in
the loading folder.

```
python -m stock_dataload convert SOURCE [SOURCE ...]
    --format NAME            use a saved format (formats.json / dashboard)
    --type excel             source type: auto (default) csv text excel json xml
    --columns date,ticker,vol,close,open,high,low    source column sequence ('skip' ignores a column)
    --map ticker=Symbol,date=#7,open=Open,...        map each target column to a field, #position or =constant
    --delimiter ';'  --no-header  --skip-rows 1  --date-format %d/%m/%Y  --encoding cp1252
    --sheet Prices  --record-path data  --record-tag quote
    --output-name X.txt  --overwrite | --rename  --move-source  --no-move  --skip-bad-rows
python -m stock_dataload convert-folder [--format NAME] [--on-conflict rename|overwrite|error] [--keep-source]
python -m stock_dataload inspect SOURCE        dry run: detected type, mapping, first converted rows
python -m stock_dataload formats               list the saved formats
```

How columns are mapped (CLI and dashboard alike):

1. an explicit **column map** (`--map`) – source field name, `#n` position or
   `=VALUE` constant (e.g. `ticker==YHOO` for files without a ticker column);
2. else an explicit **column sequence** (`--columns`);
3. else header names / JSON keys / XML tags are matched against the alias
   table in `formats.COLUMN_ALIASES` (`Symbol`→ticker, `Volume`→vol,
   `Trade Date`→date, …). Unknown columns are ignored; all seven default
   columns must be found.

Files in the conversion folder are matched against the **file pattern** of
each saved format (`NASDAQ_*.xlsx` → `excel_prices`). Output is written to
`data/work/` and only moved into `data/loading/` when complete; name clashes
are resolved by renaming (`NASDAQ_20250123_json.txt`).

The `samples/` folder holds `NASDAQ_20250123.txt` and the same data as csv,
tsv, `;`-delimited, pipe/no-header, xlsx, json and xml – converting any of
them yields a file byte-identical to the original (verified by the tests).

---

## 6. Monitoring: activity log, errors, data quality

Everything the app does is tracked in the database (next to the rotating
`logs/stock_dataload.log`) and shown on the dashboard's **Monitoring** pages:

| Table | Content | CLI |
|-------|---------|-----|
| `App_Log` | every activity: `load_started`, `reload_detected`, `previous_load_replaced`, `file_loaded`, `data_errors_found`, `data_alert`, `run_complete`, … (level, component, file, load id, JSON details) | `logs [--level ALERT] [--component loader]` |
| `App_Error` | every operational error with the **component and step** that failed (`check_file`, `previous_load_check`, `replace_rows`, `insert_rows`, `record_history`, `quality_check`, `archive_file`, conversion, CLI …), exception type and stack trace | `errors [--component loader]` |
| `Data_Error` | data quality findings (see below) with rule, severity, `Alert` flag, ticker, date, column, value, line; `Resolved` once corrected | `data-errors [--file NAME] [--alerts]` |
| `Data_Correction` | every change made on the **Data correction** page: action (updated / inserted), staging row, column, old / new value, linked data error, note | dashboard `/correct`, `/api/corrections` |
| `Load_History` | one row per load attempt: file name, sha-256 content hash, status, rows, message | `history [--file NAME]` |

All `logs` / `errors` / `data-errors` / `history` commands take `--limit N`.

### Data quality checks (strategy pattern)

After every successful load the rows are validated by pluggable business
rules (`stock_dataload/quality.py`). Findings do not fail the load (a staging
table lands data *as delivered*) but each one is recorded in `Data_Error`:

| Rule | Checks | Severity |
|------|--------|----------|
| `date_is_valid` | `date` is a real `YYYYMMDD` calendar date | error |
| `numeric_columns` | `open, high, low, close, vol` are numbers | error |
| `high_above_low` | on each row `high` is not smaller than `low` | error |
| `missing_market_day` | every stock known from previous loads of the market has **valid** data for each day the **market was open** (weekends, US holidays and `extra_holidays` are closed days) | **alert** |
| `row_format` | rows rejected by the loader itself | error |

Alerts are also written as `ALERT` entries to `App_Log` and as warnings to
the log file. Add or remove rules without touching the loader:

```python
from stock_dataload import StageLoader, load_settings
from stock_dataload.quality import DataQualityChecker, RowValidationStrategy, QualityIssue

class VolumeCapStrategy(RowValidationStrategy):
    name = "volume_cap"
    def validate_row(self, row, context):
        if float(row.values["vol"] or 0) > 1e10:
            return [QualityIssue(self.name, "volume looks wrong", line_no=row.line_no)]
        return []

checker = DataQualityChecker()               # the default rule set
checker.add_strategy(VolumeCapStrategy())    # add a rule
checker.remove_strategy("high_above_low")    # drop a rule
StageLoader(load_settings(), checker=checker).run()
```

Dataset-wide rules subclass `DatasetValidationStrategy` (see
`MissingMarketDayStrategy`).

---

## 7. Configuration (`config.ini`)

Edit in the dashboard (*Settings*) or by hand. Relative paths are resolved
against the folder containing `config.ini`; the file is found via
`--config`, `$STOCK_DATALOAD_CONFIG`, `./config.ini` or the project root.

| Section / key | Meaning |
|---------------|---------|
| `[paths] conversion_dir`, `loading_dir` | the conversion folder and the loading folder |
| `converted_dir`, `archive_dir`, `error_dir`, `work_dir`, `log_dir` | see project layout |
| `[database] sqlite_path` | SQLite file (created on first use), default `data/stock_trade.db` |
| `stage_table`, `batch_size`, `truncate_before_load` | staging table name, rows per insert batch, truncate-and-load |
| `[quality] enabled` | run the data quality strategies after every load |
| `alert_missing_days` | missing-market-day alerts on/off |
| `extra_holidays` | ad-hoc market closures, comma separated `YYYY-MM-DD` |
| `[loader] file_patterns` | which files in the loading folder are loaded (`*.txt`) |
| `market_from_filename_regex`, `default_market` | how `Market` is derived from the file name |
| `skip_bad_rows`, `encoding` | loader options |
| `[formats] file` | JSON file holding the custom formats (`formats.json`) |
| `[dashboard] host`, `port` | where the dashboard listens (`127.0.0.1:8050`) |
| `secret_key_file` | where the generated session secret is kept (`data/.secret_key`, git-ignored); `$STOCK_DATALOAD_SECRET` overrides |

---

## 8. Database

One SQLite file (`data/stock_trade.db`); all tables are created
automatically (or by `init-db`). DDL for reference in `sql/`.

| Table | Columns |
|-------|---------|
| `Stock_Stage` | `StageId`, `Market`, `Ticker`, `TradeDate`, `Open`, `High`, `Low`, `Close`, `Vol` (all as delivered, text), `SourceFile`, `LoadId`, `LoadedAt` |
| `Load_History` | `FileName`, `FileHash`, `FileSize`, `LoadId`, `Market`, `Status`, `RowsLoaded`, `Message`, `LoadedAt` |
| `App_Log` | `LoggedAt`, `Level`, `Component`, `Event`, `SourceFile`, `LoadId`, `Message`, `Details` |
| `App_Error` | `OccurredAt`, `Component`, `Operation`, `SourceFile`, `LoadId`, `ErrorType`, `ErrorMessage`, `Stacktrace`, `Details` |
| `Data_Error` | `DetectedAt`, `SourceFile`, `LoadId`, `Market`, `Ticker`, `TradeDate`, `ColumnName`, `Value`, `Rule`, `Severity`, `Alert`, `ErrorMessage`, `LineNo`, `Resolved`, `ResolvedAt`, `ResolutionNote` |
| `Data_Correction` | `CorrectedAt`, `Action`, `StageId`, `SourceFile`, `Ticker`, `TradeDate`, `ColumnName`, `OldValue`, `NewValue`, `DataErrorId`, `Note` |
| `App_User` | `Username`, `DisplayName`, `PasswordHash` (PBKDF2), `Role`, `Active`, `MustChangePassword`, `CreatedAt`, `CreatedBy`, `LastLoginAt` |

Query with any SQLite tool, e.g. `sqlite3 data/stock_trade.db "select * from Stock_Stage limit 5"`.

---

## 9. Tests

```bash
python -m unittest discover -s tests -t . -v
python -m coverage run --source=stock_dataload -m unittest discover -s tests -t .
python -m coverage report -m                  # ~97% lines, every method covered
```

No external services are needed. `tests/test_dashboard_browser.py` drives
the dashboard in a real headless Chromium via Playwright: run
`python -m playwright install chromium` once (or point
`PLAYWRIGHT_BROWSERS_PATH` at an existing Playwright browser cache); these
tests are skipped automatically if no browser can be launched.

---

## 10. Project layout

```
Stock_DataLoadGit/
├── config.ini                 settings (editable in the dashboard)
├── formats.json               custom file formats (editable in the dashboard)
├── requirements.txt
├── stock_dataload/            the package
│   ├── formats.py             default format + header alias table
│   ├── file_formats.py        FileFormat model + FormatStore (formats.json)
│   ├── readers.py             csv / text / excel / json / xml readers
│   ├── converter.py           any source -> default-format text file -> loading folder
│   ├── loader.py              loading folder -> staging table
│   ├── db.py                  staging table + Load_History (SQLite)
│   ├── audit.py               App_Log / App_Error
│   ├── auth.py                users, roles, password hashing (App_User)
│   ├── quality.py             data quality strategies + Data_Error
│   ├── correction.py          data correction of staging rows + Data_Correction
│   ├── market_calendar.py     US market trading calendar
│   ├── config.py              config.ini handling
│   ├── cli.py                 python -m stock_dataload ...
│   └── dashboard/             Flask web dashboard
├── data/
│   ├── conversion/            THE CONVERSION FOLDER: source files in any supported format
│   ├── converted/             sources after a successful conversion
│   ├── loading/               THE LOADING FOLDER: default-format .txt files waiting to be loaded
│   ├── archive/               files loaded successfully
│   ├── error/                 files that failed + .error.txt / .rejects.txt reports
│   └── work/                  scratch space for the converter
├── samples/                   NASDAQ_20250123 as txt / csv / tsv / xlsx / json / xml
├── sql/                       DDL scripts for reference
├── tests/                     unittest suite
└── logs/                      stock_dataload.log (rotating)
```

---

## 11. Scripting and scheduling

```python
from stock_dataload import load_settings, FileConverter, ConversionSpec, StageLoader

settings = load_settings()                     # finds config.ini
FileConverter(settings).convert("inbox/broker.csv", ConversionSpec(date_format="%Y-%m-%d"))
for result in StageLoader(settings).run():
    print(result)                              # broker.txt: loaded (4092 rows, ...)
```

`python -m stock_dataload load` returns 0 on success, 1 if any file failed,
2 on a configuration error – run it from Windows Task Scheduler / cron
(using the venv's Python, e.g. `C:\...\Stock_DataLoadGit\.venv\Scripts\python.exe -m stock_dataload load`)
after the files have been dropped or converted into `data/loading/`.
