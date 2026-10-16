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
/login       sign in (every other page requires a signed-in user); /logout; /account (own password)
/users       user management (administrators): add / edit / deactivate / delete accounts, reset passwords

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
from ..audit import AuditLogger, LOG_LEVELS
from ..auth import (ROLE_ADMIN, ROLE_LABELS, ROLE_OPERATOR, ROLE_VIEWER, ROLES, AuthError, User,
                    UserStore, load_secret_key)
from ..config import ConfigError, Settings, load_settings, save_settings
from ..converter import ConversionError, FileConverter
from ..correction import CorrectionError, CorrectionService, values_from_data_error
from ..db import FILE_TO_STAGE, STAGE_EDITABLE_COLUMNS, DatabaseError, StageDatabase
from ..file_formats import FILE_TYPE_LABELS, FILE_TYPES, FileFormat, FormatError, FormatStore
from ..loader import StageLoader
from ..quality import DataErrorRepository
from ..readers import SUPPORTED_EXTENSIONS, detect_file_type

log = logging.getLogger(__name__)

_SAFE_NAME = re.compile(r"^[^/\\\x00]+$")
BROWSABLE = ("conversion_dir", "converted_dir", "loading_dir", "archive_dir", "error_dir", "work_dir", "log_dir")


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
        self.loader = StageLoader(settings, self.db, audit=self.audit, data_errors=self.data_errors)
        self.corrections = CorrectionService(settings, self.db, audit=self.audit, data_errors=self.data_errors)
        self.users = UserStore(settings)

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
            }
        except (sqlite3.Error, DatabaseError, OSError) as exc:
            log.warning("monitoring counts unavailable: %s", exc)
            return {"logs": 0, "errors": 0, "data_errors": 0, "alerts": 0, "resolved": 0, "corrections": 0,
                    "loads": 0, "failed_loads": 0}

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
_OPERATOR_ENDPOINTS = frozenset({"convert", "convert_all", "load", "upload", "delete_file", "format_edit",
                                 "format_delete", "correct", "correct_save"})
_ADMIN_ENDPOINTS = frozenset({"settings_page", "users", "user_new", "user_edit", "user_delete"})
#: endpoints reachable without signing in
_PUBLIC_ENDPOINTS = frozenset({"static", "login"})


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
            session.clear()
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
        }

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
        session.clear()
        session["user_id"] = user.user_id
        session.permanent = False
        g.pop("user", None)
        state.audit.log("auth", "login", f"{user.username} signed in ({user.role})",
                        details={"username": user.username, "role": user.role, "remote": request.remote_addr})
        target = request.form.get("next") or ""
        if user.must_change_password:
            flash("Welcome! Please choose a new password before continuing", "info")
            return redirect(url_for("account"))
        if not target.startswith("/") or target.startswith("//"):
            target = url_for("index")
        return redirect(target)

    @app.post("/logout")
    def logout():
        user = current_user()
        if user is not None:
            state.audit.log("auth", "logout", f"{user.username} signed out", details={"username": user.username})
        session.clear()
        flash("You have been signed out", "info")
        return redirect(url_for("login"))

    @app.route("/account", methods=["GET", "POST"])
    def account():
        user = current_user()
        if request.method == "GET":
            return render_template("account.html", user=user, error=None)
        new, confirm = request.form.get("new_password") or "", request.form.get("confirm_password") or ""
        if new != confirm:
            return render_template("account.html", user=user, error="the new passwords do not match"), 400
        try:
            state.users.change_password(user.user_id, request.form.get("current_password") or "", new)
        except AuthError as exc:
            return render_template("account.html", user=user, error=str(exc)), 400
        state.audit.log("auth", "password_changed", f"{user.username} changed their password",
                        details={"username": user.username})
        flash("Password changed", "success")
        return redirect(url_for("index"))

    # -- user management (administrators) ---------------------------------------
    @app.route("/users")
    def users():
        return render_template("users.html", users=state.users.list_users())

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
                                              must_change_password=form.get("must_change_password") == "on")
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
                                         display_name=form.get("display_name", ""))
        except AuthError as exc:
            return render_template("user_form.html", user=user, form=form, error=str(exc)), 400
        state.audit.log("auth", "user_updated", f"{me.username} updated user '{updated.username}' "
                        f"(role={updated.role}, active={updated.is_active})",
                        details={"username": updated.username, "role": updated.role, "active": updated.is_active})
        flash(f"User '{updated.username}' updated", "success")
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
            converted_count=len(state.list_files("converted_dir")),
            formats_list=state.store.all(),
            loads=loads, total_rows=total_rows,
            supported=", ".join(SUPPORTED_EXTENSIONS),
            monitor=state.monitor_counts(),
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
            results = state.loader.run(files=files, market=market, truncate=truncate)
        except DatabaseError as exc:
            flash(f"database problem: {exc}", "error")
            return redirect(url_for("index"))
        if not results:
            flash("Nothing to load - the loading folder has no default-format .txt files", "info")
        for r in results:
            flash(str(r), "success" if r.ok else "error")
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
        path.unlink()
        note = path.with_name(path.name + ".error.txt")
        if note.exists():
            note.unlink()
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
            return render_template("settings.html", s=s, error=None)
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
            if new.batch_size <= 0:
                raise ConfigError("batch size must be > 0")
            if not new.stage_table:
                raise ConfigError("staging table name must not be empty")
            new.config_file = s.config_file or (base / "config.ini")
            new.base_dir = base
            new.ensure_folders()
            save_settings(new, new.config_file)
            state.reload(load_settings(new.config_file))
        except (ConfigError, ValueError, re.error, OSError) as exc:
            return render_template("settings.html", s=s, error=str(exc)), 400
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

    return app


def run(settings: Settings | None = None, host: str | None = None, port: int | None = None, debug: bool = False) -> None:
    settings = settings or load_settings()
    app = create_app(settings)
    host = host or settings.dashboard_host
    port = port or settings.dashboard_port
    print(f" * stock_dataload dashboard: http://{host}:{port}/   (config: {settings.config_file})")
    app.run(host=host, port=port, debug=debug, use_reloader=False)
