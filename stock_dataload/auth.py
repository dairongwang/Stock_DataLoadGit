"""
User accounts of the **dashboard** (staff): users, roles and password hashing.

Table ``App_User`` (reference script: ``sql/create_support_tables_sqlite.sql``)::

    UserId, Username (unique, case-insensitive), DisplayName, PasswordHash, Role, Active,
    MustChangePassword, CreatedAt, CreatedBy, LastLoginAt, Email, EmailVerified

Roles
-----
``viewer`` < ``operator`` < ``admin`` - every role can do what the lower ones can:

==========  ===============================================================
viewer      read-only: overview, monitoring, browse, docs, the active tables
operator    + convert, load, formats, correction, promotion, calendar, API loads
admin       + settings, users, the portal's customers
==========  ===============================================================

Passwords
---------
Only a salted **PBKDF2-HMAC-SHA256** hash is stored::

    pbkdf2_sha256$<iterations>$<salt, base64>$<hash, base64>

600,000 iterations (:data:`PBKDF2_ITERATIONS`), a random 16-byte salt, constant-time
verification.  Accounts created through Google / Apple / Facebook store the marker
:data:`EXTERNAL_PASSWORD` instead of a hash - they have no password until they set one
with a reset link.

The first administrator
-----------------------
When the table is empty the dashboard creates ``admin`` with a random one-time
password printed to the console (or ``$STOCK_DATALOAD_ADMIN_PASSWORD``); it must be
changed at the first sign-in (:meth:`UserStore.bootstrap_admin`).

Session secret
--------------
:func:`load_secret_key` - ``$STOCK_DATALOAD_SECRET``, else generated once into the
git-ignored ``[dashboard] secret_key_file`` (``data/.secret_key``).  Never in the repository.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator

from .config import Settings

log = logging.getLogger(__name__)

USER_TABLE = "App_User"

ROLE_VIEWER = "viewer"
ROLE_OPERATOR = "operator"
ROLE_ADMIN = "admin"
#: lowest to highest - a role may do everything the roles before it may
ROLES: tuple[str, ...] = (ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
ROLE_LABELS = {
    ROLE_VIEWER: "Viewer - read-only",
    ROLE_OPERATOR: "Operator - convert, load, formats, correction, promotion",
    ROLE_ADMIN: "Administrator - settings and users",
}
_ROLE_RANK = {role: i for i, role in enumerate(ROLES)}

#: the initial administrator's password (first dashboard start) - otherwise one is generated
ENV_ADMIN_PASSWORD = "STOCK_DATALOAD_ADMIN_PASSWORD"
#: the session-signing secret of the dashboard and the portal
ENV_SECRET = "STOCK_DATALOAD_SECRET"

HASH_ALGORITHM = "pbkdf2_sha256"
#: work factor of new hashes (the test-suite lowers it); stored in every hash, so old hashes keep working
PBKDF2_ITERATIONS = 600_000
SALT_BYTES = 16
#: PasswordHash of accounts that sign in through an identity provider only (no password yet)
EXTERNAL_PASSWORD = "external"

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 1024
GENERATED_PASSWORD_LENGTH = 14
_PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"   # no 0/O, 1/l/I

_USERNAME_RE = re.compile(r"^[A-Za-z0-9._@-]{2,64}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_EMAIL_LENGTH = 254

USER_DDL = f"""
CREATE TABLE IF NOT EXISTS "{USER_TABLE}" (
    "UserId"             INTEGER PRIMARY KEY AUTOINCREMENT,
    "Username"           TEXT NOT NULL UNIQUE COLLATE NOCASE,
    "DisplayName"        TEXT,
    "PasswordHash"       TEXT NOT NULL,
    "Role"               TEXT NOT NULL,
    "Active"             INTEGER NOT NULL DEFAULT 1,
    "MustChangePassword" INTEGER NOT NULL DEFAULT 0,
    "CreatedAt"          TEXT NOT NULL,
    "CreatedBy"          TEXT,
    "LastLoginAt"        TEXT
)
"""

#: columns added after the first release (added to an existing table by :meth:`UserStore.ensure_table`)
_ADDED_COLUMNS = (
    ("Email", '"Email" TEXT'),
    ("EmailVerified", '"EmailVerified" INTEGER NOT NULL DEFAULT 0'),
)

INDEXES = (
    f'CREATE INDEX IF NOT EXISTS "IX_{USER_TABLE}_Email" ON "{USER_TABLE}" ("Email" COLLATE NOCASE)',
)


class AuthError(Exception):
    """Invalid input or an impossible change (unknown user, weak password, last administrator, ...)."""


# ---------------------------------------------------------------------------
# passwords
# ---------------------------------------------------------------------------
def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def hash_password(password: str, iterations: int | None = None) -> str:
    """``pbkdf2_sha256$<iterations>$<salt>$<hash>`` of *password* with a random salt."""
    if not password:
        raise AuthError("the password must not be empty")
    rounds = int(iterations or PBKDF2_ITERATIONS)
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return f"{HASH_ALGORITHM}${rounds}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str | None, stored: str | None) -> bool:
    """True if *password* matches the *stored* hash (constant-time); False for anything
    that is not a hash of this module (``None``, ``external``, other algorithms, garbage)."""
    if not password or not stored:
        return False
    parts = stored.split("$")
    if len(parts) != 4 or parts[0] != HASH_ALGORITHM:
        return False
    try:
        rounds = int(parts[1])
        expected = base64.b64decode(parts[3], validate=True)
        salt = base64.b64decode(parts[2], validate=True)
    except (ValueError, TypeError):
        return False
    if rounds <= 0 or not expected:
        return False
    secret = password.encode("utf-8")
    if hmac.compare_digest(hashlib.pbkdf2_hmac("sha256", secret, salt, rounds), expected):
        return True
    # compatibility: hashes whose salt text itself was used as the PBKDF2 salt
    return hmac.compare_digest(hashlib.pbkdf2_hmac("sha256", secret, parts[2].encode("ascii"), rounds), expected)


def check_password_strength(password: str | None) -> None:
    """Raise :class:`AuthError` unless the password has at least 8 characters."""
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(f"the password must have at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise AuthError(f"the password must not have more than {MAX_PASSWORD_LENGTH} characters")
    if not password.strip():
        raise AuthError("the password must not consist of blanks only")


def generate_password(length: int = GENERATED_PASSWORD_LENGTH) -> str:
    """A random password (letters and digits, no look-alike characters)."""
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(max(MIN_PASSWORD_LENGTH, int(length))))


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
def validate_username(username: str | None) -> str:
    """The user name without surrounding blanks: 2-64 letters, digits, '.', '_', '@' or '-'."""
    cleaned = (username or "").strip()
    if not _USERNAME_RE.match(cleaned):
        raise AuthError("the user name must have 2-64 characters: letters, digits, '.', '_', '@' or '-'")
    return cleaned


def validate_role(role: str | None) -> str:
    cleaned = (role or "").strip().lower()
    if cleaned not in ROLES:
        raise AuthError(f"the role must be one of {', '.join(ROLES)}")
    return cleaned


def role_allows(role: str | None, needed: str | None) -> bool:
    """True if *role* may do what *needed* may (viewer < operator < admin)."""
    if role not in _ROLE_RANK or needed not in _ROLE_RANK:
        return False
    return _ROLE_RANK[role] >= _ROLE_RANK[needed]


def validate_email(email: str | None) -> str:
    """The address without surrounding blanks ('' when none is given); :class:`AuthError` when
    it does not look like an e-mail address."""
    cleaned = (email or "").strip()
    if not cleaned:
        return ""
    if len(cleaned) > MAX_EMAIL_LENGTH or not _EMAIL_RE.match(cleaned):
        raise AuthError(f"'{cleaned[:80]}' is not a valid e-mail address")
    return cleaned


# ---------------------------------------------------------------------------
# session secret
# ---------------------------------------------------------------------------
def load_secret_key(settings: Settings) -> str:
    """The session-signing secret: ``$STOCK_DATALOAD_SECRET``, else the content of
    ``settings.secret_key_file`` - generated there once (64 hex characters, mode 0600)."""
    from_env = os.environ.get(ENV_SECRET)
    if from_env:
        return from_env
    path = settings.secret_key_file
    try:
        if path.is_file():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
    except OSError as exc:
        log.warning("could not read the secret key file %s: %s", path, exc)
    key = secrets.token_hex(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(key + "\n")
        if os.name != "nt":
            os.chmod(path, 0o600)
        log.info("session secret generated into %s", path)
    except OSError as exc:                                    # still usable - only for this process
        log.warning("could not save the secret key to %s (%s) - sessions end with this process", path, exc)
    return key


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------
_USER_COLS = ("UserId", "Username", "DisplayName", "PasswordHash", "Role", "Active", "MustChangePassword",
              "CreatedAt", "CreatedBy", "LastLoginAt", "Email", "EmailVerified")


@dataclass
class User:
    user_id: int
    username: str
    display_name: str | None
    password_hash: str
    role: str
    active: bool
    must_change_password: bool
    created_at: str
    created_by: str | None
    last_login_at: str | None
    email: str | None = None
    email_verified: bool = False

    @classmethod
    def from_row(cls, row) -> "User":
        (user_id, username, display_name, password_hash, role, active, must_change, created_at, created_by,
         last_login_at, email, email_verified) = row
        return cls(int(user_id), username, display_name, password_hash, role, bool(active), bool(must_change),
                   created_at, created_by, last_login_at, email, bool(email_verified))

    @property
    def is_active(self) -> bool:
        return bool(self.active)

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @property
    def is_email_verified(self) -> bool:
        return bool(self.email_verified)

    @property
    def has_password(self) -> bool:
        """False for accounts created through Google / Apple / Facebook that never set a password."""
        return bool(self.password_hash) and self.password_hash != EXTERNAL_PASSWORD

    @property
    def name(self) -> str:
        return self.display_name or self.username

    def can(self, needed: str) -> bool:
        """May this user do what the role *needed* may?"""
        return self.is_active and role_allows(self.role, needed)

    def public(self) -> dict:
        """Everything but the password hash (forms, JSON)."""
        return {"user_id": self.user_id, "username": self.username, "display_name": self.display_name,
                "role": self.role, "active": self.is_active, "must_change_password": self.must_change_password,
                "created_at": self.created_at, "created_by": self.created_by, "last_login_at": self.last_login_at,
                "email": self.email, "email_verified": self.is_email_verified}


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def _display_name(text: str | None) -> str | None:
    return " ".join((text or "").split())[:80] or None


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------
class UserStore:
    """The ``App_User`` table: create, authenticate, change and delete dashboard users."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._ready = False

    # -- storage --------------------------------------------------------------------
    def _prepare(self, conn: sqlite3.Connection) -> None:
        conn.execute(USER_DDL)
        present = {r[1] for r in conn.execute(f'PRAGMA table_info("{USER_TABLE}")').fetchall()}
        added = False
        for name, ddl in _ADDED_COLUMNS:
            if name not in present:
                conn.execute(f'ALTER TABLE "{USER_TABLE}" ADD COLUMN {ddl}')
                added = True
        if added and "EmailVerified" not in present:
            # accounts from before e-mail verification existed were never asked for one
            conn.execute(f'UPDATE "{USER_TABLE}" SET "EmailVerified" = 1')
        for ddl in INDEXES:
            conn.execute(ddl)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """A connection with the table in place; commit on success, rollback on error."""
        path = self.settings.sqlite_path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=30.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            if not self._ready:
                self._prepare(conn)
                conn.commit()
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

    # -- reading ----------------------------------------------------------------------
    @staticmethod
    def _users(conn: sqlite3.Connection, where: str = "", params: tuple = (), tail: str = "") -> list[User]:
        cols = ", ".join(f'"{c}"' for c in _USER_COLS)
        sql = f'SELECT {cols} FROM "{USER_TABLE}"' + (f" WHERE {where}" if where else "") + tail
        return [User.from_row(r) for r in conn.execute(sql, params).fetchall()]

    def get(self, user_id: int) -> User | None:
        with self.connection() as conn:
            rows = self._users(conn, '"UserId" = ?', (int(user_id),))
        return rows[0] if rows else None

    def get_by_username(self, username: str | None) -> User | None:
        """Case-insensitive."""
        name = (username or "").strip()
        if not name:
            return None
        with self.connection() as conn:
            rows = self._users(conn, '"Username" = ? COLLATE NOCASE', (name,))
        return rows[0] if rows else None

    def get_by_email(self, email: str | None) -> User | None:
        """The user with this e-mail address (case-insensitive), the oldest if several share it."""
        address = (email or "").strip()
        if not address:
            return None
        with self.connection() as conn:
            rows = self._users(conn, '"Email" = ? COLLATE NOCASE', (address,), ' ORDER BY "UserId" LIMIT 1')
        return rows[0] if rows else None

    def list_users(self) -> list[User]:
        with self.connection() as conn:
            return self._users(conn, tail=' ORDER BY "Username" COLLATE NOCASE')

    def count(self, active_only: bool = False) -> int:
        with self.connection() as conn:
            sql = f'SELECT COUNT(*) FROM "{USER_TABLE}"' + (' WHERE "Active" = 1' if active_only else "")
            return int(conn.execute(sql).fetchone()[0])

    def admin_count(self) -> int:
        """Active administrators."""
        with self.connection() as conn:
            return int(conn.execute(f'SELECT COUNT(*) FROM "{USER_TABLE}" WHERE "Role" = ? AND "Active" = 1',
                                    (ROLE_ADMIN,)).fetchone()[0])

    def _require(self, user_id: int) -> User:
        user = self.get(user_id)
        if user is None:
            raise AuthError(f"user {user_id} does not exist")
        return user

    def _check_email_free(self, conn: sqlite3.Connection, email: str, user_id: int | None = None) -> None:
        if not email:
            return
        row = conn.execute(f'SELECT "Username" FROM "{USER_TABLE}" WHERE "Email" = ? COLLATE NOCASE AND "UserId" <> ?',
                           (email, -1 if user_id is None else int(user_id))).fetchone()
        if row is not None:
            raise AuthError(f"the e-mail address {email} is already used by another account")

    # -- creating -----------------------------------------------------------------------
    def _insert(self, username: str, password_hash: str, role: str, display_name: str | None, created_by: str | None,
                must_change_password: bool, email: str, email_verified: bool) -> User:
        with self.connection() as conn:
            if conn.execute(f'SELECT 1 FROM "{USER_TABLE}" WHERE "Username" = ? COLLATE NOCASE',
                            (username,)).fetchone():
                raise AuthError(f"a user named '{username}' already exists")
            self._check_email_free(conn, email)
            cur = conn.execute(
                f'INSERT INTO "{USER_TABLE}" ("Username","DisplayName","PasswordHash","Role","Active",'
                f'"MustChangePassword","CreatedAt","CreatedBy","Email","EmailVerified") VALUES (?,?,?,?,1,?,?,?,?,?)',
                (username, display_name, password_hash, role, 1 if must_change_password else 0, _now(),
                 (created_by or "").strip() or None, email or None, 1 if email_verified else 0))
            user_id = int(cur.lastrowid)
        log.info("user '%s' created (%s)", username, role)
        return self._require(user_id)

    def create_user(self, username: str, password: str, role: str = ROLE_VIEWER, display_name: str = "",
                    created_by: str | None = None, must_change_password: bool = False, email: str = "",
                    email_verified: bool = True) -> User:
        """A user created by an administrator (dashboard *Users* page, ``user add``).  The
        address an administrator enters counts as confirmed."""
        username = validate_username(username)
        role = validate_role(role)
        check_password_strength(password)
        email = validate_email(email)
        return self._insert(username, hash_password(password), role, _display_name(display_name), created_by,
                            must_change_password, email, email_verified)

    def register(self, username: str, password: str, email: str, display_name: str = "",
                 role: str = ROLE_VIEWER) -> User:
        """Self-registration on the sign-in page: the e-mail address is required and must be
        confirmed with a code before the first sign-in."""
        username = validate_username(username)
        role = validate_role(role)
        if role == ROLE_ADMIN:
            raise AuthError("self-registered accounts cannot be administrators")
        email = validate_email(email)
        if not email:
            raise AuthError("an e-mail address is required - the confirmation code is sent there")
        check_password_strength(password)
        return self._insert(username, hash_password(password), role, _display_name(display_name),
                            "self-registration", False, email, False)

    def create_external_user(self, email: str, display_name: str = "", role: str = ROLE_VIEWER) -> User:
        """An account created by signing in with Google / Apple / Facebook: no password
        (:data:`EXTERNAL_PASSWORD`), the user name derived from the e-mail address."""
        email = validate_email(email)
        if not email:
            raise AuthError("the identity provider did not share an e-mail address")
        role = validate_role(role)
        if role == ROLE_ADMIN:
            raise AuthError("accounts created through an identity provider cannot be administrators")
        base = re.sub(r"[^A-Za-z0-9._@-]", "_", email)[:60]
        if len(base) < 2:
            base = "user"
        username, n = base, 1
        while self.get_by_username(username) is not None:
            n += 1
            username = f"{base[:60 - len(str(n))]}-{n}"
        return self._insert(username, EXTERNAL_PASSWORD, role, _display_name(display_name), "identity-provider",
                            False, email, False)

    def bootstrap_admin(self, password: str | None = None, username: str = "admin") -> tuple[User, str | None]:
        """Create the first administrator of an empty table.  The password: *password*, else
        ``$STOCK_DATALOAD_ADMIN_PASSWORD``, else a generated one - returned as the second value
        (shown once, never stored).  It must be changed at the first sign-in."""
        if self.count() > 0:
            raise AuthError("users already exist - add more users on the Users page or with 'user add'")
        generated = None
        if not password:
            password = os.environ.get(ENV_ADMIN_PASSWORD) or None
        if not password:
            password = generated = generate_password()
        user = self.create_user(username, password, role=ROLE_ADMIN, display_name="Initial administrator",
                                created_by="bootstrap", must_change_password=True)
        return user, generated

    # -- signing in ------------------------------------------------------------------------
    def authenticate(self, username: str, password: str) -> User | None:
        """The user if the name / password are right and the account is active (else None)."""
        user = self.get_by_username(username)
        if user is None:
            # the same work for unknown names: the answer time does not tell which names exist
            verify_password(password or "x", f"{HASH_ALGORITHM}${PBKDF2_ITERATIONS}$"
                                             f"{_b64(b'0' * SALT_BYTES)}${_b64(b'0' * 32)}")
            return None
        if not user.is_active or not user.has_password or not verify_password(password, user.password_hash):
            return None
        with self.connection() as conn:
            conn.execute(f'UPDATE "{USER_TABLE}" SET "LastLoginAt" = ? WHERE "UserId" = ?', (_now(), user.user_id))
        return self.get(user.user_id)

    # -- changing -------------------------------------------------------------------------------
    def set_password(self, user_id: int, password: str, must_change: bool = False) -> User:
        """A new password (administrator reset, reset link); *must_change* = change it at the next sign-in."""
        user = self._require(user_id)
        check_password_strength(password)
        with self.connection() as conn:
            conn.execute(f'UPDATE "{USER_TABLE}" SET "PasswordHash" = ?, "MustChangePassword" = ? WHERE "UserId" = ?',
                         (hash_password(password), 1 if must_change else 0, user.user_id))
        log.info("password of '%s' set", user.username)
        return self._require(user_id)

    def change_password(self, user_id: int, current_password: str, new_password: str) -> User:
        """The user changes their own password - the current one is required."""
        user = self._require(user_id)
        if not user.has_password:
            raise AuthError("this account has no password yet - use 'Forgot your password' to set one")
        if not verify_password(current_password, user.password_hash):
            raise AuthError("the current password is not correct")
        if current_password == new_password:
            raise AuthError("the new password must differ from the current one")
        return self.set_password(user_id, new_password, must_change=False)

    def _check_last_admin(self, conn: sqlite3.Connection, user: User) -> None:
        if not (user.is_admin and user.is_active):
            return
        others = conn.execute(f'SELECT COUNT(*) FROM "{USER_TABLE}" WHERE "Role" = ? AND "Active" = 1 AND '
                              f'"UserId" <> ?', (ROLE_ADMIN, user.user_id)).fetchone()[0]
        if not others:
            raise AuthError(f"'{user.username}' is the last active administrator - make another user "
                            f"administrator first")

    def update(self, user_id: int, role: str | None = None, active: bool | None = None,
               display_name: str | None = None, email: str | None = None) -> User:
        """Change role / active flag / display name / e-mail address (None = leave as is).  A new
        e-mail address must be confirmed again; the last active administrator stays one."""
        user = self._require(user_id)
        sets: list[str] = []
        params: list = []
        new_role = validate_role(role) if role is not None else user.role
        new_active = bool(active) if active is not None else user.is_active
        with self.connection() as conn:
            if (new_role != ROLE_ADMIN or not new_active) and (new_role != user.role or new_active != user.is_active):
                self._check_last_admin(conn, user)
            if role is not None:
                sets.append('"Role" = ?')
                params.append(new_role)
            if active is not None:
                sets.append('"Active" = ?')
                params.append(1 if new_active else 0)
            if display_name is not None:
                sets.append('"DisplayName" = ?')
                params.append(_display_name(display_name))
            if email is not None:
                address = validate_email(email)
                if address.lower() != (user.email or "").lower():
                    self._check_email_free(conn, address, user.user_id)
                    sets += ['"Email" = ?', '"EmailVerified" = ?']
                    # a new address is confirmed with a code at the next sign-in; no address = nothing to confirm
                    params += [address or None, 0 if address else 1]
                elif address != (user.email or ""):
                    sets.append('"Email" = ?')
                    params.append(address or None)
            if sets:
                conn.execute(f'UPDATE "{USER_TABLE}" SET {", ".join(sets)} WHERE "UserId" = ?',
                             (*params, user.user_id))
        return self._require(user_id)

    def mark_email_verified(self, user_id: int, verified: bool = True) -> User:
        user = self._require(user_id)
        with self.connection() as conn:
            conn.execute(f'UPDATE "{USER_TABLE}" SET "EmailVerified" = ? WHERE "UserId" = ?',
                         (1 if verified else 0, user.user_id))
        return self._require(user_id)

    def delete_user(self, user_id: int) -> None:
        user = self._require(user_id)
        with self.connection() as conn:
            self._check_last_admin(conn, user)
            conn.execute(f'DELETE FROM "{USER_TABLE}" WHERE "UserId" = ?', (user.user_id,))
        log.info("user '%s' deleted", user.username)
