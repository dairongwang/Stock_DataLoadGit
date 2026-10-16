"""
Authentication and authorization: user accounts, roles and the
``App_User`` table.

* Passwords are never stored - only a salted **PBKDF2-HMAC-SHA256** hash
  (standard library ``hashlib``), so nothing secret ever lives in the
  repository or in ``config.ini``.
* Three roles::

      viewer    read-only: overview, monitoring pages, formats, JSON api
      operator  + upload / convert / load / delete files, edit formats,
                  data correction
      admin     + settings and user management

* **Initial administrator.**  Before any account exists, create one with

      python -m stock_dataload user add admin --role admin      (prompts for a password)

  or simply start the dashboard: with an empty user table it creates the
  ``admin`` account with a **random one-time password printed to the
  console** (or taken from ``STOCK_DATALOAD_ADMIN_PASSWORD``) that must
  be changed at the first login.

* The dashboard's session secret is read from ``STOCK_DATALOAD_SECRET``
  or generated once into a git-ignored file next to the database
  (:func:`load_secret_key`).
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
from pathlib import Path
from typing import Iterator

from .config import Settings

log = logging.getLogger(__name__)

USER_TABLE = "App_User"

ROLE_VIEWER = "viewer"
ROLE_OPERATOR = "operator"
ROLE_ADMIN = "admin"
ROLES: tuple[str, ...] = (ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)
ROLE_RANK = {role: i for i, role in enumerate(ROLES)}
ROLE_LABELS = {
    ROLE_VIEWER: "Viewer - read-only access to the overview and the monitoring pages",
    ROLE_OPERATOR: "Operator - upload, convert, load, manage formats and correct data",
    ROLE_ADMIN: "Administrator - everything, including settings and user management",
}

DEFAULT_ADMIN_USERNAME = "admin"
ENV_ADMIN_PASSWORD = "STOCK_DATALOAD_ADMIN_PASSWORD"
ENV_SECRET = "STOCK_DATALOAD_SECRET"

MIN_PASSWORD_LENGTH = 8
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._@-]{2,64}$")
#: PBKDF2 work factor (OWASP recommendation for SHA-256); lowered by the test-suite
PBKDF2_ITERATIONS = 600_000

_DDL = f"""
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
USER_COLUMNS = ("UserId", "Username", "DisplayName", "PasswordHash", "Role", "Active",
                "MustChangePassword", "CreatedAt", "CreatedBy", "LastLoginAt")


class AuthError(Exception):
    """Invalid user data (bad username / password / role) or an impossible change."""


# ---------------------------------------------------------------------------
# password hashing (stdlib only)
# ---------------------------------------------------------------------------
def hash_password(password: str, iterations: int | None = None) -> str:
    """``pbkdf2_sha256$<iterations>$<salt>$<hash>`` with a random 16-byte salt."""
    if not isinstance(password, str) or not password:
        raise AuthError("password must not be empty")
    iterations = iterations or PBKDF2_ITERATIONS
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations, base64.b64encode(salt).decode("ascii"), base64.b64encode(digest).decode("ascii"))


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of *password* against a :func:`hash_password` value."""
    try:
        algorithm, iterations, salt_b64, hash_b64 = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(hash_b64)
        digest = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), salt, int(iterations))
    except (ValueError, TypeError, AttributeError):
        return False
    return hmac.compare_digest(digest, expected)


def check_password_strength(password: str) -> None:
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(f"password must be at least {MIN_PASSWORD_LENGTH} characters long")


def generate_password(length: int = 14) -> str:
    """A random, readable one-time password (letters + digits, no ambiguous characters)."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def validate_username(username: str) -> str:
    username = (username or "").strip()
    if not _USERNAME_RE.match(username):
        raise AuthError("username must be 2-64 characters: letters, digits, '.', '_', '@' or '-'")
    return username


def validate_role(role: str) -> str:
    role = (role or "").strip().lower()
    if role not in ROLES:
        raise AuthError(f"role must be one of {', '.join(ROLES)}")
    return role


