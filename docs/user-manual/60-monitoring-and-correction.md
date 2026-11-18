# Monitoring and data correction

## Monitoring pages

| Page | Shows | Table |
|------|-------|-------|
| Summary | totals and the most recent errors, alerts, loads and log entries | - |
| Activity log | every noteworthy step: files picked up, rows inserted, promotions, sign-ins, portal orders … filter by level / component | `App_Log` |
| Errors | operational errors with the component and step that failed and the stack trace | `App_Error` |
| Data errors | data-quality findings and rows rejected by the promotion; *alerts* are missing-market-day findings; each has a **Correct** link | `Data_Error` |
| Load history | one row per load attempt: file, hash, status (`loaded` / `replaced` / `failed` / `skipped`), rows | `Load_History` |

The same data is available as JSON: `/api/logs`, `/api/errors`,
`/api/data-errors`, `/api/history`, `/api/promotions`, `/api/corrections`,
`/api/monitor`.

## Data-quality checks

After every load the rows are checked (`[quality] enabled`): duplicates of a
stock/day inside a file, prices / volumes that are not numbers, high < low,
rows outside the symbol's trading window, and - on every open market day -
stocks with a known first trade date that did not deliver (**alert**,
`alert_missing_days`). Findings carry rule, severity, ticker, date, column,
value and line number, and are marked **resolved** once corrected.

## Data correction

*Data correction* finds a staging row by stage id or by ticker / date /
market / file - or you arrive from a data error's **Correct** link with the
row preselected. Edit the values (validated with the same rules as a load)
and save with a note; a row that is missing (a missing-market-day alert, a
rejected line) can be **added**. Every change is tracked in
`Data_Correction` (old / new value per column) and the related data errors
are resolved. The next incremental promotion picks the corrected rows up.
