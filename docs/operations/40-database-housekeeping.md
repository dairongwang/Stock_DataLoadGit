# Database housekeeping

The database is one SQLite file in WAL mode. With a year of NASDAQ
deliveries loaded it grows to roughly 385 MB - mostly the staging table
(a text copy of the daily table) and indexes; see the [size report](45-size-report.md).

```bash
python -m stock_dataload db size                         # tables and indexes, largest first, free pages
python -m stock_dataload db purge-stage --dry-run        # promoted staging rows older than 30 days
python -m stock_dataload db purge-stage --all --vacuum   # all promoted staging rows, then compact (385 -> 244 MB)
python -m stock_dataload db vacuum                       # give free pages back to the file system
python -m stock_dataload db reset-data                   # plan: empty the stock tables, rebuild indexes
python -m stock_dataload db reset-data --yes             # do it (schema, calendar, symbols, users, customers stay)
python -m stock_dataload db keep-tickers PFE HD MRK JNJ  # plan: delete the stock data of every OTHER ticker
python -m stock_dataload db keep-tickers PFE HD MRK JNJ --yes   # do it, then compact
```

* **purge-stage** deletes staging rows whose `StageId` is referenced by a
  daily row (they were promoted); the daily table keeps the data and
  `Load_History` the registry. Keep recent rows (`--keep-days`, default 30)
  so corrections of fresh deliveries still find them.
* **reset-data** empties `Stock_Stage`, `Stock_Daily`, `Stock_Weekly`,
  `Stock_Monthly`, `Data_Error` and - unless `--keep-history` - the load /
  promotion history, drops and re-creates the secondary indexes, `REINDEX`es
  the unique constraints and vacuums. Use it to hand over a clean database.
* **keep-tickers** keeps the stock data of the named tickers only: it deletes every other ticker's
  rows from `Stock_Stage`, `Stock_EOD_Stage`, the active tables, `Data_Error` and `Data_Correction`
  (rows without a ticker stay) in one transaction, then vacuums. The symbol lists, `Stock_Symbol`,
  the calendar, the run history, users and customers are not touched. The project database
  (`data/stock_trade.db`) was trimmed this way to PFE, HD, MRK and JNJ (131.8 MB -> 4.3 MB).
* **vacuum** is needed after big deletes: SQLite keeps freed pages inside
  the file.

Backups: `sqlite3 data/stock_trade.db ".backup backup.db"` (consistent even
while the apps run). Keep the database out of the source tree with
`[database] sqlite_path` if you version the project.
