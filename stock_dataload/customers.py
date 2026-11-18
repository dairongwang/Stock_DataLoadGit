"""
Customers of the public **portal** and the token-metered **data API**.

Tables (reference script: ``sql/create_api_tables_sqlite.sql``)::

    Api_Customer  one row per e-mail address: an ACCOUNT (PasswordHash set) or a
                  GUEST who bought tokens without signing up (PasswordHash NULL);
                  role user | admin (the portal's admin pages)
    Api_Key       the secret sent with every data API request - stored as a
                  SHA-256 hash plus a 12-character display prefix only; carries
                  the TOKEN BALANCE (CHECK Balance >= 0)
    Api_Order     every purchase (stripe / paypal) and manual grant (provider
                  ``manual``, amount 0): pack, tokens, amount in cents, status
                  pending | paid | failed | cancelled, the key credited
    Api_Usage     the metering ledger: every charged or refused data API call

Keys look like ``sdl_`` + 32 URL-safe random characters and are shown to the
customer exactly once (on the receipt / the account page).  Losing a key is
harmless: :meth:`CustomerStore.rotate_key` issues a new secret for the same
balance.

Charging is atomic (one ``UPDATE ... WHERE Balance >= ?`` inside
``BEGIN IMMEDIATE``) so two concurrent requests can never spend the same
tokens twice; :meth:`CustomerStore.complete_order` is idempotent so a second
return from the payment page never credits twice.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator

from .auth import (EXTERNAL_PASSWORD, AuthError, check_password_strength, hash_password, validate_email,
                   verify_password)
from .config import Settings

log = logging.getLogger(__name__)

CUSTOMER_TABLE = "Api_Customer"
KEY_TABLE = "Api_Key"
ORDER_TABLE = "Api_Order"
USAGE_TABLE = "Api_Usage"

ROLE_USER = "user"
ROLE_ADMIN = "admin"
CUSTOMER_ROLES: tuple[str, ...] = (ROLE_USER, ROLE_ADMIN)

ORDER_PENDING = "pending"
ORDER_PAID = "paid"
ORDER_FAILED = "failed"
ORDER_CANCELLED = "cancelled"
ORDER_STATUSES = (ORDER_PENDING, ORDER_PAID, ORDER_FAILED, ORDER_CANCELLED)

PROVIDER_MANUAL = "manual"
ORDER_PROVIDERS = ("stripe", "paypal", PROVIDER_MANUAL)

USAGE_OK = "ok"
USAGE_INSUFFICIENT = "insufficient_tokens"

KEY_PREFIX = "sdl_"
KEY_DISPLAY_LENGTH = 12          # "sdl_" + 8 characters are stored for display
ORDER_REF_PREFIX = "ORD-"
ORDER_REF_LENGTH = 10
_REF_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I

CUSTOMER_DDL = f"""
CREATE TABLE IF NOT EXISTS "{CUSTOMER_TABLE}" (
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
)
"""

KEY_DDL = f"""
CREATE TABLE IF NOT EXISTS "{KEY_TABLE}" (
    "ApiKeyId"        INTEGER PRIMARY KEY AUTOINCREMENT,
    "CustomerId"      INTEGER NOT NULL REFERENCES "{CUSTOMER_TABLE}" ("CustomerId"),
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
    CONSTRAINT "CK_{KEY_TABLE}_Balance" CHECK ("Balance" >= 0)
)
"""

ORDER_DDL = f"""
CREATE TABLE IF NOT EXISTS "{ORDER_TABLE}" (
    "OrderId"       INTEGER PRIMARY KEY AUTOINCREMENT,
    "OrderRef"      TEXT NOT NULL UNIQUE,       -- the reference the customer sees (ORD-XXXXXXXX)
    "CustomerId"    INTEGER REFERENCES "{CUSTOMER_TABLE}" ("CustomerId"),
    "ApiKeyId"      INTEGER REFERENCES "{KEY_TABLE}" ("ApiKeyId"),  -- the key credited (set when paid)
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
    CONSTRAINT "CK_{ORDER_TABLE}_Tokens" CHECK ("Tokens" > 0),
    CONSTRAINT "CK_{ORDER_TABLE}_Amount" CHECK ("Amount" >= 0)
)
"""

USAGE_DDL = f"""
CREATE TABLE IF NOT EXISTS "{USAGE_TABLE}" (
    "UsageId"     INTEGER PRIMARY KEY AUTOINCREMENT,
    "ApiKeyId"    INTEGER NOT NULL REFERENCES "{KEY_TABLE}" ("ApiKeyId"),
    "RequestedAt" TEXT NOT NULL,
    "Endpoint"    TEXT NOT NULL,                -- daily | weekly | monthly
    "Tickers"     TEXT,                         -- comma separated
    "Year"        INTEGER,
    "Tokens"      INTEGER NOT NULL DEFAULT 0,   -- tokens charged (0 when refused)
    "Rows"        INTEGER NOT NULL DEFAULT 0,
    "Status"      TEXT NOT NULL,                -- ok | insufficient_tokens
    "RemoteAddr"  TEXT
)
"""

INDEXES = (
    f'CREATE INDEX IF NOT EXISTS "IX_{KEY_TABLE}_Customer" ON "{KEY_TABLE}" ("CustomerId")',
    f'CREATE INDEX IF NOT EXISTS "IX_{ORDER_TABLE}_Customer" ON "{ORDER_TABLE}" ("CustomerId")',
    f'CREATE INDEX IF NOT EXISTS "IX_{ORDER_TABLE}_Email" ON "{ORDER_TABLE}" ("Email")',
    f'CREATE INDEX IF NOT EXISTS "IX_{USAGE_TABLE}_Key" ON "{USAGE_TABLE}" ("ApiKeyId", "RequestedAt")',
)


def render_ddl() -> str:
    """The DDL of the four tables as a script (``sql/create_api_tables_sqlite.sql``)."""
    return "\n".join(s.strip() + ";" for s in (CUSTOMER_DDL, KEY_DDL, ORDER_DDL, USAGE_DDL, *INDEXES)) + "\n"


class CustomerError(Exception):
    """Invalid input or an impossible change (unknown pack / key / order, bad password, ...)."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def hash_key(plain: str) -> str:
    return hashlib.sha256((plain or "").strip().encode("utf-8")).hexdigest()


