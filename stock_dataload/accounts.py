"""
Account security shared by **staff users** (dashboard, ``App_User``) and
**portal customers** (``Api_Customer``): two-factor authentication, e-mail
verification and password-reset links.

Everything is keyed by ``(PrincipalType, PrincipalId)`` - ``staff`` /
``customer`` plus the id in the respective table - so one implementation
serves both applications.

Tables (reference script: ``sql/create_auth_tables_sqlite.sql``)::

    Auth_TwoFactor     the second factors: totp (authenticator app secret), email, sms (destination)
    Auth_Code          one-time codes (sign-in, enrolment, e-mail verification) and password-reset
                       tokens - sha-256 hashes with expiry, attempt counter and consumed flag
    Auth_RecoveryCode  single-use recovery codes (hashed)

Second factors
--------------
``totp``      RFC 6238: HMAC-SHA1, 6 digits, 30-second steps, +-1 step for clock drift, a
              160-bit base32 secret; enabled only after a first valid code
``email``     a random 6-digit code mailed to the (verified) address
``sms``       a random 6-digit code sent by text message to a confirmed phone number
``recovery``  ten single-use codes ``xxxxx-xxxxx``, shown once when generated

Codes are valid ``[accounts] code_ttl_minutes`` (10), may be tried
:data:`MAX_ATTEMPTS` (5) times and a new one is sent at most every
:data:`RESEND_SECONDS` (60) - earlier requests raise :class:`CooldownError`.
Reset tokens are 256-bit URL-safe strings valid ``reset_ttl_minutes`` (60).
Only hashes are stored; the plain codes exist in the message only.

Messages go through :mod:`messaging` (``[mail]`` / ``[sms]``); a sending
problem is reported as :class:`AccountError` ("... could not be sent").
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import sqlite3
import struct
import time
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Iterator

from .config import Settings
from .messaging import (EmailSender, MessagingError, SmsSender, mask_email, mask_phone, normalise_phone,
                        safe_email_sender, safe_sms_sender)

log = logging.getLogger(__name__)

PRINCIPAL_STAFF = "staff"
PRINCIPAL_CUSTOMER = "customer"
PRINCIPAL_TYPES = (PRINCIPAL_STAFF, PRINCIPAL_CUSTOMER)

METHOD_TOTP = "totp"
METHOD_EMAIL = "email"
METHOD_SMS = "sms"
METHOD_RECOVERY = "recovery"
#: the second factors that can be enabled (recovery codes come with them)
METHODS: tuple[str, ...] = (METHOD_TOTP, METHOD_EMAIL, METHOD_SMS)
METHOD_LABELS = {
    METHOD_TOTP: "Authenticator app",
    METHOD_EMAIL: "E-mail code",
    METHOD_SMS: "Text message (SMS)",
    METHOD_RECOVERY: "Recovery code",
}

PURPOSE_LOGIN = "login"
PURPOSE_ENROL = "enrol"
PURPOSE_VERIFY_EMAIL = "verify_email"
PURPOSE_RESET = "reset"
PURPOSES = (PURPOSE_LOGIN, PURPOSE_ENROL, PURPOSE_VERIFY_EMAIL, PURPOSE_RESET)

#: wrong entries allowed per code
MAX_ATTEMPTS = 5
#: seconds before another code of the same kind is sent
RESEND_SECONDS = 60
RECOVERY_CODE_COUNT = 10
_RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"     # no i, l, o, 0, 1

TOTP_DIGITS = 6
TOTP_STEP = 30
TOTP_WINDOW = 1
TOTP_SECRET_BYTES = 20                                   # 160 bits

TWO_FACTOR_TABLE = "Auth_TwoFactor"
CODE_TABLE = "Auth_Code"
RECOVERY_TABLE = "Auth_RecoveryCode"

TWO_FACTOR_DDL = f"""
CREATE TABLE IF NOT EXISTS "{TWO_FACTOR_TABLE}" (
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
    CONSTRAINT "UQ_{TWO_FACTOR_TABLE}" UNIQUE ("PrincipalType", "PrincipalId", "Method")
)
"""

CODE_DDL = f"""
CREATE TABLE IF NOT EXISTS "{CODE_TABLE}" (
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
)
"""

RECOVERY_DDL = f"""
CREATE TABLE IF NOT EXISTS "{RECOVERY_TABLE}" (
    "RecoveryId"    INTEGER PRIMARY KEY AUTOINCREMENT,
    "PrincipalType" TEXT NOT NULL,
    "PrincipalId"   INTEGER NOT NULL,
    "CodeHash"      TEXT NOT NULL,
    "UsedAt"        TEXT,
    "CreatedAt"     TEXT NOT NULL
)
"""

INDEXES = (
    f'CREATE INDEX IF NOT EXISTS "IX_{CODE_TABLE}_Principal" ON "{CODE_TABLE}" ("PrincipalType", "PrincipalId", "Purpose")',
    f'CREATE INDEX IF NOT EXISTS "IX_{CODE_TABLE}_Hash" ON "{CODE_TABLE}" ("CodeHash")',
    f'CREATE INDEX IF NOT EXISTS "IX_{RECOVERY_TABLE}_Principal" ON "{RECOVERY_TABLE}" ("PrincipalType", "PrincipalId")',
)


def render_ddl() -> str:
    """The DDL of the three tables as a script (``sql/create_auth_tables_sqlite.sql``,
    followed by :func:`stock_dataload.oauth.render_ddl`)."""
    return "\n".join(s.strip() + ";" for s in (TWO_FACTOR_DDL, CODE_DDL, RECOVERY_DDL, *INDEXES)) + "\n"


class AccountError(Exception):
    """The account-security action is not possible (unknown method, no address, message not sent, ...)."""


class CooldownError(AccountError):
    """A code was sent moments ago - it is still valid; a new one can be requested after the cooldown."""


# ---------------------------------------------------------------------------
# TOTP (RFC 4226 / RFC 6238)
# ---------------------------------------------------------------------------
def generate_secret() -> str:
    """A random 160-bit TOTP secret, base32 (32 characters, no padding)."""
    return base64.b32encode(secrets.token_bytes(TOTP_SECRET_BYTES)).decode("ascii").rstrip("=")


def _secret_bytes(secret: str) -> bytes:
    cleaned = "".join((secret or "").split()).upper()
    return base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))


def hotp(secret: str, counter: int, digits: int = TOTP_DIGITS) -> str:
    """RFC 4226 HMAC-SHA1 one-time password of *counter*."""
    digest = hmac.new(_secret_bytes(secret), struct.pack(">Q", int(counter)), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** digits)).zfill(digits)


def totp(secret: str, at: float | None = None, step: int = TOTP_STEP, digits: int = TOTP_DIGITS) -> str:
    """RFC 6238 code of *secret* at unix time *at* (default: now)."""
    moment = time.time() if at is None else at
    return hotp(secret, int(moment // step), digits)


def verify_totp(secret: str, code: str | None, at: float | None = None, window: int = TOTP_WINDOW,
                step: int = TOTP_STEP, digits: int = TOTP_DIGITS) -> bool:
    """True if *code* (spaces allowed) is the code of the current step or of up to *window*
    steps before / after it (clock drift)."""
    cleaned = "".join((code or "").split())
    if len(cleaned) != digits or not cleaned.isdigit() or not secret:
        return False
    moment = time.time() if at is None else at
    counter = int(moment // step)
    try:
        return any(hmac.compare_digest(hotp(secret, counter + delta, digits), cleaned)
                   for delta in range(-window, window + 1))
    except (ValueError, TypeError):            # a broken secret never verifies
        return False


def otpauth_uri(secret: str, account: str, issuer: str) -> str:
    """The ``otpauth://`` URI authenticator apps import (also the content of the QR code)."""
    label = urllib.parse.quote(f"{issuer}:{account}", safe="")
    query = urllib.parse.urlencode({"secret": secret, "issuer": issuer, "algorithm": "SHA1",
                                    "digits": TOTP_DIGITS, "period": TOTP_STEP}, quote_via=urllib.parse.quote)
    return f"otpauth://totp/{label}?{query}"


