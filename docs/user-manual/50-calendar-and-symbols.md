# Trading calendar and symbols

## Trading calendar

The `No_Trading_Day` table lists every day a market is closed - weekends,
holidays and ad-hoc closures - one row per market and date, with the reason.
Markets (`Market` table) carry a rule set: **us** (weekends + the NYSE /
NASDAQ holidays with the Saturday → Friday / Sunday → Monday observance
rules) or **weekends** (Saturdays and Sundays only; add holidays by hand).
NASDAQ, NYSE and AMEX exist from the start.

On the *Trading calendar* page:

* pick a market and a year to see its holidays (tick *all* for weekends too);
* **Generate** a year - run this **before a year begins**; it only adds
  missing days and never overwrites manual entries;
* **Add closure** (e.g. a day of mourning) / **Remove** (the market trades after all);
* administrators add markets and activate / deactivate them.

The calendar is the single source of truth for "was the market open?": the
loader skips deliveries dated on a closed day, `promote daily` rejects rows
dated on a closed day, and the missing-market-day check expects every known
stock on every open day.

```bash
python -m stock_dataload no-trading-days update --year 2027
python -m stock_dataload no-trading-days check NASDAQ 2027-07-05
python -m stock_dataload no-trading-days add NASDAQ 2025-01-09 --reason "National Day of Mourning"
```

## Symbols

`Stock_Symbol` has one row per symbol and market: the **first** and (for
delisted stocks) **last trade date** you maintain, plus the range of dates
actually loaded (`FirstSeen .. LastSeen`, kept by the loader).

| Situation | Effect |
|-----------|--------|
| a loaded row is dated before the first / after the last trade date | flagged at load time and **rejected by promote daily** |
| a stock is absent before its first / after its last trade date | fine - no alert |
| a stock has **no first trade date** | it is not checked for missing days |
| a stock is absent on an open day inside its window | **missing-market-day alert** |

On the *Symbols* page search, edit first / last trade dates, import a CSV
(`ticker, market, first_trade_date, last_trade_date, name`), and find
symbols **without a first trade date** or with **data before the listing**
(*suspicious*).

```bash
python -m stock_dataload symbols set HCMAU --first-trade-date 2025-08-01
python -m stock_dataload symbols import listings.csv
python -m stock_dataload symbols list --suspicious
```
