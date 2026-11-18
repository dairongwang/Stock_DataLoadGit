# User manual

How business users work with the **dashboard** (`python -m stock_dataload dashboard`,
http://127.0.0.1:8050): getting data in, converting unusual files, promoting
into the active tables, keeping the trading calendar and the symbol table,
watching the monitoring pages, correcting data and looking after your account.

Roles decide what you can do:

| Role | Can |
|------|-----|
| **viewer** | see every page and the JSON endpoints (read-only) |
| **operator** | + upload / convert / load files, edit formats, promote, maintain the calendar and symbols, correct data |
| **admin** | + settings, users, the portal's customers |

Self-registered accounts start as *viewer*; an administrator grants more on the *Users* page.

Prices can also be downloaded straight from an EOD data API - with
`python -m stock_dataload eod-fetch SYMBOL` / `eod-fetch-market MARKET`, or on the dashboard's
**API loading** page (stocks typed in, a whole market or a text file of symbols, with live progress,
stop and resume) - see [Downloading prices from the EOD API](35-eod-api-downloads.md#in-the-dashboard-the-api-loading-page).
