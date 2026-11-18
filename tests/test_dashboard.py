"""
The dashboard (Flask test client): every page renders, sign-in / sign-out, roles, user
management, self-registration with e-mail verification, password reset by e-mail link and
two-factor authentication (e-mail code, authenticator app, recovery codes).

:class:`DashboardTestCase` is the base of the other dashboard test modules
(``from .test_dashboard import OPERATOR, VIEWER, DashboardTestCase``): an app on isolated
settings with three dummy accounts, signed in as the administrator.  E-mail and sms use the
``memory`` stubs - codes are read from ``self.state.two_factor.email.last``.
"""

from __future__ import annotations

import html
import re
import shutil
import unittest
from pathlib import Path

from stock_dataload.accounts import METHOD_TOTP, PRINCIPAL_STAFF, totp
from stock_dataload.auth import UserStore
from stock_dataload.config import load_settings, save_settings
from stock_dataload.dashboard import create_app
from stock_dataload.dashboard.app import required_role

from .helpers import SAMPLES, TempProjectTestCase

#: (user name, password) of the accounts every dashboard test has - dummies, test-only
ADMIN = ("dummy_admin", "Dummy-Admin-Pass-1")
OPERATOR = ("dummy_operator", "Dummy-Operator-Pass-1")
VIEWER = ("dummy_viewer", "Dummy-Viewer-Pass-1")

_FLASH_RE = re.compile(r'class="flash[^"]*">(.*?)</div>', re.S)


class DashboardTestCase(TempProjectTestCase):
    """A dashboard on isolated settings (own folders, database, config.ini), signed in as ADMIN."""

    #: sign-in providers offered (through the fake identity gateway) - none by default
    OAUTH_PROVIDERS: tuple[str, ...] = ()

    def setUp(self):
        super().setUp()
        self.settings.config_file = self.root / "config.ini"
        self.settings.mail_provider = self.settings.sms_provider = "memory"
        self.settings.oauth_gateway = "fake"
        self.settings.oauth_providers = self.OAUTH_PROVIDERS
        save_settings(self.settings, self.settings.config_file)
        self.settings = load_settings(self.settings.config_file)
        users = UserStore(self.settings)
        for (name, password), role in ((ADMIN, "admin"), (OPERATOR, "operator"), (VIEWER, "viewer")):
            users.create_user(name, password, role=role, created_by="tests")
        self.app = create_app(self.settings, bootstrap=False)
        self.app.config["TESTING"] = True
        self.state = self.app.extensions["state"]
        self.client = self.app.test_client()
        self.login(*ADMIN)

    def tearDown(self):
        self.app.extensions["bulk_scheduler"].join(60)     # a 'Run now' of a schedule ...
        self.app.extensions["api_loads"].join(60)          # ... and a background API load must not outlive the folders
        super().tearDown()

    # -- helpers --------------------------------------------------------------------
    def login(self, username: str, password: str):
        r = self.client.post("/login", data={"username": username, "password": password})
        self.assertEqual(r.status_code, 302, f"sign-in of {username} failed")
        return r

    def logout(self):
        return self.client.post("/logout")

    @staticmethod
    def flash_messages(page: str) -> list[str]:
        return [html.unescape(re.sub(r"<[^>]+>", "", m)).strip() for m in _FLASH_RE.findall(page)]

    def flashes(self, response) -> str:
        """The flash messages shown after *response* (a redirect is followed), one per line."""
        if response.status_code in (301, 302, 303) and response.headers.get("Location"):
            page = self.client.get(response.headers["Location"]).get_data(as_text=True)
        else:
            page = response.get_data(as_text=True)
        return "\n".join(self.flash_messages(page))

    def drop(self, name: str, folder: str = "conversion") -> Path:
        """Copy a sample file into a folder of the dashboard (conversion / loading ...)."""
        dest = getattr(self.state.settings, f"{folder}_dir") / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(SAMPLES / name, dest)
        return dest