def format_secret(secret: str | None) -> str:
    """``JBSWY3DPEHPK3PXP...`` -> ``JBSW Y3DP EHPK 3PXP ...`` (easier to type in)."""
    cleaned = "".join((secret or "").split())
    return " ".join(cleaned[i:i + 4] for i in range(0, len(cleaned), 4))


def qr_svg(uri: str | None) -> str | None:
    """An SVG QR code of *uri* when the optional ``qrcode`` package is installed, else None
    (the pages then show the key for manual entry only)."""
    if not uri:
        return None
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:
        return None
    try:
        image = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
        svg = image.to_string()
        return svg.decode("utf-8") if isinstance(svg, bytes) else str(svg)
    except Exception as exc:  # noqa: BLE001 - a QR code is a convenience, never a failure
        log.warning("QR code could not be drawn: %s", exc)
        return None


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------
@dataclass
class Principal:
    """Who a code / factor belongs to: a staff user or a portal customer."""

    kind: str                     # staff | customer
    id: int
    email: str | None = None
    phone: str | None = None
    name: str | None = None

    @property
    def label(self) -> str:
        return self.email or self.name or f"{self.kind} {self.id}"


_METHOD_COLS = ("TwoFactorId", "PrincipalType", "PrincipalId", "Method", "Secret", "Destination", "Enabled",
                "ConfirmedAt", "LastUsedAt", "CreatedAt")


