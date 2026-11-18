# Data model

One SQLite database; every table is created on first use (`CREATE TABLE IF
NOT EXISTS`) and columns added later are migrated with `ALTER TABLE` on the
fly. Reference DDL is exported to `sql/`.

## Stock data

```
 Stock_Stage (StageId, Market, Ticker, TradeDate, Open..Vol as TEXT, SourceFile, LoadId, LoadedAt)
      │ promote_daily (validate, type, de-duplicate, upsert)
      ▼
 Stock_Daily (DailyId, Market, Ticker, TradeDate, Open, High, Low, Close REAL, Volume INTEGER, SourceFile, LoadId, StageId, PromotedAt, UpdatedAt)
      │ UNIQUE (Ticker, TradeDate)        indexes: TradeDate, UpdatedAt
      ├── promote_weekly  ──► Stock_Weekly  (Ticker, WeekStart, WeekEnd, ISOYear, ISOWeek, OHLC, Volume, TradingDays, ExpectedDays, MissingDays, MissingDates, Complete)
      └── promote_monthly ──► Stock_Monthly (Ticker, MonthStart, MonthEnd, Year, Month, ... same)
```

```
 EOD API  GET <base_url><SYMBOL>?api_token=..&fmt=json   [{"date","open","high","low","close","adjusted_close","volume"}, ...]
      │ eod-fetch: save the response (data/api_downloads) ─► load (validate, type, upsert per day)
      ▼
 Stock_EOD_Stage (EodStageId, Symbol, Ticker, Exchange, TradeDate, Open, High, Low, Close, AdjustedClose REAL, Volume INTEGER,
                  SourceFile, LoadId, LoadedAt, UpdatedAt)     UNIQUE (Symbol, TradeDate)
```

`promote_eod_daily` moves it into `Stock_Daily` converted to adjusted prices
(`rate = AdjustedClose / Close`; open / high / low × rate; close = adjusted
close), matching ticker, trade date and market.

The exchange symbol lists (`exchange-symbol-list/<MARKET>`) land in
`Stock_Symbol_Stage` (Market, Code, Name, Country, Exchange, Currency, Type,
Isin; `UNIQUE (Market, Code)`; `FirstSeenAt` / `LastSeenAt` per code).

The EOD staging table is typed (the API delivers numbers) and keeps one row
per symbol and day: overlapping responses update changed values and leave
identical ones alone. Invalid records go to `Data_Error` (rule
`eod_record`).

`Load_History` (file name, sha-256, load id, status) and
`Promotion_History` (procedure, mode, parameters, row counts, status)
register every run.

## Reference data

* `Market` - code, name, rule set (`us` / `weekends`), active.
* `No_Trading_Day` - market, date, reason, source (`generated` / `config` / `manual`); `UNIQUE (Market, TradeDate)`.
* `Stock_Symbol` - ticker, market, name, first / last trade date, first / last seen; `UNIQUE (Market, Ticker)`.

## Operations

`App_Log` (level, component, event, message, JSON details), `App_Error`
(component, operation, exception, stack trace), `Data_Error` (rule,
severity, alert, ticker, date, column, value, resolved), `Data_Correction`
(stage row, column, old / new value, note).

## Accounts

```
 App_User ─────────────┐                       Api_Customer ────────┬── Api_Key ──┬── Api_Order (pack, tokens, amount, status, provider)
 staff: role viewer /   │ PrincipalType 'staff'  portal: role user / │  balance,   └── Api_Usage (endpoint, tickers, year, tokens, rows)
 operator / admin       │                       admin, guest when    │  hash
                        ▼                       PasswordHash NULL    │ PrincipalType 'customer'
            Auth_TwoFactor (method totp/email/sms, secret, destination, enabled)
            Auth_Code      (purpose login/enrol/verify_email/reset, channel, hash, expiry, attempts, consumed)
            Auth_RecoveryCode (hash, used)
            Auth_Identity (provider google/apple/facebook, subject UNIQUE per provider, email, name)
```

The `Auth_*` tables are keyed by `(PrincipalType, PrincipalId)` so one
implementation serves staff users and customers. Special `PasswordHash`
values: `NULL` = guest (portal), `external` = signs in only through a
connected provider.

## Money and tokens

Amounts are stored in the smallest currency unit (`Amount` in cents,
`Currency` ISO code). Token balances live on the **key**: `Balance`,
`TokensPurchased`, `TokensUsed`; every change is visible through
`Api_Order` (credits, including manual grants with amount 0) and
`Api_Usage` (debits and refused requests).

Column-by-column lists: [Reference › Tables](../reference/30-tables.md).
