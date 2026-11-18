-- The stock lists of the chart dashboard (stock_chart/lists.py creates them on first use;
-- kept here for reference - generated from the module, see tests/test_lists.py).
-- Chart_List       the watchlist, the Selected list and the screen lists (per owner)
-- Chart_List_Item  the tickers of a list, in list order

CREATE TABLE IF NOT EXISTS "Chart_List" (
    "ListId"      INTEGER PRIMARY KEY AUTOINCREMENT,
    "Kind"        TEXT NOT NULL,                -- watch | selected | screen
    "Name"        TEXT NOT NULL,
    "Owner"       TEXT NOT NULL DEFAULT '',     -- the basic-auth user ('' = shared)
    "SourceFile"  TEXT,                         -- screen lists: the file they were loaded from
    "Revision"    INTEGER NOT NULL DEFAULT 0,   -- +1 with every change (0 = never edited)
    "CreatedAt"   TEXT NOT NULL,
    "UpdatedAt"   TEXT NOT NULL,
    CONSTRAINT "UQ_Chart_List_Owner_Kind_Name" UNIQUE ("Owner", "Kind", "Name"),
    CONSTRAINT "CK_Chart_List_Kind" CHECK ("Kind" IN ('watch', 'selected', 'screen'))
);
CREATE TABLE IF NOT EXISTS "Chart_List_Item" (
    "ListId"      INTEGER NOT NULL REFERENCES "Chart_List" ("ListId") ON DELETE CASCADE,
    "Position"    INTEGER NOT NULL,             -- the order of the list (gaps allowed)
    "Ticker"      TEXT NOT NULL,
    "AddedAt"     TEXT NOT NULL,
    CONSTRAINT "PK_Chart_List_Item" PRIMARY KEY ("ListId", "Ticker")
);
CREATE INDEX IF NOT EXISTS "IX_Chart_List_Item_Position" ON "Chart_List_Item" ("ListId", "Position");
