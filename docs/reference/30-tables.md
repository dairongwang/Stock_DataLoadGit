# Tables

All in `data/stock_trade.db`; DDL in `sql/`.

## Stock data

| Table | Columns |
|-------|---------|
| `Stock_Stage` | `StageId`, `Market`, `Ticker`, `TradeDate`, `Open`, `High`, `Low`, `Close`, `Vol` (text, as delivered), `SourceFile`, `LoadId`, `LoadedAt` |
| `Stock_Daily` | `DailyId`, `Market`, `Ticker`, `TradeDate`, `Open`, `High`, `Low`, `Close`, `Volume`, `SourceFile`, `LoadId`, `StageId`, `PromotedAt`, `UpdatedAt` - `UNIQUE (Ticker, TradeDate)`, indexes on `TradeDate`, `UpdatedAt` |
| `Stock_Weekly` | `WeeklyId`, `Market`, `Ticker`, `WeekStart`, `WeekEnd`, `ISOYear`, `ISOWeek`, `Open`, `High`, `Low`, `Close`, `Volume`, `TradingDays`, `ExpectedDays`, `MissingDays`, `MissingDates`, `Complete`, `FirstTradeDate`, `LastTradeDate`, `PromotedAt`, `UpdatedAt` - `UNIQUE (Ticker, WeekStart)` |
| `Stock_Monthly` | `MonthlyId`, `Market`, `Ticker`, `MonthStart`, `MonthEnd`, `Year`, `Month`, … as weekly - `UNIQUE (Ticker, MonthStart)` |
| `Stock_Symbol_Stage` | exchange symbol lists: `SymbolStageId`, `Market` (requested, e.g. `NYSE`), `Code`, `Name`, `Country`, `Exchange` (venue: `NYSE`, `NYSE ARCA` ...), `Currency`, `Type`, `Isin`, `SourceFile`, `LoadId`, `FirstSeenAt`, `LastSeenAt`, `UpdatedAt` - `UNIQUE (Market, Code)`, indexes on `Code`, `(Market, Type)` |
| `Eod_Market_Run` | the `eod-fetch-market` runs: `RunId`, `Market`, `Gateway` (`live` / `fake`), `Status` (`running` / `completed` / `completed_with_errors` / `aborted` / `interrupted`), `Parameters` (JSON; `source`: `market` / `symbols` / `file` / `bulk` (one day of a market in one call, no items; counts as a whole-market load unless `symbols_given`; `trade_date` - at most 3 bulk calls per market and trade date) - whole-market runs at most 3 per market and day, text-file runs at most 5 a day, counted by the date of `StartedAt`; `symbols_given: false` = a whole-market run), `Symbols`, `Loaded`, `NoData`, `Failed`, `Skipped`, `Rows`, `ApiCalls`, `Message`, `StartedAt`, `FinishedAt` |
| `Eod_Market_Run_Item` | one row per stock of a run: `RunId`, `Position`, `Symbol`, `Status` (`pending` / `loaded` / `no_data` / `failed` / `skipped`), `Attempts` (API calls), `HttpStatus` (of the last failure), `Received`, `Inserted`, `Updated`, `Unchanged`, `Rejected`, `FirstDate`, `LastDate`, `LoadId`, `Error`, `FinishedAt` - `PRIMARY KEY (RunId, Symbol)` |
| `Eod_Stock_Load` | the daily limit per stock: one row per successful load of a stock into `Stock_EOD_Stage` - `Ticker` (`MRK` also for `MRK.US`), `Symbol`, `LoadId`, `SourceFile`, `Records`, `LoadedAt`; at most 5 rows per ticker and day |
| `Eod_Bulk_Schedule` | the daily schedules of the one-day bulk load, one per exchange: `Market` (unique), `RunTime` (HH:MM server time), `TradeDay` (`same` / `previous`), `Fmt`, `Symbols`, `Promote`, `Aggregates`, `Strict`, `Fake`, `AllowFakeData`, `Enabled`, `CreatedBy` / `At`, `UpdatedBy` / `At`, `LastRunDate` (once per day), `LastRunAt`, `LastTradeDate`, `LastStatus`, `LastMessage`, `LastRunId` |
| `Stock_EOD_Stage` | the EOD API data: `EodStageId`, `Symbol` (as requested, `MRK` / `MRK.US`), `Ticker`, `Exchange`, `TradeDate` (`date`), `Open`, `High`, `Low`, `Close`, `AdjustedClose` (`adjusted_close`) REAL, `Volume` INTEGER, `SourceFile`, `LoadId`, `LoadedAt`, `UpdatedAt` - `UNIQUE (Symbol, TradeDate)`, indexes on `(Ticker, TradeDate)`, `SourceFile` |

## Reference and operations

