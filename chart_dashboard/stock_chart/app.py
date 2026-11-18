"""
The chart dashboard (Flask).

    python -m stock_chart                      # http://127.0.0.1:8070

Routes
------
/                             the chart page (?ticker=MRK&range=6M&interval=weekly)
/chart/<ticker>               -> /?ticker=<ticker>
/api/tickers?q=AA&limit=50    tickers with daily data: rows, first / last date, market
/api/quotes?tickers=HD,JNJ    the list figures: latest close and the change against the day before
/api/lists                    the stock lists saved in the database (watchlist, Selected, screen lists):
                              GET /api/lists, GET /api/lists/<id>, POST|PUT /api/lists/<id>/items,
                              DELETE /api/lists/<id>/items/<ticker>, POST /api/lists/<id>/reset,
                              POST /api/lists/screen (a text file of comma separated symbols), DELETE /api/lists/<id>
                              - every write needs the header "X-Stock-Chart: 1" (CSRF protection)
/api/daily/<ticker>?from=YYYY-MM-DD&to=YYYY-MM-DD&limit=N[&format=csv]
                              the daily rows (date, open, high, low, close, volume), oldest first
/api/weekly/<ticker>, /api/monthly/<ticker>   (same parameters)
                              one row per ISO week / calendar month (date = first day of the period, open,
                              high, low, close, volume, first_trade, last_trade, trading_days, complete)
/health                       liveness + data check for monitoring (no authentication)

The page draws the candlestick chart itself (``static/candlestick.js``, SVG,
no external libraries or CDNs), so the dashboard works offline and with a
strict Content-Security-Policy.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hmac
import io
import logging
import logging.handlers
import sys
from datetime import datetime

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, url_for

from . import __version__
from .config import INTERVALS, RANGES, WATCHLIST_MAX, ChartSettings, ConfigError, load_settings
from .lists import (LIST_MAX, SCREEN_FILE_MAX_BYTES, ListError, ListNotFound, ListsUnavailable, ListStore,
                    decode_file, list_name_from_file, normalize_ticker, parse_symbols)
from .repository import PERIOD_COLUMNS, DailyRepository, DataUnavailable, InvalidRequest, clean_ticker

log = logging.getLogger(__name__)

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                               "script-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
}


#: the header every write to /api/lists must carry - a cross-site page cannot send it without a CORS
#: preflight, which this app never grants (CSRF protection without sessions)
WRITE_HEADER = "X-Stock-Chart"


def create_app(settings: ChartSettings | None = None, repository: DailyRepository | None = None,
               lists: ListStore | None = None) -> Flask:
    settings = settings or load_settings()
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["JSON_SORT_KEYS"] = False
    app.config["MAX_CONTENT_LENGTH"] = SCREEN_FILE_MAX_BYTES + 64 * 1024
    repo = repository or DailyRepository(settings)

    def watch_seed() -> list[str]:
        """A new watchlist: [chart] watchlist, else every ticker with data."""
        if settings.watchlist:
            return list(settings.watchlist)
        try:
            return [t.ticker for t in repo.all_tickers()[:WATCHLIST_MAX]]
        except DataUnavailable:
            return []

    store = lists or ListStore(settings.lists_db, watch_seed)
    app.extensions["chart_settings"] = settings
    app.extensions["chart_repository"] = repo
    app.extensions["chart_lists"] = store

    # -- authentication (optional) --------------------------------------------------
    def authorized() -> bool:
        if settings.auth_mode != "basic":
            return True
        header = request.headers.get("Authorization", "")
        if not header.lower().startswith("basic "):
            return False
        try:
            user, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        ok_user = hmac.compare_digest(user.encode(), settings.username.encode())
        ok_pass = hmac.compare_digest(password.encode(), settings.password.encode())
        return ok_user and ok_pass

    @app.before_request
    def guard():
        if request.endpoint in ("health", "static") or authorized():
            if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.path.startswith("/api/lists") \
                    and request.headers.get(WRITE_HEADER) != "1":
                log.warning("write without the %s header refused: %s %s", WRITE_HEADER, request.method, request.path)
                return jsonify({"error": f"the header '{WRITE_HEADER}: 1' is required for changes", "status": 403}), 403
            return None
        log.warning("unauthorised request %s %s from %s", request.method, request.path, request.remote_addr)
        return Response("authentication required", 401, {"WWW-Authenticate": 'Basic realm="Stock chart"'})

    def owner() -> str:
        """Whose lists: the basic-auth user, '' without authentication (shared lists)."""
        if settings.auth_mode == "basic" and request.authorization:
            return request.authorization.username or ""
        return ""

    @app.after_request
    def headers(response):
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        if request.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    # -- errors ---------------------------------------------------------------------
    def api_error(status: int, message: str):
        return jsonify({"error": message, "status": status}), status

    @app.errorhandler(404)
    def not_found(exc):
        if request.path.startswith("/api/"):
            return api_error(404, "not found")
        return render_template("error.html", settings=settings, status=404, message="This page does not exist"), 404

    @app.errorhandler(405)
    def not_allowed(exc):
        return api_error(405, "method not allowed") if request.path.startswith("/api/") else (
            render_template("error.html", settings=settings, status=405, message="Method not allowed"), 405)

    # -- pages ----------------------------------------------------------------------
    @app.route("/")
    def index():
        ticker = (request.args.get("ticker") or "").strip().upper()
        from_url = bool(ticker)
        chosen_range = (request.args.get("range") or settings.default_range).upper()
        if chosen_range not in RANGES:
            chosen_range = settings.default_range
        chosen_interval = (request.args.get("interval") or settings.default_interval).lower()
        if chosen_interval not in INTERVALS:
            chosen_interval = settings.default_interval
        error = None
        try:
            ticker = clean_ticker(ticker) if ticker else (repo.default_ticker(settings.default_ticker) or "")
        except InvalidRequest as exc:
            error, ticker = str(exc), ""
        except DataUnavailable as exc:
            error, ticker = str(exc), ""
            log.warning("data unavailable: %s", exc)
        return render_template("index.html", settings=settings, ticker=ticker, chosen_range=chosen_range,
                               ranges=RANGES, chosen_interval=chosen_interval, intervals=INTERVALS, error=error,
                               version=__version__, from_url=from_url, watchlist=default_watchlist(),
                               watchlist_max=WATCHLIST_MAX)

    def default_watchlist() -> list[str]:
        """The default watchlist ([chart] watchlist, else every ticker with data) - the Reset of the page."""
        return watch_seed()

    @app.route("/chart/<ticker>")
    def chart(ticker: str):
        return redirect(url_for("index", ticker=ticker, range=request.args.get("range") or None,
                                interval=request.args.get("interval") or None))

    # -- API ------------------------------------------------------------------------
    @app.route("/api/tickers")
    def api_tickers():
        try:
            limit = int(request.args.get("limit", 50))
            found = repo.tickers(request.args.get("q"), limit)
        except ValueError:
            return api_error(400, "limit must be a whole number")
        except DataUnavailable as exc:
            return api_error(503, str(exc))
        return jsonify({"count": len(found), "tickers": [t.as_dict() for t in found]})

    @app.route("/api/quotes")
    def api_quotes():
        text = request.args.get("tickers") or ""
        tickers = [t for t in text.replace(";", ",").split(",") if t.strip()]
        if not tickers:
            return api_error(400, "the 'tickers' parameter is required (comma separated)")
        if len(tickers) > LIST_MAX:
            return api_error(400, f"at most {LIST_MAX} tickers per request")
        try:
            quotes, unknown = repo.quotes(tickers)
        except InvalidRequest as exc:
            return api_error(400, str(exc))
        except DataUnavailable as exc:
            return api_error(503, str(exc))
        return jsonify({"count": len(quotes), "quotes": quotes, "unknown": unknown})

    # -- the stock lists (saved in the database) ----------------------------------------------
    def list_call(fn):
        """Run a list operation; list / data problems become JSON errors."""
        try:
            return fn()
        except ListNotFound as exc:
            return api_error(404, str(exc))
        except (ListError, InvalidRequest) as exc:
            return api_error(400, str(exc))
        except (ListsUnavailable, DataUnavailable) as exc:
            log.warning("lists unavailable: %s", exc)
            return api_error(503, str(exc))

    def with_data(tickers: list[str]) -> tuple[list[str], list[str]]:
        """(tickers with daily data, tickers without) - in the given order."""
        if not tickers:
            return [], []
        quotes, unknown = repo.quotes(tickers)
        known = {q["ticker"] for q in quotes}
        return [t for t in tickers if t in known], unknown

    def requested_tickers() -> tuple[list[str], list[str]]:
        """The tickers of a JSON body {"tickers": [...]} or {"text": "HD, JNJ"}: (valid, invalid)."""
        body = request.get_json(silent=True) or {}
        raw = body.get("tickers")
        if isinstance(raw, list):
            text = ",".join(str(x) for x in raw)
        else:
            text = str(body.get("text") or raw or "")
        return parse_symbols(text)

    @app.get("/api/lists")
    def api_lists():
        return list_call(lambda: jsonify({"lists": [l.as_dict() for l in store.lists(owner())],
                                          "max_items": LIST_MAX}))

    @app.get("/api/lists/<int:list_id>")
    def api_list(list_id: int):
        return list_call(lambda: jsonify(store.get(list_id, owner()).as_dict()))

    @app.post("/api/lists/<int:list_id>/items")
    def api_list_add(list_id: int):
        def run():
            valid, invalid = requested_tickers()
            if not valid:
                raise ListError("no valid ticker given" + (f" ({', '.join(invalid)})" if invalid else ""))
            known, unknown = with_data(valid)
            lst, added, already = store.add(list_id, known, owner())
            return jsonify({"list": lst.as_dict(), "added": added, "already": already, "unknown": unknown,
                            "invalid": invalid})
        return list_call(run)

    @app.put("/api/lists/<int:list_id>/items")
    def api_list_replace(list_id: int):
        def run():
            valid, invalid = requested_tickers()
            known, unknown = with_data(valid)
            lst = store.replace(list_id, known, owner())
            return jsonify({"list": lst.as_dict(), "unknown": unknown, "invalid": invalid})
        return list_call(run)

    @app.delete("/api/lists/<int:list_id>/items/<ticker>")
    def api_list_remove(list_id: int, ticker: str):
        def run():
            lst, removed = store.remove(list_id, ticker, owner())
            if not removed:
                raise ListNotFound(f"{normalize_ticker(ticker) or ticker} is not in the list {lst.name}")
            return jsonify({"list": lst.as_dict(), "removed": normalize_ticker(ticker)})
        return list_call(run)

    @app.post("/api/lists/<int:list_id>/reset")
    def api_list_reset(list_id: int):
        def run():
            lst = store.get(list_id, owner())
            if lst.kind != "watch":
                raise ListError("only the watchlist has a default to go back to")
            return jsonify({"list": store.reset_watchlist(owner()).as_dict()})
        return list_call(run)

    @app.post("/api/lists/screen")
    def api_list_screen():
        def run():
            upload = request.files.get("file")
            if upload is None or not upload.filename:
                raise ListError("choose a text file with the stock symbols (separated by commas)")
            data = upload.read(SCREEN_FILE_MAX_BYTES + 1)
            if len(data) > SCREEN_FILE_MAX_BYTES:
                raise ListError(f"the file is larger than {SCREEN_FILE_MAX_BYTES // 1024} KB")
            tickers, invalid = parse_symbols(decode_file(data))
            name = (request.form.get("name") or "").strip() or list_name_from_file(upload.filename)
            _, without = with_data(tickers)
            lst, replaced = store.save_screen(name, tickers, upload.filename, owner())
            log.info("screen list '%s' %s from %s: %d symbol(s), %d without data, %d invalid", lst.name,
                     "replaced" if replaced else "loaded", upload.filename, lst.count, len(without), len(invalid))
            return jsonify({"list": lst.as_dict(), "replaced": replaced, "symbols": lst.count,
                            "without_data": without, "invalid": invalid}), 200 if replaced else 201
        return list_call(run)

    @app.delete("/api/lists/<int:list_id>")
    def api_list_delete(list_id: int):
        return list_call(lambda: jsonify({"deleted": store.delete(list_id, owner()).as_dict()}))

    @app.errorhandler(413)
    def too_large(exc):
        return api_error(413, f"the upload is too large (at most {SCREEN_FILE_MAX_BYTES // 1024} KB)")

    @app.route("/api/daily/<ticker>")
    def api_daily(ticker: str):
        try:
            limit = int(request.args["limit"]) if request.args.get("limit") else None
            series = repo.daily(ticker, request.args.get("from"), request.args.get("to"), limit)
        except ValueError:
            return api_error(400, "limit must be a whole number")
        except InvalidRequest as exc:
            return api_error(400, str(exc))
        except DataUnavailable as exc:
            return api_error(503, str(exc))
        if not series.rows:
            return api_error(404, f"no daily data for {series.ticker}")
        if (request.args.get("format") or "json").lower() == "csv":
            buf = io.StringIO()
            writer = csv.writer(buf, lineterminator="\n")
            writer.writerow(["date", "open", "high", "low", "close", "volume"])
            writer.writerows(series.rows)
            return Response(buf.getvalue(), mimetype="text/csv", headers={
                "Content-Disposition": f'attachment; filename="{series.ticker}_{series.first}_{series.last}.csv"'})
        return jsonify({"ticker": series.ticker, "interval": "daily", "source": settings.daily_table,
                        "market": series.market, "count": len(series.rows),
                        "total": series.total, "truncated": series.truncated, "first": series.first,
                        "last": series.last, "columns": ["date", "open", "high", "low", "close", "volume"],
                        "rows": series.rows})

    @app.route("/api/weekly/<ticker>", defaults={"interval": "weekly"})
    @app.route("/api/monthly/<ticker>", defaults={"interval": "monthly"})
    def api_periods(ticker: str, interval: str):
        try:
            limit = int(request.args["limit"]) if request.args.get("limit") else None
            series = repo.periods(interval, ticker, request.args.get("from"), request.args.get("to"), limit)
        except ValueError:
            return api_error(400, "limit must be a whole number")
        except InvalidRequest as exc:
            return api_error(400, str(exc))
        except DataUnavailable as exc:
            return api_error(503, str(exc))
        if not series.rows:
            return api_error(404, f"no {interval} data for {series.ticker}")
        if (request.args.get("format") or "json").lower() == "csv":
            start = "week_start" if interval == "weekly" else "month_start"
            buf = io.StringIO()
            writer = csv.writer(buf, lineterminator="\n")
            writer.writerow([start, "open", "high", "low", "close", "volume", "first_trade", "last_trade",
                             "trading_days", "complete"])
            writer.writerows([*r[:9], "" if r[9] is None else str(r[9]).lower()] for r in series.rows)
            return Response(buf.getvalue(), mimetype="text/csv", headers={
                "Content-Disposition": f'attachment; filename="{series.ticker}_{interval}_{series.first}_{series.last}.csv"'})
        return jsonify({"ticker": series.ticker, "interval": interval, "source": series.source,
                        "market": series.market, "count": len(series.rows), "total": series.total,
                        "truncated": series.truncated, "first": series.first, "last": series.last,
                        "columns": list(PERIOD_COLUMNS), "rows": series.rows})

    @app.route("/health")
    def health():
        body = {"status": "ok", "version": __version__, "time": datetime.now().replace(microsecond=0).isoformat()}
        try:
            body.update(repo.check())
        except DataUnavailable as exc:
            body.update(status="degraded", error=str(exc))
            return jsonify(body), 503
        body.update(repo.period_counts())           # informational: the aggregate tables are optional
        return jsonify(body)

    return app


# ---------------------------------------------------------------------------
# running it
# ---------------------------------------------------------------------------
def setup_logging(settings: ChartSettings) -> None:
    root = logging.getLogger()
    root.setLevel(settings.log_level)
    for h in list(root.handlers):
        root.removeHandler(h)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(console)
    if settings.log_file:
        settings.log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(settings.log_file, maxBytes=2_000_000, backupCount=5,
                                                  encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        root.addHandler(fh)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stock_chart", description="Candlestick charts of the daily stock data.")
    parser.add_argument("--config", metavar="FILE", help="chart.ini (default: $STOCK_CHART_CONFIG, ./chart.ini)")
    parser.add_argument("--host", help="bind address (default: [server] host)")
    parser.add_argument("--port", type=int, help="port (default: [server] port, 8070)")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--check", action="store_true", help="check the configuration and the data, then exit")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    setup_logging(settings)
    repo = DailyRepository(settings)
    try:
        info = repo.check()
        data_note = f"{info['rows']:,} rows in {info['table']}, last trade date {info['last_trade_date'] or '-'}"
    except DataUnavailable as exc:
        data_note = f"DATA NOT AVAILABLE: {exc}"
        if args.check:
            print(data_note, file=sys.stderr)
            return 1
    if args.check:
        print(f"ok - {settings.sqlite_path}: {data_note}")
        return 0
    host, port = args.host or settings.host, args.port or settings.port
    print(f" * stock chart dashboard {__version__}: http://{host}:{port}/   (config: {settings.config_file or 'defaults'}, "
          f"database: {settings.sqlite_path} - {data_note}; auth: {settings.auth_mode})", flush=True)
    create_app(settings, repo).run(host=host, port=port, debug=args.debug, use_reloader=False)
    return 0
