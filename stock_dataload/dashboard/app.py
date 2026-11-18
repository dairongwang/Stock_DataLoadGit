"""
Business-user dashboard (Flask).

    python -m stock_dataload dashboard            # http://127.0.0.1:8050

Pages
-----
/            overview: conversion folder -> convert -> loading folder -> load, recent loads, errors
/formats     manage custom file formats (csv / text / excel / json / xml, column mapping, ...)
/settings    configure the conversion folder, loading folder, other folders and loader options
/preview     dry-run a conversion and show source + converted sample
/upload      drop a source file into the conversion folder (or a default-format file into loading)
/monitor     operations monitoring: activity log (App_Log), operational errors (App_Error),
             data quality findings + alerts (Data_Error) and the load registry (Load_History);
             the same data as JSON under /api/logs, /api/errors, /api/data-errors, /api/history
/correct     data correction: find a staging row (or come from a data error), fix its values
             or add a missing row; changes are tracked in Data_Correction
/promote     active tables: promote staging -> daily (validated) and daily -> weekly / monthly,
             run history (Promotion_History); /active/<daily|weekly|monthly> browses the tables;
             JSON under /api/promotions and /api/active/<kind>
/load-folder bulk load every default-format file of a folder (files stay in place)
/calendar    the trading calendar: markets and their no-trading days (No_Trading_Day table) -
             generate a year, add / remove closures, add markets; JSON under /api/no-trading-days
/symbols     the stock symbol table (Stock_Symbol): market, first / last trade date per symbol -
             search, edit, import a CSV; JSON under /api/symbols
/login       sign in - with user name + password or with Google / Apple / Facebook (/oauth/<p>/start,
             /oauth/<p>/callback; /fake-oauth/... is the built-in stub); /login/verify asks for the
             second factor (authenticator app / e-mail code / sms code / recovery code) when one is
             set up; /register creates an account (role [accounts] dashboard_registration_role) that
             must confirm its e-mail address with a code (/verify-email); /logout; /account (password,
             e-mail, two-factor authentication, connected accounts); /forgot-password +
             /reset-password/<token> (by e-mail link, for users with an e-mail address)
/users       user management (administrators): add / edit / deactivate / delete accounts, reset passwords
/api-load    API loading: load stocks from the EOD API straight into the staging table - stocks typed in,
             every stock of a market (NYSE, ...) or the stocks of a text file (comma separated); runs in
             the background with live progress (/api/api-load), stop / resume, the run history
             (Eod_Market_Run) and optional promotion; daily limits (load_limits.py): a whole market at
             most 3 times a day, a stock at most 5 times a day, a text file at most 100 symbols and 5
             text-file loads a day - the page shows the counts and warns / blocks
             (/api/api-load/quota?market=NYSE, /api/api-load/limits?symbols=HD,JNJ); daily schedules of
             the "One day, all stocks" load (/api-load/schedules ..., bulk_schedule.py): a time per exchange,
             run once per trading day by the scheduler thread of this process
/docs        the built-in documentation (docs/*.md): user manual, portal guide, technical design,
             operations, reference - a nested tree with search (viewer role)
/customers   (administrators) the customers of the public portal / data API: accounts and guests,
             their API keys and token balances, orders (revenue) and API usage; grant tokens,
             disable keys or customers; JSON under /api/customers

Authorization: viewer (read-only) < operator (convert / load / formats / correction) < admin
(settings + users).  See stock_dataload/auth.py.

Everything the dashboard does goes through the same FileConverter /
StageLoader classes the CLI uses - the dashboard is just a front end.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request, send_from_directory,
                   session, url_for)

from .. import formats
from ..api_jobs import KIND_BULK, KIND_FILE, KIND_MARKET, KIND_RESUME, KIND_SYMBOLS, ApiLoadBusy, ApiLoadManager
from ..accounts import (METHOD_EMAIL, METHOD_LABELS, METHOD_RECOVERY, METHOD_SMS, METHOD_TOTP, METHODS,
                        PRINCIPAL_STAFF, AccountError, CooldownError, Principal, TwoFactorService, format_secret,
                        qr_svg)
from ..audit import AuditLogger, LOG_LEVELS
from ..auth import (ROLE_ADMIN, ROLE_LABELS, ROLE_OPERATOR, ROLE_VIEWER, ROLES, AuthError, User,
                    UserStore, load_secret_key)
from ..config import (ENV_EOD_API_TOKEN, MAIL_PROVIDERS, OAUTH_GATEWAYS, OAUTH_PROVIDERS, PAYMENT_GATEWAYS,
                      PAYMENT_METHODS, SMS_PROVIDERS, ConfigError, Settings, format_token_packs, load_settings,
                      parse_list, parse_token_packs, save_settings)
from ..converter import ConversionError, FileConverter
from ..correction import CorrectionError, CorrectionService, values_from_data_error
from ..customers import CustomerError, CustomerStore
from ..db import FILE_TO_STAGE, STAGE_EDITABLE_COLUMNS, DatabaseError, StageDatabase
from ..docs import DocError, DocTree
from ..file_formats import FILE_TYPE_LABELS, FILE_TYPES, FileFormat, FormatError, FormatStore
from ..loader import LoadError, StageLoader
from ..messaging import MessagingError, mask_email
from ..oauth import (GATEWAY_FAKE as OAUTH_FAKE, PROVIDER_APPLE, PROVIDER_LABELS, FakeIdentityGateway, IdentityStore,
                     OAuthError, make_oauth_providers, make_state, read_state)
from ..promotion import PROCEDURES, PromotionError, PromotionService
from ..quality import DataErrorRepository
from ..readers import SUPPORTED_EXTENSIONS, detect_file_type
from ..symbols import SymbolError, SymbolRegistry
from ..trading_calendar import RULE_SETS, CalendarError, TradingCalendar, default_update_years

log = logging.getLogger(__name__)

API_FILE_MAX_BYTES = 1024 * 1024       # the API loading page's text file of stock symbols

_SAFE_NAME = re.compile(r"^[^/\\\x00]+$")
BROWSABLE = ("conversion_dir", "converted_dir", "loading_dir", "archive_dir", "error_dir", "skipped_dir", "work_dir",
             "log_dir")


@dataclass
class FileInfo:
    name: str
    size: int
    modified: datetime
    kind: str = ""
    note: str = ""
    ok: bool | None = None
    suggested_format: str = ""


class DashboardState:
    """Holds settings + the worker objects; rebuilt after the settings page saves."""

    def __init__(self, settings: Settings):
        self.reload(settings)

    def reload(self, settings: Settings | None = None) -> None:
        if settings is None:
            settings = load_settings(self.settings.config_file)
        self.settings = settings
        self.settings.ensure_folders()
        self.store = FormatStore(settings.formats_file, settings.named_formats)
        self.converter = FileConverter(settings, self.store)
        self.db = StageDatabase(settings)
        self.audit = AuditLogger(settings)
        self.data_errors = DataErrorRepository(settings)
        self.calendar = TradingCalendar(settings, self.db, audit=self.audit)
        self.symbols = SymbolRegistry(settings, self.db, audit=self.audit)
        self.loader = StageLoader(settings, self.db, audit=self.audit, data_errors=self.data_errors,
                                  calendar=self.calendar, symbols=self.symbols)
        self.corrections = CorrectionService(settings, self.db, audit=self.audit, data_errors=self.data_errors)
        self.promotions = PromotionService(settings, self.db, audit=self.audit, data_errors=self.data_errors,
                                           calendar=self.calendar)
        self.users = UserStore(settings)
        self.customers = CustomerStore(settings)
        self.two_factor = TwoFactorService(settings)
        self.oauth_gateway = FakeIdentityGateway() if settings.oauth_gateway == OAUTH_FAKE else None
        self.oauth = make_oauth_providers(settings, self.oauth_gateway)
        self.identities = IdentityStore(settings)
        self.docs = DocTree()

    # -- monitoring ---------------------------------------------------------
    def monitor_counts(self) -> dict[str, int]:
        """Totals for the monitoring strip / page (0s if the database is unusable)."""
        try:
            return {
                "logs": self.audit.count_logs(),
                "errors": self.audit.count_errors(),
                "data_errors": self.data_errors.count(open_only=True),
                "alerts": self.data_errors.count(alerts_only=True, open_only=True),
                "resolved": self.data_errors.count() - self.data_errors.count(open_only=True),
                "corrections": self.corrections.count(),
                "loads": self.db.count_history(),
                "failed_loads": self.db.count_history(status="failed"),
                "promotions": self.promotions.count_history(),
                "failed_promotions": self.promotions.count_history(status="failed"),
            }
        except (sqlite3.Error, DatabaseError, OSError) as exc:
            log.warning("monitoring counts unavailable: %s", exc)
            return {"logs": 0, "errors": 0, "data_errors": 0, "alerts": 0, "resolved": 0, "corrections": 0,
                    "loads": 0, "failed_loads": 0, "promotions": 0, "failed_promotions": 0}

    def active_summaries(self) -> list:
        """Row counts etc. of the active tables (empty list if the database is unusable)."""
        try:
            return self.promotions.summaries()
        except (sqlite3.Error, DatabaseError, OSError) as exc:
            log.warning("active table summaries unavailable: %s", exc)
            return []

    # -- file helpers ------------------------------------------------------
    def folder(self, key: str) -> Path:
        if key not in BROWSABLE:
            abort(404)
        return self.settings.folders[key]

    def safe_path(self, key: str, name: str) -> Path:
        folder = self.folder(key)
        if not name or not _SAFE_NAME.match(name) or name in (".", "..") or name.startswith("."):
            abort(400, "invalid file name")
        path = (folder / name)
        try:
            path.resolve().relative_to(folder.resolve())
        except ValueError:
            abort(400, "invalid file name")
        if not path.is_file():
            abort(404, f"{name} not found in {folder}")
        return path

    def list_files(self, key: str) -> list[FileInfo]:
        folder = self.settings.folders[key]
        if not folder.is_dir():
            return []
        out = []
        for p in sorted(folder.iterdir()):
            if not p.is_file() or p.name.startswith("."):
                continue
            st = p.stat()
            out.append(FileInfo(p.name, st.st_size, datetime.fromtimestamp(st.st_mtime)))
        return out

    def conversion_files(self) -> list[FileInfo]:
        infos = []
        for info in self.list_files("conversion_dir"):
            if info.name.endswith(".error.txt") or info.name.endswith(".converting"):
                continue
            path = self.settings.conversion_dir / info.name
            try:
                info.kind = detect_file_type(path)
            except Exception as exc:  # noqa: BLE001
                info.kind = "?"
                info.note = str(exc)
                info.ok = False
            fmt = self.store.match(info.name)
            info.suggested_format = fmt.name if fmt else ""
            err = path.with_name(path.name + ".error.txt")
            if err.exists():
                info.ok = False
                note = err.read_text(encoding="utf-8", errors="replace").strip()
                info.note = re.sub(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s+", "", note)[:400]
            infos.append(info)
        return infos

    def loading_files(self) -> list[FileInfo]:
        infos = []
        patterns = self.settings.file_patterns
        import fnmatch
        for info in self.list_files("loading_dir"):
            path = self.settings.loading_dir / info.name
            if not any(fnmatch.fnmatch(info.name, p) for p in patterns):
                info.ok = False
                info.note = f"ignored by loader (file_patterns = {';'.join(patterns)})"
                infos.append(info)
                continue
            try:
                with open(path, "r", encoding=self.settings.encoding, newline="") as fh:
                    first = fh.readline().strip()
                fields = first.split(formats.DEFAULT_DELIMITER)
                info.ok = formats.is_default_header(fields)
                info.note = "default format" if info.ok else f"header '{first[:60]}' is not the default sequence"
            except OSError as exc:
                info.ok = False
                info.note = str(exc)
            infos.append(info)
        return infos


#: endpoints that need more than the viewer role
_OPERATOR_ENDPOINTS = frozenset({"convert", "convert_all", "load", "load_folder", "upload", "delete_file",
                                 "format_edit", "format_delete", "correct", "correct_save", "promote_run",
                                 "calendar_update", "calendar_day_add", "calendar_day_remove",
                                 "symbol_save", "symbol_delete", "symbols_import",
                                 "api_load_start", "api_load_stop", "api_load_resume",
                              "api_load_schedule_save", "api_load_schedule_toggle", "api_load_schedule_delete",
                              "api_load_schedule_run"})
_ADMIN_ENDPOINTS = frozenset({"settings_page", "users", "user_new", "user_edit", "user_delete", "user_reset_2fa",
                              "calendar_market_add", "calendar_market_toggle",
                              "customers", "api_customers", "customer_toggle", "customer_key_grant",
                              "customer_key_toggle"})
#: endpoints reachable without signing in
_PUBLIC_ENDPOINTS = frozenset({"static", "login", "login_verify", "forgot_password", "reset_password", "register",
                               "verify_email", "oauth_start", "oauth_callback", "fake_oauth"})


def required_role(endpoint: str | None) -> str:
    """The minimum role an endpoint needs (viewer for everything not listed)."""
    if endpoint in _ADMIN_ENDPOINTS:
        return ROLE_ADMIN
    if endpoint in _OPERATOR_ENDPOINTS:
        return ROLE_OPERATOR
    return ROLE_VIEWER


def bootstrap_admin(state: DashboardState) -> User | None:
    """Create the initial administrator when the user table is empty.

    The one-time password is printed to the console (never stored anywhere)
    unless ``$STOCK_DATALOAD_ADMIN_PASSWORD`` supplies it; it must be changed
    at the first login.
    """
    try:
        if state.users.count() > 0:
            return None
        user, generated = state.users.bootstrap_admin()
    except (AuthError, sqlite3.Error, DatabaseError) as exc:
        log.warning("could not create the initial administrator: %s", exc)
        return None
    state.audit.log("auth", "admin_bootstrapped", f"initial administrator '{user.username}' created",
                    level="WARNING", details={"username": user.username})
    banner = [
        "=" * 72,
        f"  Initial administrator created: username '{user.username}'",
        (f"  One-time password: {generated}" if generated else
         f"  Password taken from ${'STOCK_DATALOAD_ADMIN_PASSWORD'}"),
        "  It must be changed at the first login. Then add users on the 'Users' page.",
        "=" * 72,
    ]
    print("\n".join(banner), flush=True)
    log.warning("initial administrator '%s' created - the one-time password was printed to the console",
                user.username)
    return user


def create_app(settings: Settings | None = None, bootstrap: bool = True) -> Flask:
    settings = settings or load_settings()
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["SECRET_KEY"] = load_secret_key(settings)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024
    state = DashboardState(settings)
    app.extensions["state"] = state
    # API loads run in the background and outlive a settings reload (they read state.settings when they start)
    api_loads = ApiLoadManager(lambda: state.settings, audit_factory=AuditLogger)
    app.extensions["api_loads"] = api_loads
    # the daily schedules of the one-day bulk load: run by this thread when the dashboard is served (run())
    from ..bulk_schedule import BulkScheduler
    bulk_scheduler = BulkScheduler(lambda: state.settings, api_loads, audit_factory=AuditLogger)
    app.extensions["bulk_scheduler"] = bulk_scheduler
    if bootstrap:
        bootstrap_admin(state)

    # -- authentication / authorization -------------------------------------
    def current_user() -> User | None:
        if "user" not in g:
            g.user = None
            user_id = session.get("user_id")
            if user_id is not None:
                try:
                    user = state.users.get(int(user_id))
                except (ValueError, sqlite3.Error, DatabaseError):
                    user = None
                g.user = user if user is not None and user.is_active else None
        return g.user

    def can(role: str) -> bool:
        user = current_user()
        return user is not None and user.can(role)

    @app.before_request
    def require_login():
        endpoint = request.endpoint
        if endpoint in _PUBLIC_ENDPOINTS or endpoint is None:
            return None
        user = current_user()
        if user is None:
            if request.path.startswith("/api/"):
                return jsonify({"error": "authentication required"}), 401
            session.pop("user_id", None)          # keep a pending second-factor challenge alive
            flash("Please sign in to continue", "info")
            return redirect(url_for("login", next=request.full_path if request.query_string else request.path))
        if user.must_change_password and endpoint not in ("account", "logout"):
            flash("Please choose a new password before continuing", "info")
            return redirect(url_for("account"))
        needed = required_role(endpoint)
        if not user.can(needed):
            state.audit.log("auth", "access_denied", f"{user.username} ({user.role}) tried {request.method} "
                            f"{request.path} (needs {needed})", level="WARNING",
                            details={"username": user.username, "role": user.role, "endpoint": endpoint})
            if request.path.startswith("/api/"):
                return jsonify({"error": f"the {needed} role is required"}), 403
            return render_template("forbidden.html", needed=needed), 403
        return None

    @app.context_processor
    def inject():
        return {
            "settings": state.settings,
            "default_header": formats.DEFAULT_HEADER_LINE,
            "default_columns": formats.DEFAULT_COLUMNS,
            "file_types": FILE_TYPES,
            "file_type_labels": FILE_TYPE_LABELS,
            "now": datetime.now(),
            "current_user": current_user(),
            "can": can,
            "roles": ROLES,
            "role_labels": ROLE_LABELS,
            "method_labels": METHOD_LABELS,
            "mail_stub": state.two_factor.email.stub,
            "sms_stub": state.two_factor.sms.stub,
            "oauth_providers": state.oauth, "oauth_fake": state.oauth_gateway is not None,
            "registration_open": state.settings.dashboard_registration,
        }

    def principal_of(user: User) -> Principal:
        return Principal(PRINCIPAL_STAFF, user.user_id, user.email, None, user.display_name or user.username)

    def complete_login(user: User):
        session.clear()
        session["user_id"] = user.user_id
        session.permanent = False
        g.pop("user", None)
        state.audit.log("auth", "login", f"{user.username} signed in ({user.role})",
                        details={"username": user.username, "role": user.role, "remote": request.remote_addr})

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "GET":
            if current_user() is not None:
                return redirect(url_for("index"))
            return render_template("login.html", next=request.args.get("next", ""))
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        try:
            user = state.users.authenticate(username, password)
        except (sqlite3.Error, DatabaseError) as exc:
            return render_template("login.html", error=f"database problem: {exc}", next=""), 500
        if user is None:
            state.audit.log("auth", "login_failed", f"failed sign-in for '{username}'", level="WARNING",
                            details={"username": username, "remote": request.remote_addr})
            return render_template("login.html", error="unknown user name or wrong password",
                                   next=request.form.get("next", ""), username=username), 401
        target = request.form.get("next") or ""
        if not user.is_email_verified:
            return start_email_verification(user, target)
        return continue_login(user, target)

    def start_email_verification(user: User, target: str = ""):
        try:
            state.two_factor.send_email_verification(principal_of(user))
        except CooldownError:
            pass
        except AccountError as exc:
            flash(str(exc), "error")
        session.clear()
        session["pending_verify"] = user.user_id
        session["pending_next"] = target
        flash("Please confirm your e-mail address first - we sent you a code", "info")
        return redirect(url_for("verify_email"))

    def continue_login(user: User, target: str = ""):
        if state.two_factor.has_two_factor(PRINCIPAL_STAFF, user.user_id):
            session.clear()
            session["pending_2fa"] = user.user_id
            session["pending_next"] = target
            return redirect(url_for("login_verify"))
        complete_login(user)
        if user.must_change_password:
            flash("Welcome! Please choose a new password before continuing", "info")
            return redirect(url_for("account"))
        if not target.startswith("/") or target.startswith("//"):
            target = url_for("index")
        return redirect(target)

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if not state.settings.dashboard_registration:
            abort(404)
        if current_user() is not None:
            return redirect(url_for("index"))
        if request.method == "GET":
            return render_template("register.html", form={}, error=None)
        form = request.form
        if (form.get("password") or "") != (form.get("confirm_password") or ""):
            return render_template("register.html", form=form, error="the passwords do not match"), 400
        try:
            user = state.users.register(form.get("username", ""), form.get("password") or "", form.get("email", ""),
                                        display_name=form.get("display_name", ""),
                                        role=state.settings.dashboard_registration_role)
        except AuthError as exc:
            return render_template("register.html", form=form, error=str(exc)), 400
        state.audit.log("auth", "user_registered", f"'{user.username}' registered ({user.role})",
                        details={"username": user.username, "email": user.email, "remote": request.remote_addr})
        return start_email_verification(user)

    @app.route("/verify-email", methods=["GET", "POST"])
    def verify_email():
        user_id = session.get("pending_verify")
        user = state.users.get(int(user_id)) if user_id is not None else None
        if user is None or not user.is_active:
            session.pop("pending_verify", None)
            flash("Please sign in - you will be asked for the verification code", "info")
            return redirect(url_for("login"))
        if request.method == "GET":
            return render_template("verify_email.html", user=user, masked=mask_email(user.email or ""), error=None)
        if request.form.get("action") == "resend":
            try:
                state.two_factor.send_email_verification(principal_of(user))
                flash("A new code was sent", "info")
            except AccountError as exc:
                flash(str(exc), "error")
            return redirect(url_for("verify_email"))
        if not state.two_factor.verify_email(PRINCIPAL_STAFF, user.user_id, request.form.get("code", "")):
            state.audit.log("auth", "verify_email_failed", f"wrong verification code for '{user.username}'",
                            level="WARNING", details={"username": user.username})
            return render_template("verify_email.html", user=user, masked=mask_email(user.email or ""),
                                   error="the code is not correct or has expired"), 400
        user = state.users.mark_email_verified(user.user_id)
        state.audit.log("auth", "email_verified", f"'{user.username}' confirmed their e-mail address",
                        details={"username": user.username})
        flash("Your e-mail address is confirmed - welcome!", "success")
        return continue_login(user, session.get("pending_next") or "")

    # -- sign in with Google / Apple / Facebook ------------------------------------
    def _oauth_provider(name: str):
        provider = state.oauth.get(name)
        if provider is None:
            abort(404)
        return provider

    def _oauth_redirect_uri(name: str) -> str:
        return request.url_root.rstrip("/") + url_for("oauth_callback", provider=name)

    @app.route("/oauth/<provider>/start")
    def oauth_start(provider: str):
        p = _oauth_provider(provider)
        user = current_user()
        payload = {"p": provider, "next": request.args.get("next", "")}
        if user is not None and request.args.get("link") == "1":
            payload["link"] = user.user_id
        return redirect(p.authorize_url(_oauth_redirect_uri(provider), make_state(app.config["SECRET_KEY"], payload)))

    @app.route("/oauth/<provider>/callback", methods=["GET", "POST"])
    def oauth_callback(provider: str):
        p = _oauth_provider(provider)
        args = request.form if request.method == "POST" else request.args
        payload = read_state(app.config["SECRET_KEY"], args.get("state", ""))
        if payload is None or payload.get("p") != provider:
            state.audit.log("auth", "oauth_failed", f"{p.label}: invalid or expired state", level="WARNING",
                            details={"provider": provider, "remote": request.remote_addr})
            flash("The sign-in attempt has expired or was tampered with - please try again", "error")
            return redirect(url_for("login"))
        if args.get("error") or not args.get("code"):
            flash(f"{p.label} sign-in was cancelled or denied", "info")
            return redirect(url_for("login"))
        try:
            tokens = p.exchange_code(args["code"], _oauth_redirect_uri(provider))
            profile = p.profile(tokens, args)
        except OAuthError as exc:
            state.audit.log("auth", "oauth_failed", f"{p.label}: {exc}", level="WARNING", details={"provider": provider})
            flash(str(exc), "error")
            return redirect(url_for("login"))
        if payload.get("link"):                      # connecting a provider to the signed-in user
            user = current_user()
            if user is None or user.user_id != payload["link"]:
                flash("Please sign in first, then connect the account", "info")
                return redirect(url_for("login"))
            try:
                state.identities.link(PRINCIPAL_STAFF, user.user_id, profile)
            except OAuthError as exc:
                flash(str(exc), "error")
                return redirect(url_for("account"))
            state.audit.log("auth", "oauth_linked", f"'{user.username}' connected {p.label}",
                            details={"username": user.username, "provider": provider})
            flash(f"{p.label} connected - you can now sign in with it", "success")
            return redirect(url_for("account"))
        identity = state.identities.find(provider, profile.subject)
        if identity is not None:
            user = state.users.get(identity.principal_id)
            if user is None or not user.is_active:
                flash("This account is disabled", "error")
                return redirect(url_for("login"))
            state.identities.touch(identity.identity_id)
        else:
            if not profile.email:
                flash(f"{p.label} did not share an e-mail address - allow it, or sign in with your password", "error")
                return redirect(url_for("login"))
            user = state.users.get_by_email(profile.email)
            if user is not None:
                if not profile.email_verified:
                    flash(f"{p.label} has not verified {profile.email} - sign in with your password and connect "
                          f"{p.label} on your account page", "error")
                    return redirect(url_for("login"))
                if not user.is_active:
                    flash("This account is disabled", "error")
                    return redirect(url_for("login"))
            elif state.settings.dashboard_registration:
                try:
                    user = state.users.create_external_user(profile.email, profile.name or "",
                                                            state.settings.dashboard_registration_role)
                except AuthError as exc:
                    flash(str(exc), "error")
                    return redirect(url_for("login"))
                state.audit.log("auth", "user_registered", f"'{user.username}' registered via {p.label} ({user.role})",
                                details={"username": user.username, "email": user.email, "provider": provider})
            else:
                flash(f"No dashboard account is registered for {profile.email} - ask an administrator to create one",
                      "error")
                return redirect(url_for("login"))
            try:
                state.identities.link(PRINCIPAL_STAFF, user.user_id, profile)
            except OAuthError as exc:
                flash(str(exc), "error")
                return redirect(url_for("login"))
            if profile.email_verified and not user.is_email_verified:
                user = state.users.mark_email_verified(user.user_id)
        state.audit.log("auth", "oauth_login", f"'{user.username}' signed in with {p.label}",
                        details={"username": user.username, "provider": provider, "remote": request.remote_addr})
        if not user.is_email_verified:
            return start_email_verification(user, payload.get("next") or "")
        return continue_login(user, payload.get("next") or "")

    @app.post("/account/oauth/<provider>/unlink")
    def oauth_unlink(provider: str):
        user = current_user()
        if not user.has_password and len(state.identities.identities_of(PRINCIPAL_STAFF, user.user_id)) <= 1:
            flash("This is the only way into your account - set a password first (Forgot your password)", "error")
            return redirect(url_for("account"))
        if state.identities.unlink(PRINCIPAL_STAFF, user.user_id, provider):
            state.audit.log("auth", "oauth_unlinked", f"'{user.username}' disconnected {PROVIDER_LABELS.get(provider, provider)}",
                            level="WARNING", details={"username": user.username, "provider": provider})
            flash(f"{PROVIDER_LABELS.get(provider, provider)} disconnected", "success")
        return redirect(url_for("account"))

    @app.route("/fake-oauth/<provider>/authorize", methods=["GET", "POST"])
    def fake_oauth(provider: str):
        """The built-in stub of the provider's consent page (oauth gateway = fake)."""
        if state.oauth_gateway is None or provider not in state.oauth:
            abort(404)
        redirect_uri, state_token = request.values.get("redirect_uri", ""), request.values.get("state", "")
        if request.method == "GET":
            return render_template("fake_oauth.html", provider=provider, label=PROVIDER_LABELS[provider],
                                   redirect_uri=redirect_uri, state=state_token, client_id=request.args.get("client_id", ""))
        if request.form.get("action") == "cancel":
            return redirect(redirect_uri + ("&" if "?" in redirect_uri else "?") + f"error=access_denied&state={state_token}")
        grant = state.oauth_gateway.issue_code(provider, request.form.get("email", ""), request.form.get("name", ""))
        if provider == PROVIDER_APPLE:
            user = {"name": {"firstName": grant.name.split(" ")[0] if grant.name else "",
                             "lastName": " ".join(grant.name.split(" ")[1:])}, "email": grant.email}
            return render_template("fake_oauth_post.html", redirect_uri=redirect_uri, code=grant.code,
                                   state=state_token, user=user)
        return redirect(redirect_uri + ("&" if "?" in redirect_uri else "?") + f"code={grant.code}&state={state_token}")

    @app.route("/login/verify", methods=["GET", "POST"])
    def login_verify():
        user_id = session.get("pending_2fa")
        user = state.users.get(int(user_id)) if user_id is not None else None
        if user is None or not user.is_active:
            session.pop("pending_2fa", None)
            return redirect(url_for("login"))
        methods = state.two_factor.methods(PRINCIPAL_STAFF, user.user_id)
        if not methods:
            complete_login(user)
            return redirect(url_for("index"))
        available = [m.method for m in methods]
        chosen = request.values.get("method") or (METHOD_TOTP if METHOD_TOTP in available else available[0])
        if chosen not in available and chosen != METHOD_RECOVERY:
            chosen = available[0]
        pending = state.two_factor.pending_code(PRINCIPAL_STAFF, user.user_id, "login")
        recovery_left = state.two_factor.recovery_codes_left(PRINCIPAL_STAFF, user.user_id)
        if request.method == "GET":
            return render_template("login_verify.html", user=user, methods=methods, chosen=chosen, pending=pending,
                                   recovery_left=recovery_left, error=None)
        if request.form.get("action") == "send":
            try:
                masked = state.two_factor.send_login_code(PRINCIPAL_STAFF, user.user_id, chosen)
                flash(f"A code was sent to {masked}", "info")
            except AccountError as exc:
                flash(str(exc), "error")
            return redirect(url_for("login_verify", method=chosen))
        if not state.two_factor.verify_login(PRINCIPAL_STAFF, user.user_id, chosen, request.form.get("code", "")):
            state.audit.log("auth", "login_2fa_failed", f"{user.username} failed the second factor ({chosen})",
                            level="WARNING", details={"username": user.username, "method": chosen,
                                                      "remote": request.remote_addr})
            return render_template("login_verify.html", user=user, methods=methods, chosen=chosen, pending=pending,
                                   recovery_left=recovery_left, error="the code is not correct"), 401
        target = session.get("pending_next") or ""
        complete_login(user)
        state.audit.log("auth", "login_2fa", f"{user.username} passed the second factor ({chosen})",
                        details={"username": user.username, "method": chosen})
        if chosen == METHOD_RECOVERY:
            flash("You signed in with a recovery code - consider generating new ones on your account page", "info")
        if user.must_change_password:
            flash("Welcome! Please choose a new password before continuing", "info")
            return redirect(url_for("account"))
        if not target.startswith("/") or target.startswith("//"):
            target = url_for("index")
        return redirect(target)

    @app.route("/forgot-password", methods=["GET", "POST"])
    def forgot_password():
        if request.method == "GET":
            return render_template("forgot.html", sent=False, value=request.args.get("user", ""))
        value = (request.form.get("user") or "").strip()
        user = state.users.get_by_username(value) or state.users.get_by_email(value)
        if user is not None and user.is_active and user.email:
            try:
                state.two_factor.send_password_reset(
                    principal_of(user), lambda token: request.url_root.rstrip("/") + url_for("reset_password", token=token))
                state.audit.log("auth", "password_reset_requested", f"reset link sent to {user.username}",
                                details={"username": user.username, "remote": request.remote_addr})
            except CooldownError:
                pass
            except AccountError as exc:
                flash(str(exc), "error")
        else:
            state.audit.log("auth", "password_reset_unknown", f"password reset requested for '{value}' "
                            f"(unknown, disabled or without e-mail)", level="WARNING",
                            details={"value": value, "remote": request.remote_addr})
        return render_template("forgot.html", sent=True, value=value)

    @app.route("/reset-password/<token>", methods=["GET", "POST"])
    def reset_password(token: str):
        found = state.two_factor.peek_reset_token(token)
        if found is None or found[0] != PRINCIPAL_STAFF:
            flash("This password-reset link is not valid or has expired - request a new one", "error")
            return redirect(url_for("forgot_password"))
        user = state.users.get(found[1])
        if user is None or not user.is_active:
            abort(404)
        if request.method == "GET":
            return render_template("reset.html", token=token, user=user, error=None)
        new, confirm = request.form.get("new_password") or "", request.form.get("confirm_password") or ""
        if new != confirm:
            return render_template("reset.html", token=token, user=user, error="the passwords do not match"), 400
        try:
            state.users.set_password(user.user_id, new, must_change=False)
        except AuthError as exc:
            return render_template("reset.html", token=token, user=user, error=str(exc)), 400
        if state.two_factor.consume_reset_token(token) is None:
            flash("This password-reset link was already used", "error")
            return redirect(url_for("forgot_password"))
        state.audit.log("auth", "password_reset", f"{user.username} reset their password by e-mail link",
                        level="WARNING", details={"username": user.username, "remote": request.remote_addr})
        session.clear()
        flash("Your password was changed - please sign in", "success")
        return redirect(url_for("login"))

    @app.post("/logout")
    def logout():
        user = current_user()
        if user is not None:
            state.audit.log("auth", "logout", f"{user.username} signed out", details={"username": user.username})
        session.clear()
        flash("You have been signed out", "info")
        return redirect(url_for("login"))

    def _render_account(error: str | None = None, status: int = 200, **extra):
        user = current_user()
        return render_template("account.html", user=user, error=error,
                               identities=state.identities.identities_of(PRINCIPAL_STAFF, user.user_id),
                               methods=state.two_factor.methods(PRINCIPAL_STAFF, user.user_id, enabled_only=False),
                               recovery_left=state.two_factor.recovery_codes_left(PRINCIPAL_STAFF, user.user_id),
                               **extra), status

    @app.route("/account", methods=["GET", "POST"])
    def account():
        user = current_user()
        if request.method == "GET":
            return _render_account()
        new, confirm = request.form.get("new_password") or "", request.form.get("confirm_password") or ""
        if new != confirm:
            return _render_account("the new passwords do not match", 400)
        try:
            state.users.change_password(user.user_id, request.form.get("current_password") or "", new)
        except AuthError as exc:
            return _render_account(str(exc), 400)
        state.audit.log("auth", "password_changed", f"{user.username} changed their password",
                        details={"username": user.username})
        flash("Password changed", "success")
        return redirect(url_for("index"))

    @app.post("/account/email")
    def account_email():
        user = current_user()
        try:
            updated = state.users.update(user.user_id, email=request.form.get("email", ""))
        except AuthError as exc:
            return _render_account(str(exc), 400)
        state.audit.log("auth", "email_changed", f"{user.username} set their e-mail address",
                        details={"username": user.username, "email": updated.email})
        flash("E-mail address saved", "success")
        return redirect(url_for("account"))

    def _after_enrolment(user: User):
        if state.two_factor.recovery_codes_left(PRINCIPAL_STAFF, user.user_id) == 0:
            codes = state.two_factor.generate_recovery_codes(PRINCIPAL_STAFF, user.user_id)
            flash("Save your recovery codes - they let you in if you lose your phone", "info")
            return _render_account(recovery_codes=codes)
        return redirect(url_for("account"))

    @app.post("/account/security/totp/begin")
    def security_totp_begin():
        user = current_user()
        secret, uri = state.two_factor.begin_totp(principal_of(user))
        return _render_account(totp_setup={"secret": format_secret(secret), "uri": uri, "qr": qr_svg(uri)})

    @app.post("/account/security/totp/confirm")
    def security_totp_confirm():
        user = current_user()
        if not state.two_factor.confirm_totp(PRINCIPAL_STAFF, user.user_id, request.form.get("code", "")):
            row = state.two_factor.get_method(PRINCIPAL_STAFF, user.user_id, METHOD_TOTP)
            setup = {"secret": format_secret(row.secret), "uri": None, "qr": None} if row and row.secret else None
            return _render_account("the code is not correct - try again with the next code", 400, totp_setup=setup)
        state.audit.log("auth", "2fa_enabled", f"{user.username} enabled an authenticator app",
                        details={"username": user.username, "method": METHOD_TOTP})
        flash("Authenticator app enabled", "success")
        return _after_enrolment(user)

    @app.post("/account/security/<method>/begin")
    def security_code_begin(method: str):
        if method not in (METHOD_EMAIL, METHOD_SMS):
            abort(404)
        user = current_user()
        try:
            masked = state.two_factor.begin_code_method(principal_of(user), method, request.form.get("phone"))
        except (AccountError, MessagingError) as exc:
            return _render_account(str(exc), 400)
        flash(f"A code was sent to {masked} - enter it below to finish", "info")
        return _render_account(confirm_method=method)

    @app.post("/account/security/<method>/confirm")
    def security_code_confirm(method: str):
        if method not in (METHOD_EMAIL, METHOD_SMS):
            abort(404)
        user = current_user()
        if not state.two_factor.confirm_code_method(PRINCIPAL_STAFF, user.user_id, method, request.form.get("code", "")):
            return _render_account("the code is not correct or has expired", 400, confirm_method=method)
        state.audit.log("auth", "2fa_enabled", f"{user.username} enabled {METHOD_LABELS[method]}",
                        details={"username": user.username, "method": method})
        flash(f"{METHOD_LABELS[method]} enabled", "success")
        return _after_enrolment(user)

    @app.post("/account/security/<method>/disable")
    def security_disable(method: str):
        if method not in METHODS:
            abort(404)
        user = current_user()
        if state.users.authenticate(user.username, request.form.get("password") or "") is None:
            return _render_account("please confirm with your password", 400)
        state.two_factor.disable(PRINCIPAL_STAFF, user.user_id, method)
        state.audit.log("auth", "2fa_disabled", f"{user.username} removed {METHOD_LABELS[method]}", level="WARNING",
                        details={"username": user.username, "method": method})
        flash(f"{METHOD_LABELS[method]} removed", "success")
        return redirect(url_for("account"))

    @app.post("/account/security/recovery")
    def security_recovery():
        user = current_user()
        if state.users.authenticate(user.username, request.form.get("password") or "") is None:
            return _render_account("please confirm with your password", 400)
        codes = state.two_factor.generate_recovery_codes(PRINCIPAL_STAFF, user.user_id)
        state.audit.log("auth", "recovery_codes_generated", f"{user.username} generated new recovery codes",
                        details={"username": user.username})
        flash("New recovery codes generated - the old ones no longer work", "success")
        return _render_account(recovery_codes=codes)

    # -- user management (administrators) ---------------------------------------
    @app.route("/users")
    def users():
        everyone = state.users.list_users()
        two_factor = {u.user_id: [m.method for m in state.two_factor.methods(PRINCIPAL_STAFF, u.user_id)] for u in everyone}
        return render_template("users.html", users=everyone, two_factor=two_factor)

    @app.route("/users/new", methods=["GET", "POST"])
    def user_new():
        if request.method == "GET":
            return render_template("user_form.html", user=None, form={}, error=None)
        form = request.form
        if (form.get("password") or "") != (form.get("confirm_password") or ""):
            return render_template("user_form.html", user=None, form=form, error="the passwords do not match"), 400
        try:
            created = state.users.create_user(form.get("username", ""), form.get("password") or "",
                                              role=form.get("role", ROLE_VIEWER), display_name=form.get("display_name", ""),
                                              created_by=current_user().username,
                                              must_change_password=form.get("must_change_password") == "on",
                                              email=form.get("email", ""))
        except AuthError as exc:
            return render_template("user_form.html", user=None, form=form, error=str(exc)), 400
        state.audit.log("auth", "user_created", f"{current_user().username} created user '{created.username}' "
                        f"({created.role})", details={"username": created.username, "role": created.role})
        flash(f"User '{created.username}' created with role {created.role}", "success")
        return redirect(url_for("users"))

    @app.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
    def user_edit(user_id: int):
        user = state.users.get(user_id)
        if user is None:
            abort(404)
        if request.method == "GET":
            return render_template("user_form.html", user=user, form=user.public(), error=None)
        form = request.form
        me = current_user()
        try:
            if form.get("new_password"):
                if form.get("new_password") != (form.get("confirm_password") or ""):
                    raise AuthError("the passwords do not match")
                state.users.set_password(user_id, form["new_password"],
                                         must_change=form.get("must_change_password") == "on")
                state.audit.log("auth", "password_reset", f"{me.username} reset the password of '{user.username}'",
                                level="WARNING", details={"username": user.username})
            active = form.get("active") == "on" if user.user_id != me.user_id else True
            updated = state.users.update(user_id, role=form.get("role", user.role), active=active,
                                         display_name=form.get("display_name", ""), email=form.get("email", ""))
        except AuthError as exc:
            return render_template("user_form.html", user=user, form=form, error=str(exc)), 400
        state.audit.log("auth", "user_updated", f"{me.username} updated user '{updated.username}' "
                        f"(role={updated.role}, active={updated.is_active})",
                        details={"username": updated.username, "role": updated.role, "active": updated.is_active})
        flash(f"User '{updated.username}' updated", "success")
        return redirect(url_for("users"))

    @app.post("/users/<int:user_id>/reset-2fa")
    def user_reset_2fa(user_id: int):
        user = state.users.get(user_id)
        if user is None:
            abort(404)
        n = state.two_factor.disable_all(PRINCIPAL_STAFF, user_id)
        state.audit.log("auth", "2fa_reset", f"{current_user().username} removed {n} second factor(s) of "
                        f"'{user.username}'", level="WARNING", details={"username": user.username})
        flash(f"Two-factor authentication of '{user.username}' reset ({n} method(s) removed)", "success")
        return redirect(url_for("users"))

    @app.post("/users/<int:user_id>/delete")
    def user_delete(user_id: int):
        user = state.users.get(user_id)
        if user is None:
            abort(404)
        me = current_user()
        if user.user_id == me.user_id:
            flash("you cannot delete your own account", "error")
            return redirect(url_for("users"))
        try:
            state.users.delete_user(user_id)
        except AuthError as exc:
            flash(str(exc), "error")
            return redirect(url_for("users"))
        state.audit.log("auth", "user_deleted", f"{me.username} deleted user '{user.username}'", level="WARNING",
                        details={"username": user.username})
        flash(f"User '{user.username}' deleted", "success")
        return redirect(url_for("users"))

    # -- customers of the portal / data API (administrators) ----------------------
    def _customer_rows(q: str | None, limit: int):
        customers = state.customers.list_customers(limit=limit, q=q)
        keys_by_customer: dict[int, list] = {}
        for k in state.customers.list_keys(limit=5000):
            keys_by_customer.setdefault(k.customer_id, []).append(k)
        return [(c, keys_by_customer.get(c.customer_id, [])) for c in customers]

    @app.route("/customers")
    def customers():
        a = request.args
        try:
            rows = _customer_rows(a.get("q"), _limit(100))
            stats = state.customers.stats()
            orders = state.customers.list_orders(limit=_limit(25), status=a.get("status") or None)
            usage = state.customers.usage(limit=_limit(25))
        except (sqlite3.Error, DatabaseError) as exc:
            flash(f"database problem: {exc}", "error")
            rows, stats, orders, usage = [], {}, [], []
        key_prefixes = {k.api_key_id: k.key_prefix for _, keys in rows for k in keys}
        return render_template("customers.html", rows=rows, stats=stats, orders=orders, usage=usage, args=a,
                               key_prefixes=key_prefixes, packs=state.customers.packs(), limit=_limit(100))

    @app.post("/customers/<int:customer_id>/toggle")
    def customer_toggle(customer_id: int):
        customer = state.customers.get_customer(customer_id)
        if customer is None:
            abort(404)
        updated = state.customers.update_customer(customer_id, active=not customer.is_active)
        state.audit.log("portal", "customer_updated", f"{current_user().username} "
                        f"{'enabled' if updated.is_active else 'disabled'} customer {updated.email}", level="WARNING",
                        details={"email": updated.email, "active": updated.is_active})
        flash(f"Customer {updated.email} {'enabled' if updated.is_active else 'disabled'}", "success")
        return redirect(url_for("customers", q=request.form.get("q") or None))

    @app.post("/customers/keys/<int:api_key_id>/grant")
    def customer_key_grant(api_key_id: int):
        try:
            tokens = int(request.form.get("tokens") or 0)
            order, key = state.customers.grant_tokens(api_key_id, tokens, note=request.form.get("note", ""),
                                                      granted_by=current_user().username)
        except (ValueError, CustomerError) as exc:
            flash(f"Could not grant tokens: {exc}", "error")
            return redirect(url_for("customers", q=request.form.get("q") or None))
        state.audit.log("portal", "tokens_granted", f"{current_user().username} granted {tokens} tokens to key "
                        f"{key.key_prefix} ({order.order_ref}: {order.note or 'no note'})", level="WARNING",
                        details={"key": key.key_prefix, "tokens": tokens, "order": order.order_ref})
        flash(f"{tokens:,} tokens granted to key {key.display} - balance now {key.balance:,} ({order.order_ref})",
              "success")
        return redirect(url_for("customers", q=request.form.get("q") or None))

    @app.post("/customers/keys/<int:api_key_id>/toggle")
    def customer_key_toggle(api_key_id: int):
        key = state.customers.get_key(api_key_id)
        if key is None:
            abort(404)
        updated = state.customers.set_key_active(api_key_id, not key.is_active)
        state.audit.log("portal", "key_updated", f"{current_user().username} "
                        f"{'re-activated' if updated.is_active else 'disabled'} key {updated.key_prefix}",
                        level="WARNING", details={"key": updated.key_prefix, "active": updated.is_active})
        flash(f"Key {updated.display} {'re-activated' if updated.is_active else 'disabled'}", "success")
        return redirect(url_for("customers", q=request.form.get("q") or None))

    @app.route("/api/customers")
    def api_customers():
        try:
            rows = _customer_rows(request.args.get("q"), _limit(100))
            stats = state.customers.stats()
        except (sqlite3.Error, DatabaseError) as exc:
            return jsonify({"error": str(exc), "records": []}), 503
        return jsonify({"stats": stats, "count": len(rows), "records": [
            {**c.public(), "keys": [k.public() for k in keys]} for c, keys in rows]})

    # -- documentation (docs/*.md) -----------------------------------------------
    @app.route("/docs", defaults={"slug": ""})
    @app.route("/docs/<path:slug>")
    def docs(slug: str):
        if request.args.get("reload") == "1" and can(ROLE_ADMIN):
            state.docs.reload()
        query = (request.args.get("q") or "").strip()
        try:
            node, body, toc = state.docs.page(slug)
        except (DocError, OSError):
            abort(404)
        results = state.docs.search(query) if query else None
        return render_template("docs.html", node=node, body=body, toc=toc, tree=state.docs.tree, query=query,
                               results=results, ancestors=node.ancestors())

    @app.template_filter("filesize")
    def filesize(n: int) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024 or unit == "GB":
                return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
            n /= 1024
        return str(n)

    # ------------------------------------------------------------------
    @app.route("/")
    def index():
        try:
            state.db.ensure_stage_table()
            loads = state.db.summary()[-10:][::-1]
            total_rows = state.db.count_rows()
        except DatabaseError as exc:
            flash(f"database problem: {exc}", "error")
            loads, total_rows = [], 0
        return render_template(
            "index.html",
            conversion_files=state.conversion_files(),
            loading_files=state.loading_files(),
            error_files=state.list_files("error_dir")[-10:][::-1],
            archive_count=len(state.list_files("archive_dir")),
            skipped_count=len(state.list_files("skipped_dir")),
            converted_count=len(state.list_files("converted_dir")),
            formats_list=state.store.all(),
            loads=loads, total_rows=total_rows,
            supported=", ".join(SUPPORTED_EXTENSIONS),
            monitor=state.monitor_counts(),
            active=state.active_summaries(),
        )

    # -- conversion ---------------------------------------------------------
    @app.post("/convert")
    def convert():
        name = request.form.get("file", "")
        fmt_name = request.form.get("format", "") or None
        on_conflict = request.form.get("on_conflict", "rename")
        move_source = request.form.get("move_source", "on") == "on"
        path = state.safe_path("conversion_dir", name)
        try:
            res = state.converter.convert(path, fmt_name, on_conflict=on_conflict, move_source=move_source)
            err = path.with_name(path.name + ".error.txt")
            if err.exists():
                err.unlink()
            flash(f"Converted {res.source_file.name} -> {res.output_file.name} "
                  f"({res.rows_written} rows{', ' + str(res.rows_skipped) + ' skipped' if res.rows_skipped else ''}) "
                  f"into the loading folder", "success")
        except ConversionError as exc:
            flash(f"Conversion of {name} failed: {exc}", "error")
        return redirect(url_for("index"))

    @app.post("/convert-all")
    def convert_all():
        outcomes = state.converter.convert_folder(on_conflict=request.form.get("on_conflict", "rename"),
                                                  move_source=request.form.get("move_source", "on") == "on")
        ok = [o for o in outcomes if o[1] is not None]
        bad = [o for o in outcomes if o[1] is None]
        if not outcomes:
            flash("Nothing to convert - the conversion folder is empty", "info")
        if ok:
            flash(f"Converted {len(ok)} file(s) into the loading folder: " + ", ".join(o[1].output_file.name for o in ok),
                  "success")
        for src, _, err in bad:
            flash(f"{src.name}: {err}", "error")
        return redirect(url_for("index"))

    @app.route("/preview")
    def preview():
        name = request.args.get("file", "")
        fmt_name = request.args.get("format", "") or None
        folder = request.args.get("folder", "conversion_dir")
        path = state.safe_path(folder, name)
        try:
            result = state.converter.preview(path, fmt_name, limit=int(request.args.get("limit", 10)))
        except ConversionError as exc:
            result = {"source": name, "format": fmt_name or "(auto-detect)", "ok": False, "errors": [str(exc)],
                      "source_rows": [], "converted_rows": [], "mapping": {}, "source_header": None,
                      "converted_header": list(formats.DEFAULT_HEADER_FIELDS), "file_type": None,
                      "delimiter": None, "detail": "", "total_rows": None}
        if request.args.get("json"):
            return jsonify(result)
        return render_template("preview.html", p=result, formats_list=state.store.all(), file=name,
                               folder=folder, chosen=fmt_name or "")

    # -- loading ------------------------------------------------------------
    @app.post("/load")
    def load():
        name = request.form.get("file", "")
        market = request.form.get("market", "").strip() or None
        truncate = request.form.get("truncate") == "on"
        files = [state.safe_path("loading_dir", name)] if name else None
        try:
            results = state.loader.run(files=files, market=market, truncate=truncate,
                                       include_no_trading_days=request.form.get("include_no_trading_days") == "on")
        except DatabaseError as exc:
            flash(f"database problem: {exc}", "error")
            return redirect(url_for("index"))
        if not results:
            flash("Nothing to load - the loading folder has no default-format .txt files", "info")
        for r in results:
            flash(str(r), "success" if r.ok else "info" if r.skipped else "error")
        return redirect(url_for("index"))

    @app.post("/load-folder")
    def load_folder():
        folder = (request.form.get("folder") or "").strip()
        if not folder:
            flash("Please enter the folder to load", "error")
            return redirect(url_for("index"))
        patterns = [p.strip() for p in (request.form.get("patterns") or "").replace(",", ";").split(";") if p.strip()]
        try:
            summary = state.loader.load_folder(
                folder, patterns=patterns or None, recursive=request.form.get("recursive") == "on",
                market=(request.form.get("market") or "").strip() or None,
                move_files=request.form.get("move") == "on", truncate=request.form.get("truncate") == "on",
                stop_on_error=request.form.get("stop_on_error") == "on",
                include_no_trading_days=request.form.get("include_no_trading_days") == "on")
        except LoadError as exc:
            flash(f"Bulk load failed: {exc}", "error")
            return redirect(url_for("index"))
        except DatabaseError as exc:
            flash(f"database problem: {exc}", "error")
            return redirect(url_for("index"))
        flash(f"Bulk load {summary}", "success" if summary.ok else "error")
        for r in [r for r in summary.results if r.skipped][:10]:
            flash(str(r), "info")
        for r in summary.failures[:10]:
            flash(str(r), "error")
        if len(summary.failures) > 10:
            flash(f"... and {len(summary.failures) - 10} more failed file(s) - see the load history", "error")
        return redirect(url_for("index"))

    # -- upload / files -----------------------------------------------------
    @app.post("/upload")
    def upload():
        target = request.form.get("target", "conversion_dir")
        if target not in ("conversion_dir", "loading_dir"):
            abort(400)
        folder = state.settings.folders[target]
        saved = []
        for f in request.files.getlist("files"):
            name = Path(f.filename or "").name
            if not name or not _SAFE_NAME.match(name) or name.startswith("."):
                continue
            if target == "loading_dir" and not name.lower().endswith(".txt"):
                flash(f"{name}: only default-format .txt files belong in the loading folder - "
                      f"upload it to the conversion folder instead", "error")
                continue
            dest = folder / name
            f.save(dest)
            saved.append(name)
        if saved:
            flash(f"Uploaded {', '.join(saved)} to the {target.replace('_dir', '')} folder", "success")
        else:
            flash("No file uploaded", "info")
        return redirect(url_for("index"))

    @app.route("/files/<key>/<path:name>")
    def download(key: str, name: str):
        path = state.safe_path(key, name)
        return send_from_directory(path.parent, path.name, as_attachment=request.args.get("dl") == "1")

    @app.post("/files/<key>/<path:name>/delete")
    def delete_file(key: str, name: str):
        path = state.safe_path(key, name)
        try:
            path.unlink()
            note = path.with_name(path.name + ".error.txt")
            if note.exists():
                note.unlink()
        except OSError as exc:       # e.g. Windows: the file is open in another program
            flash(f"Could not delete {name}: {exc}", "error")
            return redirect(request.form.get("next") or url_for("index"))
        flash(f"Deleted {name} from {key.replace('_dir', '')} folder", "success")
        return redirect(request.form.get("next") or url_for("index"))

    @app.route("/browse/<key>")
    def browse(key: str):
        folder = state.folder(key)
        return render_template("browse.html", key=key, folder=folder, files=state.list_files(key)[::-1])

    # -- formats --------------------------------------------------------------
    @app.route("/formats")
    def formats_page():
        return render_template("formats.html", formats_list=state.store.all())

    def _render_format_form(fmt: FileFormat, old_name: str | None, preview_result=None, error: str | None = None):
        return render_template("format_edit.html", fmt=fmt, old_name=old_name, preview=preview_result, error=error,
                               conversion_files=[f.name for f in state.conversion_files()])

    @app.route("/formats/new", methods=["GET", "POST"])
    @app.route("/formats/<name>/edit", methods=["GET", "POST"])
    def format_edit(name: str | None = None):
        if request.method == "GET":
            if name is None:
                fmt = FileFormat(name="", has_header=True)
                if request.args.get("clone"):
                    try:
                        fmt = FileFormat.from_dict(state.store.get(request.args["clone"]).to_dict())
                        fmt.name = f"{fmt.name}_copy"
                        fmt.builtin = False
                    except FormatError:
                        pass
                return _render_format_form(fmt, None)
            try:
                return _render_format_form(state.store.get(name), name)
            except FormatError:
                abort(404)
        fmt = FileFormat.from_form(request.form)
        if name is not None:
            try:
                fmt.builtin = state.store.get(name).builtin
            except FormatError:
                pass
        action = request.form.get("action", "save")
        if action == "test":
            test_file = request.form.get("test_file", "")
            try:
                fmt.validate()
                path = state.safe_path("conversion_dir", test_file) if test_file else None
                if path is None:
                    return _render_format_form(fmt, name, error="choose a file from the conversion folder to test with")
                result = state.converter.preview(path, fmt)
            except FormatError as exc:
                return _render_format_form(fmt, name, error=str(exc))
            return _render_format_form(fmt, name, preview_result=result)
        try:
            if name is None and state.store.exists(fmt.name):
                raise FormatError(f"a format named '{fmt.name}' already exists")
            if name is not None and fmt.name != name and state.store.exists(fmt.name):
                raise FormatError(f"a format named '{fmt.name}' already exists")
            state.store.put(fmt, old_name=name)
        except FormatError as exc:
            return _render_format_form(fmt, name, error=str(exc))
        flash(f"Format '{fmt.name}' saved", "success")
        return redirect(url_for("formats_page"))

    @app.post("/formats/<name>/delete")
    def format_delete(name: str):
        try:
            state.store.delete(name)
            flash(f"Format '{name}' deleted", "success")
        except FormatError as exc:
            flash(str(exc), "error")
        return redirect(url_for("formats_page"))

    # -- settings -------------------------------------------------------------
    @app.route("/settings", methods=["GET", "POST"])
    def settings_page():
        s = state.settings
        if request.method == "GET":
            return render_template("settings.html", s=s, error=None, packs_text=format_token_packs(s.token_packs))
        form = request.form
        base = s.base_dir

        def path_of(key: str) -> Path:
            raw = (form.get(key) or "").strip()
            if not raw:
                raise ConfigError(f"{key.replace('_', ' ')} must not be empty")
            p = Path(os.path.expandvars(raw)).expanduser()
            return p if p.is_absolute() else (base / p).resolve()

        try:
            new = load_settings(s.config_file) if s.config_file else Settings()
            for key in ("conversion_dir", "converted_dir", "loading_dir", "archive_dir", "error_dir", "work_dir", "log_dir"):
                setattr(new, key, path_of(key))
            if "skipped_dir" in form:
                new.skipped_dir = path_of("skipped_dir")
            if new.conversion_dir == new.loading_dir:
                raise ConfigError("the conversion folder and the loading folder must be different")
            new.sqlite_path = path_of("sqlite_path")
            new.formats_file = path_of("formats_file")
            new.stage_table = (form.get("stage_table") or "Stock_Stage").strip()
            new.batch_size = int(form.get("batch_size") or 5000)
            new.truncate_before_load = form.get("truncate_before_load") == "on"
            new.file_patterns = tuple(p.strip() for p in (form.get("file_patterns") or "*.txt").replace(",", ";").split(";") if p.strip())
            new.market_from_filename_regex = (form.get("market_from_filename_regex") or "").strip()
            re.compile(new.market_from_filename_regex)
            new.default_market = (form.get("default_market") or "").strip()
            new.skip_bad_rows = form.get("skip_bad_rows") == "on"
            new.encoding = (form.get("encoding") or "utf-8-sig").strip()
            if "date_from_filename_regex" in form:
                new.date_from_filename_regex = (form.get("date_from_filename_regex") or "").strip()
                re.compile(new.date_from_filename_regex)
                new.skip_no_trading_days = form.get("skip_no_trading_days") == "on"
            if "daily_table" in form:     # active tables / promotion (absent in older forms: keep as is)
                new.daily_table = (form.get("daily_table") or "Stock_Daily").strip()
                new.weekly_table = (form.get("weekly_table") or "Stock_Weekly").strip()
                new.monthly_table = (form.get("monthly_table") or "Stock_Monthly").strip()
                new.reject_market_closed_days = form.get("reject_market_closed_days") == "on"
                new.check_price_range = form.get("check_price_range") == "on"
                new.check_trading_window = form.get("check_trading_window") == "on"
                new.check_completeness = form.get("check_completeness") == "on"
            if "portal_port" in form:     # portal / data API
                new.portal_host = (form.get("portal_host") or "127.0.0.1").strip() or "127.0.0.1"
                new.portal_port = int(form.get("portal_port") or 8060)
                new.portal_public_url = (form.get("portal_public_url") or "").strip().rstrip("/")
                new.payment_methods = parse_list(",".join(form.getlist("payment_methods")), PAYMENT_METHODS,
                                                 "payment methods")
                if not new.payment_methods:
                    raise ConfigError("choose at least one payment method")
                new.payment_gateway = (form.get("payment_gateway") or "fake").strip().lower()
                if new.payment_gateway not in PAYMENT_GATEWAYS:
                    raise ConfigError("the payment gateway must be fake or live")
                new.paypal_client_id = (form.get("paypal_client_id") or "").strip()
                new.paypal_environment = (form.get("paypal_environment") or "sandbox").strip().lower()
                if new.paypal_environment not in ("sandbox", "live"):
                    raise ConfigError("the PayPal environment must be sandbox or live")
                new.oauth_providers = parse_list(",".join(form.getlist("oauth_providers")), OAUTH_PROVIDERS,
                                                 "sign-in providers")
                new.oauth_gateway = (form.get("oauth_gateway") or "fake").strip().lower()
                if new.oauth_gateway not in OAUTH_GATEWAYS:
                    raise ConfigError("the identity gateway must be fake or live")
                new.google_client_id = (form.get("google_client_id") or "").strip()
                new.facebook_app_id = (form.get("facebook_app_id") or "").strip()
                new.apple_client_id = (form.get("apple_client_id") or "").strip()
                new.currency = (form.get("currency") or "USD").strip().upper()[:3] or "USD"
                new.token_packs = parse_token_packs(form.get("token_packs") or "")
                new.tokens_per_query = int(form.get("tokens_per_query") or 1)
                new.max_tickers_per_request = int(form.get("max_tickers_per_request") or 50)
                if new.tokens_per_query <= 0 or new.max_tickers_per_request <= 0:
                    raise ConfigError("tokens per ticker-year and max tickers per request must be > 0")
                if not 1 <= new.portal_port <= 65535:
                    raise ConfigError("the portal port must be between 1 and 65535")
            if "mail_provider" in form:     # accounts / mail / sms
                new.issuer = (form.get("issuer") or "").strip() or new.issuer
                new.require_email_verification = form.get("require_email_verification") == "on"
                new.admin_requires_2fa = form.get("admin_requires_2fa") == "on"
                new.code_ttl_minutes = int(form.get("code_ttl_minutes") or 10)
                new.reset_ttl_minutes = int(form.get("reset_ttl_minutes") or 60)
                if new.code_ttl_minutes <= 0 or new.reset_ttl_minutes <= 0:
                    raise ConfigError("code and reset-link validity must be > 0 minutes")
                new.mail_provider = (form.get("mail_provider") or "console").strip().lower()
                new.sms_provider = (form.get("sms_provider") or "console").strip().lower()
                if new.mail_provider not in MAIL_PROVIDERS or new.sms_provider not in SMS_PROVIDERS:
                    raise ConfigError("unknown mail / sms provider")
                new.mail_from = (form.get("mail_from") or "").strip() or new.mail_from
                new.smtp_host = (form.get("smtp_host") or "").strip()
                new.smtp_port = int(form.get("smtp_port") or 587)
                new.smtp_user = (form.get("smtp_user") or "").strip()
                new.smtp_starttls = form.get("smtp_starttls") == "on"
                new.twilio_account_sid = (form.get("twilio_account_sid") or "").strip()
                new.twilio_from = (form.get("twilio_from") or "").strip()
                if new.mail_provider == "smtp" and not new.smtp_host:
                    raise ConfigError("the smtp provider needs an SMTP server")
            if new.batch_size <= 0:
                raise ConfigError("batch size must be > 0")
            if not new.stage_table:
                raise ConfigError("staging table name must not be empty")
            names = [new.stage_table, new.daily_table, new.weekly_table, new.monthly_table]
            if len({n.lower() for n in names}) != len(names):
                raise ConfigError("the staging, daily, weekly and monthly table names must differ")
            new.config_file = s.config_file or (base / "config.ini")
            new.base_dir = base
            new.ensure_folders()
            save_settings(new, new.config_file)
            state.reload(load_settings(new.config_file))
        except (ConfigError, ValueError, re.error, OSError) as exc:
            return render_template("settings.html", s=s, error=str(exc),
                                   packs_text=form.get("token_packs") or format_token_packs(s.token_packs)), 400
        flash(f"Settings saved to {state.settings.config_file}", "success")
        return redirect(url_for("settings_page"))

    # -- monitoring (App_Log / App_Error / Data_Error / Load_History) ---------
    def _limit(default: int = 100) -> int:
        try:
            return max(1, min(int(request.args.get("limit", default)), 5000))
        except ValueError:
            return default

    def _monitor_query(kind: str):
        """Fetch monitoring records for *kind* according to the request filters."""
        limit = _limit()
        if kind == "logs":
            return state.audit.fetch_logs(limit=limit, component=request.args.get("component") or None,
                                          level=request.args.get("level") or None)
        if kind == "errors":
            return state.audit.fetch_errors(limit=limit, component=request.args.get("component") or None)
        if kind == "data_errors":
            return state.data_errors.fetch(limit=limit, source_file=request.args.get("file") or None,
                                           alerts_only=request.args.get("alerts") == "1",
                                           open_only=request.args.get("open") == "1")
        if kind == "corrections":
            return state.corrections.history(limit=limit)
        if kind == "history":
            return state.db.load_history(limit=limit, file_name=request.args.get("file") or None)
        if kind == "promotions":
            return state.promotions.history(limit=limit, procedure=request.args.get("procedure") or None)
        abort(404)

    def _monitor_page(kind: str, template: str, **extra):
        try:
            records = _monitor_query(kind)
            counts = state.monitor_counts()
        except (sqlite3.Error, DatabaseError) as exc:
            flash(f"database problem: {exc}", "error")
            records, counts = [], state.monitor_counts()
        return render_template(template, records=records, counts=counts, kind=kind,
                               args=request.args, limit=_limit(), **extra)

    @app.route("/monitor")
    def monitor():
        try:
            recent_errors = state.audit.fetch_errors(limit=5)
            recent_alerts = state.data_errors.fetch(limit=5, alerts_only=True, open_only=True)
            recent_loads = state.db.load_history(limit=8)
            recent_logs = state.audit.fetch_logs(limit=8)
        except (sqlite3.Error, DatabaseError) as exc:
            flash(f"database problem: {exc}", "error")
            recent_errors, recent_alerts, recent_loads, recent_logs = [], [], [], []
        return render_template("monitor.html", counts=state.monitor_counts(), recent_errors=recent_errors,
                               recent_alerts=recent_alerts, recent_loads=recent_loads, recent_logs=recent_logs)

    @app.route("/monitor/logs")
    def monitor_logs():
        return _monitor_page("logs", "monitor_logs.html", levels=LOG_LEVELS)

    @app.route("/monitor/errors")
    def monitor_errors():
        return _monitor_page("errors", "monitor_errors.html")

    @app.route("/monitor/data-errors")
    def monitor_data_errors():
        return _monitor_page("data_errors", "monitor_data_errors.html")

    @app.route("/monitor/history")
    def monitor_history():
        return _monitor_page("history", "monitor_history.html")

    def _api_monitor(kind: str):
        try:
            records = _monitor_query(kind)
        except (sqlite3.Error, DatabaseError) as exc:
            return jsonify({"error": str(exc), "records": []}), 503
        return jsonify({"kind": kind, "count": len(records), "records": [asdict(r) for r in records]})

    @app.route("/api/logs")
    def api_logs():
        return _api_monitor("logs")

    @app.route("/api/errors")
    def api_errors():
        return _api_monitor("errors")

    @app.route("/api/data-errors")
    def api_data_errors():
        return _api_monitor("data_errors")

    @app.route("/api/history")
    def api_history():
        return _api_monitor("history")

    @app.route("/api/corrections")
    def api_corrections():
        return _api_monitor("corrections")

    @app.route("/api/promotions")
    def api_promotions():
        return _api_monitor("promotions")

    # -- active tables / promotion ----------------------------------------------
    @app.route("/promote")
    def promote():
        try:
            summaries = state.promotions.summaries()
            records = state.promotions.history(limit=_limit(20), procedure=request.args.get("procedure") or None)
        except (sqlite3.Error, DatabaseError) as exc:
            flash(f"database problem: {exc}", "error")
            summaries, records = [], []
        return render_template("promote.html", summaries=summaries, records=records, procedures=PROCEDURES,
                               counts=state.monitor_counts(), args=request.args, limit=_limit(20))

    @app.post("/promote")
    def promote_run():
        form = request.form
        what = form.get("what", "all")
        full = form.get("full") == "on"
        filters = {k: (form.get(k) or "").strip() or None for k in ("source_file", "load_id", "ticker",
                                                                    "date_from", "date_to")}
        names = ("daily", "weekly", "monthly") if what == "all" else (what,)
        if what not in ("daily", "weekly", "monthly", "all", "eod"):
            abort(400)
        try:
            for name in names:
                if name == "eod":
                    r = state.promotions.promote_eod_daily(
                        full=full, symbol=(form.get("symbol") or "").strip() or None,
                        market=(form.get("market") or "").strip() or None, ticker=filters["ticker"],
                        load_id=filters["load_id"], date_from=filters["date_from"], date_to=filters["date_to"])
                    flash(str(r), "success" if not r.rows_rejected else "info")
                    if r.rows_rejected:
                        flash(f"{r.rows_rejected:,} EOD row(s) were rejected - see the data errors page"
                              + (" (market_unknown: enter the market, register the symbol on the Symbols page or "
                                 "set [eod_api] default_market)" if "market_unknown" in r.reject_reasons else ""),
                              "error")
                    continue
                if name == "daily":
                    r = state.promotions.promote_daily(full=full, **filters)
                elif name == "weekly":
                    r = state.promotions.promote_weekly(full=full, ticker=filters["ticker"])
                else:
                    r = state.promotions.promote_monthly(full=full, ticker=filters["ticker"])
                flash(str(r), "success" if not r.rows_rejected else "info")
                if r.rows_rejected:
                    flash(f"{r.rows_rejected:,} staging row(s) were rejected by the validation - see the data "
                          f"errors page, correct them and promote again", "error")
        except PromotionError as exc:
            flash(str(exc), "error")
        return redirect(url_for("promote"))

    @app.route("/active/<kind>")
    def active_table(kind: str):
        if kind not in ("daily", "weekly", "monthly"):
            abort(404)
        a = request.args
        rows, error = [], None
        try:
            rows = state.promotions.fetch(kind, ticker=a.get("ticker"), date_from=a.get("from"),
                                          date_to=a.get("to"), limit=_limit(100),
                                          newest_first=a.get("order") != "asc",
                                          incomplete_only=a.get("incomplete") == "1")
            summary = state.promotions.summary(kind)
        except PromotionError as exc:
            error, summary = str(exc), None
        except (sqlite3.Error, DatabaseError) as exc:
            error, summary = f"database problem: {exc}", None
        if a.get("json"):
            return jsonify({"kind": kind, "count": len(rows), "records": rows, "error": error})
        return render_template("active.html", kind=kind, rows=rows, summary=summary, error=error, args=a,
                               limit=_limit(100), columns=list(rows[0].keys()) if rows else [])

    # -- trading calendar (No_Trading_Day / Market) -----------------------------------
    def _calendar_args():
        markets = state.calendar.markets()
        market = (request.args.get("market") or request.form.get("market") or "").strip().upper()
        if not market and markets:
            known = {m.market.upper() for m in markets}
            preferred = [state.settings.default_market.upper(), "NASDAQ"]
            market = next((m for m in preferred if m in known), None) or \
                next((m.market for m in markets if m.is_active), markets[0].market)
        try:
            year = int(request.args.get("year") or request.form.get("year") or datetime.now().year)
        except ValueError:
            year = datetime.now().year
        return markets, market, year

    @app.route("/calendar")
    def calendar_page():
        try:
            markets, market, year = _calendar_args()
            show_all = request.args.get("all") == "1"
            days = state.calendar.days(market, year, holidays_only=not show_all) if market else []
            years = state.calendar.years_available(market) if market else []
        except (sqlite3.Error, DatabaseError, CalendarError) as exc:
            flash(f"database problem: {exc}", "error")
            markets, market, year, days, years, show_all = [], "", datetime.now().year, [], [], False
        market_years = {m.market: state.calendar.years_available(m.market) for m in markets}
        return render_template("calendar.html", markets=markets, market=market, year=year, days=days, years=years,
                               show_all=show_all, rule_sets=RULE_SETS, market_years=market_years,
                               suggested_years=default_update_years(), args=request.args)

    def _back_to_calendar(market: str | None = None, year: int | None = None):
        params = {}
        if market:
            params["market"] = market
        if year:
            params["year"] = year
        if request.form.get("all") == "1":
            params["all"] = 1
        return redirect(url_for("calendar_page", **params))

    @app.post("/calendar/update")
    def calendar_update():
        try:
            year = int(request.form.get("year") or 0)
            markets = [m for m in request.form.getlist("markets") if m] or None
            results = state.calendar.update_year(year, markets)
        except (ValueError, CalendarError) as exc:
            flash(f"Could not update the calendar: {exc}", "error")
            return _back_to_calendar()
        for r in results:
            flash(str(r), "success")
        if not results:
            flash("No active market to update", "info")
        return _back_to_calendar(markets[0] if markets else None, year)

    @app.post("/calendar/days/add")
    def calendar_day_add():
        market = (request.form.get("market") or "").strip().upper()
        try:
            row = state.calendar.add_day(market, request.form.get("date") or "", request.form.get("reason") or "")
        except CalendarError as exc:
            flash(f"Could not add the closure: {exc}", "error")
            return _back_to_calendar(market)
        flash(f"{row.market} is closed on {row.trade_date}: {row.reason}", "success")
        return _back_to_calendar(row.market, int(row.trade_date[:4]))

    @app.post("/calendar/days/remove")
    def calendar_day_remove():
        market = (request.form.get("market") or "").strip().upper()
        try:
            row = state.calendar.remove_day(market, request.form.get("date") or "")
        except CalendarError as exc:
            flash(f"Could not remove the closure: {exc}", "error")
            return _back_to_calendar(market)
        flash(f"{row.market} trades on {row.trade_date} ({row.reason} removed)", "success")
        return _back_to_calendar(row.market, int(row.trade_date[:4]))

    @app.post("/calendar/markets/add")
    def calendar_market_add():
        try:
            m = state.calendar.add_market(request.form.get("market") or "", request.form.get("name") or "",
                                          request.form.get("rules") or "us")
        except CalendarError as exc:
            flash(f"Could not add the market: {exc}", "error")
            return _back_to_calendar()
        flash(f"Market {m.market} added ({RULE_SETS[m.rules]}) - generate its no-trading days below", "success")
        return _back_to_calendar(m.market)

    @app.post("/calendar/markets/<market>/toggle")
    def calendar_market_toggle(market: str):
        try:
            current = state.calendar.get_market(market)
            if current is None:
                abort(404)
            m = state.calendar.set_market_active(market, not current.is_active)
        except CalendarError as exc:
            flash(str(exc), "error")
            return _back_to_calendar()
        flash(f"Market {m.market} {'activated' if m.is_active else 'deactivated'}", "success")
        return _back_to_calendar()

    # -- stock symbols (Stock_Symbol) ---------------------------------------------
    @app.route("/symbols")
    def symbols_page():
        a = request.args
        try:
            rows = state.symbols.find(a.get("ticker"), a.get("market"), missing_dates=a.get("missing") == "1",
                                      suspicious=a.get("suspicious") == "1", limit=_limit(100))
            counts = state.symbols.counts(a.get("market") or None)
            markets = state.symbols.markets()
        except (sqlite3.Error, DatabaseError) as exc:
            flash(f"database problem: {exc}", "error")
            rows, counts, markets = [], {}, []
        edit_symbol = None
        if a.get("edit"):
            try:
                edit_symbol = state.symbols.get(a["edit"], a.get("edit_market"))
            except SymbolError as exc:
                flash(str(exc), "error")
        return render_template("symbols.html", rows=rows, counts=counts, markets=markets, args=a, limit=_limit(100),
                               edit_symbol=edit_symbol)

    @app.post("/symbols/save")
    def symbol_save():
        form = request.form
        try:
            s = state.symbols.set(form.get("ticker") or "", (form.get("market") or "").strip() or None,
                                  first_trade_date=form.get("first_trade_date") or None,
                                  last_trade_date=form.get("last_trade_date") or None,
                                  name=form.get("name") or "", note=form.get("note") or "")
        except SymbolError as exc:
            flash(f"Could not save the symbol: {exc}", "error")
            return redirect(url_for("symbols_page", ticker=form.get("ticker", "")))
        flash(f"{s.ticker} ({s.market or 'no market'}): first trade {s.first_trade_date or '-'}, "
              f"last trade {s.last_trade_date or '-'}", "success")
        if s.data_before_listing:
            flash(f"{s.ticker}: data loaded from {s.first_seen} is before the first trade date {s.first_trade_date} - "
                  f"it is invalid and will be flagged / rejected", "info")
        return redirect(url_for("symbols_page", ticker=s.ticker))

    @app.post("/symbols/delete")
    def symbol_delete():
        try:
            s = state.symbols.delete(request.form.get("ticker") or "", (request.form.get("market") or "").strip() or None)
        except SymbolError as exc:
            flash(str(exc), "error")
            return redirect(url_for("symbols_page"))
        flash(f"{s.ticker} ({s.market}) removed from the symbol table", "success")
        return redirect(url_for("symbols_page"))

    @app.post("/symbols/import")
    def symbols_import():
        f = request.files.get("file")
        if f is None or not f.filename:
            flash("Choose a CSV file to import", "error")
            return redirect(url_for("symbols_page"))
        name = Path(f.filename).name
        if not _SAFE_NAME.match(name) or name.startswith("."):
            abort(400)
        target = state.settings.work_dir / f"symbols_{datetime.now():%Y%m%d_%H%M%S}_{name}"
        target.parent.mkdir(parents=True, exist_ok=True)
        f.save(target)
        try:
            result = state.symbols.import_csv(target, (request.form.get("market") or "").strip() or None,
                                              display_name=name)
        except SymbolError as exc:
            flash(f"Import failed: {exc}", "error")
            return redirect(url_for("symbols_page"))
        finally:
            try:
                target.unlink()
            except OSError:  # pragma: no cover
                pass
        flash(f"Imported {result}", "success" if not result.errors else "error")
        for e in result.errors[:10]:
            flash(e, "error")
        return redirect(url_for("symbols_page"))

    @app.route("/api/symbols")
    def api_symbols():
        a = request.args
        try:
            rows = state.symbols.find(a.get("ticker"), a.get("market"), missing_dates=a.get("missing") == "1",
                                      suspicious=a.get("suspicious") == "1", limit=_limit(100))
        except (sqlite3.Error, DatabaseError) as exc:
            return jsonify({"error": str(exc), "records": []}), 503
        return jsonify({"count": len(rows), "records": [asdict(r) for r in rows],
                        "counts": state.symbols.counts(a.get("market") or None)})

    @app.route("/api/no-trading-days")
    def api_no_trading_days():
        try:
            markets, market, year = _calendar_args()
            days = state.calendar.days(market, year, holidays_only=request.args.get("all") != "1")
        except (sqlite3.Error, DatabaseError, CalendarError) as exc:
            return jsonify({"error": str(exc), "records": []}), 503
        return jsonify({"market": market, "year": year, "count": len(days),
                        "markets": [asdict(m) for m in markets],
                        "records": [{**asdict(d), "weekday": d.weekday} for d in days]})

    @app.route("/api/active/<kind>")
    def api_active(kind: str):
        if kind not in ("daily", "weekly", "monthly"):
            abort(404)
        a = request.args
        try:
            rows = state.promotions.fetch(kind, ticker=a.get("ticker"), date_from=a.get("from"),
                                          date_to=a.get("to"), limit=_limit(100),
                                          newest_first=a.get("order") != "asc",
                                          incomplete_only=a.get("incomplete") == "1")
        except PromotionError as exc:
            return jsonify({"error": str(exc), "records": []}), 400
        except (sqlite3.Error, DatabaseError) as exc:
            return jsonify({"error": str(exc), "records": []}), 503
        return jsonify({"kind": kind, "table": state.promotions.tables[kind], "count": len(rows), "records": rows})

    # -- data correction --------------------------------------------------------
    def _form_values(source) -> dict[str, str]:
        return {c: (source.get(c) or "").strip() for c in STAGE_EDITABLE_COLUMNS}

    def _render_correct(mode: str, row=None, rows=None, data_error=None, values=None, issues=None,
                        error: str | None = None, status: int = 200):
        values = values if values is not None else (_form_values(row) if row else {})
        history, open_errors = [], []
        if row:
            try:
                history = state.corrections.history(limit=20, stage_id=row["StageId"])
                open_errors = [d for d in state.data_errors.fetch(limit=200, source_file=row["SourceFile"],
                                                                 open_only=True)
                               if d.ticker == row["Ticker"] and d.trade_date == row["TradeDate"]]
            except (sqlite3.Error, DatabaseError) as exc:
                flash(f"database problem: {exc}", "error")
        recent = []
        if mode == "search":
            try:
                recent = state.corrections.history(limit=15)
            except (sqlite3.Error, DatabaseError):
                recent = []
        bad = {FILE_TO_STAGE.get(i.column, i.column) for i in (issues or [])}
        return render_template("correct.html", mode=mode, row=row, rows=rows or [], data_error=data_error,
                               values=values, issues=issues or [], bad=bad, error=error, history=history,
                               open_errors=open_errors, recent=recent, columns=STAGE_EDITABLE_COLUMNS,
                               args=request.args), status

    @app.route("/correct")
    def correct():
        args = request.args
        data_error = None
        if args.get("data_error_id"):
            try:
                data_error = state.data_errors.get(int(args["data_error_id"]))
            except (ValueError, sqlite3.Error, DatabaseError):
                data_error = None
            if data_error is None:
                flash(f"data error #{args['data_error_id']} not found", "error")
                return _render_correct("search")
        try:
            if args.get("stage_id"):
                row = state.db.get_stage_row(int(args["stage_id"]))
                if row is None:
                    flash(f"staging row {args['stage_id']} not found", "error")
                    return _render_correct("search", data_error=data_error)
                return _render_correct("edit", row=row, data_error=data_error)
            if data_error is not None:
                rows = state.db.find_stage_rows(source_file=data_error.source_file, ticker=data_error.ticker,
                                                trade_date=data_error.trade_date)
                if len(rows) == 1:
                    return _render_correct("edit", row=rows[0], data_error=data_error)
                if rows:
                    return _render_correct("choose", rows=rows, data_error=data_error)
                values = values_from_data_error(data_error)
                values.setdefault("SourceFile", data_error.source_file or "")
                return _render_correct("insert", data_error=data_error, values=values)
            criteria = {k: (args.get(k) or "").strip() for k in ("source_file", "ticker", "trade_date", "market")}
            if any(criteria.values()):
                rows = state.db.find_stage_rows(**criteria)
                if len(rows) == 1:
                    return _render_correct("edit", row=rows[0])
                if rows:
                    return _render_correct("choose", rows=rows)
                flash("no staging row matches - you can add the row below", "info")
                values = {"Market": criteria["market"], "Ticker": criteria["ticker"],
                          "TradeDate": criteria["trade_date"], "SourceFile": criteria["source_file"]}
                return _render_correct("insert", values=values)
        except (ValueError, sqlite3.Error, DatabaseError) as exc:
            flash(f"could not look up the row: {exc}", "error")
        return _render_correct("search")

    @app.post("/correct")
    def correct_save():
        form = request.form
        action = form.get("action", "update")
        values = _form_values(form)
        note = (form.get("note") or "").strip()
        data_error_id = int(form["data_error_id"]) if (form.get("data_error_id") or "").isdigit() else None
        data_error = state.data_errors.get(data_error_id) if data_error_id else None
        issues = state.corrections.validate(values)
        if action == "insert":
            values["SourceFile"] = (form.get("SourceFile") or "").strip()
            if issues:
                return _render_correct("insert", data_error=data_error, values=values, issues=issues,
                                       error="please fix the highlighted values", status=400)
            try:
                result = state.corrections.add_row(values, source_file=values["SourceFile"], note=note,
                                                   data_error_id=data_error_id)
            except CorrectionError as exc:
                return _render_correct("insert", data_error=data_error, values=values, error=str(exc), status=400)
        else:
            stage_id = int(form["stage_id"]) if (form.get("stage_id") or "").isdigit() else None
            row = state.db.get_stage_row(stage_id) if stage_id is not None else None
            if row is None:
                flash("staging row not found", "error")
                return redirect(url_for("correct"))
            if issues:
                return _render_correct("edit", row=row, data_error=data_error, values=values, issues=issues,
                                       error="please fix the highlighted values", status=400)
            try:
                result = state.corrections.correct(stage_id, values, note=note, data_error_id=data_error_id)
            except CorrectionError as exc:
                return _render_correct("edit", row=row, data_error=data_error, values=values, error=str(exc),
                                       status=400)
        flash(f"Correction saved: {result}", "success" if result.action != "unchanged" else "info")
        return redirect(url_for("correct", stage_id=result.stage_id))

    @app.route("/api/monitor")
    def api_monitor_counts():
        return jsonify(state.monitor_counts())

    # -- json api -------------------------------------------------------------
    @app.route("/api/status")
    def api_status():
        try:
            state.db.ensure_stage_table()
            loads = state.db.summary()
        except DatabaseError:
            loads = []
        return jsonify({
            "folders": {k: str(v) for k, v in state.settings.folders.items()},
            "conversion_files": [f.name for f in state.conversion_files()],
            "loading_files": [{"name": f.name, "default_format": f.ok} for f in state.loading_files()],
            "formats": state.store.names(),
            "loads": [{"source_file": a, "load_id": b, "rows": c, "loaded_at": d} for a, b, c, d in loads],
        })

    @app.route("/api/formats/<name>")
    def api_format(name: str):
        try:
            return jsonify(state.store.get(name).to_dict())
        except FormatError:
            abort(404)

    # -- API loading: the EOD API straight into the staging table (background loads) ---------------
    _DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

    def _api_token_state() -> tuple[str, bool]:
        """(where the token comes from, usable) - the token itself is never shown."""
        import os as _os
        if _os.environ.get(ENV_EOD_API_TOKEN):
            return f"${ENV_EOD_API_TOKEN}", True
        token = (state.settings.eod_api_token or "").strip()
        if not token:
            return "not set", False
        if token.lower() == "dummy":
            return "the placeholder 'dummy' in config.ini", False
        return "[eod_api] api_token in config.ini", True

    def _token_refused() -> str | None:
        """Why a live load cannot work (the placeholder / no token) - None when the token is usable."""
        source, ok = _api_token_state()
        if ok:
            return None
        return (f"the EOD API token is {source} - the live API refuses it: set ${ENV_EOD_API_TOKEN} (or "
                f"[eod_api] api_token) to your own token, or use the built-in stub to try it out")

    def _fetcher():
        from ..eod_market import MarketFetcher
        return MarketFetcher(state.settings, db=state.db, fake=False)

    def _market_lists() -> list:
        from ..eod_symbols import SymbolListLoader
        try:
            return SymbolListLoader(state.settings, state.db).summary()
        except (sqlite3.Error, DatabaseError) as exc:
            log.warning("symbol lists unavailable: %s", exc)
            return []

    def _market_quotas(markets) -> dict:
        """Today's whole-market loads per market ({market: MarketLoadQuota}) - the daily limit."""
        try:
            return _fetcher().market_load_quotas(markets)
        except (sqlite3.Error, DatabaseError) as exc:
            log.warning("market load quotas unavailable: %s", exc)
            return {}

    def _limit_state() -> dict:
        """Today's text-file loads and the stocks at their daily limit (for the page and its script)."""
        from ..load_limits import MAX_FILE_LOADS_PER_DAY, MAX_FILE_SYMBOLS, MAX_LOADS_PER_STOCK_PER_DAY, FileLoadQuota
        try:
            fetcher = _fetcher()
            file_quota = fetcher.file_load_quota()
            at_limit = fetcher.ledger.exhausted()
        except (sqlite3.Error, DatabaseError) as exc:
            log.warning("load limits unavailable: %s", exc)
            file_quota, at_limit = FileLoadQuota(datetime.now().date().isoformat()), {}
        return {"file_quota": file_quota, "stocks_at_limit": at_limit, "stock_limit": MAX_LOADS_PER_STOCK_PER_DAY,
                "max_file_symbols": MAX_FILE_SYMBOLS, "file_load_limit": MAX_FILE_LOADS_PER_DAY}

    @app.route("/api-load")
    def api_load():
        from ..eod_market import MAX_MARKET_LOADS_PER_DAY
        try:
            runs = _fetcher().runs(limit=_limit(15))
        except (sqlite3.Error, DatabaseError) as exc:
            flash(f"database problem: {exc}", "error")
            runs = []
        import json as _json
        for r in runs:                                 # what kind of load: market / symbols / file
            try:
                p = _json.loads(r.get("Parameters") or "{}")
            except ValueError:
                p = {}
            r["Source"] = p.get("source") or ("symbols" if p.get("symbols_given") else "market")
        token_source, token_ok = _api_token_state()
        lists = _market_lists()
        quotas = _market_quotas([m.market for m in lists])
        from ..eod_api import redact
        return render_template("api_load.html", status=api_loads.status(log_lines=150), runs=runs, lists=lists,
                               lists_json=[{"market": m.market, "codes": m.codes, "types": m.types} for m in lists],
                               quotas=quotas, quotas_json={m: q.as_dict() for m, q in quotas.items()},
                               market_load_limit=MAX_MARKET_LOADS_PER_DAY, limits=_limit_state(),
                               schedules=_schedule_rows(), scheduler=bulk_scheduler,
                               schedule_form=_schedule_form(), trade_days=_trade_days(),
                               bulk_quota=_bulk_quota_now(),
                               token_source=token_source, token_ok=token_ok,
                               endpoint=redact(state.settings.eod_api_base_url),
                               form=session.pop("api_load_form", None) or {})

    @app.route("/api/api-load/quota")
    def api_load_quota():
        """Today's whole-market loads of a market: {"market", "day", "limit", "used", "remaining", ...}."""
        from ..eod_api import EodApiError
        try:
            quota = _fetcher().market_load_quota(request.args.get("market") or "")
        except EodApiError as exc:
            return jsonify({"error": str(exc)}), 400
        except (sqlite3.Error, DatabaseError) as exc:
            return jsonify({"error": f"database problem: {exc}"}), 503
        return jsonify(quota.as_dict())

    # -- daily schedules of the one-day bulk load ---------------------------------------------
    def _trade_days():
        from ..bulk_schedule import TRADE_DAYS
        return TRADE_DAYS

    def _schedule_rows() -> list[dict]:
        from ..bulk_schedule import ScheduleStore, next_run
        try:
            schedules = ScheduleStore(state.settings).list()
        except (sqlite3.Error, DatabaseError) as exc:
            log.warning("schedules unavailable: %s", exc)
            return []
        now = datetime.now()
        return [{"s": s, "next": next_run(s, now, state.settings.extra_market_holidays)} for s in schedules]

    def _schedule_form() -> dict:
        """The schedule shown in the form: ?schedule=ID to edit one, else the values typed before / defaults."""
        from ..bulk_schedule import ScheduleStore
        kept = session.pop("schedule_form", None)
        if kept:
            return kept
        wanted = request.args.get("schedule")
        if wanted and wanted.isdigit():
            s = ScheduleStore(state.settings).get(int(wanted))
            if s is not None:
                return {"market": s.market, "run_time": s.run_time, "trade_day": s.trade_day, "fmt": s.fmt,
                        "symbols": s.symbols or "", "promote": "on" if s.promote else "",
                        "aggregates": "on" if s.aggregates else "", "strict": "on" if s.strict else "",
                        "fake": "on" if s.fake else "", "allow_fake_data": "on" if s.allow_fake_data else "",
                        "enabled": "on" if s.enabled else "", "editing": s.schedule_id}
        return {"market": "US", "run_time": "18:30", "trade_day": "same", "fmt": state.settings.eod_bulk_fmt,
                "promote": "on", "enabled": "on"}

    def _bulk_quota_now():
        """Calls of the bulk endpoint for US and its latest trading day (the bulk tab's first view)."""
        from ..eod_bulk import BulkDayLoader
        try:
            return BulkDayLoader(state.settings, db=state.db, fake=False).bulk_call_quota("US")
        except (sqlite3.Error, DatabaseError) as exc:
            log.warning("bulk call quota unavailable: %s", exc)
            return None

    @app.route("/api/api-load/bulk-quota")
    def api_load_bulk_quota():
        """Calls of the bulk endpoint for ?market=US and ?date=YYYY-MM-DD (default: the latest trading day)."""
        from ..eod_api import EodApiError
        from ..eod_bulk import BulkDayLoader, check_trade_day
        try:
            market = request.args.get("market") or "US"
            day = check_trade_day(market.upper(), request.args.get("date") or None, force=True)
            quota = BulkDayLoader(state.settings, db=state.db, fake=False).bulk_call_quota(market, day)
        except EodApiError as exc:
            return jsonify({"error": str(exc)}), 400
        except (sqlite3.Error, DatabaseError) as exc:
            return jsonify({"error": f"database problem: {exc}"}), 503
        return jsonify(quota.as_dict())

    @app.route("/api/api-load/schedules")
    def api_load_schedules():
        rows = _schedule_rows()
        return jsonify({"scheduler_running": bulk_scheduler.running, "server_time": datetime.now().isoformat(sep=" ",
                                                                                                     timespec="seconds"),
                        "count": len(rows), "records": [{**r["s"].public(), "next_run": r["next"].isoformat(sep=" ")
                                                         if r["next"] else None} for r in rows]})

    def _schedule_back(message: str | None = None, category: str = "error", form=None):
        if message:
            flash(message, category)
        if form is not None:
            session["schedule_form"] = {k: v for k, v in form.items()}
        return redirect(url_for("api_load") + "#schedules")

    @app.post("/api-load/schedules")
    def api_load_schedule_save():
        from ..bulk_schedule import ScheduleError, ScheduleStore, next_run, token_problem
        form = request.form
        user = current_user().username
        try:
            schedule = ScheduleStore(state.settings).save(
                form.get("market") or "", form.get("run_time") or "", form.get("trade_day") or "same",
                form.get("fmt") or "json", form.get("symbols") or None, promote=form.get("promote") == "on",
                aggregates=form.get("aggregates") == "on", strict=form.get("strict") == "on",
                fake=form.get("fake") == "on", allow_fake_data=form.get("allow_fake_data") == "on",
                enabled=form.get("enabled") == "on", user=user)
        except ScheduleError as exc:
            return _schedule_back(f"Schedule not saved - {exc}", form=form)
        state.audit.log("api_load_schedule", "schedule_saved", f"{user} saved the daily schedule of {schedule.market}: "
                        f"{schedule.run_time}, {schedule.trade_day_label}" + ("" if schedule.enabled else " (disabled)"),
                        details=schedule.public())
        upcoming = next_run(schedule, datetime.now(), state.settings.extra_market_holidays)
        flash(f"Schedule saved: {schedule.market} every trading day at {schedule.run_time} ({schedule.trade_day_label})"
              + (f" - next run {upcoming:%Y-%m-%d %H:%M}" if upcoming else " - disabled"), "success")
        if not schedule.fake and state.settings.eod_api_gateway != "fake" and token_problem(state.settings):
            flash(f"The schedule will be refused until the token is set: {token_problem(state.settings)}", "warning")
        if not bulk_scheduler.running:
            flash("The scheduler does not run in this process - start the dashboard with "
                  "'python -m stock_dataload dashboard', or run 'eod-bulk-schedule run-due' regularly", "warning")
        return _schedule_back()

    def _schedule_or_404(schedule_id: int):
        from ..bulk_schedule import ScheduleStore
        schedule = ScheduleStore(state.settings).get(schedule_id)
        if schedule is None:
            abort(404)
        return schedule

    @app.post("/api-load/schedules/<int:schedule_id>/toggle")
    def api_load_schedule_toggle(schedule_id: int):
        from ..bulk_schedule import ScheduleStore
        schedule = _schedule_or_404(schedule_id)
        updated = ScheduleStore(state.settings).set_enabled(schedule_id, not schedule.enabled, current_user().username)
        word = "enabled" if updated.enabled else "disabled"
        state.audit.log("api_load_schedule", f"schedule_{word}", f"{current_user().username} {word} the daily schedule "
                        f"of {updated.market}", level="INFO" if updated.enabled else "WARNING",
                        details={"schedule": schedule_id, "market": updated.market})
        return _schedule_back(f"Schedule of {updated.market} {word}", "success")

    @app.post("/api-load/schedules/<int:schedule_id>/delete")
    def api_load_schedule_delete(schedule_id: int):
        from ..bulk_schedule import ScheduleStore
        schedule = _schedule_or_404(schedule_id)
        ScheduleStore(state.settings).delete(schedule_id)
        state.audit.log("api_load_schedule", "schedule_deleted", f"{current_user().username} deleted the daily "
                        f"schedule of {schedule.market} ({schedule.run_time})", level="WARNING",
                        details=schedule.public())
        return _schedule_back(f"Schedule of {schedule.market} deleted", "success")

    @app.post("/api-load/schedules/<int:schedule_id>/run")
    def api_load_schedule_run(schedule_id: int):
        import threading as _threading
        schedule = _schedule_or_404(schedule_id)
        if api_loads.busy:
            return _schedule_back("an API load is running - wait for it or stop it first")
        user = current_user().username
        worker = _threading.Thread(target=bulk_scheduler.run_now, args=(schedule, f"{user} (run now)"),
                                   name=f"schedule-run-{schedule_id}", daemon=True)
        bulk_scheduler.worker = worker
        worker.start()
        worker.join(1.0)                                 # refusals come back at once; a load goes on in the background
        from ..bulk_schedule import ScheduleStore
        after = ScheduleStore(state.settings).get(schedule_id)
        if after is not None and after.last_status in ("refused", "failed"):
            return _schedule_back(f"Run now of {schedule.market}: {after.last_status} - {after.last_message}")
        return _schedule_back(f"Run now of {schedule.market} started - see Current load", "success")

    @app.route("/api/api-load/limits")
    def api_load_limits():
        """The daily limits and today's use: per stock (?symbols=HD,JNJ - default: the stocks at their
        limit), text-file loads, and (?market=NYSE) the whole-market loads of a market."""
        from ..eod_api import EodApiError
        from ..eod_market import parse_symbol_text
        state_now = _limit_state()
        try:
            wanted, invalid = parse_symbol_text(request.args.get("symbols") or "")
            counts = _fetcher().ledger.counts(wanted) if wanted else state_now["stocks_at_limit"]
            market = _fetcher().market_load_quota(request.args["market"]).as_dict() \
                if request.args.get("market") else None
        except EodApiError as exc:
            return jsonify({"error": str(exc)}), 400
        except (sqlite3.Error, DatabaseError) as exc:
            return jsonify({"error": f"database problem: {exc}"}), 503
        limit = state_now["stock_limit"]
        return jsonify({
            "day": state_now["file_quota"].day, "stock_limit": limit, "max_file_symbols": state_now["max_file_symbols"],
            "stocks": {t: {"used": n, "remaining": max(0, limit - n), "exhausted": n >= limit} for t, n in counts.items()},
            "invalid": invalid, "file_loads": state_now["file_quota"].as_dict(), "market": market})

    def _bad_start(message: str, form=None):
        flash(message, "error")
        if form is not None:                                # keep what was typed (never the file)
            session["api_load_form"] = {k: v for k, v in form.items() if k != "file"}
        return redirect(url_for("api_load"))

    @app.post("/api-load/start")
    def api_load_start():
        from ..eod_api import EodApiError, FaultPlan, validate_market
        from ..eod_market import EodMarketError, MarketFetcher, parse_symbol_text
        from ..load_limits import LoadLimitError, audit_refusal, check_file_symbols
        form = request.form
        mode = form.get("mode", KIND_SYMBOLS)
        if mode not in (KIND_SYMBOLS, KIND_MARKET, KIND_FILE, KIND_BULK):
            abort(400)
        try:
            if mode == KIND_BULK:                        # the exchange code of the bulk endpoint (US = all US exchanges)
                market = validate_market(form.get("bulk_market") or "US")
            else:
                market = validate_market(form.get("market") or form.get("market_other") or "")
        except EodApiError as exc:
            return _bad_start(f"Market: {exc}", form)
        params = {}
        for key, name in (("date_from", "from"), ("date_to", "to")):
            value = (form.get(key) or "").strip()
            if value:
                if not _DATE_RE.match(value):
                    return _bad_start(f"'{value}' is not a date like 2026-01-31", form)
                params[name] = value
        if params.get("from") and params.get("to") and params["from"] > params["to"]:
            return _bad_start("the first date is after the last date", form)
        fake = form.get("fake") == "on"
        faults = (form.get("fake_faults") or "").strip() or None
        if faults:
            if not fake:
                return _bad_start("simulated failures need the built-in stub (tick 'use the built-in stub')", form)
            try:
                FaultPlan.parse(faults)
            except EodApiError as exc:
                return _bad_start(str(exc), form)
        if not fake and state.settings.eod_api_gateway != "fake":
            refused = _token_refused()
            if refused:
                return _bad_start(refused, form)
        allow_fake = form.get("allow_fake_data") == "on"
        if fake and not allow_fake:
            try:
                MarketFetcher(state.settings, db=state.db, fake=True)._check_fake_target()
            except EodMarketError as exc:
                return _bad_start(str(exc), form)
        symbols, types, limit, title = None, None, None, ""
        invalid: list[str] = []
        try:
            if mode == KIND_SYMBOLS:
                symbols, invalid = parse_symbol_text(form.get("symbols"))
                if not symbols:
                    return _bad_start("type one or more stock symbols, separated by commas (e.g. HD, JNJ, PFE)"
                                      + (f" - not a symbol: {', '.join(invalid)}" if invalid else ""), form)
                title = f"{len(symbols):,} selected stock(s) ({market})"
            elif mode == KIND_FILE:
                upload = request.files.get("file")
                if upload is None or not upload.filename:
                    return _bad_start("choose a text file with the stock symbols (separated by commas)", form)
                try:
                    data = upload.read(API_FILE_MAX_BYTES + 1)
                finally:
                    upload.close()
                if len(data) > API_FILE_MAX_BYTES:
                    return _bad_start(f"the file is larger than {API_FILE_MAX_BYTES // 1024} KB", form)
                try:
                    text = data.decode("utf-8-sig")
                except UnicodeDecodeError:
                    text = data.decode("cp1252", errors="replace")
                symbols, invalid = parse_symbol_text(text)
                if not symbols:
                    return _bad_start(f"{Path(upload.filename).name} contains no stock symbols (separate them with "
                                      f"commas, e.g. HD, JNJ, PFE)", form)
                try:
                    check_file_symbols(symbols, Path(upload.filename).name)
                except LoadLimitError as exc:
                    audit_refusal(state.audit, "api_load", exc, file=Path(upload.filename).name,
                                  symbols=len(symbols), requested_by=current_user().username)
                    return _bad_start(f"Not started - {exc}", form)
                title = f"{len(symbols):,} stock(s) of {Path(upload.filename).name} ({market})"
            elif mode == KIND_BULK:
                bulk_symbols = (form.get("bulk_symbols") or "").strip()
                if bulk_symbols:
                    symbols, invalid = parse_symbol_text(bulk_symbols)
                    if not symbols:
                        return _bad_start("the stocks to narrow the bulk load to are no symbols"
                                          + (f": {', '.join(invalid)}" if invalid else ""), form)
                day = (form.get("bulk_date") or "").strip()
                title = (f"{market} {day or 'last trading day'}: "
                         + (f"{len(symbols):,} stock(s) (bulk)" if symbols else "every stock (bulk)"))
            else:
                types = [] if form.get("all_types") == "on" else [t for t in form.getlist("types") if t.strip()]
                if form.get("all_types") != "on" and not types:
                    types = None                               # [eod_api] market_types
                if (form.get("limit") or "").strip():
                    try:
                        limit = max(1, int(form["limit"]))
                    except ValueError:
                        return _bad_start("the number of stocks must be a whole number", form)
                shown = "every type" if types == [] else ", ".join(types or state.settings.eod_market_types)
                title = f"{market}: {shown}" + (f" (first {limit:,})" if limit else "")
        except EodMarketError as exc:
            return _bad_start(str(exc), form)
        user = current_user().username
        used = ""                                         # which of the day's allowed loads this is
        try:
            if mode == KIND_MARKET or (mode == KIND_BULK and not symbols):
                q = _fetcher().market_load_quota(market)
                used = f" - whole-market load {q.used + 1} of {q.limit} today"
            elif mode == KIND_FILE:
                q = _fetcher().file_load_quota()
                used = f" - text-file load {q.used + 1} of {q.limit} today"
        except (sqlite3.Error, DatabaseError):
            pass
        try:
            job = api_loads.start(mode, market, title, user, symbols=symbols, types=types, limit=limit, params=params,
                                  promote=form.get("promote") == "on", aggregates=form.get("aggregates") == "on",
                                  skip_bad_rows=((False if form.get("bulk_strict") == "on" else None)
                                                 if mode == KIND_BULK else form.get("skip_bad_rows") == "on"),
                                  refresh_list=form.get("refresh_list") == "on", fake=True if fake else None,
                                  fake_faults=faults, allow_fake_data=allow_fake,
                                  date=(form.get("bulk_date") or "").strip() or None if mode == KIND_BULK else None,
                                  fmt=(form.get("bulk_fmt") or None) if mode == KIND_BULK else None,
                                  force=mode == KIND_BULK and form.get("bulk_force") == "on")
        except ApiLoadBusy as exc:
            return _bad_start(str(exc), form)
        except LoadLimitError as exc:                     # a daily limit: recorded in the audit trail (WARNING)
            return _bad_start(f"Not started - {exc}", form)
        except EodApiError as exc:                        # a bulk load's day / token - nothing was requested
            return _bad_start(f"Not started - {exc}", form)
        state.audit.log("api_load", "api_load_started", f"{user} started the API load {title}",
                        details={"job": job.job_id, "kind": mode, "market": market, "symbols": len(symbols or []),
                                 "fake": fake, "params": params})
        flash(f"Started: {title}" + (f" - not a symbol, left out: {', '.join(invalid[:20])}" if invalid else "") + used,
              "success" if not invalid else "info")
        if job.at_limit:
            from ..load_limits import MAX_LOADS_PER_STOCK_PER_DAY
            flash(f"Already loaded {MAX_LOADS_PER_STOCK_PER_DAY} times today - {len(job.at_limit)} stock(s) will be "
                  f"skipped (daily limit per stock): {', '.join(job.at_limit[:20])}"
                  + (f" and {len(job.at_limit) - 20} more" if len(job.at_limit) > 20 else ""), "warning")
        return redirect(url_for("api_load"))

    @app.post("/api-load/stop")
    def api_load_stop():
        job = api_loads.stop()
        if job is None:
            flash("No API load is running", "info")
        else:
            state.audit.log("api_load", "api_load_stop", f"{current_user().username} stopped the API load {job.title}",
                            level="WARNING", details={"job": job.job_id, "run": job.run_id})
            flash("Stopping after the current stock - the run can be resumed later", "info")
        return redirect(url_for("api_load"))

    @app.post("/api-load/runs/<run_id>/resume")
    def api_load_resume(run_id: str):
        row = _fetcher().find_run(run_id)
        if row is None:
            abort(404)
        if row["Gateway"] != "fake" and state.settings.eod_api_gateway != "fake":
            refused = _token_refused()
            if refused:
                flash(refused, "error")
                return redirect(url_for("api_load"))
        try:
            job = api_loads.start(KIND_RESUME, row["Market"], f"resume run {row['RunId']} ({row['Market']})",
                                  current_user().username, resume=row["RunId"], fake=True if row["Gateway"] == "fake" else None,
                                  allow_fake_data=row["Gateway"] == "fake",
                                  promote=request.form.get("promote") == "on",
                                  aggregates=request.form.get("aggregates") == "on")
        except ApiLoadBusy as exc:
            flash(str(exc), "error")
            return redirect(url_for("api_load"))
        state.audit.log("api_load", "api_load_started", f"{current_user().username} resumed run {row['RunId']}",
                        details={"job": job.job_id, "run": row["RunId"], "market": row["Market"]})
        flash(f"Resuming run {row['RunId']} ({row['Market']}): the stocks not loaded yet", "success")
        return redirect(url_for("api_load"))

    @app.route("/api-load/runs/<run_id>")
    def api_load_run(run_id: str):
        from ..eod_market import ITEM_STATUSES
        fetcher = _fetcher()
        row = fetcher.find_run(run_id)
        if row is None:
            abort(404)
        wanted = request.args.get("status") or ""
        statuses = [wanted] if wanted in ITEM_STATUSES else None
        items = fetcher.run_items(row["RunId"], statuses)
        counts: dict[str, int] = {}
        for i in fetcher.run_items(row["RunId"]):
            counts[i["Status"]] = counts.get(i["Status"], 0) + 1
        import json as _json
        return render_template("api_load_run.html", run=row, items=items[:_limit(2000)], total=len(items),
                               counts=counts, wanted=wanted, statuses=ITEM_STATUSES, busy=api_loads.busy,
                               parameters=_json.loads(row["Parameters"] or "{}"))

    @app.route("/api/api-load")
    def api_load_status():
        try:
            lines = max(0, min(int(request.args.get("log", 100)), 300))
        except ValueError:
            lines = 100
        return jsonify(api_loads.status(log_lines=lines))

    @app.route("/api/api-load/runs")
    def api_load_runs():
        try:
            runs = _fetcher().runs(request.args.get("market") or None, _limit(20))
        except Exception as exc:  # noqa: BLE001 - bad market code etc.
            return jsonify({"error": str(exc)}), 400
        return jsonify({"count": len(runs), "records": runs})

    return app


def run(settings: Settings | None = None, host: str | None = None, port: int | None = None, debug: bool = False,
        scheduler: bool = True) -> None:
    settings = settings or load_settings()
    app = create_app(settings)
    host = host or settings.dashboard_host
    port = port or settings.dashboard_port
    print(f" * stock_dataload dashboard: http://{host}:{port}/   (config: {settings.config_file})")
    if scheduler:
        app.extensions["bulk_scheduler"].start()
        print(" * daily schedules of 'One day, all stocks' are checked every "
              f"{int(app.extensions['bulk_scheduler'].interval)} s (server time {datetime.now():%H:%M})")
    else:
        print(" * the scheduler is OFF (--no-scheduler) - schedules run only with 'eod-bulk-schedule run-due'")
    try:
        app.run(host=host, port=port, debug=debug, use_reloader=False)
    finally:
        app.extensions["bulk_scheduler"].stop()
