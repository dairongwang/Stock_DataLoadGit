"""
Payment providers of the token shop: **Stripe Checkout** and **PayPal Checkout**.

Both use the hosted-page flow: the portal creates a payment at the provider,
sends the customer to the provider's page, and when the customer comes back
(``/checkout/<order>/return``) the portal asks the provider whether the
payment really went through - the return URL alone is never trusted.

::

    buy page ──► start_checkout(order) ──► redirect to Stripe / PayPal ──► customer pays
                                                                              │
    receipt  ◄── complete_order ◄── verify_checkout(order, return args) ◄──────┘

``[portal] payment_methods`` lists the methods offered on the purchase page
(``stripe, paypal``); ``[portal] payment_gateway`` selects where the API
calls go:

``live``
    the real APIs (``api.stripe.com``; ``api-m.paypal.com`` or PayPal's own
    sandbox ``api-m.sandbox.paypal.com`` with ``paypal_environment``).
    Credentials: ``stripe_secret_key`` / ``$STOCK_DATALOAD_STRIPE_SECRET_KEY``,
    ``paypal_client_id`` + ``paypal_client_secret`` /
    ``$STOCK_DATALOAD_PAYPAL_CLIENT_SECRET``.
``fake`` (default)
    :class:`FakeGateway` - an in-process STUB of both APIs for development
    and tests.  No credentials, no network: the providers talk to the fake
    exactly as they would to Stripe / PayPal, and the hosted payment pages
    are served by the portal itself (``/fake-checkout/<stripe|paypal>/<id>``)
    with a *Pay* and a *Cancel* button.  Nothing is charged, ever.

All HTTP goes through :class:`UrllibTransport` (standard library) so the
providers can be unit-tested against a mocked transport as well.
"""

from __future__ import annotations

import base64
import logging
import secrets
import urllib.parse
from dataclasses import dataclass, field
from typing import Mapping

from .config import Settings
from .customers import Order
from .httpclient import HttpError, Transport
from .httpclient import UrllibTransport as _UrllibTransport

log = logging.getLogger(__name__)

PROVIDER_STRIPE = "stripe"
PROVIDER_PAYPAL = "paypal"
PAYMENT_METHODS = (PROVIDER_STRIPE, PROVIDER_PAYPAL)
METHOD_LABELS = {PROVIDER_STRIPE: "Card (Stripe)", PROVIDER_PAYPAL: "PayPal"}

GATEWAY_LIVE = "live"
GATEWAY_FAKE = "fake"

STRIPE_API = "https://api.stripe.com/v1"
PAYPAL_API_LIVE = "https://api-m.paypal.com"
PAYPAL_API_SANDBOX = "https://api-m.sandbox.paypal.com"