def generate_key() -> str:
    """``sdl_`` + 32 URL-safe random characters."""
    return KEY_PREFIX + secrets.token_urlsafe(24)


def generate_order_ref() -> str:
    return ORDER_REF_PREFIX + "".join(secrets.choice(_REF_ALPHABET) for _ in range(ORDER_REF_LENGTH))


def format_money(cents: int, currency: str) -> str:
    """``500, "USD"`` -> ``"5.00 USD"``."""
    return f"{(cents or 0) / 100:,.2f} {currency}"


def _email(email: str | None) -> str:
    try:
        cleaned = validate_email(email)
    except AuthError as exc:
        raise CustomerError(str(exc)) from exc
    if not cleaned:
        raise CustomerError("an e-mail address is required")
    return cleaned


def _password(password: str) -> str:
    try:
        check_password_strength(password)
    except AuthError as exc:
        raise CustomerError(str(exc)) from exc
    return hash_password(password)


def _label(text: str | None) -> str | None:
    text = " ".join((text or "").split())[:60]
    return text or None


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------
@dataclass
class Pack:
    """A token pack on sale (``[portal] packs``)."""
    name: str
    tokens: int
    cents: int
    currency: str = "USD"

    @property
    def key(self) -> str:
        return self.name.lower()

    @property
    def price(self) -> str:
        return format_money(self.cents, self.currency)

    @property
    def per_token(self) -> str:
        return f"{self.cents / self.tokens / 100:.4f}".rstrip("0").rstrip(".") + f" {self.currency}" \
            if self.tokens else "-"

    def as_dict(self) -> dict:
        return {"name": self.name, "key": self.key, "tokens": self.tokens, "price": round(self.cents / 100, 2),
                "amount_cents": self.cents, "currency": self.currency, "display_price": self.price,
                "per_token": self.per_token}


_CUSTOMER_COLS = ("CustomerId", "Email", "DisplayName", "PasswordHash", "Role", "EmailVerified", "Phone", "Active",
                  "CreatedAt", "LastLoginAt")


@dataclass
class Customer:
    customer_id: int
    email: str
    display_name: str | None
    password_hash: str | None
    role: str
    email_verified: int
    phone: str | None
    active: int
    created_at: str
    last_login_at: str | None

    @property
    def is_guest(self) -> bool:
        """Bought tokens without signing up (no password, cannot sign in)."""
        return self.password_hash is None

    @property
    def has_password(self) -> bool:
        """False for guests and for accounts that sign in through Google / Apple / Facebook only."""
        return bool(self.password_hash) and self.password_hash != EXTERNAL_PASSWORD

    @property
    def is_active(self) -> bool:
        return bool(self.active)

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN and not self.is_guest

    @property
    def is_email_verified(self) -> bool:
        return bool(self.email_verified)

    @property
    def name(self) -> str:
        return self.display_name or self.email

    def public(self) -> dict:
        return {"customer_id": self.customer_id, "email": self.email, "display_name": self.display_name,
                "type": "guest" if self.is_guest else "account", "role": self.role, "active": self.is_active,
                "email_verified": self.is_email_verified, "has_password": self.has_password,
                "phone": self.phone, "created_at": self.created_at, "last_login_at": self.last_login_at}


_KEY_COLS = ("ApiKeyId", "CustomerId", "Label", "KeyPrefix", "KeyHash", "Balance", "TokensPurchased", "TokensUsed",
             "Active", "CreatedAt", "LastUsedAt", "RevokedAt")


@dataclass
class ApiKey:
    api_key_id: int
    customer_id: int
    label: str | None
    key_prefix: str
    key_hash: str
    balance: int
    tokens_purchased: int
    tokens_used: int
    active: int
    created_at: str
    last_used_at: str | None
    revoked_at: str | None

    @property
    def is_active(self) -> bool:
        return bool(self.active)

    @property
    def display(self) -> str:
        """What pages show instead of the secret: ``sdl_AbCdEfGh…``."""
        return f"{self.key_prefix}…"

    def public(self) -> dict:
        return {"api_key_id": self.api_key_id, "customer_id": self.customer_id, "key": self.display,
                "key_prefix": self.key_prefix, "label": self.label, "balance": self.balance,
                "tokens_purchased": self.tokens_purchased, "tokens_used": self.tokens_used,
                "active": self.is_active, "created_at": self.created_at, "last_used_at": self.last_used_at,
                "revoked_at": self.revoked_at}


_ORDER_COLS = ("OrderId", "OrderRef", "CustomerId", "ApiKeyId", "Email", "Guest", "Pack", "Tokens", "Amount",
               "Currency", "Status", "Provider", "ProviderRef", "PaymentDetail", "KeyLabel", "Note", "CreatedAt",
               "PaidAt")


@dataclass
class Order:
    order_id: int
    order_ref: str
    customer_id: int | None
    api_key_id: int | None
    email: str
    guest: int
    pack: str
    tokens: int
    amount: int
    currency: str
    status: str
    provider: str
    provider_ref: str | None
    payment_detail: str | None
    key_label: str | None
    note: str | None
    created_at: str
    paid_at: str | None

    @property
    def is_paid(self) -> bool:
        return self.status == ORDER_PAID

    @property
    def is_guest(self) -> bool:
        return bool(self.guest)

    @property
    def price(self) -> str:
        return format_money(self.amount, self.currency)

    def public(self) -> dict:
        return {"order_ref": self.order_ref, "customer_id": self.customer_id, "api_key_id": self.api_key_id,
                "email": self.email, "guest": self.is_guest, "pack": self.pack, "tokens": self.tokens,
                "amount_cents": self.amount, "currency": self.currency, "price": self.price, "status": self.status,
                "provider": self.provider, "provider_ref": self.provider_ref, "payment_detail": self.payment_detail,
                "note": self.note, "created_at": self.created_at, "paid_at": self.paid_at}


