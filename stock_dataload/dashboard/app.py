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

Everything the dashboard does goes through the same FileConverter /
StageLoader classes the CLI uses - the dashboard is just a front end.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from flask import (Flask, abort, flash, jsonify, redirect, render_template, request, send_from_directory,
                   url_for)

from .. import formats
from ..config import ConfigError, Settings, load_settings, save_settings
from ..converter import ConversionError, FileConverter
from ..db import DatabaseError, StageDatabase
from ..file_formats import FILE_TYPE_LABELS, FILE_TYPES, FileFormat, FormatError, FormatStore
from ..loader import StageLoader
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
        self.loader = StageLoader(settings, self.db)

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


def create_app(settings: Settings | None = None) -> Flask:
    settings = settings or load_settings()
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["SECRET_KEY"] = os.environ.get("STOCK_DATALOAD_SECRET", secrets.token_hex(16))
    app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024
    state = DashboardState(settings)
    app.extensions["state"] = state

    @app.context_processor
    def inject():
        return {
            "settings": state.settings,
            "default_header": formats.DEFAULT_HEADER_LINE,
            "default_columns": formats.DEFAULT_COLUMNS,
            "file_types": FILE_TYPES,
            "file_type_labels": FILE_TYPE_LABELS,
            "now": datetime.now(),
        }

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
            new.replace_existing_file_rows = form.get("replace_existing_file_rows") == "on"
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
