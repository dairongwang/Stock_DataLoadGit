-- Account-security tables of stock_dataload (created automatically by the
-- dashboard / portal / `python -m stock_dataload init-db`; kept here for reference).
-- Shared by staff users (App_User, PrincipalType 'staff') and portal customers
-- (Api_Customer, PrincipalType 'customer').
--
-- Auth_TwoFactor     second factors: totp (authenticator app secret), email, sms (destination)
-- Auth_Code          one-time codes (sign-in, enrolment, e-mail verification) and password-reset
--                    tokens - sha-256 hashes with expiry, attempt counter and consumed flag
-- Auth_RecoveryCode  single-use recovery codes (hashed)
-- Auth_Identity      Google / Apple / Facebook identities linked to customer accounts

CREATE TABLE IF NOT EXISTS "Auth_TwoFactor" (
    "TwoFactorId"   INTEGER PRIMARY KEY AUTOINCREMENT,
    "PrincipalType" TEXT NOT NULL,              -- staff | customer
    "PrincipalId"   INTEGER NOT NULL,
    "Method"        TEXT NOT NULL,              -- totp | email | sms
    "Secret"        TEXT,                       -- totp: base32 secret
    "Destination"   TEXT,                       -- email address / phone number the codes go to
    "Enabled"       INTEGER NOT NULL DEFAULT 0, -- 1 once confirmed with a first code
    "ConfirmedAt"   TEXT,
    "LastUsedAt"    TEXT,
    "CreatedAt"     TEXT NOT NULL,
    CONSTRAINT "UQ_Auth_TwoFactor" UNIQUE ("PrincipalType", "PrincipalId", "Method")
);
CREATE TABLE IF NOT EXISTS "Auth_Code" (
    "CodeId"        INTEGER PRIMARY KEY AUTOINCREMENT,
    "PrincipalType" TEXT NOT NULL,
    "PrincipalId"   INTEGER NOT NULL,
    "Purpose"       TEXT NOT NULL,              -- login | enrol | verify_email | reset
    "Channel"       TEXT NOT NULL,              -- email | sms
    "Destination"   TEXT NOT NULL,
    "CodeHash"      TEXT NOT NULL,              -- sha-256 of the code / token
    "ExpiresAt"     TEXT NOT NULL,
    "Attempts"      INTEGER NOT NULL DEFAULT 0,
    "ConsumedAt"    TEXT,
    "CreatedAt"     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS "Auth_RecoveryCode" (
    "RecoveryId"    INTEGER PRIMARY KEY AUTOINCREMENT,
    "PrincipalType" TEXT NOT NULL,
    "PrincipalId"   INTEGER NOT NULL,
    "CodeHash"      TEXT NOT NULL,
    "UsedAt"        TEXT,
    "CreatedAt"     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS "IX_Auth_Code_Principal" ON "Auth_Code" ("PrincipalType", "PrincipalId", "Purpose");
CREATE INDEX IF NOT EXISTS "IX_Auth_Code_Hash" ON "Auth_Code" ("CodeHash");
CREATE INDEX IF NOT EXISTS "IX_Auth_RecoveryCode_Principal" ON "Auth_RecoveryCode" ("PrincipalType", "PrincipalId");
CREATE TABLE IF NOT EXISTS "Auth_Identity" (
    "IdentityId"    INTEGER PRIMARY KEY AUTOINCREMENT,
    "PrincipalType" TEXT NOT NULL,              -- customer
    "PrincipalId"   INTEGER NOT NULL,
    "Provider"      TEXT NOT NULL,              -- google | apple | facebook
    "Subject"       TEXT NOT NULL,              -- the provider's stable user id
    "Email"         TEXT,
    "Name"          TEXT,
    "CreatedAt"     TEXT NOT NULL,
    "LastLoginAt"   TEXT,
    CONSTRAINT "UQ_Auth_Identity_Provider_Subject" UNIQUE ("Provider", "Subject")
);
CREATE INDEX IF NOT EXISTS "IX_Auth_Identity_Principal" ON "Auth_Identity" ("PrincipalType", "PrincipalId");