| Table | Columns |
|-------|---------|
| `Market` | `Market`, `Name`, `Rules` (`us` / `weekends`), `Active`, `CreatedAt` |
| `No_Trading_Day` | `Market`, `TradeDate`, `Reason`, `Source` (`generated` / `config` / `manual`), `CreatedAt` - `UNIQUE (Market, TradeDate)` |
| `Stock_Symbol` | `Ticker`, `Market`, `Name`, `FirstTradeDate`, `LastTradeDate`, `FirstSeen`, `LastSeen`, `Source`, `Note`, `CreatedAt`, `UpdatedAt` - `UNIQUE (Market, Ticker)` |
| `Load_History` | `FileName`, `FileHash`, `FileSize`, `LoadId`, `Market`, `Status` (`loaded` / `replaced` / `failed` / `skipped`; also the loads into `Stock_EOD_Stage`), `RowsLoaded`, `Message`, `LoadedAt` |
| `Promotion_History` | `Procedure`, `TargetTable`, `Mode`, `Filtered`, `Parameters`, `StartedAt`, `FinishedAt`, `Status`, `RowsRead`, `RowsInserted`, `RowsUpdated`, `RowsUnchanged`, `RowsRejected`, `RowsSkipped`, `RowsDeleted`, `RowsIncomplete`, `Message` |
| `App_Log` | `LoggedAt`, `Level`, `Component`, `Event`, `SourceFile`, `LoadId`, `Message`, `Details` (JSON) |
| `App_Error` | `OccurredAt`, `Component`, `Operation`, `SourceFile`, `LoadId`, `ErrorType`, `ErrorMessage`, `Stacktrace`, `Details` |
| `Data_Error` | `DetectedAt`, `SourceFile`, `LoadId`, `Market`, `Ticker`, `TradeDate`, `ColumnName`, `Value`, `Rule`, `Severity`, `Alert`, `ErrorMessage`, `LineNo`, `Resolved`, `ResolvedAt`, `ResolutionNote` |
| `Data_Correction` | `CorrectedAt`, `Action`, `StageId`, `SourceFile`, `Ticker`, `TradeDate`, `ColumnName`, `OldValue`, `NewValue`, `DataErrorId`, `Note` |

## Accounts

| Table | Columns |
|-------|---------|
| `App_User` | `UserId`, `Username` (unique, case-insensitive), `DisplayName`, `PasswordHash` (PBKDF2 or `external`), `Role`, `Active`, `MustChangePassword`, `CreatedAt`, `CreatedBy`, `LastLoginAt`, `Email`, `EmailVerified` |
| `Api_Customer` | `CustomerId`, `Email` (unique), `DisplayName`, `PasswordHash` (NULL = guest, `external` = provider only), `Role` (`user` / `admin`), `EmailVerified`, `Phone`, `Active`, `CreatedAt`, `LastLoginAt` |
| `Api_Key` | `ApiKeyId`, `CustomerId`, `Label`, `KeyPrefix`, `KeyHash` (SHA-256, unique), `Balance` (≥ 0), `TokensPurchased`, `TokensUsed`, `Active`, `CreatedAt`, `LastUsedAt`, `RevokedAt` |
| `Api_Order` | `OrderId`, `OrderRef` (unique), `CustomerId`, `ApiKeyId`, `Email`, `Guest`, `Pack`, `Tokens`, `Amount` (cents), `Currency`, `Status` (`pending` / `paid` / `failed` / `cancelled`), `Provider` (`stripe` / `paypal` / `manual`), `ProviderRef`, `PaymentDetail`, `KeyLabel`, `Note`, `CreatedAt`, `PaidAt` |
| `Api_Usage` | `UsageId`, `ApiKeyId`, `RequestedAt`, `Endpoint`, `Tickers`, `Year`, `Tokens`, `Rows`, `Status` (`ok` / `insufficient_tokens`), `RemoteAddr` |
| `Auth_TwoFactor` | `PrincipalType` (`staff` / `customer`), `PrincipalId`, `Method` (`totp` / `email` / `sms`), `Secret`, `Destination`, `Enabled`, `ConfirmedAt`, `LastUsedAt`, `CreatedAt` - unique per principal and method |
| `Auth_Code` | `PrincipalType`, `PrincipalId`, `Purpose` (`login` / `enrol` / `verify_email` / `reset`), `Channel`, `Destination`, `CodeHash`, `ExpiresAt`, `Attempts`, `ConsumedAt`, `CreatedAt` |
| `Auth_RecoveryCode` | `PrincipalType`, `PrincipalId`, `CodeHash`, `UsedAt`, `CreatedAt` |
| `Auth_Identity` | `PrincipalType`, `PrincipalId`, `Provider`, `Subject`, `Email`, `Name`, `CreatedAt`, `LastLoginAt` - `UNIQUE (Provider, Subject)` |
