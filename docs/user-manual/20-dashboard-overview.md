# Overview page: convert, load, bulk load

The Overview page shows the pipeline from left to right: the **conversion
folder**, the **loading folder** and the **staging table**.

## Uploading files

*Upload* drops files into the conversion folder (any supported type: csv,
txt, tsv, xlsx, json, xml) or - for files already in the default format -
straight into the loading folder. Files can also be copied into the folders
by any other means (network share, scheduler).

## Converting

Each file in the conversion folder shows its detected type and, if the
name matches a saved format's file pattern, the suggested **format**.
*Preview* shows the first rows before and after conversion; *Convert*
writes a default-format `.txt` file into the loading folder and moves the
source to `data/converted/`. *Convert all* processes the whole folder.

The default format is

```
<ticker>,<date>,<open>,<high>,<low>,<close>,<vol>
AACG,20250123,0.9,0.9229,0.89,0.9128,2800
```

Conversion never touches the database.

## Loading

*Load* takes one default-format file (or the whole loading folder) into the
staging table `Stock_Stage`:

* the **market** comes from the file name (`NASDAQ_20250123.txt` → NASDAQ)
  unless you type one into the *Market* field;
* the **trade date** in the file name decides whether the delivery is
  accepted: a file dated on a day the market was closed (weekend, holiday,
  closure - see [Trading calendar](50-calendar-and-symbols.md)) is
  **skipped** and moved to `data/skipped/`; tick *include no-trading days*
  to load it anyway;
* re-loading a file **replaces** its previous rows - there is always exactly
  one copy of a file in the staging table; every attempt is recorded in the
  load history;
* a broken file is rolled back completely and moved to `data/error/` with a
  `.error.txt` report; *skip bad rows* loads the good rows and writes the
  rejected lines to a `.rejects.txt`.

After a successful load the data-quality checks run and their findings
appear on the Monitoring pages.

## Bulk-loading a folder

*Bulk load a folder* loads every default-format file of a folder on the
server (for example a year of daily deliveries), sorted by name. Files stay
where they are unless *move* is ticked; a failing file does not stop the
others (unless *stop on error*); *promote afterwards* runs the three
promotion procedures when the folder is done.

## What the monitoring strip means

The counters at the top link to the Monitoring pages: open data errors,
alerts (a known stock missing on a trading day), operational errors, failed
loads and promotions.