class PageTests(DashboardTestCase):
    PAGES = ("/", "/formats", "/formats/new", "/settings", "/monitor", "/monitor/logs", "/monitor/errors",
             "/monitor/data-errors", "/monitor/history", "/correct", "/promote", "/active/daily", "/active/weekly",
             "/active/monthly", "/calendar", "/symbols", "/users", "/users/new", "/account", "/api-load", "/docs",
             "/customers", "/browse/loading_dir", "/api/status", "/api/logs", "/api/errors", "/api/data-errors",
             "/api/history", "/api/monitor", "/api/promotions", "/api/active/daily", "/api/symbols",
             "/api/no-trading-days", "/api/api-load", "/api/api-load/runs", "/api/customers", "/api/corrections")

    def test_every_page_renders_for_the_admin(self):
        for url in self.PAGES:
            with self.subTest(url=url):
                r = self.client.get(url)
                self.assertEqual(r.status_code, 200, f"{url}: {r.get_data(as_text=True)[:300]}")

    def test_preview_of_a_sample(self):
        self.drop("NASDAQ_20250123.csv")
        r = self.client.get("/preview?file=NASDAQ_20250123.csv")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:300])
        self.assertIn("AACG", r.get_data(as_text=True))

    def test_sign_in_required(self):
        self.logout()
        r = self.client.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])
        self.assertEqual(self.client.get("/api/status").status_code, 401)
        self.assertEqual(self.client.get("/login").status_code, 200)
        r = self.client.post("/login", data={"username": ADMIN[0], "password": "wrong-dummy-pass"})
        self.assertEqual(r.status_code, 401)
        self.assertIn("wrong password", r.get_data(as_text=True))
        self.assertEqual(self.state.audit.fetch_logs(limit=1, component="auth")[0].event, "login_failed")

    def test_roles(self):
        self.assertEqual(required_role("users"), "admin")
        self.assertEqual(required_role("load"), "operator")
        self.assertEqual(required_role("index"), "viewer")
        self.logout()
        self.login(*VIEWER)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/users").status_code, 403)
        self.assertEqual(self.client.post("/load", data={}).status_code, 403)
        self.assertEqual(self.client.get("/api/customers").status_code, 403)
        self.logout()
        self.login(*OPERATOR)
        self.assertEqual(self.client.get("/users").status_code, 403)
        self.assertEqual(self.client.post("/load", data={}).status_code, 302)


class FormatsAndSettingsTests(DashboardTestCase):
    """R5: business users configure custom file formats and the folders on the dashboard."""

    def test_custom_format_saved_used_and_deleted(self):
        r = self.client.post("/formats/new", data={"name": "dummy_vendor_json", "file_type": "json",
                                                   "file_pattern": "vendor_*.json", "record_path": "data",
                                                   "mapping_mode": "auto", "action": "save"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.state.store.get("dummy_vendor_json").record_path, "data")
        self.assertIn("dummy_vendor_json", self.client.get("/formats").get_data(as_text=True))
        self.assertEqual(self.client.get("/formats/dummy_vendor_json/edit").status_code, 200)
        self.assertEqual(self.client.get("/api/formats/dummy_vendor_json").get_json()["record_path"], "data")
        r = self.client.post("/formats/dummy_vendor_json/delete")
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("dummy_vendor_json", [f.name for f in self.state.store.all()])

    def test_upload_convert_and_load(self):
        from io import BytesIO
        data = (SAMPLES / "NASDAQ_20250123.csv").read_bytes()
        r = self.client.post("/upload", data={"target": "conversion_dir", "files": (BytesIO(data), "NASDAQ_20250123.csv")},
                             content_type="multipart/form-data")
        self.assertIn("Uploaded NASDAQ_20250123.csv", self.flashes(r))
        r = self.client.post("/convert", data={"file": "NASDAQ_20250123.csv"})
        self.assertEqual(r.status_code, 302)
        converted = self.state.settings.loading_dir / "NASDAQ_20250123.txt"
        self.assertEqual(converted.read_text(encoding="utf-8"), (SAMPLES / "NASDAQ_20250123.txt").read_text(encoding="utf-8"))
        r = self.client.post("/load", data={})
        self.assertIn("NASDAQ_20250123.txt: loaded (4092 rows", self.flashes(r))

    def test_folders_on_the_settings_page(self):
        from .test_dashboard_promotion import PromotionDashboardTests
        form = PromotionDashboardTests.settings_form(self, conversion_dir="dummy_drop_zone", loading_dir="dummy_to_load")
        r = self.client.post("/settings", data=form)
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:500])
        reloaded = load_settings(self.settings.config_file)
        self.assertEqual((reloaded.conversion_dir, reloaded.loading_dir),
                         (self.root / "dummy_drop_zone", self.root / "dummy_to_load"))
        self.assertTrue((self.root / "dummy_drop_zone").is_dir())
        self.assertEqual(self.state.settings.loading_dir, self.root / "dummy_to_load")    # the app was reloaded