@dataclass
class TwoFactorMethod:
    two_factor_id: int
    principal_type: str
    principal_id: int
    method: str
    secret: str | None
    destination: str | None
    enabled: int
    confirmed_at: str | None
    last_used_at: str | None
    created_at: str

    @property
    def is_enabled(self) -> bool:
        return bool(self.enabled)

    @property
    def label(self) -> str:
        return METHOD_LABELS.get(self.method, self.method)

    @property
    def masked_destination(self) -> str | None:
        if not self.destination:
            return None
        return mask_phone(self.destination) if self.method == METHOD_SMS else mask_email(self.destination)


def _now_dt() -> datetime:
    return datetime.now().replace(microsecond=0)


def _fmt(moment: datetime) -> str:
    return moment.isoformat(sep=" ")


def _now() -> str:
    return _fmt(_now_dt())


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _code_hash(kind: str, pid: int, purpose: str, code: str) -> str:
    """Short codes are hashed together with their owner and purpose."""
    return _digest(f"{kind}:{int(pid)}:{purpose}:{code}")


def _clean_code(code: str | None) -> str:
    return "".join((code or "").split())


def _normalise_recovery(code: str | None) -> str:
    cleaned = "".join(ch for ch in (code or "").lower() if ch.isalnum())
    return f"{cleaned[:5]}-{cleaned[5:]}" if len(cleaned) == 10 else cleaned


def generate_recovery_code() -> str:
    raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(10))
    return f"{raw[:5]}-{raw[5:]}"


