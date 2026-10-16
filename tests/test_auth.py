"""Unit tests for stock_dataload.auth (users, roles, password hashing, bootstrap, secret key)."""

import os
import sqlite3
import unittest
import unittest.mock as mock

from stock_dataload.auth import (ENV_ADMIN_PASSWORD, ENV_SECRET, USER_TABLE, AuthError, UserStore,
                                 check_password_strength, generate_password, hash_password, load_secret_key,
                                 role_allows, validate_role, validate_username, verify_password)

from .helpers import TempProjectTestCase


class PasswordTests(unittest.TestCase):
    def test_hash_and_verify(self):
        stored = hash_password("correct horse", iterations=1000)
        self.assertTrue(stored.startswith("pbkdf2_sha256$1000$"))
        self.assertTrue(verify_password("correct horse", stored))
        self.assertFalse(verify_password("wrong", stored))
        self.assertFalse(verify_password("", stored))

    def test_hashes_are_salted(self):
        self.assertNotEqual(hash_password("same", 1000), hash_password("same", 1000))

    def test_empty_password_rejected(self):
        with self.assertRaises(AuthError):
            hash_password("")

    def test_verify_rejects_garbage(self):
        self.assertFalse(verify_password("x", "not-a-hash"))
        self.assertFalse(verify_password("x", "md5$1$abc$def"))
        self.assertFalse(verify_password("x", None))

    def test_strength_and_generator(self):
        with self.assertRaises(AuthError):
            check_password_strength("short")
        check_password_strength("long enough")
        pw = generate_password()
        self.assertEqual(len(pw), 14)
        self.assertNotEqual(pw, generate_password())
        check_password_strength(pw)


class ValidationTests(unittest.TestCase):
    def test_username(self):
        self.assertEqual(validate_username("  Jane.Doe@corp "), "Jane.Doe@corp")
        for bad in ("", "a", "has space", "x" * 65, "semi;colon"):
            with self.assertRaises(AuthError):
                validate_username(bad)

    def test_role(self):
        self.assertEqual(validate_role(" Admin "), "admin")
        with self.assertRaises(AuthError):
            validate_role("superuser")

    def test_role_allows(self):
        self.assertTrue(role_allows("admin", "viewer"))
        self.assertTrue(role_allows("operator", "operator"))
        self.assertFalse(role_allows("viewer", "operator"))
        self.assertFalse(role_allows("nope", "viewer"))
        self.assertFalse(role_allows("admin", "nope"))


