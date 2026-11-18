"""
Sign in with Google / Apple / Facebook: the signed ``state``, and the whole flow through the
fake identity provider on the portal (new customer account, Apple's form post) and on the
dashboard (a new staff account without a password, ``auth.UserStore.create_external_user``).

No network: ``[oauth] gateway = fake``.  Every e-mail address is a dummy.
"""

from __future__ import annotations

import re
import unittest
import urllib.parse

from stock_dataload.accounts import PRINCIPAL_CUSTOMER, PRINCIPAL_STAFF
from stock_dataload.auth import EXTERNAL_PASSWORD
from stock_dataload.oauth import make_state, read_state

from .test_dashboard import DashboardTestCase
from .test_portal import CSRF, PortalTestCase


class StateTests(unittest.TestCase):
    def test_signed_state(self):
        token = make_state("dummy-secret", {"p": "google", "next": "/account"})
        self.assertEqual(read_state("dummy-secret", token)["p"], "google")
        self.assertIsNone(read_state("other-dummy-secret", token))               # another key
        self.assertIsNone(read_state("dummy-secret", token[:-2] + "xx"))          # tampered
        self.assertIsNone(read_state("dummy-secret", token, max_age=-1))          # expired
        self.assertIsNone(read_state("dummy-secret", "garbage"))


def _query(location: str) -> dict:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(location).query))


class PortalOAuthTests(PortalTestCase):
    def test_google_creates_a_verified_account(self):
        r = self.client.get("/oauth/google/start")
        consent = r.headers["Location"]
        self.assertIn("/fake-oauth/google/authorize", consent)
        self.assertEqual(self.client.get(consent).status_code, 200)
        r = self.post(consent, {"email": "google.dummy@example.com", "name": "Google Dummy"})
        callback = r.headers["Location"]
        self.assertIn("code=code_dummy_", callback)
        r = self.client.get(callback)
        self.assertEqual(r.status_code, 302)
        customer = self.store.get_customer_by_email("google.dummy@example.com")
        self.assertTrue(customer.is_email_verified)
        self.assertFalse(customer.has_password)
        self.assertEqual(len(self.state.identities.identities_of(PRINCIPAL_CUSTOMER, customer.customer_id)), 1)
        self.assertEqual(self.client.get("/account").status_code, 200)            # signed in

    def test_apple_answers_with_a_form_post(self):
        consent = self.client.get("/oauth/apple/start").headers["Location"]
        page = self.post(consent, {"email": "apple.dummy@example.com", "name": "Apple Dummy"}).get_data(as_text=True)
        fields = dict(re.findall(r'name="(code|state)" value="([^"]+)"', page))
        r = self.client.post("/oauth/apple/callback", data=fields)                 # cross-site: no CSRF token
        self.assertEqual(r.status_code, 302)
        self.assertIsNotNone(self.store.get_customer_by_email("apple.dummy@example.com"))

    def test_a_forged_state_is_refused(self):
        r = self.client.get("/oauth/google/callback?code=code_dummy_x&state=forged")
        self.assertIn("/login", r.headers["Location"])
        with self.client.session_transaction() as sess:
            sess["csrf"] = CSRF
        self.assertEqual(self.client.get("/oauth/twitter/start").status_code, 404)


class DashboardOAuthTests(DashboardTestCase):
    OAUTH_PROVIDERS = ("google", "facebook")

    def test_google_creates_a_viewer_without_password(self):
        self.assertEqual(sorted(self.state.oauth), ["facebook", "google"])
        self.logout()
        consent = self.client.get("/oauth/google/start").headers["Location"]
        r = self.client.post(consent, data={"email": "staff.dummy@example.com", "name": "Staff Dummy"})
        r = self.client.get(r.headers["Location"])
        self.assertEqual(r.status_code, 302)
        user = self.state.users.get_by_email("staff.dummy@example.com")
        self.assertEqual((user.username, user.role, user.password_hash), ("staff.dummy@example.com", "viewer",
                                                                          EXTERNAL_PASSWORD))
        self.assertFalse(user.has_password)
        self.assertTrue(user.is_email_verified)
        self.assertIsNone(self.state.users.authenticate(user.username, EXTERNAL_PASSWORD))   # no password sign-in
        self.assertEqual(len(self.state.identities.identities_of(PRINCIPAL_STAFF, user.user_id)), 1)
        self.assertEqual(self.client.get("/").status_code, 200)


if __name__ == "__main__":
    unittest.main()
