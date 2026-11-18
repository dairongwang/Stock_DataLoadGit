# Accounts and security

Two kinds of principals share one security implementation:

| Principal | Table | Signs in at | Roles |
|-----------|-------|-------------|-------|
| **staff** | `App_User` (`auth.UserStore`) | the dashboard | viewer < operator < admin |
| **customer** | `Api_Customer` (`customers.CustomerStore`) | the portal | user, admin (the portal's admin pages) |

`accounts.TwoFactorService` keys everything by `(PrincipalType, PrincipalId)`
so second factors, codes and recovery codes work the same for both.

## Passwords

* Only a salted **PBKDF2-HMAC-SHA256** hash is stored
  (`pbkdf2_sha256$<iterations>$<salt>$<hash>`, 600,000 iterations, random
  16-byte salt); verification is constant-time (`hmac.compare_digest`).
* Minimum length 8. Changing a password requires the current one.
* Accounts created through Google / Apple / Facebook store the marker
  `external` instead of a hash - they have no password until they set one
  with a reset link. Portal **guests** (bought without an account) have
  `PasswordHash NULL` and cannot sign in.
* The initial dashboard administrator gets a random one-time password
  printed to the console and must change it at the first sign-in.

## Sessions and CSRF

* Flask's signed session cookie, `HttpOnly`, `SameSite=Lax`; the portal's
  cookie is `Secure` when `public_url` is `https://`. The signing secret is
  `$STOCK_DATALOAD_SECRET` or generated once into the git-ignored
  `data/.secret_key` - never shipped.
* The session holds only the principal id; it is cleared on sign-in and
  sign-out (no fixation). A pending e-mail verification or second factor is
  a separate session key - the account is not signed in until it is passed.
* The portal checks a per-session **CSRF token** on every form post (except
  the OAuth callback, which is protected by the signed `state`).

## Two-factor authentication

| Method | Implementation |
|--------|----------------|
| Authenticator app | RFC 6238 TOTP: SHA-1, 6 digits, 30-second steps, ±1 step for clock drift, a 160-bit base32 secret; enabled only after a first valid code |
| E-mail / SMS code | a random 6-digit code, stored as a SHA-256 hash with an expiry (`code_ttl_minutes`), at most 5 attempts, a new code every 60 seconds at most |
| Recovery codes | ten single-use codes, hashed, shown once when generated |

Portal administrators must have a second factor before the admin pages
open (`[accounts] admin_requires_2fa`).

## E-mail verification and password reset

Self-registered dashboard users and new portal accounts confirm their
address with a code before the first sign-in. A password reset e-mails a
single-use URL-safe token (hashed in `Auth_Code`, valid
`reset_ttl_minutes`); the *forgot password* answer is the same whether the
address is known or not, so accounts cannot be enumerated.

## Sign in with Google / Apple / Facebook

The OAuth 2.0 / OpenID Connect authorization-code flow (`oauth.py`). The
`state` parameter is an HMAC-signed, time-limited payload (provider,
target page, optional "link to this account"), so a callback cannot be
forged or replayed later. Identities are stored in `Auth_Identity`
(provider + stable subject). A provider login is matched to an existing
account only if the provider verified the e-mail address.

## Roles and authorization

* Dashboard: `required_role(endpoint)` maps every endpoint to viewer /
  operator / admin; denials are logged (`access_denied`) and answered 403.
* Portal: `login_required` for the account pages, `admin_required` (role
  admin + 2FA) for `/admin/...`.

## API keys

Data API keys are `sdl_` + 32 random URL-safe characters; only the SHA-256
hash and a 12-character display prefix are stored, so a database leak does
not leak keys. Rotation replaces the secret and keeps the balance.

## Secrets in configuration

Every secret of `config.ini` can come from an environment variable instead
([Configuration](../operations/10-configuration.md)). The EOD API token is
never written to logs: URLs are logged as `...?api_token=***&fmt=json`.
