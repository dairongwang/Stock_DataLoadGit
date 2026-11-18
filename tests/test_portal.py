"""
The public portal (Flask test client): the public pages, buying tokens as a guest through the
fake payment gateway (no card data anywhere), the receipt with the key, the metered data API
(401 / 402 / charging per ticker with data / CSV), customer accounts with e-mail verification,
and the admin area that needs two-factor authentication.

No network: payments use the fake gateway, e-mail the memory stub.  Every e-mail address,
password and key here is a dummy.
"""

from __future__ import annotations

import re
import unittest

from stock_dataload.accounts import PRINCIPAL_CUSTOMER
from stock_dataload.customers import CustomerStore
from stock_dataload.eod_api import SAMPLE_RESPONSE
from stock_dataload.eod_stage import EodStageLoader
from stock_dataload.portal.app import create_app
from stock_dataload.promotion import PromotionService

from .helpers import TempProjectTestCase

CSRF = "dummy-csrf-token-for-tests"
KEY_RE = re.compile(r"sdl_[A-Za-z0-9_\-]{20,}")


class PortalTestCase(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.settings.mail_provider = self.settings.sms_provider = "memory"
        self.settings.payment_gateway = "fake"
        self.settings.oauth_gateway = "fake"
        self.settings.oauth_providers = ("google", "apple", "facebook")
        self.app = create_app(self.settings)
        self.app.config["TESTING"] = True
        self.state = self.app.extensions["state"]
        self.store: CustomerStore = self.state.customers
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["csrf"] = CSRF

    def post(self, url: str, data: dict | None = None, **kwargs):
        """A form post with the session's CSRF token (signing in starts a new session - like a browser
        that renders the next form, the token is put back first)."""
        with self.client.session_transaction() as sess:
            sess["csrf"] = CSRF
        return self.client.post(url, data={"_csrf": CSRF, **(data or {})}, **kwargs)

    def seed_daily(self) -> int:
        """MRK from the sample API answer in the daily table (market NYSE); returns a year with data."""
        assert EodStageLoader(self.settings).load_file(SAMPLE_RESPONSE, "MRK").ok
        PromotionService(self.settings).promote_eod_daily(market="NYSE", symbols=["MRK"])
        self.state.data.invalidate()
        return int(self.state.data.catalog()["kinds"]["daily"]["years"][0])

    def buy_as_guest(self, email: str = "guest.dummy@example.com", pack: str = "starter") -> str:
        """The whole guest checkout through the fake Stripe page; returns the key of the receipt."""
        r = self.post("/buy", {"pack": pack, "email": email, "email_confirm": email, "terms": "on",
                               "payment_method": "stripe"})
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:500])
        checkout = r.headers["Location"]
        self.assertIn("/fake-checkout/stripe/", checkout)
        self.assertEqual(self.client.get(checkout).status_code, 200)
        r = self.post(checkout, {"action": "pay", "payer_email": email})
        self.assertEqual(r.status_code, 302)
        page = self.client.get(r.headers["Location"]).get_data(as_text=True)
        self.assertIn("Thank you", page)
        return KEY_RE.search(page).group(0)


class PublicPageTests(PortalTestCase):
    def test_pages_render(self):
        for url in ("/", "/docs", "/buy", "/login", "/signup", "/orders/lookup", "/forgot-password"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)
        home = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="pricing"', home)
        self.assertIn("Starter", home)
        self.assertIn("End-of-day stock data", home)
        docs = self.client.get("/docs").get_data(as_text=True)
        self.assertIn("/api/v1/daily", docs)
        self.assertIn("week_start", docs)                                   # the published fields
        self.assertIn("Authorization: Bearer", docs)
        self.assertEqual(self.client.get("/nope").status_code, 404)

    def test_catalog_on_the_home_page(self):
        year = self.seed_daily()
        home = self.client.get("/").get_data(as_text=True)
        self.assertIn("tickers with daily data", home)
        self.assertIn(str(year), home)
        self.assertIn("NYSE", home)

    def test_forms_need_the_csrf_token(self):
        self.assertEqual(self.client.post("/buy", data={"pack": "starter"}).status_code, 400)


