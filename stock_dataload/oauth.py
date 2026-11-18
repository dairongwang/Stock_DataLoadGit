"""
Sign in with **Google**, **Apple** or **Facebook** (OAuth 2.0 / OpenID
Connect, authorization-code flow) for portal customers.

::

    /oauth/<provider>/start ──► provider's consent page ──► /oauth/<provider>/callback?code=..&state=..
                                                                │
                        exchange the code for tokens ◄──────────┘
                        fetch the profile (e-mail, name, subject)
                        find the linked account / link by verified e-mail / create the account
                        -> the normal sign-in continues (second factor if enabled)

* the ``state`` parameter is **signed** (HMAC with the app secret, with a
  timestamp) - CSRF protection that also survives Apple's cross-site
  ``form_post`` response, where no session cookie is sent;
* identities are stored in ``Auth_Identity`` (provider + subject, unique);
  an account can have several, and they can be connected / disconnected on
  the security page;
* an account created through a provider has no password (``PasswordHash =
  'external'``) - *Forgot your password* sets one if wanted.

``[oauth] gateway = fake`` (default) replaces the three identity providers
by :class:`FakeIdentityGateway`, an in-process STUB: the consent page is a
form served by the portal (``/fake-oauth/<provider>/authorize``) where any
e-mail / name can be typed in, and the token and profile endpoints are
answered in memory.  ``live`` talks to the real providers with the
credentials from ``[oauth]``.

Apple notes: Apple's token endpoint needs a *client secret* that is a JWT
signed (ES256) with the private key of your Sign in with Apple key; the
standard library cannot sign ES256, so the pre-generated JWT (valid up to
six months) is configured as ``apple_client_secret``.  The e-mail address
and subject come from the ``id_token`` Apple returns directly to the server
over TLS (so the payload is read without a second signature check); the
name is only delivered with the very first authorisation (``user`` field).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import time
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator, Mapping

from .config import Settings
from .httpclient import HttpError, Transport, UrllibTransport

log = logging.getLogger(__name__)

PROVIDER_GOOGLE = "google"
PROVIDER_APPLE = "apple"
PROVIDER_FACEBOOK = "facebook"
OAUTH_PROVIDERS = (PROVIDER_GOOGLE, PROVIDER_APPLE, PROVIDER_FACEBOOK)
PROVIDER_LABELS = {PROVIDER_GOOGLE: "Google", PROVIDER_APPLE: "Apple", PROVIDER_FACEBOOK: "Facebook"}

GATEWAY_LIVE = "live"
GATEWAY_FAKE = "fake"

IDENTITY_TABLE = "Auth_Identity"
STATE_MAX_AGE = 600          # seconds a sign-in attempt may take

IDENTITY_DDL = f"""
CREATE TABLE IF NOT EXISTS "{IDENTITY_TABLE}" (
    "IdentityId"    INTEGER PRIMARY KEY AUTOINCREMENT,
    "PrincipalType" TEXT NOT NULL,              -- customer
    "PrincipalId"   INTEGER NOT NULL,
    "Provider"      TEXT NOT NULL,              -- google | apple | facebook
    "Subject"       TEXT NOT NULL,              -- the provider's stable user id
    "Email"         TEXT,
    "Name"          TEXT,
    "CreatedAt"     TEXT NOT NULL,
    "LastLoginAt"   TEXT,
    CONSTRAINT "UQ_{IDENTITY_TABLE}_Provider_Subject" UNIQUE ("Provider", "Subject")
)
"""
IDENTITY_INDEX = f'CREATE INDEX IF NOT EXISTS "IX_{IDENTITY_TABLE}_Principal" ON "{IDENTITY_TABLE}" ("PrincipalType", "PrincipalId")'


def render_ddl() -> str:
    return "\n".join(s.strip() + ";" for s in (IDENTITY_DDL, IDENTITY_INDEX)) + "\n"


class OAuthError(Exception):
    """The sign-in with the provider failed (denied, bad code, no e-mail, provider down)."""


@dataclass
class Profile:
    provider: str
    subject: str
    email: str | None
    name: str | None
    email_verified: bool

    @property
    def label(self) -> str:
        return PROVIDER_LABELS.get(self.provider, self.provider)


@dataclass
class Identity:
    identity_id: int
    principal_type: str
    principal_id: int
    provider: str
    subject: str
    email: str | None
    name: str | None
    created_at: str
    last_login_at: str | None

    @property
    def label(self) -> str:
        return PROVIDER_LABELS.get(self.provider, self.provider)


# ---------------------------------------------------------------------------
# signed state (CSRF protection without a session)
# ---------------------------------------------------------------------------
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_state(secret: str, payload: Mapping[str, object]) -> str:
    body = dict(payload)
    body["n"] = secrets.token_hex(8)
    body["t"] = int(time.time())
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    return _b64(raw) + "." + _b64(sig)


def read_state(secret: str, state: str, max_age: int = STATE_MAX_AGE) -> dict | None:
    """The payload if *state* is genuine and younger than *max_age* seconds, else None."""
    try:
        raw_b64, sig_b64 = (state or "").split(".", 1)
        raw, sig = _unb64(raw_b64), _unb64(sig_b64)
    except (ValueError, TypeError):
        return None
    expected = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        body = json.loads(raw.decode("utf-8"))
    except ValueError:
        return None
    if not isinstance(body, dict) or time.time() - int(body.get("t", 0)) > max_age:
        return None
    return body


def decode_jwt_payload(token: str) -> dict:
    """The claims of a JWT **without** verifying its signature (only for tokens received
    directly from the issuer's token endpoint over TLS)."""
    try:
        return json.loads(_unb64(token.split(".")[1]).decode("utf-8"))
    except (IndexError, ValueError, TypeError) as exc:
        raise OAuthError("the identity provider returned an unreadable id token") from exc


# ---------------------------------------------------------------------------
# providers
# ---------------------------------------------------------------------------
class OAuthProvider:
    name = "abstract"
    label = "abstract"
    authorize_endpoint = ""
    token_endpoint = ""
    userinfo_endpoint = ""
    scope = ""
    #: Apple answers with an HTTP POST (form_post) instead of a redirect
    response_mode: str | None = None

    def __init__(self, client_id: str, client_secret: str, transport: Transport | None = None):
        if not client_id or not client_secret:
            raise OAuthError(f"{self.label}: client id and secret are required ([oauth] section)")
        self.client_id, self.client_secret = client_id, client_secret
        self.transport = transport or UrllibTransport()

    def authorize_url(self, redirect_uri: str, state: str) -> str:
        params = {"client_id": self.client_id, "redirect_uri": redirect_uri, "response_type": "code",
                  "scope": self.scope, "state": state}
        if self.response_mode:
            params["response_mode"] = self.response_mode
        return self.authorize_endpoint + "?" + urllib.parse.urlencode(params)

    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        try:
            return self.transport.request("POST", self.token_endpoint, {
                "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                "client_id": self.client_id, "client_secret": self.client_secret})
        except HttpError as exc:
            raise OAuthError(f"{self.label} did not accept the sign-in: {exc}") from exc

    def profile(self, tokens: Mapping[str, object], form: Mapping[str, str] | None = None) -> Profile:
        raise NotImplementedError

    def _get(self, url: str, access_token: str, params: dict | None = None) -> dict:
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        try:
            return self.transport.request("GET", url, None, {"Authorization": f"Bearer {access_token}"})
        except HttpError as exc:
            raise OAuthError(f"{self.label} did not return the profile: {exc}") from exc


class GoogleProvider(OAuthProvider):
    name, label = PROVIDER_GOOGLE, "Google"
    authorize_endpoint = "https://accounts.google.com/o/oauth2/v2/auth"
    token_endpoint = "https://oauth2.googleapis.com/token"
    userinfo_endpoint = "https://openidconnect.googleapis.com/v1/userinfo"
    scope = "openid email profile"

    def authorize_url(self, redirect_uri: str, state: str) -> str:
        return super().authorize_url(redirect_uri, state) + "&" + urllib.parse.urlencode(
            {"access_type": "online", "prompt": "select_account"})

    def profile(self, tokens, form=None) -> Profile:
        info = self._get(self.userinfo_endpoint, str(tokens.get("access_token", "")))
        if not info.get("sub"):
            raise OAuthError("Google did not return a user id")
        return Profile(self.name, str(info["sub"]), info.get("email"), info.get("name"),
                       bool(info.get("email_verified", False)))


class FacebookProvider(OAuthProvider):
    name, label = PROVIDER_FACEBOOK, "Facebook"
    authorize_endpoint = "https://www.facebook.com/v19.0/dialog/oauth"
    token_endpoint = "https://graph.facebook.com/v19.0/oauth/access_token"
    userinfo_endpoint = "https://graph.facebook.com/v19.0/me"
    scope = "email public_profile"

    def profile(self, tokens, form=None) -> Profile:
        info = self._get(self.userinfo_endpoint, str(tokens.get("access_token", "")), {"fields": "id,name,email"})
        if not info.get("id"):
            raise OAuthError("Facebook did not return a user id")
        # Facebook only hands out addresses it has verified
        return Profile(self.name, str(info["id"]), info.get("email"), info.get("name"), bool(info.get("email")))


class AppleProvider(OAuthProvider):
    name, label = PROVIDER_APPLE, "Apple"
    authorize_endpoint = "https://appleid.apple.com/auth/authorize"
    token_endpoint = "https://appleid.apple.com/auth/token"
    scope = "name email"
    response_mode = "form_post"

    def profile(self, tokens, form=None) -> Profile:
        claims = decode_jwt_payload(str(tokens.get("id_token", "")))
        if not claims.get("sub"):
            raise OAuthError("Apple did not return a user id")
        name = None
        try:                                     # only present at the first authorisation
            user = json.loads((form or {}).get("user") or "{}")
            parts = user.get("name") or {}
            name = " ".join(p for p in (parts.get("firstName"), parts.get("lastName")) if p) or None
        except ValueError:
            name = None
        verified = claims.get("email_verified")
        return Profile(self.name, str(claims["sub"]), claims.get("email"), name,
                       verified is True or str(verified).lower() == "true")


# ---------------------------------------------------------------------------
# the fake identity gateway (STUB of Google / Apple / Facebook)
# ---------------------------------------------------------------------------
@dataclass
class FakeGrant:
    provider: str
    code: str
    email: str
    name: str
    subject: str
    access_token: str
    used: bool = False


class FakeIdentityGateway(Transport):
    """Answers the token / profile endpoints of the three providers in memory and
    hands out authorization codes for whatever e-mail is typed on the portal's
    fake consent page.  Subjects are stable per provider + e-mail."""

    BASE = "https://fake.oauth.local"

    def __init__(self, consent_path: str = "/fake-oauth"):
        self.consent_path = consent_path.rstrip("/")
        self.grants: dict[str, FakeGrant] = {}
        self.fail_with: str | None = None
        self.calls: list[tuple[str, str]] = []

    def issue_code(self, provider: str, email: str, name: str = "") -> FakeGrant:
        subject = "fake-" + hashlib.sha256(f"{provider}:{email.lower()}".encode()).hexdigest()[:16]
        grant = FakeGrant(provider, "code_dummy_" + secrets.token_hex(8), email.strip(), name.strip(), subject,
                          "at_dummy_" + secrets.token_hex(8))
        self.grants[grant.code] = grant
        return grant

    def request(self, method, url, data=None, headers=None, json_body=False) -> dict:
        self.calls.append((method, url))
        if self.fail_with:
            raise HttpError(self.fail_with, 503)
        path = url[len(self.BASE):].split("?")[0]
        provider = path.strip("/").split("/")[0]
        if path.endswith("/token"):
            grant = self.grants.get((data or {}).get("code", ""))
            if grant is None or grant.used or grant.provider != provider:
                raise HttpError("invalid_grant", 400, {"error": "invalid_grant"})
            if (data or {}).get("client_secret") != f"dummy-fake-{provider}-secret":
                raise HttpError("invalid_client", 401, {"error": "invalid_client"})
            grant.used = True
            tokens = {"access_token": grant.access_token, "token_type": "Bearer", "expires_in": 3600}
            if provider == PROVIDER_APPLE:
                claims = {"iss": "https://appleid.apple.com", "sub": grant.subject, "email": grant.email,
                          "email_verified": "true", "aud": "dummy-fake-apple"}
                tokens["id_token"] = "e30." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".sig"
            return tokens
        if path.endswith("/userinfo") or path.endswith("/me"):
            token = (headers or {}).get("Authorization", "").replace("Bearer ", "")
            grant = next((g for g in self.grants.values() if g.access_token == token and g.provider == provider), None)
            if grant is None:
                raise HttpError("invalid_token", 401)
            if provider == PROVIDER_FACEBOOK:
                return {"id": grant.subject, "name": grant.name, "email": grant.email}
            return {"sub": grant.subject, "email": grant.email, "email_verified": True, "name": grant.name}
        raise HttpError(f"the fake identity gateway does not serve {url}", 404)


def _fake_provider(cls, gateway: FakeIdentityGateway):
    name = cls.name

    class Fake(cls):          # type: ignore[misc, valid-type]
        authorize_endpoint = f"{gateway.consent_path}/{name}/authorize"
        token_endpoint = f"{FakeIdentityGateway.BASE}/{name}/token"
        userinfo_endpoint = f"{FakeIdentityGateway.BASE}/{name}/" + ("me" if name == PROVIDER_FACEBOOK else "userinfo")

    Fake.__name__ = f"Fake{cls.__name__}"
    return Fake(f"dummy-fake-{name}", f"dummy-fake-{name}-secret", gateway)


def make_oauth_providers(settings: Settings, gateway: FakeIdentityGateway | None = None) -> dict[str, OAuthProvider]:
    """The providers of ``[oauth] providers`` - live, or all stubbed by the fake gateway."""
    classes = {PROVIDER_GOOGLE: GoogleProvider, PROVIDER_APPLE: AppleProvider, PROVIDER_FACEBOOK: FacebookProvider}
    creds = {PROVIDER_GOOGLE: (settings.google_client_id, settings.google_client_secret),
             PROVIDER_APPLE: (settings.apple_client_id, settings.apple_client_secret),
             PROVIDER_FACEBOOK: (settings.facebook_app_id, settings.facebook_app_secret)}
    fake = settings.oauth_gateway == GATEWAY_FAKE
    if fake and gateway is None:
        gateway = FakeIdentityGateway()
    out: dict[str, OAuthProvider] = {}
    for name in settings.oauth_providers:
        if fake:
            out[name] = _fake_provider(classes[name], gateway)
        else:
            out[name] = classes[name](*creds[name])
    return out


# ---------------------------------------------------------------------------
# linked identities
# ---------------------------------------------------------------------------
_COLS = ("IdentityId", "PrincipalType", "PrincipalId", "Provider", "Subject", "Email", "Name", "CreatedAt",
         "LastLoginAt")


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


class IdentityStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._ready = False

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.settings.sqlite_path), timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            if not self._ready:
                conn.execute(IDENTITY_DDL)
                conn.execute(IDENTITY_INDEX)
                self._ready = True
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def ensure_table(self) -> None:
        with self.connection():
            pass

    def _rows(self, conn, where: str, params: tuple) -> list[Identity]:
        cols = ", ".join(f'"{c}"' for c in _COLS)
        return [Identity(*r) for r in conn.execute(
            f'SELECT {cols} FROM "{IDENTITY_TABLE}" WHERE {where} ORDER BY "Provider"', params).fetchall()]

    def find(self, provider: str, subject: str) -> Identity | None:
        with self.connection() as conn:
            rows = self._rows(conn, '"Provider" = ? AND "Subject" = ?', (provider, subject))
        return rows[0] if rows else None

    def identities_of(self, kind: str, pid: int) -> list[Identity]:
        with self.connection() as conn:
            return self._rows(conn, '"PrincipalType" = ? AND "PrincipalId" = ?', (kind, int(pid)))

    def link(self, kind: str, pid: int, profile: Profile) -> Identity:
        existing = self.find(profile.provider, profile.subject)
        if existing is not None and (existing.principal_type, existing.principal_id) != (kind, int(pid)):
            raise OAuthError(f"this {profile.label} account is already connected to another account")
        with self.connection() as conn:
            conn.execute(
                f'INSERT INTO "{IDENTITY_TABLE}" ("PrincipalType","PrincipalId","Provider","Subject","Email","Name",'
                f'"CreatedAt","LastLoginAt") VALUES (?,?,?,?,?,?,?,?) '
                f'ON CONFLICT("Provider","Subject") DO UPDATE SET "Email" = excluded."Email", '
                f'"Name" = COALESCE(excluded."Name", "Name"), "LastLoginAt" = excluded."LastLoginAt"',
                (kind, int(pid), profile.provider, profile.subject, profile.email, profile.name, _now(), _now()))
        return self.find(profile.provider, profile.subject)

    def touch(self, identity_id: int) -> None:
        with self.connection() as conn:
            conn.execute(f'UPDATE "{IDENTITY_TABLE}" SET "LastLoginAt" = ? WHERE "IdentityId" = ?', (_now(), int(identity_id)))

    def unlink(self, kind: str, pid: int, provider: str) -> int:
        with self.connection() as conn:
            return conn.execute(f'DELETE FROM "{IDENTITY_TABLE}" WHERE "PrincipalType" = ? AND "PrincipalId" = ? '
                                f'AND "Provider" = ?', (kind, int(pid), provider)).rowcount

    def unlink_all(self, kind: str, pid: int) -> int:
        with self.connection() as conn:
            return conn.execute(f'DELETE FROM "{IDENTITY_TABLE}" WHERE "PrincipalType" = ? AND "PrincipalId" = ?',
                                (kind, int(pid))).rowcount