# ---------------------------------------------------------------------------
# the service
# ---------------------------------------------------------------------------
class TwoFactorService:
    """Second factors, one-time codes, recovery codes, e-mail verification and reset tokens.

    *email* / *sms* default to the senders configured in ``[mail]`` / ``[sms]``
    (a misconfigured provider becomes a :class:`~stock_dataload.messaging.BrokenSender`).
    """

    def __init__(self, settings: Settings, email: EmailSender | None = None, sms: SmsSender | None = None):
        self.settings = settings
        self.email = email if email is not None else safe_email_sender(settings)
        self.sms = sms if sms is not None else safe_sms_sender(settings)
        self._ready = False

    # -- storage --------------------------------------------------------------------
    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        path = self.settings.sqlite_path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            if not self._ready:
                for ddl in (TWO_FACTOR_DDL, CODE_DDL, RECOVERY_DDL, *INDEXES):
                    conn.execute(ddl)
                self._ready = True
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def ensure_tables(self) -> None:
        with self.connection():
            pass

    @property
    def issuer(self) -> str:
        return getattr(self.settings, "issuer", "") or "Stock Data API"

    @property
    def code_ttl(self) -> int:
        return int(getattr(self.settings, "code_ttl_minutes", 10) or 10)

    @property
    def reset_ttl(self) -> int:
        return int(getattr(self.settings, "reset_ttl_minutes", 60) or 60)

    # -- second factors --------------------------------------------------------------------
    @staticmethod
    def _methods(conn, where: str, params: tuple) -> list[TwoFactorMethod]:
        cols = ", ".join(f'"{c}"' for c in _METHOD_COLS)
        rows = [TwoFactorMethod(*r) for r in conn.execute(
            f'SELECT {cols} FROM "{TWO_FACTOR_TABLE}" WHERE {where}', params).fetchall()]
        order = {m: i for i, m in enumerate(METHODS)}
        return sorted(rows, key=lambda m: order.get(m.method, len(order)))

    def methods(self, kind: str, pid: int, enabled_only: bool = True) -> list[TwoFactorMethod]:
        """The second factors of a principal (enabled ones only by default), totp / email / sms order."""
        where = '"PrincipalType" = ? AND "PrincipalId" = ?' + (' AND "Enabled" = 1' if enabled_only else "")
        with self.connection() as conn:
            return self._methods(conn, where, (kind, int(pid)))

    def get_method(self, kind: str, pid: int, method: str) -> TwoFactorMethod | None:
        """The row of one method, enabled or still being set up."""
        with self.connection() as conn:
            rows = self._methods(conn, '"PrincipalType" = ? AND "PrincipalId" = ? AND "Method" = ?',
                                 (kind, int(pid), method))
        return rows[0] if rows else None

    def has_two_factor(self, kind: str, pid: int) -> bool:
        return bool(self.methods(kind, pid))

    @staticmethod
    def _upsert_method(conn, kind: str, pid: int, method: str, secret: str | None, destination: str | None,
                       enabled: bool) -> None:
        now = _now()
        conn.execute(
            f'INSERT INTO "{TWO_FACTOR_TABLE}" ("PrincipalType","PrincipalId","Method","Secret","Destination",'
            f'"Enabled","ConfirmedAt","LastUsedAt","CreatedAt") VALUES (?,?,?,?,?,?,?,?,?) '
            f'ON CONFLICT("PrincipalType","PrincipalId","Method") DO UPDATE SET "Secret" = excluded."Secret", '
            f'"Destination" = excluded."Destination", "Enabled" = excluded."Enabled", '
            f'"ConfirmedAt" = excluded."ConfirmedAt", "LastUsedAt" = excluded."LastUsedAt", '
            f'"CreatedAt" = excluded."CreatedAt"',
            (kind, int(pid), method, secret, destination, 1 if enabled else 0, now if enabled else None,
             now if enabled else None, now))

    def _touch(self, kind: str, pid: int, method: str) -> None:
        with self.connection() as conn:
            conn.execute(f'UPDATE "{TWO_FACTOR_TABLE}" SET "LastUsedAt" = ? WHERE "PrincipalType" = ? AND '
                         f'"PrincipalId" = ? AND "Method" = ?', (_now(), kind, int(pid), method))

    def begin_totp(self, principal: Principal) -> tuple[str, str]:
        """Start setting up an authenticator app: a new secret (not enabled until
        :meth:`confirm_totp`).  Returns the secret and the ``otpauth://`` URI."""
        secret = generate_secret()
        with self.connection() as conn:
            self._upsert_method(conn, principal.kind, principal.id, METHOD_TOTP, secret, None, enabled=False)
        return secret, otpauth_uri(secret, principal.label, self.issuer)

    def confirm_totp(self, kind: str, pid: int, code: str) -> bool:
        """Enable the authenticator app with a first valid code."""
        row = self.get_method(kind, pid, METHOD_TOTP)
        if row is None or not row.secret or not verify_totp(row.secret, code):
            return False
        now = _now()
        with self.connection() as conn:
            conn.execute(f'UPDATE "{TWO_FACTOR_TABLE}" SET "Enabled" = 1, "ConfirmedAt" = ?, "LastUsedAt" = ? '
                         f'WHERE "TwoFactorId" = ?', (now, now, row.two_factor_id))
        log.info("authenticator app enabled for %s %s", kind, pid)
        return True

    def begin_code_method(self, principal: Principal, method: str, phone: str | None = None) -> str:
        """Start enabling e-mail or sms codes: sends a code to the address / phone number.
        Returns the masked destination; :meth:`confirm_code_method` finishes."""
        if method == METHOD_EMAIL:
            if not principal.email:
                raise AccountError("save an e-mail address first")
            destination = principal.email
        elif method == METHOD_SMS:
            try:
                destination = normalise_phone(phone or principal.phone)
            except MessagingError as exc:
                raise AccountError(str(exc)) from exc
        else:
            raise AccountError(f"unknown second factor '{method}' - use {METHOD_EMAIL} or {METHOD_SMS}")
        return self._send_code(principal.kind, principal.id, PURPOSE_ENROL, method, destination)

    def confirm_code_method(self, kind: str, pid: int, method: str, code: str) -> bool:
        """Enable e-mail / sms codes with the code sent by :meth:`begin_code_method`."""
        if method not in (METHOD_EMAIL, METHOD_SMS):
            return False
        found = self._check_code(kind, pid, PURPOSE_ENROL, code, channel=method)
        if found is None:
            return False
        with self.connection() as conn:
            self._upsert_method(conn, kind, pid, method, None, found, enabled=True)
        log.info("%s codes enabled for %s %s", method, kind, pid)
        return True

    def disable(self, kind: str, pid: int, method: str) -> int:
        """Remove one second factor; with the last one the recovery codes go too."""
        with self.connection() as conn:
            n = conn.execute(f'DELETE FROM "{TWO_FACTOR_TABLE}" WHERE "PrincipalType" = ? AND "PrincipalId" = ? '
                             f'AND "Method" = ?', (kind, int(pid), method)).rowcount
            left = conn.execute(f'SELECT COUNT(*) FROM "{TWO_FACTOR_TABLE}" WHERE "PrincipalType" = ? AND '
                                f'"PrincipalId" = ? AND "Enabled" = 1', (kind, int(pid))).fetchone()[0]
            if not left:
                conn.execute(f'DELETE FROM "{RECOVERY_TABLE}" WHERE "PrincipalType" = ? AND "PrincipalId" = ?',
                             (kind, int(pid)))
        return n

    def disable_all(self, kind: str, pid: int) -> int:
        """Remove every second factor, the recovery codes and open sign-in / enrolment codes
        (an administrator resets a locked-out account).  Returns the number of methods removed."""
        with self.connection() as conn:
            n = conn.execute(f'DELETE FROM "{TWO_FACTOR_TABLE}" WHERE "PrincipalType" = ? AND "PrincipalId" = ?',
                             (kind, int(pid))).rowcount
            conn.execute(f'DELETE FROM "{RECOVERY_TABLE}" WHERE "PrincipalType" = ? AND "PrincipalId" = ?',
                         (kind, int(pid)))
            conn.execute(f'DELETE FROM "{CODE_TABLE}" WHERE "PrincipalType" = ? AND "PrincipalId" = ? AND '
                         f'"Purpose" IN (?, ?)', (kind, int(pid), PURPOSE_LOGIN, PURPOSE_ENROL))
        log.info("two-factor authentication of %s %s reset (%d method(s))", kind, pid, n)
        return n

    # -- one-time codes ---------------------------------------------------------------
    def _latest_code(self, conn, kind: str, pid: int, purpose: str, channel: str | None = None):
        """The newest usable code (not consumed, not expired, attempts left)."""
        sql = (f'SELECT "CodeId", "Channel", "Destination", "CodeHash", "CreatedAt" FROM "{CODE_TABLE}" '
               f'WHERE "PrincipalType" = ? AND "PrincipalId" = ? AND "Purpose" = ? AND "ConsumedAt" IS NULL '
               f'AND "ExpiresAt" > ? AND "Attempts" < ?')
        params: list = [kind, int(pid), purpose, _now(), MAX_ATTEMPTS]
        if channel:
            sql += ' AND "Channel" = ?'
            params.append(channel)
        return conn.execute(sql + ' ORDER BY "CodeId" DESC LIMIT 1', params).fetchone()

    def pending_code(self, kind: str, pid: int, purpose: str) -> tuple[str, str] | None:
        """``(channel, masked destination)`` of a code that was sent and can still be entered."""
        with self.connection() as conn:
            row = self._latest_code(conn, kind, pid, purpose)
        if row is None:
            return None
        channel, destination = row[1], row[2]
        return channel, (mask_phone(destination) if channel == METHOD_SMS else mask_email(destination))

    def _check_cooldown(self, conn, kind: str, pid: int, purpose: str, channel: str) -> None:
        row = self._latest_code(conn, kind, pid, purpose, channel)
        if row is None:
            return
        try:
            age = (_now_dt() - datetime.fromisoformat(row[4])).total_seconds()
        except ValueError:
            return
        if age < RESEND_SECONDS:
            wait = int(RESEND_SECONDS - age) or 1
            raise CooldownError(f"a code was sent moments ago - it is still valid; a new one can be sent in "
                                f"{wait} second(s)")

    def _message(self, purpose: str, channel: str, code: str) -> tuple[str, str]:
        issuer, ttl = self.issuer, self.code_ttl
        what = {PURPOSE_LOGIN: "sign-in code", PURPOSE_ENROL: "confirmation code",
                PURPOSE_VERIFY_EMAIL: "verification code"}.get(purpose, "code")
        if channel == METHOD_SMS:
            return "", f"{code} is your {issuer} {what}. It is valid for {ttl} minutes."
        intro = {
            PURPOSE_LOGIN: "Use this code to finish signing in:",
            PURPOSE_ENROL: "Use this code to turn on e-mail codes for two-factor authentication:",
            PURPOSE_VERIFY_EMAIL: "Use this code to confirm your e-mail address:",
        }.get(purpose, "Your code:")
        body = (f"Hello,\n\n{intro}\n\n    {code}\n\nThe code is valid for {ttl} minutes. If you did not ask for it, "
                f"you can ignore this message - somebody may have typed your address by mistake.\n\n-- {issuer}\n")
        return f"{issuer}: your {what} {code}", body

    def _send_code(self, kind: str, pid: int, purpose: str, channel: str, destination: str) -> str:
        code = f"{secrets.randbelow(10 ** 6):06d}"
        now = _now_dt()
        with self.connection() as conn:
            self._check_cooldown(conn, kind, pid, purpose, channel)
            # a new code replaces the ones sent before for the same purpose through the same channel
            conn.execute(f'UPDATE "{CODE_TABLE}" SET "ExpiresAt" = ? WHERE "PrincipalType" = ? AND "PrincipalId" = ? '
                         f'AND "Purpose" = ? AND "Channel" = ? AND "ConsumedAt" IS NULL AND "ExpiresAt" > ?',
                         (_fmt(now), kind, int(pid), purpose, channel, _fmt(now)))
            cur = conn.execute(
                f'INSERT INTO "{CODE_TABLE}" ("PrincipalType","PrincipalId","Purpose","Channel","Destination",'
                f'"CodeHash","ExpiresAt","Attempts","CreatedAt") VALUES (?,?,?,?,?,?,?,0,?)',
                (kind, int(pid), purpose, channel, destination, _code_hash(kind, pid, purpose, code),
                 _fmt(now + timedelta(minutes=self.code_ttl)), _fmt(now)))
            code_id = cur.lastrowid
        subject, body = self._message(purpose, channel, code)
        masked = mask_phone(destination) if channel == METHOD_SMS else mask_email(destination)
        try:
            if channel == METHOD_SMS:
                self.sms.send(destination, body)
            else:
                self.email.send(destination, subject, body)
        except MessagingError as exc:
            with self.connection() as conn:
                conn.execute(f'DELETE FROM "{CODE_TABLE}" WHERE "CodeId" = ?', (code_id,))
            log.warning("%s code for %s %s could not be sent: %s", purpose, kind, pid, exc)
            raise AccountError(f"the code could not be sent to {masked}: {exc}") from exc
        log.info("%s code sent to %s %s via %s (%s)", purpose, kind, pid, channel, masked)
        return masked

    def _check_code(self, kind: str, pid: int, purpose: str, code: str, channel: str | None = None) -> str | None:
        """Consume the code if it is right; a wrong one counts as an attempt.  Returns the
        destination the code was sent to (None = not accepted)."""
        cleaned = _clean_code(code)
        with self.connection() as conn:
            row = self._latest_code(conn, kind, pid, purpose, channel)
            if row is None or not cleaned:
                return None
            code_id, _, destination, stored, _ = row
            if not hmac.compare_digest(stored, _code_hash(kind, pid, purpose, cleaned)):
                conn.execute(f'UPDATE "{CODE_TABLE}" SET "Attempts" = "Attempts" + 1 WHERE "CodeId" = ?', (code_id,))
                return None
            conn.execute(f'UPDATE "{CODE_TABLE}" SET "ConsumedAt" = ? WHERE "CodeId" = ?', (_now(), code_id))
        return destination

    # -- signing in --------------------------------------------------------------------------
    def send_login_code(self, kind: str, pid: int, method: str) -> str:
        """Send a sign-in code through an enabled e-mail / sms factor; returns the masked destination."""
        if method not in (METHOD_EMAIL, METHOD_SMS):
            raise AccountError(f"{METHOD_LABELS.get(method, method)} does not send codes")
        row = self.get_method(kind, pid, method)
        if row is None or not row.is_enabled or not row.destination:
            raise AccountError(f"{METHOD_LABELS[method]} is not set up for this account")
        return self._send_code(kind, pid, PURPOSE_LOGIN, method, row.destination)

    def verify_login(self, kind: str, pid: int, method: str, code: str) -> bool:
        """The second factor at sign-in: an authenticator code, an e-mail / sms code or a recovery code."""
        if method == METHOD_RECOVERY:
            return self._use_recovery_code(kind, pid, code)
        row = self.get_method(kind, pid, method)
        if row is None or not row.is_enabled:
            return False
        if method == METHOD_TOTP:
            ok = verify_totp(row.secret or "", code)
        elif method in (METHOD_EMAIL, METHOD_SMS):
            ok = self._check_code(kind, pid, PURPOSE_LOGIN, code, channel=method) is not None
        else:
            return False
        if ok:
            self._touch(kind, pid, method)
        return ok

    # -- recovery codes ---------------------------------------------------------------------
    def generate_recovery_codes(self, kind: str, pid: int, count: int = RECOVERY_CODE_COUNT) -> list[str]:
        """A new set of single-use codes (the old ones stop working); shown once, stored hashed."""
        codes = [generate_recovery_code() for _ in range(count)]
        now = _now()
        with self.connection() as conn:
            conn.execute(f'DELETE FROM "{RECOVERY_TABLE}" WHERE "PrincipalType" = ? AND "PrincipalId" = ?',
                         (kind, int(pid)))
            conn.executemany(f'INSERT INTO "{RECOVERY_TABLE}" ("PrincipalType","PrincipalId","CodeHash","CreatedAt") '
                             f'VALUES (?,?,?,?)', [(kind, int(pid), _digest(c), now) for c in codes])
        return codes

    def recovery_codes_left(self, kind: str, pid: int) -> int:
        with self.connection() as conn:
            return int(conn.execute(f'SELECT COUNT(*) FROM "{RECOVERY_TABLE}" WHERE "PrincipalType" = ? AND '
                                    f'"PrincipalId" = ? AND "UsedAt" IS NULL', (kind, int(pid))).fetchone()[0])

    def _use_recovery_code(self, kind: str, pid: int, code: str) -> bool:
        cleaned = _normalise_recovery(code)
        if not cleaned:
            return False
        with self.connection() as conn:
            cur = conn.execute(f'UPDATE "{RECOVERY_TABLE}" SET "UsedAt" = ? WHERE "PrincipalType" = ? AND '
                               f'"PrincipalId" = ? AND "CodeHash" = ? AND "UsedAt" IS NULL',
                               (_now(), kind, int(pid), _digest(cleaned)))
            ok = cur.rowcount == 1
        if ok:
            log.info("recovery code used by %s %s", kind, pid)
        return ok

    # -- e-mail verification -------------------------------------------------------------------
    def send_email_verification(self, principal: Principal) -> str:
        """Mail a code that confirms the principal's e-mail address; returns the masked address."""
        if not principal.email:
            raise AccountError("there is no e-mail address to confirm - save one first")
        return self._send_code(principal.kind, principal.id, PURPOSE_VERIFY_EMAIL, METHOD_EMAIL, principal.email)

    def verify_email(self, kind: str, pid: int, code: str) -> bool:
        return self._check_code(kind, pid, PURPOSE_VERIFY_EMAIL, code) is not None

    # -- password reset -------------------------------------------------------------------------
    def send_password_reset(self, principal: Principal, link_for: Callable[[str], str]) -> str:
        """Mail a single-use link (``link_for(token)``) to choose a new password; returns the masked address."""
        if not principal.email:
            raise AccountError("there is no e-mail address to send the link to")
        kind, pid = principal.kind, principal.id
        token = secrets.token_urlsafe(32)
        now = _now_dt()
        with self.connection() as conn:
            self._check_cooldown(conn, kind, pid, PURPOSE_RESET, METHOD_EMAIL)
            cur = conn.execute(
                f'INSERT INTO "{CODE_TABLE}" ("PrincipalType","PrincipalId","Purpose","Channel","Destination",'
                f'"CodeHash","ExpiresAt","Attempts","CreatedAt") VALUES (?,?,?,?,?,?,?,0,?)',
                (kind, int(pid), PURPOSE_RESET, METHOD_EMAIL, principal.email, _digest(token),
                 _fmt(now + timedelta(minutes=self.reset_ttl)), _fmt(now)))
            code_id = cur.lastrowid
        link = link_for(token)
        body = (f"Hello,\n\nsomebody (hopefully you) asked to reset the password of your {self.issuer} account.\n"
                f"Open this link to choose a new password - it works once and for {self.reset_ttl} minutes:\n\n"
                f"{link}\n\nIf you did not ask for it, ignore this message: your password stays unchanged.\n\n"
                f"-- {self.issuer}\n")
        masked = mask_email(principal.email)
        try:
            self.email.send(principal.email, f"{self.issuer}: reset your password", body)
        except MessagingError as exc:
            with self.connection() as conn:
                conn.execute(f'DELETE FROM "{CODE_TABLE}" WHERE "CodeId" = ?', (code_id,))
            raise AccountError(f"the link could not be sent to {masked}: {exc}") from exc
        log.info("password-reset link sent to %s %s (%s)", kind, pid, masked)
        return masked

    def _reset_row(self, conn, token: str):
        if not token:
            return None
        return conn.execute(
            f'SELECT "CodeId", "PrincipalType", "PrincipalId" FROM "{CODE_TABLE}" WHERE "CodeHash" = ? AND '
            f'"Purpose" = ? AND "ConsumedAt" IS NULL AND "ExpiresAt" > ?',
            (_digest(token.strip()), PURPOSE_RESET, _now())).fetchone()

    def peek_reset_token(self, token: str) -> tuple[str, int] | None:
        """``(principal type, id)`` of a valid, unused reset token - without using it up."""
        with self.connection() as conn:
            row = self._reset_row(conn, token)
        return (row[1], int(row[2])) if row else None

    def consume_reset_token(self, token: str) -> tuple[str, int] | None:
        """Use the token up (and every other open reset link of the same principal)."""
        with self.connection() as conn:
            row = self._reset_row(conn, token)
            if row is None:
                return None
            conn.execute(f'UPDATE "{CODE_TABLE}" SET "ConsumedAt" = ? WHERE "PrincipalType" = ? AND "PrincipalId" = ? '
                         f'AND "Purpose" = ? AND "ConsumedAt" IS NULL', (_now(), row[1], row[2], PURPOSE_RESET))
        return row[1], int(row[2])