class PurchaseAndApiTests(PortalTestCase):
    def test_guest_purchase_and_metered_requests(self):
        year = self.seed_daily()
        key = self.buy_as_guest()
        auth = {"Authorization": f"Bearer {key}"}
        balance = self.client.get("/api/v1/balance", headers=auth).get_json()
        self.assertEqual(balance["tokens_remaining"], 100)
        r = self.client.get(f"/api/v1/daily?ticker=MRK,XYZ&year={year}", headers=auth)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertEqual((body["tickers_with_data"], body["tickers_without_data"]), (["MRK"], ["XYZ"]))
        self.assertEqual((body["tokens_charged"], body["tokens_remaining"]), (1, 99))
        self.assertEqual(r.headers["X-Tokens-Remaining"], "99")
        self.assertTrue(body["records"] and set(body["records"][0]) >= {"ticker", "date", "close", "volume"})
        r = self.client.get(f"/api/v1/monthly?ticker=MRK&year={year}&format=csv", headers={"X-API-Key": key})
        self.assertEqual(r.mimetype, "text/csv")
        self.assertTrue(r.get_data(as_text=True).startswith("market,ticker,month_start"))
        usage = self.client.get(f"/api/v1/usage?api_key={key}").get_json()
        self.assertEqual(usage["count"], 2)
        # a ticker without data is free
        self.assertEqual(self.client.get(f"/api/v1/weekly?ticker=NODATA&year={year}", headers=auth)
                         .get_json()["tokens_charged"], 0)

    def test_api_errors(self):
        year = self.seed_daily()
        self.assertEqual(self.client.get(f"/api/v1/daily?ticker=MRK&year={year}").status_code, 401)
        r = self.client.get(f"/api/v1/daily?ticker=MRK&year={year}",
                            headers={"Authorization": "Bearer sdl_dummyUnknownKey000000000000"})
        self.assertEqual(r.status_code, 401)
        guest = self.store.get_or_create_guest("empty.dummy@example.com")
        _, plain = self.store.create_key(guest.customer_id, "empty")
        r = self.client.get(f"/api/v1/daily?ticker=MRK&year={year}", headers={"X-API-Key": plain})
        self.assertEqual(r.status_code, 402)
        self.assertIn("not enough tokens", r.get_json()["error"])
        self.assertEqual(self.client.get("/api/v1/yearly?ticker=MRK&year=2025", headers={"X-API-Key": plain})
                         .status_code, 404)
        self.assertEqual(self.client.get("/api/v1/daily?ticker=MRK&year=abc", headers={"X-API-Key": plain})
                         .status_code, 400)
        self.assertEqual(self.client.get("/api/v1/catalog").status_code, 200)

    def test_cancelled_checkout_charges_nothing(self):
        r = self.post("/buy", {"pack": "starter", "email": "cancel.dummy@example.com",
                               "email_confirm": "cancel.dummy@example.com", "terms": "on", "payment_method": "paypal"})
        checkout = r.headers["Location"]
        r = self.post(checkout, {"action": "cancel"})
        r = self.client.get(r.headers["Location"])
        self.assertEqual(r.status_code, 302)
        orders = self.store.list_orders(limit=5)
        self.assertEqual(orders[0].status, "cancelled")

    def test_receipt_lookup(self):
        self.buy_as_guest("lookup.dummy@example.com")
        order = self.store.list_orders(limit=1)[0]
        fresh = self.app.test_client()
        self.assertEqual(fresh.get(f"/orders/{order.order_ref}").status_code, 302)   # not this session's order
        with fresh.session_transaction() as sess:
            sess["csrf"] = CSRF
        r = fresh.post("/orders/lookup", data={"_csrf": CSRF, "ref": order.order_ref, "email": "lookup.dummy@example.com"})
        self.assertEqual(r.status_code, 302)
        self.assertIn(order.order_ref, fresh.get(r.headers["Location"]).get_data(as_text=True))


class AccountTests(PortalTestCase):
    def test_signup_verify_and_keys(self):
        r = self.post("/signup", {"email": "new.dummy@example.com", "password": "Dummy-Portal-Pass-1",
                                  "password_confirm": "Dummy-Portal-Pass-1", "display_name": "Dummy"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/verify-email", r.headers["Location"])
        code = self.state.two_factor.email.last.code
        r = self.post("/verify-email", {"code": code})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get("/account").status_code, 200)
        page = self.post("/account/keys", {"label": "notebook"}).get_data(as_text=True)
        self.assertTrue(KEY_RE.search(page))
        self.post("/logout")
        self.assertEqual(self.client.get("/account").status_code, 302)
        r = self.post("/login", {"email": "new.dummy@example.com", "password": "wrong-dummy"})
        self.assertEqual(r.status_code, 401)
        r = self.post("/login", {"email": "new.dummy@example.com", "password": "Dummy-Portal-Pass-1"})
        self.assertEqual(r.status_code, 302)

    def test_admin_pages_need_two_factor(self):
        admin = self.store.register("admin.dummy@example.com", "Dummy-Admin-Pass-1", role="admin", email_verified=True)
        r = self.post("/login", {"email": "admin.dummy@example.com", "password": "Dummy-Admin-Pass-1"})
        self.assertEqual(r.status_code, 302)
        r = self.client.get("/admin")
        self.assertIn("/account/security", r.headers["Location"])
        self.post("/account/security/email/begin")
        self.post("/account/security/email/confirm", {"code": self.state.two_factor.email.last.code})
        self.assertTrue(self.state.two_factor.has_two_factor(PRINCIPAL_CUSTOMER, admin.customer_id))
        for url in ("/admin", "/admin/customers", "/admin/orders", "/admin/usage", "/admin/api/customers"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_users_cannot_open_the_admin_pages(self):
        self.store.register("user.dummy@example.com", "Dummy-User-Pass-1", email_verified=True)
        self.post("/login", {"email": "user.dummy@example.com", "password": "Dummy-User-Pass-1"})
        self.assertEqual(self.client.get("/admin").status_code, 403)

    def test_password_reset_link(self):
        self.store.register("reset.dummy@example.com", "Dummy-Old-Pass-1", email_verified=True)
        self.post("/forgot-password", {"email": "reset.dummy@example.com"})
        link = self.state.two_factor.email.last.link
        path = link.split("localhost", 1)[1]
        r = self.post(path, {"password": "Dummy-New-Pass-2", "password_confirm": "Dummy-New-Pass-2"})
        self.assertEqual(r.status_code, 302)
        self.assertIsNotNone(self.store.authenticate("reset.dummy@example.com", "Dummy-New-Pass-2"))


if __name__ == "__main__":
    unittest.main()