class UserManagementTests(DashboardTestCase):
    def test_add_edit_delete_a_user(self):
        r = self.client.post("/users/new", data={"username": "dummy_jane", "password": "Dummy-Jane-Pass-1",
                                                 "confirm_password": "Dummy-Jane-Pass-1", "role": "operator",
                                                 "display_name": "Jane Dummy", "email": "jane.dummy@example.com"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("User 'dummy_jane' created with role operator", self.flashes(r))
        jane = self.state.users.get_by_username("dummy_jane")
        self.assertTrue(jane.is_email_verified)                    # an address entered by an admin counts as confirmed
        r = self.client.post("/users/new", data={"username": "dummy_jane", "password": "Dummy-Jane-Pass-1",
                                                 "confirm_password": "Dummy-Jane-Pass-1"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("already exists", r.get_data(as_text=True))
        self.assertEqual(self.client.get(f"/users/{jane.user_id}/edit").status_code, 200)
        r = self.client.post(f"/users/{jane.user_id}/edit", data={"role": "viewer", "display_name": "Jane",
                                                                  "email": "jane.dummy@example.com"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.state.users.get(jane.user_id).role, "viewer")
        self.assertFalse(self.state.users.get(jane.user_id).is_active)   # the box was not ticked
        r = self.client.post(f"/users/{jane.user_id}/delete")
        self.assertIn("User 'dummy_jane' deleted", self.flashes(r))
        me = self.state.users.get_by_username(ADMIN[0])
        r = self.client.post(f"/users/{me.user_id}/delete")
        self.assertIn("you cannot delete your own account", self.flashes(r))

    def test_must_change_password(self):
        self.client.post("/users/new", data={"username": "dummy_temp", "password": "Dummy-Temp-Pass-1",
                                             "confirm_password": "Dummy-Temp-Pass-1", "role": "viewer",
                                             "must_change_password": "on"})
        self.logout()
        r = self.client.post("/login", data={"username": "dummy_temp", "password": "Dummy-Temp-Pass-1"})
        self.assertIn("/account", r.headers["Location"])
        self.assertIn("/account", self.client.get("/monitor").headers["Location"])
        r = self.client.post("/account", data={"current_password": "Dummy-Temp-Pass-1", "new_password": "Dummy-Own-Pass-2",
                                               "confirm_password": "Dummy-Own-Pass-2"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get("/monitor").status_code, 200)


class AccountFlowTests(DashboardTestCase):
    def test_self_registration_with_email_verification(self):
        self.logout()
        self.assertEqual(self.client.get("/register").status_code, 200)
        r = self.client.post("/register", data={"username": "dummy_new", "password": "Dummy-New-Pass-1",
                                                "confirm_password": "Dummy-New-Pass-1",
                                                "email": "new.dummy@example.com"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/verify-email", r.headers["Location"])
        message = self.state.two_factor.email.last
        self.assertEqual(message.to, "new.dummy@example.com")
        self.assertEqual(self.client.post("/verify-email", data={"code": "000000"}).status_code, 400)
        r = self.client.post("/verify-email", data={"code": message.code})
        self.assertEqual(r.status_code, 302)
        user = self.state.users.get_by_username("dummy_new")
        self.assertTrue(user.is_email_verified)
        self.assertEqual(user.role, "viewer")
        self.assertEqual(self.client.get("/").status_code, 200)           # signed in

    def test_password_reset_by_email_link(self):
        self.state.users.update(self.state.users.get_by_username(VIEWER[0]).user_id, email="viewer.dummy@example.com")
        self.logout()
        r = self.client.post("/forgot-password", data={"user": VIEWER[0]})
        self.assertEqual(r.status_code, 200)
        link = self.state.two_factor.email.last.link
        path = link.split("localhost", 1)[1]
        self.assertEqual(self.client.get(path).status_code, 200)
        r = self.client.post(path, data={"new_password": "Dummy-Reset-Pass-9", "confirm_password": "Dummy-Reset-Pass-9"})
        self.assertEqual(r.status_code, 302)
        self.assertIsNotNone(self.state.users.authenticate(VIEWER[0], "Dummy-Reset-Pass-9"))
        self.assertIn("/forgot-password", self.client.get(path).headers["Location"])     # single use
        # unknown users get the same answer (no account enumeration)
        self.assertEqual(self.client.post("/forgot-password", data={"user": "nobody"}).status_code, 200)

    def test_two_factor_with_email_codes(self):
        me = self.state.users.get_by_username(ADMIN[0])
        self.state.users.update(me.user_id, email="admin.dummy@example.com")
        self.state.users.mark_email_verified(me.user_id)
        self.client.post("/account/security/email/begin")
        code = self.state.two_factor.email.last.code
        r = self.client.post("/account/security/email/confirm", data={"code": code})
        self.assertEqual(r.status_code, 200)
        self.assertIn("recovery codes", r.get_data(as_text=True))
        self.assertEqual(self.state.two_factor.recovery_codes_left(PRINCIPAL_STAFF, me.user_id), 10)
        self.logout()
        r = self.client.post("/login", data={"username": ADMIN[0], "password": ADMIN[1]})
        self.assertIn("/login/verify", r.headers["Location"])
        self.assertEqual(self.client.get("/").status_code, 302)               # not signed in yet
        self.client.post("/login/verify", data={"method": "email", "action": "send"})
        code = self.state.two_factor.email.last.code
        self.assertEqual(self.client.post("/login/verify", data={"method": "email", "code": "111111"}).status_code, 401)
        r = self.client.post("/login/verify", data={"method": "email", "code": code})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_two_factor_with_an_authenticator_app(self):
        me = self.state.users.get_by_username(OPERATOR[0])
        self.logout()
        self.login(*OPERATOR)
        page = self.client.post("/account/security/totp/begin").get_data(as_text=True)
        self.assertIn("otpauth://", page)
        secret = self.state.two_factor.get_method(PRINCIPAL_STAFF, me.user_id, METHOD_TOTP).secret
        self.assertEqual(self.client.post("/account/security/totp/confirm", data={"code": "000000"}).status_code, 400)
        self.assertEqual(self.client.post("/account/security/totp/confirm", data={"code": totp(secret)}).status_code, 200)
        self.logout()
        self.client.post("/login", data={"username": OPERATOR[0], "password": OPERATOR[1]})
        r = self.client.post("/login/verify", data={"method": "totp", "code": totp(secret)})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get("/api-load").status_code, 200)
        # an administrator resets the second factor of a locked-out user
        self.logout()
        self.login(*ADMIN)
        r = self.client.post(f"/users/{me.user_id}/reset-2fa")
        self.assertIn("1 method(s) removed", self.flashes(r))

    def test_stub_senders_are_reported(self):
        self.assertTrue(self.state.two_factor.email.stub)
        self.assertEqual(self.state.two_factor.email.name, "memory")


class BootstrapTests(TempProjectTestCase):
    def test_first_start_creates_the_admin(self):
        import io
        from contextlib import redirect_stdout
        self.settings.mail_provider = self.settings.sms_provider = "memory"
        out = io.StringIO()
        with redirect_stdout(out):
            app = create_app(self.settings)
        self.assertIn("Initial administrator created", out.getvalue())
        users = app.extensions["state"].users.list_users()
        self.assertEqual([(u.username, u.role, u.must_change_password) for u in users], [("admin", "admin", True)])
        with redirect_stdout(io.StringIO()):
            create_app(self.settings)                                      # users exist: nothing happens
        self.assertEqual(app.extensions["state"].users.count(), 1)


if __name__ == "__main__":
    unittest.main()
