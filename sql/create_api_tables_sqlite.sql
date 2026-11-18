-- Customer / data API tables of stock_dataload (created automatically by the
-- portal / `python -m stock_dataload init-db`; kept here for reference).
--
-- Api_Customer  one row per e-mail: an ACCOUNT (PasswordHash set) or a GUEST
--               who bought tokens without signing up (PasswordHash NULL)
-- Api_Key       the secret sent with every data API request, stored as a
--               sha-256 hash only; carries the TOKEN BALANCE
-- Api_Order     every purchase / manual grant: pack, tokens, amount (cents),
--               payment status + provider reference, the key credited
-- Api_Usage     the metering ledger: every charged or refused data API call

CREATE TABLE IF NOT EXISTS "Api_Customer" (
    "CustomerId"    INTEGER PRIMARY KEY AUTOINCREMENT,
    "Email"         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    "DisplayName"   TEXT,
    "PasswordHash"  TEXT,                       -- NULL = guest (bought without an account)
    "Role"          TEXT NOT NULL DEFAULT 'user',   -- user | admin (the portal's admin pages)
    "EmailVerified" INTEGER NOT NULL DEFAULT 0, -- 1 once the address was confirmed with a code
    "Phone"         TEXT,                       -- verified phone number (sms second factor)
    "Active"        INTEGER NOT NULL DEFAULT 1,
    "CreatedAt"     TEXT NOT NULL,
    "LastLoginAt"   TEXT
);
CREATE TABLE IF NOT EXISTS "Api_Key" (
    "ApiKeyId"        INTEGER PRIMARY KEY AUTOINCREMENT,
    "CustomerId"      INTEGER NOT NULL REFERENCES "Api_Customer" ("CustomerId"),
    "Label"           TEXT,
    "KeyPrefix"       TEXT NOT NULL,            -- first characters, for display only
    "KeyHash"         TEXT NOT NULL UNIQUE,     -- sha-256 of the key; the key itself is never stored
    "Balance"         INTEGER NOT NULL DEFAULT 0,   -- tokens left
    "TokensPurchased" INTEGER NOT NULL DEFAULT 0,
    "TokensUsed"      INTEGER NOT NULL DEFAULT 0,
    "Active"          INTEGER NOT NULL DEFAULT 1,
    "CreatedAt"       TEXT NOT NULL,
    "LastUsedAt"      TEXT,
    "RevokedAt"       TEXT,
    CONSTRAINT "CK_Api_Key_Balance" CHECK ("Balance" >= 0)
);
CREATE TABLE IF NOT EXISTS "Api_Order" (
    "OrderId"       INTEGER PRIMARY KEY AUTOINCREMENT,
    "OrderRef"      TEXT NOT NULL UNIQUE,       -- the reference the customer sees (ORD-XXXXXXXX)
    "CustomerId"    INTEGER REFERENCES "Api_Customer" ("CustomerId"),
    "ApiKeyId"      INTEGER REFERENCES "Api_Key" ("ApiKeyId"),  -- the key credited (set when paid)
    "Email"         TEXT NOT NULL,
    "Guest"         INTEGER NOT NULL DEFAULT 0, -- 1 = bought without an account
    "Pack"          TEXT NOT NULL,
    "Tokens"        INTEGER NOT NULL,
    "Amount"        INTEGER NOT NULL,           -- in the smallest currency unit (cents)
    "Currency"      TEXT NOT NULL,
    "Status"        TEXT NOT NULL,              -- pending | paid | failed | cancelled
    "Provider"      TEXT NOT NULL,              -- stripe | paypal | manual
    "ProviderRef"   TEXT,                       -- the provider's payment / session id
    "PaymentDetail" TEXT,                       -- e.g. "Visa ending 4242" (never the card number)
    "KeyLabel"      TEXT,                       -- label of the key to create when the order is paid
    "Note"          TEXT,
    "CreatedAt"     TEXT NOT NULL,
    "PaidAt"        TEXT,
    CONSTRAINT "CK_Api_Order_Tokens" CHECK ("Tokens" > 0),
    CONSTRAINT "CK_Api_Order_Amount" CHECK ("Amount" >= 0)
);
CREATE TABLE IF NOT EXISTS "Api_Usage" (
    "UsageId"     INTEGER PRIMARY KEY AUTOINCREMENT,
    "ApiKeyId"    INTEGER NOT NULL REFERENCES "Api_Key" ("ApiKeyId"),
    "RequestedAt" TEXT NOT NULL,
    "Endpoint"    TEXT NOT NULL,                -- daily | weekly | monthly
    "Tickers"     TEXT,                         -- comma separated
    "Year"        INTEGER,
    "Tokens"      INTEGER NOT NULL DEFAULT 0,   -- tokens charged (0 when refused)
    "Rows"        INTEGER NOT NULL DEFAULT 0,
    "Status"      TEXT NOT NULL,                -- ok | insufficient_tokens
    "RemoteAddr"  TEXT
);
CREATE INDEX IF NOT EXISTS "IX_Api_Key_Customer" ON "Api_Key" ("CustomerId");
CREATE INDEX IF NOT EXISTS "IX_Api_Order_Customer" ON "Api_Order" ("CustomerId");
CREATE INDEX IF NOT EXISTS "IX_Api_Order_Email" ON "Api_Order" ("Email");
CREATE INDEX IF NOT EXISTS "IX_Api_Usage_Key" ON "Api_Usage" ("ApiKeyId", "RequestedAt");