_USAGE_COLS = ("UsageId", "ApiKeyId", "RequestedAt", "Endpoint", "Tickers", "Year", "Tokens", "Rows", "Status",
               "RemoteAddr")


@dataclass
class Usage:
    usage_id: int
    api_key_id: int
    requested_at: str
    endpoint: str
    tickers: str | None
    year: int | None
    tokens: int
    rows: int
    status: str
    remote_addr: str | None


@dataclass
class ChargeResult:
    ok: bool
    tokens: int           # tokens charged (0 when refused)
    balance: int          # the key's balance afterwards
    message: str = ""


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------
class CustomerStore:
    """Customers, API keys, orders and the usage ledger in the app's SQLite database."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._ready = False

    # -- connections -----------------------------------------------------------
    def _connect(self, autocommit: bool = False) -> sqlite3.Connection:
        path = self.settings.sqlite_path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None if autocommit else "")
        conn.execute("PRAGMA journal_mode=WAL")
        if not self._ready:
            for ddl in (CUSTOMER_DDL, KEY_DDL, ORDER_DDL, USAGE_DDL, *INDEXES):
                conn.execute(ddl)
            self._ready = True
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """``BEGIN IMMEDIATE`` ... ``COMMIT``: the write lock is taken up front, so a
        read-check-write sequence (charging, completing an order) cannot interleave."""
        conn = self._connect(autocommit=True)
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def ensure_tables(self) -> None:
        with self.connection():
            pass

    # -- packs -------------------------------------------------------------------
    def packs(self) -> list[Pack]:
        return [Pack(name, tokens, cents, self.settings.currency) for name, tokens, cents in self.settings.token_packs]

    def pack(self, name: str) -> Pack:
        wanted = (name or "").strip().lower()
        for p in self.packs():
            if p.key == wanted:
                return p
        raise CustomerError("please choose a token pack" if not wanted else f"unknown token pack '{name}'")

    # -- customers ----------------------------------------------------------------
    @staticmethod
    def _customers(conn: sqlite3.Connection, where: str = "", params: tuple = (), tail: str = "") -> list[Customer]:
        cols = ", ".join(f'"{c}"' for c in _CUSTOMER_COLS)
        sql = f'SELECT {cols} FROM "{CUSTOMER_TABLE}"' + (f" WHERE {where}" if where else "") + tail
        return [Customer(*r) for r in conn.execute(sql, params).fetchall()]

    def get_customer(self, customer_id: int) -> Customer | None:
        with self.connection() as conn:
            rows = self._customers(conn, '"CustomerId" = ?', (int(customer_id),))
        return rows[0] if rows else None

    def get_customer_by_email(self, email: str | None) -> Customer | None:
        email = (email or "").strip()
        if not email:
            return None
        with self.connection() as conn:
            rows = self._customers(conn, '"Email" = ? COLLATE NOCASE', (email,))
        return rows[0] if rows else None

    def _require_customer(self, customer_id: int) -> Customer:
        customer = self.get_customer(customer_id)
        if customer is None:
            raise CustomerError(f"customer {customer_id} does not exist")
        return customer

    def list_customers(self, limit: int = 100, q: str | None = None) -> list[Customer]:
        where, params = "", []
        if q and q.strip():
            where = '"Email" LIKE ? OR "DisplayName" LIKE ?'
            params = [f"%{q.strip()}%"] * 2
        with self.connection() as conn:
            return self._customers(conn, where, tuple(params) + (int(limit),),
                                   ' ORDER BY "CreatedAt" DESC, "CustomerId" DESC LIMIT ?')

    def get_or_create_guest(self, email: str) -> Customer:
        """The customer with this e-mail address; a GUEST row is created if there is none."""
        email = _email(email)
        existing = self.get_customer_by_email(email)
        if existing is not None:
            return existing
        with self.connection() as conn:
            conn.execute(f'INSERT OR IGNORE INTO "{CUSTOMER_TABLE}" ("Email","Role","EmailVerified","Active","CreatedAt") '
                         f'VALUES (?,?,0,1,?)', (email, ROLE_USER, _now()))
        log.info("guest customer %s created", email)
        return self.get_customer_by_email(email)

    def register(self, email: str, password: str, display_name: str = "", role: str = ROLE_USER,
                 email_verified: bool = False) -> Customer:
        """A new ACCOUNT.  An address that only has guest purchases must be claimed with an
        order reference (:meth:`claim_guest`) - it proves the purchases are yours."""
        email = _email(email)
        role = self._role(role)
        password_hash = _password(password)
        existing = self.get_customer_by_email(email)
        if existing is not None:
            if existing.is_guest:
                raise CustomerError(f"{email} bought tokens as a guest before - enter one of the order references "
                                    f"(ORD-…) to create the account and link those purchases")
            raise CustomerError(f"an account with the e-mail address {email} already exists - please sign in")
        with self.connection() as conn:
            cur = conn.execute(
                f'INSERT INTO "{CUSTOMER_TABLE}" ("Email","DisplayName","PasswordHash","Role","EmailVerified","Active",'
                f'"CreatedAt") VALUES (?,?,?,?,?,1,?)',
                (email, (display_name or "").strip()[:80] or None, password_hash, role, 1 if email_verified else 0, _now()))
            customer_id = int(cur.lastrowid)
        log.info("customer account %s created (%s)", email, role)
        return self.get_customer(customer_id)

    def register_external(self, email: str, display_name: str = "", email_verified: bool = False) -> Customer:
        """An account created by signing in with Google / Apple / Facebook (no password).  A guest
        with the same address becomes this account only if the provider verified the address."""
        email = _email(email)
        existing = self.get_customer_by_email(email)
        if existing is not None and not existing.is_guest:
            raise CustomerError(f"an account with the e-mail address {email} already exists - sign in with your "
                                f"password and connect the provider on the security page")
        if existing is not None:
            if not email_verified:
                raise CustomerError(f"{email} bought tokens as a guest before - create the account with one of the "
                                    f"order references (Create account) to link those purchases")
            with self.connection() as conn:
                conn.execute(f'UPDATE "{CUSTOMER_TABLE}" SET "PasswordHash" = ?, "EmailVerified" = 1, '
                             f'"DisplayName" = COALESCE("DisplayName", ?) WHERE "CustomerId" = ?',
                             (EXTERNAL_PASSWORD, (display_name or "").strip()[:80] or None, existing.customer_id))
                conn.execute(f'UPDATE "{ORDER_TABLE}" SET "Guest" = 0 WHERE "CustomerId" = ?', (existing.customer_id,))
            log.info("guest %s became an account through an identity provider", email)
            return self.get_customer(existing.customer_id)
        with self.connection() as conn:
            cur = conn.execute(
                f'INSERT INTO "{CUSTOMER_TABLE}" ("Email","DisplayName","PasswordHash","Role","EmailVerified","Active",'
                f'"CreatedAt") VALUES (?,?,?,?,?,1,?)',
                (email, (display_name or "").strip()[:80] or None, EXTERNAL_PASSWORD, ROLE_USER,
                 1 if email_verified else 0, _now()))
            customer_id = int(cur.lastrowid)
        log.info("customer account %s created through an identity provider", email)
        return self.get_customer(customer_id)

    def claim_guest(self, email: str, password: str, order_ref: str, display_name: str = "") -> Customer:
        """A guest turns into an account: the order reference of one of their purchases proves
        the purchases are theirs; every key and order of the guest stays with the account."""
        email = _email(email)
        password_hash = _password(password)
        customer = self.get_customer_by_email(email)
        if customer is not None and not customer.is_guest:
            raise CustomerError(f"an account with the e-mail address {email} already exists - please sign in")
        order = self.find_order(order_ref, email)
        if customer is None or order is None or order.customer_id != customer.customer_id:
            raise CustomerError("no guest purchase matches this order reference and e-mail address")
        with self.connection() as conn:
            conn.execute(f'UPDATE "{CUSTOMER_TABLE}" SET "PasswordHash" = ?, "DisplayName" = COALESCE(?, "DisplayName") '
                         f'WHERE "CustomerId" = ?',
                         (password_hash, (display_name or "").strip()[:80] or None, customer.customer_id))
            conn.execute(f'UPDATE "{ORDER_TABLE}" SET "Guest" = 0 WHERE "CustomerId" = ?', (customer.customer_id,))
        log.info("guest %s claimed its purchases with %s", email, order.order_ref)
        return self.get_customer(customer.customer_id)

    def authenticate(self, email: str, password: str) -> Customer | None:
        """The account if the e-mail / password are valid and it is active (guests cannot sign in)."""
        customer = self.get_customer_by_email(email)
        if customer is None or not customer.is_active or not customer.has_password \
                or not verify_password(password or "", customer.password_hash or ""):
            return None
        with self.connection() as conn:
            conn.execute(f'UPDATE "{CUSTOMER_TABLE}" SET "LastLoginAt" = ? WHERE "CustomerId" = ?',
                         (_now(), customer.customer_id))
        return self.get_customer(customer.customer_id)

    def set_password(self, customer_id: int, password: str) -> Customer:
        customer = self._require_customer(customer_id)
        if customer.is_guest:
            raise CustomerError(f"{customer.email} is a guest - create the account with an order reference first")
        password_hash = _password(password)
        with self.connection() as conn:
            conn.execute(f'UPDATE "{CUSTOMER_TABLE}" SET "PasswordHash" = ? WHERE "CustomerId" = ?',
                         (password_hash, customer.customer_id))
        return self.get_customer(customer_id)

    def change_password(self, customer_id: int, current_password: str, new_password: str) -> Customer:
        customer = self._require_customer(customer_id)
        if not customer.has_password:
            raise CustomerError("this account has no password yet - use 'Forgot your password' to set one")
        if not verify_password(current_password or "", customer.password_hash or ""):
            raise CustomerError("the current password is not correct")
        if current_password == new_password:
            raise CustomerError("the new password must differ from the current one")
        return self.set_password(customer_id, new_password)

    def mark_email_verified(self, customer_id: int, verified: bool = True) -> Customer:
        self._require_customer(customer_id)
        with self.connection() as conn:
            conn.execute(f'UPDATE "{CUSTOMER_TABLE}" SET "EmailVerified" = ? WHERE "CustomerId" = ?',
                         (1 if verified else 0, int(customer_id)))
        return self.get_customer(customer_id)

    @staticmethod
    def _role(role: str) -> str:
        role = (role or "").strip().lower()
        if role not in CUSTOMER_ROLES:
            raise CustomerError(f"role must be one of {', '.join(CUSTOMER_ROLES)}")
        return role

    def set_role(self, customer_id: int, role: str) -> Customer:
        role = self._role(role)
        customer = self._require_customer(customer_id)
        if customer.is_guest and role == ROLE_ADMIN:
            raise CustomerError(f"{customer.email} is a guest (no password) and cannot be an administrator")
        with self.connection() as conn:
            conn.execute(f'UPDATE "{CUSTOMER_TABLE}" SET "Role" = ? WHERE "CustomerId" = ?', (role, customer.customer_id))
        log.info("customer %s is now %s", customer.email, role)
        return self.get_customer(customer_id)

    def set_phone(self, customer_id: int, phone: str | None) -> Customer:
        self._require_customer(customer_id)
        with self.connection() as conn:
            conn.execute(f'UPDATE "{CUSTOMER_TABLE}" SET "Phone" = ? WHERE "CustomerId" = ?',
                         ((phone or "").strip() or None, int(customer_id)))
        return self.get_customer(customer_id)

    def update_customer(self, customer_id: int, active: bool | None = None,
                        display_name: str | None = None) -> Customer:
        customer = self._require_customer(customer_id)
        with self.connection() as conn:
            conn.execute(f'UPDATE "{CUSTOMER_TABLE}" SET "Active" = ?, "DisplayName" = ? WHERE "CustomerId" = ?',
                         (customer.active if active is None else (1 if active else 0),
                          customer.display_name if display_name is None else (display_name.strip()[:80] or None),
                          customer.customer_id))
        return self.get_customer(customer_id)

    # -- keys ----------------------------------------------------------------------
    @staticmethod
    def _keys(conn: sqlite3.Connection, where: str = "", params: tuple = (), tail: str = "") -> list[ApiKey]:
        cols = ", ".join(f'"{c}"' for c in _KEY_COLS)
        sql = f'SELECT {cols} FROM "{KEY_TABLE}"' + (f" WHERE {where}" if where else "") + tail
        return [ApiKey(*r) for r in conn.execute(sql, params).fetchall()]

    def get_key(self, api_key_id: int) -> ApiKey | None:
        with self.connection() as conn:
            rows = self._keys(conn, '"ApiKeyId" = ?', (int(api_key_id),))
        return rows[0] if rows else None

    def _require_key(self, api_key_id: int) -> ApiKey:
        key = self.get_key(api_key_id)
        if key is None:
            raise CustomerError(f"API key {api_key_id} does not exist")
        return key

    def keys_of(self, customer_id: int, active_only: bool = False) -> list[ApiKey]:
        with self.connection() as conn:
            return self._keys(conn, '"CustomerId" = ?' + (' AND "Active" = 1' if active_only else ""),
                              (int(customer_id),), ' ORDER BY "Active" DESC, "ApiKeyId"')

    def list_keys(self, limit: int = 500) -> list[ApiKey]:
        with self.connection() as conn:
            return self._keys(conn, "", (int(limit),), ' ORDER BY "ApiKeyId" LIMIT ?')

    def find_key(self, plain: str | None) -> ApiKey | None:
        """The key of a plain-text secret (active or not) - a hash lookup."""
        plain = (plain or "").strip()
        if not plain:
            return None
        with self.connection() as conn:
            rows = self._keys(conn, '"KeyHash" = ?', (hash_key(plain),))
        if rows and hmac.compare_digest(rows[0].key_hash, hash_key(plain)):
            return rows[0]
        return None

    def find_key_by_prefix(self, prefix: str | None) -> ApiKey | None:
        """The key whose display prefix is (or starts with) *prefix* - for the CLI; None if
        nothing matches; an ambiguous prefix is an error."""
        prefix = (prefix or "").strip().rstrip("…").rstrip(".")
        if not prefix:
            return None
        with self.connection() as conn:
            rows = self._keys(conn, '"KeyPrefix" = ?', (prefix,))
            if not rows:
                rows = self._keys(conn, 'substr("KeyPrefix", 1, ?) = ?', (len(prefix), prefix))
        if len(rows) > 1:
            raise CustomerError(f"'{prefix}' matches {len(rows)} keys - use the full prefix shown by 'customers list'")
        return rows[0] if rows else None

    def authenticate_key(self, plain: str | None) -> ApiKey | None:
        """The key for a data API request: known, active, and its customer active."""
        key = self.find_key(plain)
        if key is None or not key.is_active:
            return None
        customer = self.get_customer(key.customer_id)
        if customer is None or not customer.is_active:
            return None
        return key

    def create_key(self, customer_id: int, label: str | None = "") -> tuple[ApiKey, str]:
        """A new, empty key; returns ``(key, plain secret)`` - the secret is never stored."""
        customer = self._require_customer(customer_id)
        with self.connection() as conn:
            key, plain = self._insert_key(conn, customer.customer_id, label)
        log.info("key %s created for %s", key.key_prefix, customer.email)
        return key, plain

    def _insert_key(self, conn: sqlite3.Connection, customer_id: int, label: str | None,
                    balance: int = 0) -> tuple[ApiKey, str]:
        plain = generate_key()
        cur = conn.execute(
            f'INSERT INTO "{KEY_TABLE}" ("CustomerId","Label","KeyPrefix","KeyHash","Balance","TokensPurchased",'
            f'"TokensUsed","Active","CreatedAt") VALUES (?,?,?,?,?,?,0,1,?)',
            (int(customer_id), _label(label), plain[:KEY_DISPLAY_LENGTH], hash_key(plain), int(balance), int(balance),
             _now()))
        return self._keys(conn, '"ApiKeyId" = ?', (int(cur.lastrowid),))[0], plain

    def rotate_key(self, api_key_id: int) -> tuple[ApiKey, str]:
        """A new secret for the key; balance, label and history stay.  The old secret stops working."""
        key = self._require_key(api_key_id)
        if not key.is_active:
            raise CustomerError(f"key {key.display} is revoked or disabled and cannot be rotated")
        plain = generate_key()
        with self.connection() as conn:
            conn.execute(f'UPDATE "{KEY_TABLE}" SET "KeyPrefix" = ?, "KeyHash" = ? WHERE "ApiKeyId" = ?',
                         (plain[:KEY_DISPLAY_LENGTH], hash_key(plain), key.api_key_id))
        log.info("key %s rotated -> %s", key.key_prefix, plain[:KEY_DISPLAY_LENGTH])
        return self.get_key(api_key_id), plain

    def revoke_key(self, api_key_id: int) -> ApiKey:
        key = self._require_key(api_key_id)
        with self.connection() as conn:
            conn.execute(f'UPDATE "{KEY_TABLE}" SET "Active" = 0, "RevokedAt" = COALESCE("RevokedAt", ?) '
                         f'WHERE "ApiKeyId" = ?', (_now(), key.api_key_id))
        log.info("key %s revoked (%d tokens left on it)", key.key_prefix, key.balance)
        return self.get_key(api_key_id)

    def set_key_active(self, api_key_id: int, active: bool) -> ApiKey:
        key = self._require_key(api_key_id)
        with self.connection() as conn:
            conn.execute(f'UPDATE "{KEY_TABLE}" SET "Active" = ?, "RevokedAt" = ? WHERE "ApiKeyId" = ?',
                         (1 if active else 0, None if active else (key.revoked_at or _now()), key.api_key_id))
        return self.get_key(api_key_id)

    def relabel_key(self, api_key_id: int, label: str | None) -> ApiKey:
        key = self._require_key(api_key_id)
        with self.connection() as conn:
            conn.execute(f'UPDATE "{KEY_TABLE}" SET "Label" = ? WHERE "ApiKeyId" = ?', (_label(label), key.api_key_id))
        return self.get_key(api_key_id)

    # -- orders ----------------------------------------------------------------------
    @staticmethod
    def _orders(conn: sqlite3.Connection, where: str = "", params: tuple = (), tail: str = "") -> list[Order]:
        cols = ", ".join(f'"{c}"' for c in _ORDER_COLS)
        sql = f'SELECT {cols} FROM "{ORDER_TABLE}"' + (f" WHERE {where}" if where else "") + tail
        return [Order(*r) for r in conn.execute(sql, params).fetchall()]

    def get_order(self, order_ref: str | None) -> Order | None:
        ref = (order_ref or "").strip().upper()
        if not ref:
            return None
        with self.connection() as conn:
            rows = self._orders(conn, '"OrderRef" = ?', (ref,))
        return rows[0] if rows else None

    def find_order(self, order_ref: str | None, email: str | None) -> Order | None:
        """The order with this reference *and* e-mail address (the guest receipt lookup)."""
        order = self.get_order(order_ref)
        if order is None or order.email.lower() != (email or "").strip().lower():
            return None
        return order

    def orders_of(self, customer_id: int, limit: int = 50) -> list[Order]:
        with self.connection() as conn:
            return self._orders(conn, '"CustomerId" = ?', (int(customer_id), int(limit)),
                                ' ORDER BY "OrderId" DESC LIMIT ?')

    def list_orders(self, limit: int = 50, status: str | None = None) -> list[Order]:
        with self.connection() as conn:
            if status:
                return self._orders(conn, '"Status" = ?', (status, int(limit)), ' ORDER BY "OrderId" DESC LIMIT ?')
            return self._orders(conn, "", (int(limit),), ' ORDER BY "OrderId" DESC LIMIT ?')

    def create_order(self, pack_name: str, email: str, provider: str, customer_id: int | None = None,
                     api_key_id: int | None = None, key_label: str | None = "") -> Order:
        """A PENDING order for a pack.  Signed-in customers pass their ``customer_id``; a guest
        is identified by the e-mail address (a guest row is created) - an address that belongs
        to an account must sign in instead.  *api_key_id* tops up an existing key."""
        pack = self.pack(pack_name)
        provider = (provider or "").strip().lower()
        if provider not in ("stripe", "paypal"):
            raise CustomerError("please choose a payment method")
        if customer_id is not None:
            customer = self._require_customer(customer_id)
            if not customer.is_active:
                raise CustomerError("this account is disabled")
            guest = customer.is_guest
        else:
            email = _email(email)
            customer = self.get_customer_by_email(email)
            if customer is not None and not customer.is_guest:
                raise CustomerError(f"an account exists for {email} - please sign in to buy tokens for it")
            if customer is not None and not customer.is_active:
                raise CustomerError(f"{email} is disabled - please contact support")
            customer = customer or self.get_or_create_guest(email)
            guest = True
        if api_key_id is not None:
            key = self._require_key(api_key_id)
            if not key.is_active:
                raise CustomerError(f"key {key.display} is revoked or disabled")
        with self.connection() as conn:
            for _ in range(5):
                ref = generate_order_ref()
                try:
                    conn.execute(
                        f'INSERT INTO "{ORDER_TABLE}" ("OrderRef","CustomerId","ApiKeyId","Email","Guest","Pack",'
                        f'"Tokens","Amount","Currency","Status","Provider","KeyLabel","CreatedAt") '
                        f'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (ref, customer.customer_id, api_key_id, customer.email, 1 if guest else 0, pack.name,
                         pack.tokens, pack.cents, pack.currency, ORDER_PENDING, provider, _label(key_label), _now()))
                    break
                except sqlite3.IntegrityError:            # a reference collision: try another one
                    continue
            else:  # pragma: no cover - 32^10 references
                raise CustomerError("could not allocate an order reference - please try again")
        log.info("order %s created: %s pack for %s via %s", ref, pack.name, customer.email, provider)
        return self.get_order(ref)

    def set_provider_ref(self, order_ref: str, provider_ref: str | None) -> Order:
        order = self._require_order(order_ref)
        with self.connection() as conn:
            conn.execute(f'UPDATE "{ORDER_TABLE}" SET "ProviderRef" = ? WHERE "OrderId" = ?',
                         (provider_ref, order.order_id))
        return self.get_order(order.order_ref)

    def _require_order(self, order_ref: str) -> Order:
        order = self.get_order(order_ref)
        if order is None:
            raise CustomerError(f"order {order_ref} does not exist")
        return order

    def fail_order(self, order_ref: str, message: str = "", cancelled: bool = False) -> Order:
        """A pending order that will not be paid (provider error / the customer cancelled)."""
        order = self._require_order(order_ref)
        if order.status == ORDER_PENDING:
            with self.connection() as conn:
                conn.execute(f'UPDATE "{ORDER_TABLE}" SET "Status" = ?, "Note" = ? WHERE "OrderId" = ? AND "Status" = ?',
                             (ORDER_CANCELLED if cancelled else ORDER_FAILED, (message or "")[:500] or None,
                              order.order_id, ORDER_PENDING))
        return self.get_order(order.order_ref)

    def complete_order(self, order_ref: str, provider_ref: str | None = None,
                       payment_detail: str | None = None) -> tuple[Order, ApiKey, str | None]:
        """The payment was verified: mark the order paid and credit its tokens - to the key chosen
        at purchase time, or to a NEW key (also when the chosen key was revoked meanwhile).

        Returns ``(order, key, plain secret of a new key or None)``.  Idempotent: an order that is
        already paid is returned unchanged (``plain`` None) and nothing is credited twice."""
        with self.transaction() as conn:
            rows = self._orders(conn, '"OrderRef" = ?', ((order_ref or "").strip().upper(),))
            if not rows:
                raise CustomerError(f"order {order_ref} does not exist")
            order = rows[0]
            if order.is_paid:                              # a second return: nothing is credited again
                found = self._keys(conn, '"ApiKeyId" = ?', (order.api_key_id,)) if order.api_key_id else []
                if not found:
                    raise CustomerError(f"order {order.order_ref} is paid but its key no longer exists")
                return order, found[0], None
            if order.status != ORDER_PENDING:
                log.warning("order %s was %s but the payment arrived - crediting it anyway", order.order_ref,
                            order.status)
            key, plain = None, None
            if order.api_key_id:
                found = self._keys(conn, '"ApiKeyId" = ?', (order.api_key_id,))
                if found and found[0].is_active:
                    key = found[0]
            customer_id = order.customer_id
            if customer_id is None:
                conn.execute(f'INSERT OR IGNORE INTO "{CUSTOMER_TABLE}" ("Email","Role","EmailVerified","Active",'
                             f'"CreatedAt") VALUES (?,?,0,1,?)', (order.email, ROLE_USER, _now()))
                customer_id = self._customers(conn, '"Email" = ? COLLATE NOCASE', (order.email,))[0].customer_id
            if key is None:
                key, plain = self._insert_key(conn, customer_id, order.key_label or f"{order.pack} pack")
            conn.execute(f'UPDATE "{KEY_TABLE}" SET "Balance" = "Balance" + ?, "TokensPurchased" = "TokensPurchased" + ? '
                         f'WHERE "ApiKeyId" = ?', (order.tokens, order.tokens, key.api_key_id))
            conn.execute(f'UPDATE "{ORDER_TABLE}" SET "Status" = ?, "PaidAt" = ?, "ApiKeyId" = ?, '
                         f'"ProviderRef" = COALESCE(?, "ProviderRef"), "PaymentDetail" = ?, "CustomerId" = ? '
                         f'WHERE "OrderId" = ?',
                         (ORDER_PAID, _now(), key.api_key_id, provider_ref, payment_detail, customer_id, order.order_id))
            order = self._orders(conn, '"OrderId" = ?', (order.order_id,))[0]
            key = self._keys(conn, '"ApiKeyId" = ?', (key.api_key_id,))[0]
        log.info("order %s paid: %d tokens credited to key %s", order.order_ref, order.tokens, key.key_prefix)
        return order, key, plain

    def grant_tokens(self, api_key_id: int, tokens: int, note: str = "", granted_by: str = "") -> tuple[Order, ApiKey]:
        """Credit tokens by hand (refund, trial, invoiced deal): a PAID order of provider ``manual``
        with amount 0, so the ledger stays complete."""
        tokens = int(tokens)
        if tokens <= 0:
            raise CustomerError("the number of tokens must be greater than 0")
        key = self._require_key(api_key_id)
        if not key.is_active:
            raise CustomerError(f"key {key.display} is revoked or disabled - re-activate it first")
        customer = self._require_customer(key.customer_id)
        note_text = " - ".join(p for p in ((note or "").strip(), f"granted by {granted_by}" if granted_by else "") if p)
        with self.transaction() as conn:
            for _ in range(5):
                ref = generate_order_ref()
                try:
                    conn.execute(
                        f'INSERT INTO "{ORDER_TABLE}" ("OrderRef","CustomerId","ApiKeyId","Email","Guest","Pack",'
                        f'"Tokens","Amount","Currency","Status","Provider","PaymentDetail","Note","CreatedAt","PaidAt") '
                        f'VALUES (?,?,?,?,?,?,?,0,?,?,?,?,?,?,?)',
                        (ref, customer.customer_id, key.api_key_id, customer.email, 1 if customer.is_guest else 0,
                         "Manual grant", tokens, self.settings.currency, ORDER_PAID, PROVIDER_MANUAL,
                         "manual grant", note_text[:500] or None, _now(), _now()))
                    break
                except sqlite3.IntegrityError:
                    continue
            else:  # pragma: no cover
                raise CustomerError("could not allocate an order reference - please try again")
            conn.execute(f'UPDATE "{KEY_TABLE}" SET "Balance" = "Balance" + ?, "TokensPurchased" = "TokensPurchased" + ? '
                         f'WHERE "ApiKeyId" = ?', (tokens, tokens, key.api_key_id))
        log.info("%d tokens granted to key %s (%s)", tokens, key.key_prefix, ref)
        return self.get_order(ref), self.get_key(key.api_key_id)

    # -- metering -----------------------------------------------------------------------
    def charge(self, api_key_id: int, tokens: int, endpoint: str, tickers: str | None, year: int | None,
               rows: int, remote_addr: str | None = None) -> ChargeResult:
        """Charge *tokens* for a data API request - all or nothing - and record it in the ledger.
        A refused request (balance too low, key disabled) is recorded too, with 0 tokens."""
        tokens = max(0, int(tokens))
        now = _now()
        with self.transaction() as conn:
            cur = conn.execute(
                f'UPDATE "{KEY_TABLE}" SET "Balance" = "Balance" - ?, "TokensUsed" = "TokensUsed" + ?, "LastUsedAt" = ? '
                f'WHERE "ApiKeyId" = ? AND "Active" = 1 AND "Balance" >= ?',
                (tokens, tokens, now, int(api_key_id), tokens))
            ok = cur.rowcount == 1
            found = self._keys(conn, '"ApiKeyId" = ?', (int(api_key_id),))
            balance = found[0].balance if found else 0
            conn.execute(
                f'INSERT INTO "{USAGE_TABLE}" ("ApiKeyId","RequestedAt","Endpoint","Tickers","Year","Tokens","Rows",'
                f'"Status","RemoteAddr") VALUES (?,?,?,?,?,?,?,?,?)',
                (int(api_key_id), now, endpoint, (tickers or "")[:2000] or None, year, tokens if ok else 0,
                 int(rows) if ok else 0, USAGE_OK if ok else USAGE_INSUFFICIENT, remote_addr))
        if ok:
            return ChargeResult(True, tokens, balance)
        if found and not found[0].is_active:
            message = "the API key is disabled"
        else:
            message = (f"not enough tokens: this request costs {tokens} token(s) and the key has {balance} left - "
                       f"nothing was charged")
        return ChargeResult(False, 0, balance, message)

    @staticmethod
    def _usage_filter(api_key_id: int | None, customer_id: int | None) -> tuple[str, list]:
        if api_key_id is not None:
            return '"ApiKeyId" = ?', [int(api_key_id)]
        if customer_id is not None:
            return f'"ApiKeyId" IN (SELECT "ApiKeyId" FROM "{KEY_TABLE}" WHERE "CustomerId" = ?)', [int(customer_id)]
        return "", []

    def usage(self, api_key_id: int | None = None, customer_id: int | None = None, limit: int = 50) -> list[Usage]:
        where, params = self._usage_filter(api_key_id, customer_id)
        cols = ", ".join(f'"{c}"' for c in _USAGE_COLS)
        sql = f'SELECT {cols} FROM "{USAGE_TABLE}"' + (f" WHERE {where}" if where else "") + \
            ' ORDER BY "UsageId" DESC LIMIT ?'
        with self.connection() as conn:
            return [Usage(*r) for r in conn.execute(sql, params + [int(limit)]).fetchall()]

    def usage_totals(self, api_key_id: int | None = None, customer_id: int | None = None) -> dict:
        """``{"requests": served requests, "tokens": tokens charged, "rows": rows delivered, "refused": n}``."""
        where, params = self._usage_filter(api_key_id, customer_id)
        sql = (f'SELECT COALESCE(SUM(CASE WHEN "Status" = ? THEN 1 ELSE 0 END), 0), COALESCE(SUM("Tokens"), 0), '
               f'COALESCE(SUM("Rows"), 0), COALESCE(SUM(CASE WHEN "Status" <> ? THEN 1 ELSE 0 END), 0) '
               f'FROM "{USAGE_TABLE}"' + (f" WHERE {where}" if where else ""))
        with self.connection() as conn:
            requests, tokens, rows, refused = conn.execute(sql, [USAGE_OK, USAGE_OK] + params).fetchone()
        return {"requests": int(requests), "tokens": int(tokens), "rows": int(rows), "refused": int(refused)}

    # -- totals -----------------------------------------------------------------------------
    def stats(self) -> dict:
        with self.connection() as conn:
            one = lambda sql, *p: conn.execute(sql, p).fetchone()[0] or 0  # noqa: E731
            customers = one(f'SELECT COUNT(*) FROM "{CUSTOMER_TABLE}"')
            guests = one(f'SELECT COUNT(*) FROM "{CUSTOMER_TABLE}" WHERE "PasswordHash" IS NULL')
            admins = one(f'SELECT COUNT(*) FROM "{CUSTOMER_TABLE}" WHERE "Role" = ? AND "PasswordHash" IS NOT NULL',
                         ROLE_ADMIN)
            keys = one(f'SELECT COUNT(*) FROM "{KEY_TABLE}"')
            active_keys = one(f'SELECT COUNT(*) FROM "{KEY_TABLE}" WHERE "Active" = 1')
            orders = one(f'SELECT COUNT(*) FROM "{ORDER_TABLE}"')
            paid = one(f'SELECT COUNT(*) FROM "{ORDER_TABLE}" WHERE "Status" = ?', ORDER_PAID)
            pending = one(f'SELECT COUNT(*) FROM "{ORDER_TABLE}" WHERE "Status" = ?', ORDER_PENDING)
            revenue = one(f'SELECT SUM("Amount") FROM "{ORDER_TABLE}" WHERE "Status" = ?', ORDER_PAID)
            sold = one(f'SELECT SUM("Tokens") FROM "{ORDER_TABLE}" WHERE "Status" = ?', ORDER_PAID)
            used = one(f'SELECT SUM("TokensUsed") FROM "{KEY_TABLE}"')
            outstanding = one(f'SELECT SUM("Balance") FROM "{KEY_TABLE}" WHERE "Active" = 1')
            requests = one(f'SELECT COUNT(*) FROM "{USAGE_TABLE}"')
            refused = one(f'SELECT COUNT(*) FROM "{USAGE_TABLE}" WHERE "Status" <> ?', USAGE_OK)
        return {"customers": int(customers), "accounts": int(customers - guests), "guests": int(guests),
                "admins": int(admins), "keys": int(keys), "active_keys": int(active_keys), "orders": int(orders),
                "paid_orders": int(paid), "pending_orders": int(pending), "revenue": int(revenue),
                "currency": self.settings.currency, "tokens_sold": int(sold), "tokens_used": int(used),
                "tokens_outstanding": int(outstanding), "requests": int(requests), "refused_requests": int(refused)}