def role_allows(user_role: str, required: str) -> bool:
    """True if *user_role* is at least as powerful as *required*."""
    return ROLE_RANK.get(user_role, -1) >= ROLE_RANK.get(required, len(ROLES))


# ---------------------------------------------------------------------------
# session secret
# ---------------------------------------------------------------------------
def load_secret_key(settings: Settings) -> str:
    """The dashboard's session-signing secret: ``$STOCK_DATALOAD_SECRET``, else a
    random key generated once into ``settings.secret_key_file`` (git-ignored,
    owner-readable only).  Never ships with the repository."""
    env = os.environ.get(ENV_SECRET)
    if env:
        return env
    path = Path(settings.secret_key_file)
    try:
        if path.is_file():
            key = path.read_text(encoding="utf-8").strip()
            if key:
                return key
        path.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_hex(32)
        path.write_text(key + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:  # pragma: no cover - e.g. Windows without POSIX permissions
            pass
        log.info("generated a new dashboard secret key in %s", path)
        return key
    except OSError as exc:  # pragma: no cover - unwritable location: fall back to a per-process key
        log.warning("could not persist the secret key in %s (%s); sessions will not survive a restart", path, exc)
        return secrets.token_hex(32)


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------
@dataclass
class User:
    user_id: int
    username: str
    display_name: str | None
    password_hash: str
    role: str
    active: int
    must_change_password: int
    created_at: str
    created_by: str | None
    last_login_at: str | None

    @property
    def is_active(self) -> bool:
        return bool(self.active)

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    def can(self, required_role: str) -> bool:
        return self.is_active and role_allows(self.role, required_role)

    def public(self) -> dict:
        """Safe representation (no hash) for templates / JSON."""
        return {"user_id": self.user_id, "username": self.username, "display_name": self.display_name,
                "role": self.role, "active": bool(self.active),
                "must_change_password": bool(self.must_change_password), "created_at": self.created_at,
                "created_by": self.created_by, "last_login_at": self.last_login_at}


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


class UserStore:
    """User accounts in the ``App_User`` table of the app's SQLite database."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._table_ready = False

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        path = self.settings.sqlite_path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            if not self._table_ready:
                conn.execute(_DDL)
                self._table_ready = True
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

    # -- queries ---------------------------------------------------------
    def _select(self, conn: sqlite3.Connection, where: str = "", params: tuple = ()) -> list[User]:
        cols = ", ".join(f'"{c}"' for c in USER_COLUMNS)
        sql = f'SELECT {cols} FROM "{USER_TABLE}"' + (f" WHERE {where}" if where else "") + ' ORDER BY "Username"'
        return [User(*r) for r in conn.execute(sql, params).fetchall()]

    def count(self, active_only: bool = False) -> int:
        with self.connection() as conn:
            sql = f'SELECT COUNT(*) FROM "{USER_TABLE}"' + (' WHERE "Active" = 1' if active_only else "")
            return int(conn.execute(sql).fetchone()[0])

    def list_users(self) -> list[User]:
        with self.connection() as conn:
            return self._select(conn)

    def get(self, user_id: int) -> User | None:
        with self.connection() as conn:
            users = self._select(conn, '"UserId" = ?', (int(user_id),))
        return users[0] if users else None

    def get_by_username(self, username: str) -> User | None:
        with self.connection() as conn:
            users = self._select(conn, '"Username" = ?', ((username or "").strip(),))
        return users[0] if users else None

    def admin_count(self) -> int:
        with self.connection() as conn:
            return int(conn.execute(f'SELECT COUNT(*) FROM "{USER_TABLE}" WHERE "Role" = ? AND "Active" = 1',
                                    (ROLE_ADMIN,)).fetchone()[0])

    # -- changes ---------------------------------------------------------
    def create_user(self, username: str, password: str, role: str = ROLE_VIEWER, display_name: str = "",
                    created_by: str = "", must_change_password: bool = False) -> User:
        username = validate_username(username)
        role = validate_role(role)
        check_password_strength(password)
        if self.get_by_username(username) is not None:
            raise AuthError(f"user '{username}' already exists")
        with self.connection() as conn:
            cur = conn.execute(
                f'INSERT INTO "{USER_TABLE}" ("Username","DisplayName","PasswordHash","Role","Active",'
                '"MustChangePassword","CreatedAt","CreatedBy") VALUES (?,?,?,?,1,?,?,?)',
                (username, (display_name or "").strip() or None, hash_password(password), role,
                 1 if must_change_password else 0, _now(), created_by or None))
            user_id = int(cur.lastrowid)
        log.info("user '%s' created with role %s", username, role)
        return self.get(user_id)

    def authenticate(self, username: str, password: str) -> User | None:
        """The user if *username* / *password* are valid and the account is active."""
        user = self.get_by_username(username)
        if user is None or not user.is_active or not verify_password(password, user.password_hash):
            return None
        with self.connection() as conn:
            conn.execute(f'UPDATE "{USER_TABLE}" SET "LastLoginAt" = ? WHERE "UserId" = ?', (_now(), user.user_id))
        return self.get(user.user_id)

    def set_password(self, user_id: int, password: str, must_change: bool = False) -> User:
        check_password_strength(password)
        self._require(user_id)
        with self.connection() as conn:
            conn.execute(f'UPDATE "{USER_TABLE}" SET "PasswordHash" = ?, "MustChangePassword" = ? WHERE "UserId" = ?',
                         (hash_password(password), 1 if must_change else 0, int(user_id)))
        return self.get(user_id)

    def change_password(self, user_id: int, current_password: str, new_password: str) -> User:
        """A user changing their own password (must know the current one)."""
        user = self._require(user_id)
        if not verify_password(current_password, user.password_hash):
            raise AuthError("the current password is not correct")
        if current_password == new_password:
            raise AuthError("the new password must differ from the current one")
        return self.set_password(user_id, new_password, must_change=False)

    def update(self, user_id: int, role: str | None = None, active: bool | None = None,
               display_name: str | None = None) -> User:
        user = self._require(user_id)
        new_role = validate_role(role) if role is not None else user.role
        new_active = user.is_active if active is None else bool(active)
        if user.is_admin and user.is_active and (new_role != ROLE_ADMIN or not new_active) and self.admin_count() <= 1:
            raise AuthError("cannot demote or deactivate the last active administrator")
        with self.connection() as conn:
            conn.execute(f'UPDATE "{USER_TABLE}" SET "Role" = ?, "Active" = ?, "DisplayName" = ? WHERE "UserId" = ?',
                         (new_role, 1 if new_active else 0,
                          (display_name.strip() or None) if display_name is not None else user.display_name,
                          int(user_id)))
        return self.get(user_id)

    def delete_user(self, user_id: int) -> None:
        user = self._require(user_id)
        if user.is_admin and user.is_active and self.admin_count() <= 1:
            raise AuthError("cannot delete the last active administrator")
        with self.connection() as conn:
            conn.execute(f'DELETE FROM "{USER_TABLE}" WHERE "UserId" = ?', (int(user_id),))
        log.info("user '%s' deleted", user.username)

    def _require(self, user_id: int) -> User:
        user = self.get(user_id)
        if user is None:
            raise AuthError(f"user {user_id} does not exist")
        return user

    # -- bootstrap -------------------------------------------------------
    def bootstrap_admin(self, password: str | None = None,
                        username: str = DEFAULT_ADMIN_USERNAME) -> tuple[User, str | None]:
        """Create the initial administrator if **no user exists yet**.

        Returns ``(user, generated_password)``; the generated password is
        ``None`` when *password* (or ``$STOCK_DATALOAD_ADMIN_PASSWORD``) was
        supplied.  The account must change its password at the first login.
        Raises :class:`AuthError` if users already exist.
        """
        if self.count() > 0:
            raise AuthError("users already exist - add further accounts on the user management page")
        supplied = password or os.environ.get(ENV_ADMIN_PASSWORD) or ""
        generated = None if supplied else generate_password()
        user = self.create_user(username, supplied or generated, role=ROLE_ADMIN,
                                display_name="Initial administrator", created_by="bootstrap",
                                must_change_password=True)
        return user, generated
