"""
The public **portal**: token shop, customer accounts and the data API (Flask).

    python -m stock_dataload portal               # http://127.0.0.1:8060

It is a separate site from the internal dashboard (different port, own
session cookie, no staff login) so it can be published on the internet
while the dashboard stays inside.

Pages
-----
/                 what the API offers, the token packs, how it works
/docs             API documentation with live examples
/buy              buy a token pack - as a GUEST (e-mail only) or with an ACCOUNT; a guest can also
                  top up an existing key; paid by card via Stripe Checkout or with PayPal
                  (hosted pages; /fake-checkout/... are the built-in stubs of both)
/oauth/<p>/start  sign in with Google / Apple / Facebook (/fake-oauth/... is the built-in stub)
/orders/<ref>     the receipt: tokens, the API key (shown in full ONCE), balance
/orders/lookup    guests find their receipt again with order reference + e-mail
/signup /login    customer accounts (a guest can claim earlier purchases with an order reference):
                  register -> confirm the e-mail address with a code -> sign in -> second factor
/login/verify     the second factor: authenticator app (TOTP), e-mail code, sms code or a recovery code
/forgot-password  password reset by e-mail link (/reset-password/<token>)
/account          keys (create / rotate / revoke), balances, orders, usage, password
/account/security two-factor authentication: enrol an authenticator app / e-mail / sms, recovery codes
/admin/...        the ADMIN pages (customers, keys, orders, usage, roles) - role 'admin' only, and
                  only with two-factor authentication enabled ([accounts] admin_requires_2fa);
                  completely separate from the user pages

Data API (``Authorization: Bearer <key>``, ``X-API-Key`` or ``?api_key=``)
-----------------------------------------------------------------------
GET /api/v1/catalog                              free: data sets, years, markets, prices
GET /api/v1/tickers?year=2023&q=AA               free: tickers with data
GET /api/v1/balance                              free: the key's balance and usage
GET /api/v1/<daily|weekly|monthly>?ticker=AAPL,MSFT&year=2023[&market=NASDAQ][&format=csv]
                                                 costs tokens_per_query tokens per ticker that returns data
GET /api/v1/usage                                free: the key's recent requests

Errors are JSON: 400 bad parameters, 401 no / unknown / revoked key, 402 not
enough tokens (nothing is charged), 404 unknown data set.
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
from datetime import datetime
from functools import wraps

from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template, request, session,
                   url_for)

from .. import __version__
from ..accounts import (METHOD_EMAIL, METHOD_LABELS, METHOD_RECOVERY, METHOD_SMS, METHOD_TOTP, METHODS,
                        PRINCIPAL_CUSTOMER, AccountError, CooldownError, Principal, TwoFactorService, format_secret,
                        qr_svg)
from ..audit import AuditLogger
from ..auth import load_secret_key
from ..config import Settings, load_settings
from ..customers import CUSTOMER_ROLES, ORDER_PAID, CustomerError, CustomerStore
from ..messaging import MessagingError, mask_email
from ..dataapi import (KINDS, PUBLIC_COLUMNS, DataApiError, DataApiService, current_year, parse_kind,
                       parse_tickers, parse_year)
from ..db import DatabaseError, StageDatabase
from ..oauth import (GATEWAY_FAKE as OAUTH_FAKE, PROVIDER_APPLE, PROVIDER_LABELS, FakeIdentityGateway, IdentityStore,
                     OAuthError, make_oauth_providers, make_state, read_state)
from ..payments import (GATEWAY_FAKE, METHOD_LABELS as PAYMENT_LABELS, FakeGateway, PaymentError, make_providers)

log = logging.getLogger(__name__)

COMPONENT = "portal"


class PortalState:
    def __init__(self, settings: Settings):
        self.settings = settings
        settings.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = StageDatabase(settings)
        self.customers = CustomerStore(settings)
        self.customers.ensure_tables()
        self.data = DataApiService(settings, self.db)
        self.audit = AuditLogger(settings)
        # payments: Stripe + PayPal, live or through the in-process fake gateway
        self.gateway: FakeGateway | None = FakeGateway() if settings.payment_gateway == GATEWAY_FAKE else None
        self.providers = make_providers(settings, self.gateway)
        # sign in with Google / Apple / Facebook, live or through the fake identity gateway
        self.oauth_gateway: FakeIdentityGateway | None = \
            FakeIdentityGateway() if settings.oauth_gateway == OAUTH_FAKE else None
        self.oauth = make_oauth_providers(settings, self.oauth_gateway)
        self.identities = IdentityStore(settings)
        self.identities.ensure_table()
        self.two_factor = TwoFactorService(settings)
        self.two_factor.ensure_tables()

    def packs(self):
        return self.customers.packs()


def create_app(settings: Settings | None = None) -> Flask:
    settings = settings or load_settings()
    app = Flask(__name__, template_folder="templates", static_folder="static", static_url_path="/static")
    app.config["SECRET_KEY"] = load_secret_key(settings)
    app.config["SESSION_COOKIE_NAME"] = "stock_portal"
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = settings.portal_public_url.lower().startswith("https://")
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
    state = PortalState(settings)
    app.extensions["state"] = state
    store = state.customers

    # -- helpers -------------------------------------------------------------------
    def base_url() -> str:
        return settings.portal_public_url or request.url_root.rstrip("/")

    def current_customer():
        if "customer" not in g:
            g.customer = None
            cid = session.get("customer_id")
            if cid is not None:
                try:
                    c = store.get_customer(int(cid))
                except (ValueError, sqlite3.Error):
                    c = None
                g.customer = c if c is not None and c.is_active and not c.is_guest else None
        return g.customer

    def csrf_token() -> str:
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        return session["csrf"]

    def login_required(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if current_customer() is None:
                flash("Please sign in to open your account", "info")
                return redirect(url_for("login", next=request.path))
            return view(*args, **kwargs)
        return wrapper

    def admin_required(view):
        """The admin pages: signed in, role admin and (by default) a second factor enabled."""
        @wraps(view)
        def wrapper(*args, **kwargs):
            customer = current_customer()
            if customer is None:
                flash("Please sign in", "info")
                return redirect(url_for("login", next=request.path))
            if not customer.is_admin:
                audit("access_denied", f"{customer.email} tried to open {request.path}", level="WARNING",
                      email=customer.email)
                return render_template("error.html", status=403,
                                       message="This area is for administrators only"), 403
            if settings.admin_requires_2fa and not state.two_factor.has_two_factor(PRINCIPAL_CUSTOMER, customer.customer_id):
                flash("Administrators must set up two-factor authentication before the admin pages open", "info")
                return redirect(url_for("security"))
            return view(*args, **kwargs)
        return wrapper

    def principal_of(customer) -> Principal:
        return Principal(PRINCIPAL_CUSTOMER, customer.customer_id, customer.email, customer.phone, customer.name)

    def sign_in(customer, target: str | None = None):
        """Complete the sign-in: a fresh session with the customer id only."""
        orders = session.get("orders", [])
        session.clear()
        session["customer_id"] = customer.customer_id
        session["orders"] = orders
        g.pop("customer", None)
        audit("login", f"customer {customer.email} signed in", email=customer.email)
        if not target or not target.startswith("/") or target.startswith("//"):
            target = url_for("admin_home") if customer.is_admin else url_for("account")
        return redirect(target)

    def start_email_verification(customer, target: str | None = None):
        """Send the verification code and park the sign-in until it is entered."""
        try:
            state.two_factor.send_email_verification(principal_of(customer))
        except CooldownError:
            pass                                   # the earlier code is still valid
        except AccountError as exc:
            flash(str(exc), "error")
        session["pending_verify"] = customer.customer_id
        session["pending_next"] = target or ""
        return redirect(url_for("verify_email"))

    def remember_order(ref: str) -> None:
        refs = list(session.get("orders", []))
        if ref not in refs:
            refs.append(ref)
        session["orders"] = refs[-20:]

    def audit(event: str, message: str, level: str = "INFO", **details) -> None:
        details.setdefault("remote", request.remote_addr)
        state.audit.log(COMPONENT, event, message, level=level, details=details)

    @app.before_request
    def guard():
        if request.path.startswith("/api/"):
            return None
        if request.method == "POST" and request.endpoint != "oauth_callback":
            token = request.form.get("_csrf") or request.headers.get("X-CSRF-Token")
            if not token or not secrets.compare_digest(token, session.get("csrf", "")):
                abort(400, "the form has expired - please try again")
        return None

    @app.context_processor
    def inject():
        return {
            "settings": settings, "packs": state.packs(), "providers": state.providers,
            "payment_labels": PAYMENT_LABELS, "payments_fake": state.gateway is not None,
            "oauth_providers": state.oauth, "oauth_labels": PROVIDER_LABELS, "oauth_fake": state.oauth_gateway is not None,
            "current_customer": current_customer(), "csrf_token": csrf_token, "base_url": base_url,
            "now": datetime.now(), "version": __version__, "kinds": KINDS, "public_columns": PUBLIC_COLUMNS,
            "sample_year": _sample_year(), "method_labels": METHOD_LABELS,
            "mail_stub": state.two_factor.email.stub, "sms_stub": state.two_factor.sms.stub,
        }

    def _sample_year() -> int:
        """A year to use in the examples: the newest complete one (the newest year is
        usually still being delivered), else the newest, else last year."""
        try:
            years = state.data.catalog()["kinds"]["daily"]["years"]
        except (sqlite3.Error, DatabaseError, KeyError):
            years = []
        if len(years) >= 2:
            return years[1]
        return years[0] if years else current_year() - 1

    def _catalog():
        try:
            return state.data.catalog()
        except (sqlite3.Error, DatabaseError) as exc:
            log.warning("catalogue unavailable: %s", exc)
            return None

    # -- public pages ----------------------------------------------------------------
    @app.route("/")
    def index():
        return render_template("index.html", catalog=_catalog())

    @app.route("/docs")
    def docs():
        return render_template("docs.html", catalog=_catalog())

    # -- buying tokens -----------------------------------------------------------------
    def _render_buy(form=None, error: str | None = None, status: int = 200):
        # card details are never echoed back into the page (nor logged or kept in the session)
        form = {k: v for k, v in (form or {}).items() if k not in ("card_number", "card_cvc")}
        customer = current_customer()
        keys = store.keys_of(customer.customer_id, active_only=True) if customer else []
        chosen = (form.get("pack") or request.args.get("pack") or "").lower()
        return render_template("buy.html", form=form, error=error, keys=keys, chosen=chosen), status

    @app.route("/buy")
    def buy():
        return _render_buy()

    @app.post("/buy")
    def buy_submit():
        form = request.form
        customer = current_customer()
        try:
            pack = store.pack(form.get("pack", ""))
            key_id, key_label = None, (form.get("key_label") or "").strip()
            if customer is not None:
                email = customer.email
                choice = (form.get("key_choice") or "new").strip()
                if choice != "new":
                    key = store.get_key(int(choice)) if choice.isdigit() else None
                    if key is None or key.customer_id != customer.customer_id or not key.is_active:
                        raise CustomerError("please choose one of your active keys (or a new key)")
                    key_id = key.api_key_id
            else:
                email = (form.get("email") or "").strip()
                if (form.get("email_confirm") or "").strip().lower() != email.lower():
                    raise CustomerError("the e-mail addresses do not match")
                topup = (form.get("topup_key") or "").strip()
                if topup:
                    key = store.find_key(topup)
                    if key is None or not key.is_active:
                        raise CustomerError("the key to top up is unknown or was revoked - leave the field empty "
                                            "to get a new key")
                    key_id = key.api_key_id
            if form.get("terms") != "on":
                raise CustomerError("please accept the terms of use")
            method = (form.get("payment_method") or "").strip().lower()
            if method not in state.providers:
                raise CustomerError("please choose a payment method")
            order = store.create_order(pack.name, email, method,
                                       customer_id=customer.customer_id if customer else None,
                                       api_key_id=key_id, key_label=key_label)
        except CustomerError as exc:
            return _render_buy(form, str(exc), 400)
        remember_order(order.order_ref)
        provider = state.providers[order.provider]
        try:
            url, provider_ref = provider.start_checkout(
                order, success_url=base_url() + url_for("checkout_return", ref=order.order_ref),
                cancel_url=base_url() + url_for("checkout_cancel", ref=order.order_ref))
        except PaymentError as exc:
            store.fail_order(order.order_ref, str(exc))
            audit("order_failed", f"order {order.order_ref}: {exc}", level="WARNING", order=order.order_ref)
            return _render_buy(form, f"The payment could not be started: {exc}", 502)
        store.set_provider_ref(order.order_ref, provider_ref)
        audit("order_created", f"order {order.order_ref} ({pack.name}) - redirected to {provider.label}",
              order=order.order_ref, email=order.email, provider=order.provider)
        return redirect(url)

    def _finish_order(ref: str, provider_ref: str | None, detail: str | None):
        order, key, plain = store.complete_order(ref, provider_ref, detail)
        audit("order_paid", f"order {order.order_ref}: {order.pack} pack, {order.tokens} tokens for {order.email}"
              f"{' (guest)' if order.is_guest else ''} - {order.price} via {order.provider}",
              order=order.order_ref, email=order.email, tokens=order.tokens, amount=order.amount,
              key=key.key_prefix, new_key=plain is not None)
        remember_order(order.order_ref)
        return render_template("receipt.html", order=order, key=key, plain_key=plain, just_paid=True)

    @app.route("/checkout/<ref>/return")
    def checkout_return(ref: str):
        order = store.get_order(ref)
        if order is None:
            abort(404)
        if order.is_paid:
            return redirect(url_for("order_page", ref=order.order_ref))
        provider = state.providers.get(order.provider)
        if provider is None:
            flash(f"The payment method '{order.provider}' is no longer available", "error")
            return redirect(url_for("order_page", ref=order.order_ref))
        try:
            result = provider.verify_checkout(order, request.args)
        except PaymentError as exc:
            flash(f"The payment could not be verified: {exc}", "error")
            return redirect(url_for("order_page", ref=order.order_ref))
        if not result.ok:
            flash(f"The payment is not complete: {result.message}", "error")
            return redirect(url_for("order_page", ref=order.order_ref))
        return _finish_order(order.order_ref, result.provider_ref, result.detail)

    @app.route("/checkout/<ref>/cancel")
    def checkout_cancel(ref: str):
        order = store.get_order(ref)
        if order is not None and not order.is_paid:
            store.fail_order(order.order_ref, "cancelled by the customer", cancelled=True)
            audit("order_cancelled", f"order {order.order_ref} cancelled by the customer", order=order.order_ref)
        flash("The purchase was cancelled - nothing was charged", "info")
        return redirect(url_for("buy"))

    # -- the FAKE hosted payment pages (payment_gateway = fake) ---------------------------
    @app.route("/fake-checkout/<kind>/<sid>", methods=["GET", "POST"])
    def fake_checkout(kind: str, sid: str):
        if state.gateway is None:
            abort(404)
        fake = state.gateway.get(sid)
        if fake is None or fake.kind != kind:
            abort(404)
        if request.method == "GET":
            return render_template("fake_checkout.html", s=fake, label=PAYMENT_LABELS.get(kind, kind))
        if request.form.get("action") == "cancel":
            state.gateway.cancel(sid)
            return redirect(fake.cancel_url)
        state.gateway.approve(sid, request.form.get("payer_email") or None)
        return redirect(fake.success_url)

    # -- sign in with Google / Apple / Facebook ------------------------------------------
    def _oauth_provider(name: str):
        provider = state.oauth.get(name)
        if provider is None:
            abort(404)
        return provider

    def _oauth_redirect_uri(name: str) -> str:
        return base_url() + url_for("oauth_callback", provider=name)

    @app.route("/oauth/<provider>/start")
    def oauth_start(provider: str):
        p = _oauth_provider(provider)
        customer = current_customer()
        payload = {"p": provider, "next": request.args.get("next", "")}
        if customer is not None and request.args.get("link") == "1":
            payload["link"] = customer.customer_id
        state_token = make_state(app.config["SECRET_KEY"], payload)
        return redirect(p.authorize_url(_oauth_redirect_uri(provider), state_token))

    @app.route("/oauth/<provider>/callback", methods=["GET", "POST"])
    def oauth_callback(provider: str):
        p = _oauth_provider(provider)
        args = request.form if request.method == "POST" else request.args
        payload = read_state(app.config["SECRET_KEY"], args.get("state", ""))
        if payload is None or payload.get("p") != provider:
            audit("oauth_failed", f"{p.label}: invalid or expired state", level="WARNING", provider=provider)
            flash("The sign-in attempt has expired or was tampered with - please try again", "error")
            return redirect(url_for("login"))
        if args.get("error") or not args.get("code"):
            flash(f"{p.label} sign-in was cancelled or denied", "info")
            return redirect(url_for("login"))
        try:
            tokens = p.exchange_code(args["code"], _oauth_redirect_uri(provider))
            profile = p.profile(tokens, args)
        except OAuthError as exc:
            audit("oauth_failed", f"{p.label}: {exc}", level="WARNING", provider=provider)
            flash(str(exc), "error")
            return redirect(url_for("login"))
        # connecting a provider to the signed-in account
        if payload.get("link"):
            customer = current_customer()
            if customer is None or customer.customer_id != payload["link"]:
                flash("Please sign in first, then connect the account", "info")
                return redirect(url_for("login"))
            try:
                state.identities.link(PRINCIPAL_CUSTOMER, customer.customer_id, profile)
            except OAuthError as exc:
                flash(str(exc), "error")
                return redirect(url_for("security"))
            audit("oauth_linked", f"{customer.email} connected {p.label}", email=customer.email, provider=provider)
            flash(f"{p.label} connected - you can now sign in with it", "success")
            return redirect(url_for("security"))
        # signing in
        identity = state.identities.find(provider, profile.subject)
        if identity is not None:
            customer = store.get_customer(identity.principal_id)
            if customer is None or not customer.is_active:
                flash("This account is disabled", "error")
                return redirect(url_for("login"))
            state.identities.touch(identity.identity_id)
        else:
            if not profile.email:
                flash(f"{p.label} did not share an e-mail address - allow it, or create an account with a password",
                      "error")
                return redirect(url_for("login"))
            customer = store.get_customer_by_email(profile.email)
            if customer is not None and not customer.is_guest:
                if not profile.email_verified:
                    flash(f"{p.label} has not verified {profile.email} - sign in with your password and connect "
                          f"{p.label} on the security page", "error")
                    return redirect(url_for("login", next=url_for("security")))
                if not customer.is_active:
                    flash("This account is disabled", "error")
                    return redirect(url_for("login"))
            else:
                try:
                    customer = store.register_external(profile.email, profile.name or "", profile.email_verified)
                except CustomerError as exc:
                    flash(str(exc), "error")
                    return redirect(url_for("login"))
                audit("customer_registered", f"account created for {customer.email} via {p.label}",
                      email=customer.email, provider=provider)
            try:
                state.identities.link(PRINCIPAL_CUSTOMER, customer.customer_id, profile)
            except OAuthError as exc:
                flash(str(exc), "error")
                return redirect(url_for("login"))
            if profile.email_verified and not customer.is_email_verified:
                customer = store.mark_email_verified(customer.customer_id)
        audit("oauth_login", f"{customer.email} signed in with {p.label}", email=customer.email, provider=provider)
        if settings.require_email_verification and not customer.is_email_verified:
            return start_email_verification(customer, payload.get("next"))
        return _continue_login(customer, payload.get("next"))

    @app.post("/account/security/oauth/<provider>/unlink")
    @login_required
    def oauth_unlink(provider: str):
        customer = current_customer()
        if not customer.has_password and len(state.identities.identities_of(PRINCIPAL_CUSTOMER, customer.customer_id)) <= 1:
            flash("This is the only way into your account - set a password first (Forgot your password)", "error")
            return redirect(url_for("security"))
        n = state.identities.unlink(PRINCIPAL_CUSTOMER, customer.customer_id, provider)
        if n:
            audit("oauth_unlinked", f"{customer.email} disconnected {PROVIDER_LABELS.get(provider, provider)}",
                  level="WARNING", email=customer.email, provider=provider)
            flash(f"{PROVIDER_LABELS.get(provider, provider)} disconnected", "success")
        return redirect(url_for("security"))

    # -- the FAKE identity provider pages (oauth gateway = fake) ---------------------------
    @app.route("/fake-oauth/<provider>/authorize", methods=["GET", "POST"])
    def fake_oauth(provider: str):
        if state.oauth_gateway is None or provider not in state.oauth:
            abort(404)
        redirect_uri, state_token = request.values.get("redirect_uri", ""), request.values.get("state", "")
        if request.method == "GET":
            return render_template("fake_oauth.html", provider=provider, label=PROVIDER_LABELS[provider],
                                   redirect_uri=redirect_uri, state=state_token,
                                   client_id=request.args.get("client_id", ""))
        if request.form.get("action") == "cancel":
            return redirect(redirect_uri + ("&" if "?" in redirect_uri else "?") +
                            f"error=access_denied&state={state_token}")
        grant = state.oauth_gateway.issue_code(provider, request.form.get("email", ""), request.form.get("name", ""))
        if provider == PROVIDER_APPLE:           # Apple answers with a cross-site form POST
            user = {"name": {"firstName": grant.name.split(" ")[0] if grant.name else "",
                             "lastName": " ".join(grant.name.split(" ")[1:])}, "email": grant.email}
            return render_template("fake_oauth_post.html", redirect_uri=redirect_uri, code=grant.code,
                                   state=state_token, user=user)
        return redirect(redirect_uri + ("&" if "?" in redirect_uri else "?") +
                        f"code={grant.code}&state={state_token}")

    # -- receipts ------------------------------------------------------------------------
    def _order_or_lookup(ref: str):
        """``(order, None)`` if this session may see the receipt (the signed-in owner,
        or a guest who paid / looked it up in this session) - else ``(None, redirect)``."""
        order = store.get_order(ref)
        if order is None:
            abort(404)
        customer = current_customer()
        if customer is not None and order.customer_id == customer.customer_id:
            return order, None
        if order.order_ref in session.get("orders", []):
            return order, None
        flash("Enter the order reference and the e-mail address of the purchase to open the receipt", "info")
        return None, redirect(url_for("order_lookup", ref=order.order_ref))

    @app.route("/orders/lookup", methods=["GET", "POST"])
    def order_lookup():
        if request.method == "GET":
            return render_template("lookup.html", ref=request.args.get("ref", ""), error=None)
        ref = (request.form.get("ref") or "").strip().upper()
        email = (request.form.get("email") or "").strip()
        order = store.find_order(ref, email)
        if order is None:
            audit("lookup_failed", f"receipt lookup failed for {ref}", level="WARNING", order=ref)
            return render_template("lookup.html", ref=ref, error="no order matches this reference and e-mail address"), 404
        remember_order(order.order_ref)
        return redirect(url_for("order_page", ref=order.order_ref))

    @app.route("/orders/<ref>")
    def order_page(ref: str):
        order, response = _order_or_lookup(ref)
        if response is not None:
            return response
        key = store.get_key(order.api_key_id) if order.api_key_id else None
        return render_template("receipt.html", order=order, key=key, plain_key=None, just_paid=False)

    @app.post("/orders/<ref>/rotate-key")
    def order_rotate_key(ref: str):
        order, response = _order_or_lookup(ref)
        if response is not None:
            return response
        if not order.is_paid or not order.api_key_id:
            abort(400)
        try:
            key, plain = store.rotate_key(order.api_key_id)
        except CustomerError as exc:
            flash(str(exc), "error")
            return redirect(url_for("order_page", ref=order.order_ref))
        audit("key_rotated", f"key {key.key_prefix} rotated from receipt {order.order_ref}", key=key.key_prefix)
        flash("A new key was generated - the old one stopped working. Copy the new key now.", "success")
        return render_template("receipt.html", order=order, key=key, plain_key=plain, just_paid=False)

    # -- accounts: register, verify e-mail, sign in (+ second factor), sign out, password reset ------
    @app.route("/signup", methods=["GET", "POST"])
    def signup():
        if current_customer() is not None:
            return redirect(url_for("account"))
        if request.method == "GET":
            return render_template("signup.html", form={"email": request.args.get("email", "")}, error=None)
        form = request.form
        email = (form.get("email") or "").strip()
        password = form.get("password") or ""
        if password != (form.get("password_confirm") or ""):
            return render_template("signup.html", form=form, error="the passwords do not match"), 400
        try:
            claim_ref = (form.get("order_ref") or "").strip()
            if claim_ref:
                customer = store.claim_guest(email, password, claim_ref, display_name=form.get("display_name", ""))
                event, message = "customer_claimed", f"guest {email} created an account (order {claim_ref.upper()})"
            else:
                customer = store.register(email, password, display_name=form.get("display_name", ""),
                                          email_verified=not settings.require_email_verification)
                event, message = "customer_registered", f"account created for {email}"
        except CustomerError as exc:
            return render_template("signup.html", form=form, error=str(exc)), 400
        audit(event, message, email=email)
        if settings.require_email_verification and not customer.is_email_verified:
            flash("Almost there - we sent a code to your e-mail address. Enter it to activate the account.", "info")
            return start_email_verification(customer)
        flash("Welcome! Your account is ready.", "success")
        return sign_in(customer)

    def _pending(key: str):
        cid = session.get(key)
        if cid is None:
            return None
        customer = store.get_customer(int(cid))
        if customer is None or not customer.is_active or customer.is_guest:
            session.pop(key, None)
            return None
        return customer

    @app.route("/verify-email", methods=["GET", "POST"])
    def verify_email():
        customer = _pending("pending_verify")
        if customer is None:
            flash("Please sign in - you will be asked for the verification code", "info")
            return redirect(url_for("login"))
        pending = state.two_factor.pending_code(PRINCIPAL_CUSTOMER, customer.customer_id, "verify_email")
        if request.method == "GET":
            return render_template("verify_email.html", customer=customer, masked=pending[1] if pending else
                                   mask_email(customer.email), error=None)
        if request.form.get("action") == "resend":
            try:
                state.two_factor.send_email_verification(principal_of(customer))
                flash("A new code was sent", "info")
            except AccountError as exc:
                flash(str(exc), "error")
            return redirect(url_for("verify_email"))
        if not state.two_factor.verify_email(PRINCIPAL_CUSTOMER, customer.customer_id, request.form.get("code", "")):
            audit("verify_email_failed", f"wrong e-mail verification code for {customer.email}", level="WARNING",
                  email=customer.email)
            return render_template("verify_email.html", customer=customer, masked=mask_email(customer.email),
                                   error="the code is not correct or has expired"), 400
        store.mark_email_verified(customer.customer_id)
        audit("email_verified", f"{customer.email} confirmed their e-mail address", email=customer.email)
        target = session.pop("pending_next", "")
        flash("Your e-mail address is confirmed - welcome!", "success")
        return _continue_login(store.get_customer(customer.customer_id), target)

    def _continue_login(customer, target: str | None):
        """After the password (and e-mail verification): ask for the second factor if one is set up."""
        if state.two_factor.has_two_factor(PRINCIPAL_CUSTOMER, customer.customer_id):
            session.pop("pending_verify", None)
            session["pending_2fa"] = customer.customer_id
            session["pending_next"] = target or ""
            return redirect(url_for("login_verify"))
        return sign_in(customer, target)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "GET":
            if current_customer() is not None:
                return redirect(url_for("account"))
            return render_template("login.html", next=request.args.get("next", ""), error=None, email="")
        email = (request.form.get("email") or "").strip()
        customer = store.authenticate(email, request.form.get("password") or "")
        if customer is None:
            audit("login_failed", f"failed customer sign-in for '{email}'", level="WARNING", email=email)
            return render_template("login.html", next=request.form.get("next", ""), email=email,
                                   error="unknown e-mail address or wrong password"), 401
        target = request.form.get("next") or ""
        if settings.require_email_verification and not customer.is_email_verified:
            flash("Please confirm your e-mail address first - we sent you a code", "info")
            return start_email_verification(customer, target)
        return _continue_login(customer, target)

    @app.route("/login/verify", methods=["GET", "POST"])
    def login_verify():
        customer = _pending("pending_2fa")
        if customer is None:
            return redirect(url_for("login"))
        methods = state.two_factor.methods(PRINCIPAL_CUSTOMER, customer.customer_id)
        if not methods:
            return sign_in(customer, session.pop("pending_next", ""))
        available = [m.method for m in methods]
        chosen = request.values.get("method") or (METHOD_TOTP if METHOD_TOTP in available else available[0])
        if chosen not in available and chosen != METHOD_RECOVERY:
            chosen = available[0]
        pending = state.two_factor.pending_code(PRINCIPAL_CUSTOMER, customer.customer_id, "login")
        if request.method == "GET":
            return render_template("login_verify.html", customer=customer, methods=methods, chosen=chosen,
                                   pending=pending, recovery_left=state.two_factor.recovery_codes_left(
                                       PRINCIPAL_CUSTOMER, customer.customer_id), error=None)
        if request.form.get("action") == "send":
            try:
                masked = state.two_factor.send_login_code(PRINCIPAL_CUSTOMER, customer.customer_id, chosen)
                flash(f"A code was sent to {masked}", "info")
            except AccountError as exc:
                flash(str(exc), "error")
            return redirect(url_for("login_verify", method=chosen))
        if state.two_factor.verify_login(PRINCIPAL_CUSTOMER, customer.customer_id, chosen, request.form.get("code", "")):
            audit("login_2fa", f"{customer.email} passed the second factor ({chosen})", email=customer.email,
                  method=chosen)
            if chosen == METHOD_RECOVERY:
                flash("You signed in with a recovery code - consider setting up your second factor again "
                      "and generating new recovery codes", "info")
            return sign_in(customer, session.get("pending_next", ""))
        audit("login_2fa_failed", f"{customer.email} failed the second factor ({chosen})", level="WARNING",
              email=customer.email, method=chosen)
        return render_template("login_verify.html", customer=customer, methods=methods, chosen=chosen, pending=pending,
                               recovery_left=state.two_factor.recovery_codes_left(PRINCIPAL_CUSTOMER, customer.customer_id),
                               error="the code is not correct"), 401

    @app.post("/logout")
    def logout():
        customer = current_customer()
        if customer is not None:
            audit("logout", f"customer {customer.email} signed out", email=customer.email)
        session.clear()
        flash("You have been signed out", "info")
        return redirect(url_for("index"))

    @app.route("/forgot-password", methods=["GET", "POST"])
    def forgot_password():
        if request.method == "GET":
            return render_template("forgot.html", sent=False, email=request.args.get("email", ""))
        email = (request.form.get("email") or "").strip()
        customer = store.get_customer_by_email(email)
        if customer is not None and customer.is_active and not customer.is_guest:
            try:
                state.two_factor.send_password_reset(
                    principal_of(customer), lambda token: base_url() + url_for("reset_password", token=token))
                audit("password_reset_requested", f"password reset link sent to {customer.email}", email=customer.email)
            except CooldownError:
                pass
            except AccountError as exc:
                flash(str(exc), "error")
        else:
            audit("password_reset_unknown", f"password reset requested for unknown / guest address {email}",
                  level="WARNING", email=email)
        # the same answer whether or not the address is known (no account enumeration)
        return render_template("forgot.html", sent=True, email=email)

    @app.route("/reset-password/<token>", methods=["GET", "POST"])
    def reset_password(token: str):
        found = state.two_factor.peek_reset_token(token)
        if found is None or found[0] != PRINCIPAL_CUSTOMER:
            flash("This password-reset link is not valid or has expired - request a new one", "error")
            return redirect(url_for("forgot_password"))
        customer = store.get_customer(found[1])
        if customer is None or not customer.is_active:
            abort(404)
        if request.method == "GET":
            return render_template("reset.html", token=token, email=customer.email, error=None)
        new, confirm = request.form.get("password") or "", request.form.get("password_confirm") or ""
        if new != confirm:
            return render_template("reset.html", token=token, email=customer.email, error="the passwords do not match"), 400
        try:
            store.set_password(customer.customer_id, new)
        except CustomerError as exc:
            return render_template("reset.html", token=token, email=customer.email, error=str(exc)), 400
        if state.two_factor.consume_reset_token(token) is None:
            flash("This password-reset link was already used", "error")
            return redirect(url_for("forgot_password"))
        if not customer.is_email_verified:
            store.mark_email_verified(customer.customer_id)     # the link proves control of the mailbox
        audit("password_reset", f"{customer.email} reset their password", level="WARNING", email=customer.email)
        session.clear()
        flash("Your password was changed - please sign in", "success")
        return redirect(url_for("login"))

    # -- two-factor authentication (account security) -----------------------------------
    def _render_security(error: str | None = None, status: int = 200, **extra):
        customer = current_customer()
        return render_template("security.html", customer=customer,
                               identities=state.identities.identities_of(PRINCIPAL_CUSTOMER, customer.customer_id),
                               methods=state.two_factor.methods(PRINCIPAL_CUSTOMER, customer.customer_id, enabled_only=False),
                               recovery_left=state.two_factor.recovery_codes_left(PRINCIPAL_CUSTOMER, customer.customer_id),
                               error=error, **extra), status

    @app.route("/account/security")
    @login_required
    def security():
        return _render_security()

    @app.post("/account/security/totp/begin")
    @login_required
    def security_totp_begin():
        customer = current_customer()
        secret, uri = state.two_factor.begin_totp(principal_of(customer))
        return _render_security(totp_setup={"secret": format_secret(secret), "uri": uri, "qr": qr_svg(uri)})

    @app.post("/account/security/totp/confirm")
    @login_required
    def security_totp_confirm():
        customer = current_customer()
        if not state.two_factor.confirm_totp(PRINCIPAL_CUSTOMER, customer.customer_id, request.form.get("code", "")):
            row = state.two_factor.get_method(PRINCIPAL_CUSTOMER, customer.customer_id, METHOD_TOTP)
            setup = {"secret": format_secret(row.secret), "uri": None, "qr": None} if row and row.secret else None
            return _render_security("the code is not correct - try again with the next code", 400, totp_setup=setup)
        audit("2fa_enabled", f"{customer.email} enabled an authenticator app", email=customer.email, method=METHOD_TOTP)
        flash("Authenticator app enabled", "success")
        return _after_enrolment(customer)

    def _after_enrolment(customer):
        """The first method also gets recovery codes (shown once)."""
        if state.two_factor.recovery_codes_left(PRINCIPAL_CUSTOMER, customer.customer_id) == 0:
            codes = state.two_factor.generate_recovery_codes(PRINCIPAL_CUSTOMER, customer.customer_id)
            flash("Save your recovery codes - they let you in if you lose your phone", "info")
            return _render_security(recovery_codes=codes)
        return redirect(url_for("security"))

    @app.post("/account/security/<method>/begin")
    @login_required
    def security_code_begin(method: str):
        if method not in (METHOD_EMAIL, METHOD_SMS):
            abort(404)
        customer = current_customer()
        try:
            masked = state.two_factor.begin_code_method(principal_of(customer), method, request.form.get("phone"))
        except (AccountError, MessagingError) as exc:
            return _render_security(str(exc), 400)
        flash(f"A code was sent to {masked} - enter it below to finish", "info")
        return _render_security(confirm_method=method)

    @app.post("/account/security/<method>/confirm")
    @login_required
    def security_code_confirm(method: str):
        if method not in (METHOD_EMAIL, METHOD_SMS):
            abort(404)
        customer = current_customer()
        if not state.two_factor.confirm_code_method(PRINCIPAL_CUSTOMER, customer.customer_id, method,
                                                    request.form.get("code", "")):
            return _render_security("the code is not correct or has expired", 400, confirm_method=method)
        if method == METHOD_SMS:
            row = state.two_factor.get_method(PRINCIPAL_CUSTOMER, customer.customer_id, METHOD_SMS)
            store.set_phone(customer.customer_id, row.destination if row else None)
        audit("2fa_enabled", f"{customer.email} enabled {METHOD_LABELS[method]}", email=customer.email, method=method)
        flash(f"{METHOD_LABELS[method]} enabled", "success")
        return _after_enrolment(customer)

    @app.post("/account/security/<method>/disable")
    @login_required
    def security_disable(method: str):
        if method not in METHODS:
            abort(404)
        customer = current_customer()
        if store.authenticate(customer.email, request.form.get("password") or "") is None:
            return _render_security("please confirm with your password", 400)
        state.two_factor.disable(PRINCIPAL_CUSTOMER, customer.customer_id, method)
        audit("2fa_disabled", f"{customer.email} removed {METHOD_LABELS[method]}", level="WARNING",
              email=customer.email, method=method)
        flash(f"{METHOD_LABELS[method]} removed", "success")
        return redirect(url_for("security"))

    @app.post("/account/security/recovery")
    @login_required
    def security_recovery():
        customer = current_customer()
        if store.authenticate(customer.email, request.form.get("password") or "") is None:
            return _render_security("please confirm with your password", 400)
        codes = state.two_factor.generate_recovery_codes(PRINCIPAL_CUSTOMER, customer.customer_id)
        audit("recovery_codes_generated", f"{customer.email} generated new recovery codes", email=customer.email)
        flash("New recovery codes generated - the old ones no longer work", "success")
        return _render_security(recovery_codes=codes)

    def _render_account(new_key: tuple | None = None, error: str | None = None, status: int = 200):
        customer = current_customer()
        keys = store.keys_of(customer.customer_id)
        return render_template("account.html", customer=customer, keys=keys,
                               balance=sum(k.balance for k in keys if k.is_active),
                               orders=store.orders_of(customer.customer_id, limit=50),
                               usage=store.usage(customer_id=customer.customer_id, limit=30),
                               totals=store.usage_totals(customer_id=customer.customer_id),
                               new_key=new_key, error=error), status

    @app.route("/account")
    @login_required
    def account():
        return _render_account()

    @app.post("/account/keys")
    @login_required
    def account_key_new():
        try:
            key, plain = store.create_key(current_customer().customer_id, request.form.get("label", ""))
        except CustomerError as exc:
            return _render_account(error=str(exc), status=400)
        audit("key_created", f"{current_customer().email} created key {key.key_prefix}", key=key.key_prefix)
        flash("New key created - copy it now, it is shown only once", "success")
        return _render_account(new_key=(key, plain))

    def _own_key(api_key_id: int):
        key = store.get_key(api_key_id)
        if key is None or key.customer_id != current_customer().customer_id:
            abort(404)
        return key

    @app.post("/account/keys/<int:api_key_id>/rotate")
    @login_required
    def account_key_rotate(api_key_id: int):
        key = _own_key(api_key_id)
        try:
            key, plain = store.rotate_key(key.api_key_id)
        except CustomerError as exc:
            return _render_account(error=str(exc), status=400)
        audit("key_rotated", f"{current_customer().email} rotated key {key.key_prefix}", key=key.key_prefix)
        flash("The key was rotated - the old key stopped working. Copy the new key now.", "success")
        return _render_account(new_key=(key, plain))

    @app.post("/account/keys/<int:api_key_id>/revoke")
    @login_required
    def account_key_revoke(api_key_id: int):
        key = _own_key(api_key_id)
        store.revoke_key(key.api_key_id)
        audit("key_revoked", f"{current_customer().email} revoked key {key.key_prefix}", level="WARNING",
              key=key.key_prefix, balance=key.balance)
        flash(f"Key {key.display} revoked", "success")
        return redirect(url_for("account"))

    @app.post("/account/keys/<int:api_key_id>/label")
    @login_required
    def account_key_label(api_key_id: int):
        key = _own_key(api_key_id)
        store.relabel_key(key.api_key_id, request.form.get("label", ""))
        return redirect(url_for("account"))

    @app.post("/account/password")
    @login_required
    def account_password():
        form = request.form
        if (form.get("new_password") or "") != (form.get("confirm_password") or ""):
            return _render_account(error="the new passwords do not match", status=400)
        try:
            store.change_password(current_customer().customer_id, form.get("current_password") or "",
                                  form.get("new_password") or "")
        except CustomerError as exc:
            return _render_account(error=str(exc), status=400)
        audit("password_changed", f"{current_customer().email} changed their password")
        flash("Password changed", "success")
        return redirect(url_for("account"))

    # -- the ADMIN pages (role admin + second factor) - separate from the user pages ------------
    @app.route("/admin")
    @admin_required
    def admin_home():
        return render_template("admin_home.html", stats=store.stats(), orders=store.list_orders(limit=8),
                               usage=store.usage(limit=8), key_prefixes=_key_prefixes(),
                               catalog=_catalog(), audit_log=state.audit.fetch_logs(limit=12, component=COMPONENT))

    def _key_prefixes() -> dict:
        return {k.api_key_id: k.key_prefix for k in store.list_keys(limit=5000)}

    def _customer_rows(q: str | None, limit: int = 200):
        keys_by_customer: dict = {}
        for k in store.list_keys(limit=5000):
            keys_by_customer.setdefault(k.customer_id, []).append(k)
        rows = []
        for c in store.list_customers(limit=limit, q=q):
            rows.append((c, keys_by_customer.get(c.customer_id, []),
                         [m.method for m in state.two_factor.methods(PRINCIPAL_CUSTOMER, c.customer_id)]))
        return rows

    @app.route("/admin/customers")
    @admin_required
    def admin_customers():
        return render_template("admin_customers.html", rows=_customer_rows(request.args.get("q")), args=request.args,
                               roles=CUSTOMER_ROLES, packs=state.packs())

    def _back_to_customers():
        return redirect(url_for("admin_customers", q=request.form.get("q") or None))

    def _admin_action(event: str, message: str, **details):
        audit(event, f"{current_customer().email}: {message}", level="WARNING", **details)

    @app.post("/admin/customers/<int:customer_id>/toggle")
    @admin_required
    def admin_customer_toggle(customer_id: int):
        customer = store.get_customer(customer_id)
        if customer is None:
            abort(404)
        if customer.customer_id == current_customer().customer_id:
            flash("you cannot disable your own account", "error")
            return _back_to_customers()
        try:
            updated = store.update_customer(customer_id, active=not customer.is_active)
        except CustomerError as exc:
            flash(str(exc), "error")
            return _back_to_customers()
        _admin_action("customer_updated", f"{'enabled' if updated.is_active else 'disabled'} {updated.email}",
                      email=updated.email, active=updated.is_active)
        flash(f"{updated.email} {'enabled' if updated.is_active else 'disabled'}", "success")
        return _back_to_customers()

    @app.post("/admin/customers/<int:customer_id>/role")
    @admin_required
    def admin_customer_role(customer_id: int):
        customer = store.get_customer(customer_id)
        if customer is None:
            abort(404)
        try:
            updated = store.set_role(customer_id, request.form.get("role", ""))
        except CustomerError as exc:
            flash(str(exc), "error")
            return _back_to_customers()
        _admin_action("customer_role", f"{updated.email} is now {updated.role}", email=updated.email, role=updated.role)
        flash(f"{updated.email} is now {updated.role}", "success")
        return _back_to_customers()

    @app.post("/admin/customers/<int:customer_id>/reset-2fa")
    @admin_required
    def admin_customer_reset_2fa(customer_id: int):
        customer = store.get_customer(customer_id)
        if customer is None:
            abort(404)
        n = state.two_factor.disable_all(PRINCIPAL_CUSTOMER, customer_id)
        _admin_action("2fa_reset", f"removed {n} second factor(s) of {customer.email}", email=customer.email)
        flash(f"Two-factor authentication of {customer.email} reset ({n} method(s) removed) - they sign in with "
              f"the password only until they set it up again", "success")
        return _back_to_customers()

    @app.post("/admin/customers/<int:customer_id>/verify-email")
    @admin_required
    def admin_customer_verify_email(customer_id: int):
        customer = store.get_customer(customer_id)
        if customer is None:
            abort(404)
        store.mark_email_verified(customer_id)
        _admin_action("email_verified", f"marked {customer.email} as verified", email=customer.email)
        flash(f"{customer.email} marked as verified", "success")
        return _back_to_customers()

    @app.post("/admin/keys/<int:api_key_id>/grant")
    @admin_required
    def admin_key_grant(api_key_id: int):
        try:
            tokens = int(request.form.get("tokens") or 0)
            order, key = store.grant_tokens(api_key_id, tokens, note=request.form.get("note", ""),
                                            granted_by=current_customer().email)
        except (ValueError, CustomerError) as exc:
            flash(f"Could not grant tokens: {exc}", "error")
            return _back_to_customers()
        _admin_action("tokens_granted", f"granted {tokens} tokens to key {key.key_prefix} ({order.order_ref})",
                      key=key.key_prefix, tokens=tokens, order=order.order_ref)
        flash(f"{tokens:,} tokens granted to {key.display} - balance now {key.balance:,} ({order.order_ref})", "success")
        return _back_to_customers()

    @app.post("/admin/keys/<int:api_key_id>/toggle")
    @admin_required
    def admin_key_toggle(api_key_id: int):
        key = store.get_key(api_key_id)
        if key is None:
            abort(404)
        updated = store.set_key_active(api_key_id, not key.is_active)
        _admin_action("key_updated", f"{'re-activated' if updated.is_active else 'disabled'} key {updated.key_prefix}",
                      key=updated.key_prefix, active=updated.is_active)
        flash(f"Key {updated.display} {'re-activated' if updated.is_active else 'disabled'}", "success")
        return _back_to_customers()

    @app.route("/admin/orders")
    @admin_required
    def admin_orders():
        status = request.args.get("status") or None
        return render_template("admin_orders.html", orders=store.list_orders(limit=200, status=status), args=request.args,
                               key_prefixes=_key_prefixes())

    @app.route("/admin/usage")
    @admin_required
    def admin_usage():
        return render_template("admin_usage.html", usage=store.usage(limit=200), key_prefixes=_key_prefixes())

    @app.route("/admin/api/customers")
    @admin_required
    def admin_api_customers():
        rows = _customer_rows(request.args.get("q"))
        return jsonify({"stats": store.stats(), "count": len(rows), "records": [
            {**c.public(), "two_factor": methods, "keys": [k.public() for k in keys]} for c, keys, methods in rows]})

    # -- the data API --------------------------------------------------------------------------
    def api_error(status: int, message: str, **extra):
        payload = {"error": message, "status": status, **extra}
        return jsonify(payload), status

    def key_from_request() -> str | None:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        if request.headers.get("X-API-Key"):
            return request.headers["X-API-Key"].strip()
        return (request.args.get("api_key") or "").strip() or None

    def require_key():
        plain = key_from_request()
        if not plain:
            return None, api_error(401, "an API key is required: send it as 'Authorization: Bearer <key>', "
                                        "an 'X-API-Key' header or the 'api_key' query parameter",
                                   buy_tokens=base_url() + url_for("buy"))
        key = store.authenticate_key(plain)
        if key is None:
            return None, api_error(401, "unknown or revoked API key")
        return key, None

    @app.errorhandler(404)
    def not_found(exc):
        if request.path.startswith("/api/"):
            return api_error(404, "not found - see " + base_url() + url_for("docs"))
        return render_template("error.html", status=404, message="This page does not exist"), 404

    @app.errorhandler(400)
    def bad_request(exc):
        if request.path.startswith("/api/"):
            return api_error(400, getattr(exc, "description", "bad request"))
        return render_template("error.html", status=400, message=getattr(exc, "description", "bad request")), 400

    @app.errorhandler(405)
    def not_allowed(exc):
        if request.path.startswith("/api/"):
            return api_error(405, "method not allowed")
        return render_template("error.html", status=405, message="Method not allowed"), 405

    @app.route("/api/v1/catalog")
    def api_catalog():
        try:
            catalog = state.data.catalog()
        except (sqlite3.Error, DatabaseError) as exc:
            return api_error(503, f"the data is temporarily unavailable: {exc}")
        catalog["packs"] = [p.as_dict() for p in state.packs()]
        catalog["currency"] = settings.currency
        catalog["docs"] = base_url() + url_for("docs")
        return jsonify(catalog)

    @app.route("/api/v1/tickers")
    def api_tickers():
        try:
            kind = parse_kind(request.args.get("kind", "daily"))
            year = parse_year(request.args["year"]) if request.args.get("year") else None
            limit = max(1, min(int(request.args.get("limit", 500)), 5000))
            rows = state.data.tickers(kind, year, request.args.get("market"), request.args.get("q"), limit)
        except (DataApiError, ValueError) as exc:
            return api_error(400, str(exc))
        except (sqlite3.Error, DatabaseError) as exc:
            return api_error(503, f"the data is temporarily unavailable: {exc}")
        return jsonify({"kind": kind, "year": year, "count": len(rows), "tickers": rows})

    @app.route("/api/v1/balance")
    def api_balance():
        key, err = require_key()
        if err:
            return err
        totals = store.usage_totals(api_key_id=key.api_key_id)
        return jsonify({"key": key.display, "label": key.label, "tokens_remaining": key.balance,
                        "tokens_purchased": key.tokens_purchased, "tokens_used": key.tokens_used,
                        "requests": totals["requests"], "rows_delivered": totals["rows"],
                        "last_used_at": key.last_used_at, "tokens_per_ticker_year": settings.tokens_per_query})

    @app.route("/api/v1/usage")
    def api_usage():
        key, err = require_key()
        if err:
            return err
        limit = max(1, min(int(request.args.get("limit", 50) or 50), 500))
        rows = store.usage(api_key_id=key.api_key_id, limit=limit)
        return jsonify({"key": key.display, "count": len(rows), "usage": [
            {"requested_at": u.requested_at, "endpoint": u.endpoint, "tickers": u.tickers, "year": u.year,
             "tokens": u.tokens, "rows": u.rows, "status": u.status} for u in rows]})

    @app.route("/api/v1/<kind>")
    def api_data(kind: str):
        if kind not in KINDS:
            return api_error(404, f"unknown data set '{kind}' - use one of {', '.join(KINDS)}")
        key, err = require_key()
        if err:
            return err
        try:
            tickers = parse_tickers(request.args.getlist("ticker") or None, settings.max_tickers_per_request)
            year = parse_year(request.args.get("year"))
        except DataApiError as exc:
            return api_error(400, str(exc))
        fmt = (request.args.get("format") or "json").lower()
        if fmt not in ("json", "csv"):
            return api_error(400, "format must be json or csv")
        per = settings.tokens_per_query
        if key.balance < per:
            store.charge(key.api_key_id, per, kind, ",".join(tickers), year, 0, request.remote_addr)
            return api_error(402, f"not enough tokens: this request needs at least {per} token(s) and the key has "
                                  f"{key.balance} left", tokens_remaining=key.balance,
                             buy_tokens=base_url() + url_for("buy"))
        try:
            result = state.data.query(kind, tickers, year, request.args.get("market"))
        except DataApiError as exc:
            return api_error(400, str(exc))
        except (sqlite3.Error, DatabaseError) as exc:
            return api_error(503, f"the data is temporarily unavailable: {exc}")
        cost = len(result.tickers_with_data) * per
        charge = store.charge(key.api_key_id, cost, kind, ",".join(tickers), year, result.count, request.remote_addr)
        if not charge.ok:
            return api_error(402, charge.message, tokens_required=cost, tokens_remaining=charge.balance,
                             tickers_with_data=result.tickers_with_data, buy_tokens=base_url() + url_for("buy"))
        headers = {"X-Tokens-Charged": str(charge.tokens), "X-Tokens-Remaining": str(charge.balance)}
        if fmt == "csv":
            name = f"{kind}_{year}_{'_'.join(tickers[:5])}{'_etc' if len(tickers) > 5 else ''}.csv"
            return Response(state.data.to_csv(kind, result.records()), mimetype="text/csv",
                            headers={**headers, "Content-Disposition": f'attachment; filename="{name}"'})
        body = {"kind": kind, "year": year, "market": result.market, "tickers": tickers,
                "tickers_with_data": result.tickers_with_data, "tickers_without_data": result.tickers_without_data,
                "count": result.count, "tokens_charged": charge.tokens, "tokens_remaining": charge.balance,
                "records": result.records()}
        response = jsonify(body)
        response.headers.extend(headers)
        return response

    return app


def run(settings: Settings | None = None, host: str | None = None, port: int | None = None, debug: bool = False) -> None:
    settings = settings or load_settings()
    app = create_app(settings)
    host = host or settings.portal_host
    port = port or settings.portal_port
    st = app.extensions["state"]
    print(f" * stock_dataload portal: http://{host}:{port}/   (config: {settings.config_file}, "
          f"payments: {', '.join(st.providers)} via {'the FAKE gateway - nothing is charged' if st.gateway else 'the live APIs'}, "
          f"sign-in: {', '.join(st.oauth) or 'password only'}{' via the FAKE identity provider' if st.oauth and st.oauth_gateway else ''}, "
          f"mail: {st.two_factor.email.name}{' STUB' if st.two_factor.email.stub else ''}, "
          f"sms: {st.two_factor.sms.name}{' STUB' if st.two_factor.sms.stub else ''})")
    if st.two_factor.email.stub:
        print("   verification / sign-in codes and reset links are PRINTED HERE (no e-mail is sent) - "
              "set [mail] provider = smtp to send them")
    app.run(host=host, port=port, debug=debug, use_reloader=False)