class PaymentError(Exception):
    """The payment could not be started / verified (provider down, bad credentials, ...)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class PaymentResult:
    ok: bool
    provider_ref: str | None = None     # the provider's id of the payment (payment intent / capture id)
    detail: str | None = None           # shown on the receipt, e.g. "Stripe" / "PayPal "
    message: str = ""                   # why it failed


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------
class UrllibTransport(_UrllibTransport):
    """The real thing; provider errors surface as :class:`PaymentError`."""

    def request(self, method, url, data=None, headers=None, json_body=False) -> dict:
        try:
            return super().request(method, url, data, headers, json_body)
        except HttpError as exc:
            if exc.status is not None:
                raise PaymentError(f"the payment provider refused the request: {exc}", exc.status) from exc
            raise PaymentError(f"could not reach the payment provider: {exc}") from exc


# ---------------------------------------------------------------------------
# providers
# ---------------------------------------------------------------------------
class PaymentProvider:
    name = "abstract"
    label = "abstract"
    #: shown on the purchase page
    notice = ""

    def start_checkout(self, order: Order, success_url: str, cancel_url: str) -> tuple[str, str]:
        """Create the hosted payment; returns ``(url to send the customer to, provider reference)``."""
        raise NotImplementedError

    def verify_checkout(self, order: Order, args: Mapping[str, str]) -> PaymentResult:
        """Called when the customer comes back with the return-URL query *args*:
        confirm with the provider that the order was paid."""
        raise NotImplementedError


class StripeCheckoutProvider(PaymentProvider):
    """Stripe Checkout Sessions (``mode=payment``)."""

    name = PROVIDER_STRIPE
    label = METHOD_LABELS[PROVIDER_STRIPE]
    notice = "Pay by card on Stripe's secure page; you return here afterwards."

    def __init__(self, secret_key: str, transport: Transport | None = None, api_base: str = STRIPE_API):
        if not secret_key:
            raise PaymentError("Stripe needs a secret key ($STOCK_DATALOAD_STRIPE_SECRET_KEY or [portal] stripe_secret_key)")
        self.secret_key = secret_key
        self.transport = transport or UrllibTransport()
        self.api_base = api_base.rstrip("/")

    def _call(self, method: str, path: str, data: dict | None = None) -> dict:
        return self.transport.request(method, f"{self.api_base}{path}", data,
                                      {"Authorization": f"Bearer {self.secret_key}"})

    def start_checkout(self, order: Order, success_url: str, cancel_url: str) -> tuple[str, str]:
        params = {
            "mode": "payment",
            "client_reference_id": order.order_ref,
            "customer_email": order.email,
            "success_url": success_url + ("&" if "?" in success_url else "?") + "session_id={CHECKOUT_SESSION_ID}",
            "cancel_url": cancel_url,
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": order.currency.lower(),
            "line_items[0][price_data][unit_amount]": str(order.amount),
            "line_items[0][price_data][product_data][name]": f"{order.pack} pack - {order.tokens:,} data API tokens",
            "metadata[order_ref]": order.order_ref,
        }
        session = self._call("POST", "/checkout/sessions", params)
        url, sid = session.get("url"), session.get("id")
        if not url or not sid:
            raise PaymentError("Stripe did not return a checkout session")
        log.info("order %s: stripe checkout session %s created", order.order_ref, sid)
        return url, sid

    def verify_checkout(self, order: Order, args: Mapping[str, str]) -> PaymentResult:
        session_ref = (args.get("session_id") or "").strip()
        if not session_ref.startswith("cs_"):
            return PaymentResult(False, message="missing or invalid Stripe session id")
        if order.provider_ref and session_ref != order.provider_ref:
            return PaymentResult(False, message="the Stripe session does not belong to this order")
        session = self._call("GET", f"/checkout/sessions/{urllib.parse.quote(session_ref)}")
        if session.get("client_reference_id") != order.order_ref:
            return PaymentResult(False, message="the Stripe session does not belong to this order")
        if session.get("payment_status") != "paid":
            return PaymentResult(False, message=f"the payment is not complete (status: {session.get('payment_status')})")
        if int(session.get("amount_total") or 0) != order.amount or \
                (session.get("currency") or "").upper() != order.currency.upper():
            return PaymentResult(False, message="the amount paid does not match the order")
        intent = session.get("payment_intent")
        ref = intent if isinstance(intent, str) else (intent or {}).get("id") or session.get("id")
        return PaymentResult(True, provider_ref=ref, detail="Stripe")


class PayPalCheckoutProvider(PaymentProvider):
    """PayPal Checkout - Orders API v2 (create with intent CAPTURE, capture on return)."""

    name = PROVIDER_PAYPAL
    label = METHOD_LABELS[PROVIDER_PAYPAL]
    notice = "Pay with your PayPal account (or a card through PayPal); you return here afterwards."

    def __init__(self, client_id: str, client_secret: str, transport: Transport | None = None,
                 api_base: str = PAYPAL_API_SANDBOX):
        if not (client_id and client_secret):
            raise PaymentError("PayPal needs [portal] paypal_client_id and paypal_client_secret "
                               "(or $STOCK_DATALOAD_PAYPAL_CLIENT_SECRET)")
        self.client_id, self.client_secret = client_id, client_secret
        self.transport = transport or UrllibTransport()
        self.api_base = api_base.rstrip("/")
        self._token: str | None = None

    def _access_token(self) -> str:
        if self._token:
            return self._token
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode("utf-8")).decode("ascii")
        payload = self.transport.request("POST", f"{self.api_base}/v1/oauth2/token",
                                         {"grant_type": "client_credentials"}, {"Authorization": f"Basic {basic}"})
        token = payload.get("access_token")
        if not token:
            raise PaymentError("PayPal did not issue an access token - check the client id / secret")
        self._token = token
        return token

    def _call(self, method: str, path: str, data: dict | None = None, retry: bool = True) -> dict:
        headers = {"Authorization": f"Bearer {self._access_token()}", "Accept": "application/json"}
        try:
            return self.transport.request(method, f"{self.api_base}{path}", data, headers, json_body=True)
        except PaymentError as exc:
            if retry and self._token and exc.status == 401:      # an expired token: fetch a new one once
                self._token = None
                return self._call(method, path, data, retry=False)
            raise

    def start_checkout(self, order: Order, success_url: str, cancel_url: str) -> tuple[str, str]:
        payload = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": order.order_ref,
                "description": f"{order.pack} pack - {order.tokens:,} data API tokens",
                "custom_id": order.order_ref,
                "amount": {"currency_code": order.currency.upper(), "value": f"{order.amount / 100:.2f}"},
            }],
            "application_context": {"return_url": success_url, "cancel_url": cancel_url, "user_action": "PAY_NOW",
                                    "shipping_preference": "NO_SHIPPING", "brand_name": "Stock Data API"},
        }
        created = self._call("POST", "/v2/checkout/orders", payload)
        paypal_id = created.get("id")
        approve = next((l.get("href") for l in created.get("links", []) if l.get("rel") == "approve"), None)
        if not paypal_id or not approve:
            raise PaymentError("PayPal did not return an approval link")
        log.info("order %s: paypal order %s created", order.order_ref, paypal_id)
        return approve, paypal_id

    def verify_checkout(self, order: Order, args: Mapping[str, str]) -> PaymentResult:
        paypal_id = (args.get("token") or "").strip()          # PayPal appends ?token=<order id>&PayerID=...
        if not paypal_id:
            return PaymentResult(False, message="missing PayPal order id")
        if order.provider_ref and paypal_id != order.provider_ref:
            return PaymentResult(False, message="the PayPal order does not belong to this order")
        captured = self._call("POST", f"/v2/checkout/orders/{urllib.parse.quote(paypal_id)}/capture", {})
        if captured.get("status") != "COMPLETED":
            return PaymentResult(False, message=f"the payment is not complete (status: {captured.get('status')})")
        units = captured.get("purchase_units") or [{}]
        unit = units[0]
        if unit.get("reference_id") != order.order_ref:
            return PaymentResult(False, message="the PayPal order does not belong to this order")
        captures = ((unit.get("payments") or {}).get("captures") or [{}])
        capture = captures[0]
        amount = capture.get("amount") or {}
        if round(float(amount.get("value") or 0) * 100) != order.amount or \
                (amount.get("currency_code") or "").upper() != order.currency.upper():
            return PaymentResult(False, message="the amount paid does not match the order")
        payer = (captured.get("payer") or {}).get("email_address")
        return PaymentResult(True, provider_ref=capture.get("id") or paypal_id,
                             detail=f"PayPal ({payer})" if payer else "PayPal")


# ---------------------------------------------------------------------------
# the fake gateway: an in-process stub of the Stripe and PayPal APIs
# ---------------------------------------------------------------------------
@dataclass
class FakeSession:
    kind: str                       # stripe | paypal
    id: str
    order_ref: str
    amount: int
    currency: str
    email: str
    success_url: str
    cancel_url: str
    status: str = "open"            # open | paid | cancelled
    payer_email: str | None = None
    description: str = ""
    extra: dict = field(default_factory=dict)


class FakeGateway(Transport):
    """Behaves like Stripe's and PayPal's REST APIs, in memory.

    The providers are pointed at it as their transport; the checkout URLs it
    hands out are pages of the portal (``/fake-checkout/<kind>/<id>``) where
    a *Pay* button calls :meth:`approve` and *Cancel* calls :meth:`cancel`.
    ``fail_with`` simulates an outage.
    """

    STRIPE_BASE = "https://fake.stripe.local/v1"
    PAYPAL_BASE = "https://fake.paypal.local"

    def __init__(self, checkout_path: str = "/fake-checkout"):
        self.checkout_path = checkout_path.rstrip("/")
        self.sessions: dict[str, FakeSession] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_with: str | None = None
        self.tokens_issued = 0

    # -- what the portal pages use -------------------------------------------------
    def get(self, session_id: str) -> FakeSession | None:
        return self.sessions.get(session_id)

    def approve(self, session_id: str, payer_email: str | None = None) -> FakeSession:
        s = self._require(session_id)
        if s.status == "open":
            s.status = "paid"
            s.payer_email = payer_email or s.email
        return s

    def cancel(self, session_id: str) -> FakeSession:
        s = self._require(session_id)
        if s.status == "open":
            s.status = "cancelled"
        return s

    def _require(self, session_id: str) -> FakeSession:
        s = self.sessions.get(session_id)
        if s is None:
            raise PaymentError(f"unknown fake checkout session {session_id}", 404)
        return s

    # -- the transport -----------------------------------------------------------------
    def request(self, method, url, data=None, headers=None, json_body=False) -> dict:
        self.calls.append((method, url))
        if self.fail_with:
            raise PaymentError(self.fail_with)
        if url.startswith(self.STRIPE_BASE):
            return self._stripe(method, url[len(self.STRIPE_BASE):], data or {}, headers or {})
        if url.startswith(self.PAYPAL_BASE):
            return self._paypal(method, url[len(self.PAYPAL_BASE):], data or {}, headers or {})
        raise PaymentError(f"the fake gateway does not serve {url}")

    # Stripe ------------------------------------------------------------------------------
    def _stripe(self, method: str, path: str, data: dict, headers: dict) -> dict:
        if not headers.get("Authorization", "").startswith("Bearer "):
            raise PaymentError("the payment provider refused the request: no API key (stripe)", 401)
        if method == "POST" and path == "/checkout/sessions":
            sid = "cs_fake_" + secrets.token_hex(8)
            s = FakeSession(PROVIDER_STRIPE, sid, data.get("client_reference_id", ""),
                            int(data.get("line_items[0][price_data][unit_amount]") or 0),
                            (data.get("line_items[0][price_data][currency]") or "usd").upper(),
                            data.get("customer_email", ""), data["success_url"].replace("{CHECKOUT_SESSION_ID}", sid),
                            data["cancel_url"], description=data.get("line_items[0][price_data][product_data][name]", ""))
            self.sessions[sid] = s
            return {"id": sid, "object": "checkout.session", "url": f"{self.checkout_path}/stripe/{sid}"}
        if method == "GET" and path.startswith("/checkout/sessions/"):
            s = self._require(urllib.parse.unquote(path.rsplit("/", 1)[1]))
            return {"id": s.id, "object": "checkout.session", "client_reference_id": s.order_ref,
                    "payment_status": "paid" if s.status == "paid" else "unpaid", "amount_total": s.amount,
                    "currency": s.currency.lower(), "payment_intent": f"pi_fake_{s.id[8:]}" if s.status == "paid" else None,
                    "customer_email": s.email}
        raise PaymentError(f"the fake gateway does not serve {method} {path} (stripe)")

    # PayPal ------------------------------------------------------------------------------
    def _paypal(self, method: str, path: str, data: dict, headers: dict) -> dict:
        if method == "POST" and path == "/v1/oauth2/token":
            if not headers.get("Authorization", "").startswith("Basic "):
                raise PaymentError("the payment provider refused the request: no client credentials (paypal)", 401)
            self.tokens_issued += 1
            return {"access_token": f"A21AA_dummy_fake_{self.tokens_issued}", "token_type": "Bearer", "expires_in": 32400}
        if not headers.get("Authorization", "").startswith("Bearer A21AA_dummy_fake_"):
            raise PaymentError("the payment provider refused the request: invalid access token (paypal)", 401)
        if method == "POST" and path == "/v2/checkout/orders":
            unit = data["purchase_units"][0]
            pid = "FAKE" + secrets.token_hex(6).upper()
            ctx = data.get("application_context", {})
            s = FakeSession(PROVIDER_PAYPAL, pid, unit.get("reference_id", ""),
                            round(float(unit["amount"]["value"]) * 100), unit["amount"]["currency_code"], "",
                            ctx.get("return_url", "") + ("&" if "?" in ctx.get("return_url", "") else "?") + f"token={pid}&PayerID=FAKEPAYER",
                            ctx.get("cancel_url", ""), description=unit.get("description", ""))
            self.sessions[pid] = s
            return {"id": pid, "status": "CREATED",
                    "links": [{"rel": "self", "href": f"{self.PAYPAL_BASE}/v2/checkout/orders/{pid}"},
                              {"rel": "approve", "href": f"{self.checkout_path}/paypal/{pid}"}]}
        if method == "POST" and path.endswith("/capture") and path.startswith("/v2/checkout/orders/"):
            s = self._require(urllib.parse.unquote(path.split("/")[4]))
            if s.status != "paid":
                raise PaymentError("the payment provider refused the request: ORDER_NOT_APPROVED", 422)
            return {"id": s.id, "status": "COMPLETED", "payer": {"email_address": s.payer_email},
                    "purchase_units": [{"reference_id": s.order_ref, "payments": {"captures": [
                        {"id": f"CAP{s.id[4:]}", "status": "COMPLETED",
                         "amount": {"currency_code": s.currency, "value": f"{s.amount / 100:.2f}"}}]}}]}
        raise PaymentError(f"the fake gateway does not serve {method} {path} (paypal)")


# ---------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------
def make_providers(settings: Settings, gateway: FakeGateway | None = None) -> dict[str, PaymentProvider]:
    """The providers of ``[portal] payment_methods``, wired to the live APIs or to the fake gateway."""
    providers: dict[str, PaymentProvider] = {}
    fake = settings.payment_gateway == GATEWAY_FAKE
    if fake and gateway is None:
        gateway = FakeGateway()
    for method in settings.payment_methods:
        if method == PROVIDER_STRIPE:
            providers[method] = (StripeCheckoutProvider("sk_test_dummy_fake", gateway, FakeGateway.STRIPE_BASE) if fake
                                 else StripeCheckoutProvider(settings.stripe_secret_key))
        elif method == PROVIDER_PAYPAL:
            if fake:
                providers[method] = PayPalCheckoutProvider("dummy-fake-client", "dummy-fake-secret", gateway, FakeGateway.PAYPAL_BASE)
            else:
                base = PAYPAL_API_LIVE if settings.paypal_environment == "live" else PAYPAL_API_SANDBOX
                providers[method] = PayPalCheckoutProvider(settings.paypal_client_id, settings.paypal_client_secret,
                                                           api_base=base)
    if not providers:
        raise PaymentError("[portal] payment_methods must list at least one of: " + ", ".join(PAYMENT_METHODS))
    return providers
