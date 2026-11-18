-- Staging table of the exchange symbol lists (created automatically by `python -m stock_dataload init-db`,
-- `eod-symbols-fetch` and `eod-symbols-load`; generated from stock_dataload/eod_symbols.py - kept here for reference).
--
-- One row per market and code of the API answer
--   GET https://eodhd.com/api/exchange-symbol-list/NYSE?api_token=...&fmt=json
--   {"Code":"MRK","Name":"Merck & Company Inc","Country":"USA","Exchange":"NYSE","Currency":"USD",
--    "Type":"Common Stock","Isin":"US58933Y1055"}
-- Market comes from the request; a later list of the same market updates changed values, keeps FirstSeenAt
-- and moves LastSeenAt (codes missing from a newer list keep their older LastSeenAt - they are not deleted).

CREATE TABLE IF NOT EXISTS "Stock_Symbol_Stage" (
    "SymbolStageId" INTEGER PRIMARY KEY AUTOINCREMENT,
    "Market"        TEXT NOT NULL,              -- the market the list was requested for: NYSE
    "Code"          TEXT NOT NULL,              -- "Code": the ticker on that market (MRK, BRK-B)
    "Name"          TEXT,                       -- "Name": company / fund name
    "Country"       TEXT,                       -- "Country"
    "Exchange"      TEXT,                       -- "Exchange": the venue (NYSE, NYSE ARCA, NYSE MKT ...)
    "Currency"      TEXT,                       -- "Currency"
    "Type"          TEXT,                       -- "Type": Common Stock, ETF, Preferred Stock, FUND ...
    "Isin"          TEXT,                       -- "Isin" (may be empty)
    "SourceFile"    TEXT NOT NULL,              -- the saved list the values came from
    "LoadId"        TEXT NOT NULL,              -- the load that saw the code last
    "FirstSeenAt"   TEXT NOT NULL,              -- first list that contained the code
    "LastSeenAt"    TEXT NOT NULL,              -- latest list that contained the code
    "UpdatedAt"     TEXT NOT NULL,              -- last change of a value
    CONSTRAINT "UQ_Stock_Symbol_Stage_Market_Code" UNIQUE ("Market", "Code")
);
CREATE INDEX IF NOT EXISTS "IX_Stock_Symbol_Stage_Code" ON "Stock_Symbol_Stage" ("Code");
CREATE INDEX IF NOT EXISTS "IX_Stock_Symbol_Stage_Market_Type" ON "Stock_Symbol_Stage" ("Market", "Type");