class UserStoreTests(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.store = UserStore(self.settings)

    def test_table_created(self):
        self.store.ensure_table()
        with sqlite3.connect(self.settings.sqlite_path) as conn:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn(USER_TABLE, names)
        self.assertEqual(self.store.count(), 0)
        self.assertEqual(self.store.list_users(), [])

    def test_create_and_get(self):
        user = self.store.create_user("jane", "Jane-Pass-1", role="operator", display_name="Jane Doe",
                                      created_by="tests")
        self.assertEqual(user.username, "jane")
        self.assertEqual(user.role, "operator")
        self.assertTrue(user.is_active)
        self.assertFalse(user.is_admin)
        self.assertEqual(user.display_name, "Jane Doe")
        self.assertEqual(user.created_by, "tests")
        self.assertNotIn("Jane-Pass-1", user.password_hash)
        self.assertEqual(self.store.get(user.user_id).username, "jane")
        self.assertEqual(self.store.get_by_username("JANE").user_id, user.user_id)     # case-insensitive
        self.assertIsNone(self.store.get(999))
        self.assertIsNone(self.store.get_by_username("nobody"))
        self.assertEqual(user.public()["role"], "operator")
        self.assertNotIn("password_hash", user.public())

    def test_create_validates(self):
        self.store.create_user("jane", "Jane-Pass-1")
        with self.assertRaisesRegex(AuthError, "already exists"):
            self.store.create_user("Jane", "Other-Pass-1")
        with self.assertRaises(AuthError):
            self.store.create_user("bad name", "Jane-Pass-1")
        with self.assertRaises(AuthError):
            self.store.create_user("ok", "short")
        with self.assertRaises(AuthError):
            self.store.create_user("ok", "Jane-Pass-1", role="god")

    def test_authenticate(self):
        user = self.store.create_user("jane", "Jane-Pass-1")
        self.assertIsNone(user.last_login_at)
        ok = self.store.authenticate("jane", "Jane-Pass-1")
        self.assertEqual(ok.user_id, user.user_id)
        self.assertTrue(ok.last_login_at)
        self.assertIsNone(self.store.authenticate("jane", "wrong"))
        self.assertIsNone(self.store.authenticate("nobody", "Jane-Pass-1"))
        self.store.update(user.user_id, active=False)
        self.assertIsNone(self.store.authenticate("jane", "Jane-Pass-1"))

    def test_set_and_change_password(self):
        user = self.store.create_user("jane", "Jane-Pass-1")
        self.store.set_password(user.user_id, "New-Pass-22", must_change=True)
        self.assertTrue(self.store.get(user.user_id).must_change_password)
        self.assertIsNotNone(self.store.authenticate("jane", "New-Pass-22"))
        with self.assertRaisesRegex(AuthError, "not correct"):
            self.store.change_password(user.user_id, "wrong", "Another-Pass-3")
        with self.assertRaisesRegex(AuthError, "must differ"):
            self.store.change_password(user.user_id, "New-Pass-22", "New-Pass-22")
        changed = self.store.change_password(user.user_id, "New-Pass-22", "Another-Pass-3")
        self.assertFalse(changed.must_change_password)
        self.assertIsNotNone(self.store.authenticate("jane", "Another-Pass-3"))
        with self.assertRaises(AuthError):
            self.store.set_password(999, "Whatever-Pass-1")

    def test_update_role_active_and_display_name(self):
        self.store.create_user("root", "Root-Pass-1", role="admin")
        user = self.store.create_user("jane", "Jane-Pass-1")
        updated = self.store.update(user.user_id, role="operator", display_name="  Jane  ")
        self.assertEqual((updated.role, updated.display_name, updated.is_active), ("operator", "Jane", True))
        updated = self.store.update(user.user_id, active=False)
        self.assertFalse(updated.is_active)
        self.assertEqual(updated.display_name, "Jane")          # untouched when not given
        updated = self.store.update(user.user_id, display_name="")
        self.assertIsNone(updated.display_name)
        with self.assertRaises(AuthError):
            self.store.update(999, role="admin")

    def test_last_admin_is_protected(self):
        root = self.store.create_user("root", "Root-Pass-1", role="admin")
        self.assertEqual(self.store.admin_count(), 1)
        with self.assertRaisesRegex(AuthError, "last active administrator"):
            self.store.update(root.user_id, role="viewer")
        with self.assertRaisesRegex(AuthError, "last active administrator"):
            self.store.update(root.user_id, active=False)
        with self.assertRaisesRegex(AuthError, "last active administrator"):
            self.store.delete_user(root.user_id)
        # with a second admin everything is allowed
        self.store.create_user("root2", "Root-Pass-2", role="admin")
        self.store.update(root.user_id, role="viewer")
        self.assertEqual(self.store.admin_count(), 1)

    def test_delete(self):
        self.store.create_user("root", "Root-Pass-1", role="admin")
        jane = self.store.create_user("jane", "Jane-Pass-1")
        self.store.delete_user(jane.user_id)
        self.assertIsNone(self.store.get(jane.user_id))
        self.assertEqual(self.store.count(), 1)
        with self.assertRaises(AuthError):
            self.store.delete_user(jane.user_id)

    def test_connection_rolls_back_on_error(self):
        with self.assertRaises(RuntimeError):
            with self.store.connection() as conn:
                conn.execute(f'INSERT INTO "{USER_TABLE}" ("Username","PasswordHash","Role","CreatedAt") '
                             "VALUES ('x','h','viewer','t')")
                raise RuntimeError("abort")
        self.assertEqual(self.store.count(), 0)

    def test_count_active_only(self):
        self.store.create_user("root", "Root-Pass-1", role="admin")
        jane = self.store.create_user("jane", "Jane-Pass-1")
        self.store.update(jane.user_id, active=False)
        self.assertEqual(self.store.count(), 2)
        self.assertEqual(self.store.count(active_only=True), 1)


class BootstrapTests(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.store = UserStore(self.settings)

    def test_generates_one_time_password(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ENV_ADMIN_PASSWORD, None)
            user, generated = self.store.bootstrap_admin()
        self.assertEqual(user.username, "admin")
        self.assertEqual(user.role, "admin")
        self.assertTrue(user.must_change_password)
        self.assertEqual(user.created_by, "bootstrap")
        self.assertIsNotNone(generated)
        self.assertIsNotNone(self.store.authenticate("admin", generated))

    def test_password_from_environment(self):
        with mock.patch.dict(os.environ, {ENV_ADMIN_PASSWORD: "Env-Admin-Pass-1"}):
            user, generated = self.store.bootstrap_admin()
        self.assertIsNone(generated)
        self.assertIsNotNone(self.store.authenticate("admin", "Env-Admin-Pass-1"))

    def test_explicit_password_and_username(self):
        user, generated = self.store.bootstrap_admin("Given-Pass-1", username="root")
        self.assertEqual(user.username, "root")
        self.assertIsNone(generated)

    def test_refused_when_users_exist(self):
        self.store.create_user("jane", "Jane-Pass-1")
        with self.assertRaisesRegex(AuthError, "already exist"):
            self.store.bootstrap_admin()


class SecretKeyTests(TempProjectTestCase):
    def test_generated_once_into_file(self):
        self.settings.secret_key_file = self.root / "data" / ".secret_key"
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ENV_SECRET, None)
            key = load_secret_key(self.settings)
            self.assertEqual(len(key), 64)
            self.assertTrue(self.settings.secret_key_file.is_file())
            self.assertEqual(load_secret_key(self.settings), key)             # stable across restarts
            if os.name != "nt":
                self.assertEqual(self.settings.secret_key_file.stat().st_mode & 0o777, 0o600)

    def test_empty_file_is_regenerated(self):
        self.settings.secret_key_file = self.root / "k"
        self.settings.secret_key_file.write_text("")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ENV_SECRET, None)
            self.assertTrue(load_secret_key(self.settings))

    def test_environment_wins(self):
        self.settings.secret_key_file = self.root / "k"
        with mock.patch.dict(os.environ, {ENV_SECRET: "from-env"}):
            self.assertEqual(load_secret_key(self.settings), "from-env")
        self.assertFalse(self.settings.secret_key_file.exists())

    def test_secret_key_not_in_repository(self):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        self.assertFalse((root / "data" / ".secret_key").exists() and
                         "data/.secret_key" not in (root / ".gitignore").read_text())
        self.assertIn("data/.secret_key", (root / ".gitignore").read_text())


if __name__ == "__main__":
    unittest.main()
