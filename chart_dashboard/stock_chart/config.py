"""
Settings of the chart dashboard: ``chart.ini`` + environment variables.

Lookup of the config file: ``--config`` argument, ``$STOCK_CHART_CONFIG``,
``chart.ini`` in the current directory, ``chart.ini`` next to this package's
folder.  Relative paths in the file are resolved against the folder that
contains it.  Without a file the built-in defaults apply.

Environment variables win over the file (useful for containers / services):

    STOCK_CHART_DB          the SQLite database file
    STOCK_CHART_LISTS_DB    where the stock lists are saved (default: the database above)
    STOCK_CHART_HOST        bind address
    STOCK_CHART_PORT        port
    STOCK_CHART_PASSWORD    the password of [auth] mode = basic (never put it into the file)
    STOCK_CHART_SECRET      not needed - the app keeps no sessions
"""

from __future__ import annotations

import configparser
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent          # chart_dashboard/
CONFIG_FILENAME = "chart.ini"
ENV_CONFIG = "STOCK_CHART_CONFIG"

RANGES = ("1M", "3M", "6M", "YTD", "1Y", "5Y", "ALL")
#: one candle per trading day / ISO week / calendar month
INTERVALS = ("daily", "weekly", "monthly")
#: where weekly / monthly candles come from: auto = the pipeline's aggregate tables when they are up to date
#: for the ticker, else aggregated from the daily rows | tables = the aggregate tables only | daily = always
#: aggregated from the daily rows
PERIOD_SOURCES = ("auto", "tables", "daily")
AUTH_MODES = ("none", "basic")
#: most tickers [chart] watchlist names
WATCHLIST_MAX = 200
_TICKER_RE = re.compile(r"^[A-Za-z0-9.\-^=_]{1,20}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class ConfigError(Exception):
    """Invalid or missing configuration."""


@dataclass
class ChartSettings:
    # [database] - read-only access to the daily table of the stock_dataload database
    sqlite_path: Path = APP_ROOT.parent / "data" / "stock_trade.db"
    daily_table: str = "Stock_Daily"
    weekly_table: str = "Stock_Weekly"     # the pipeline's weekly aggregates (promote weekly)
    monthly_table: str = "Stock_Monthly"   # the pipeline's monthly aggregates (promote monthly)
    # [lists] - the watchlist, the Selected list and the screen lists (read-write; None = sqlite_path)
    lists_path: Path | None = None
    # [server]
    host: str = "127.0.0.1"
    port: int = 8070
    # [chart]
    title: str = "Stock chart"
    default_ticker: str = ""               # empty: the ticker with the most daily rows
    default_range: str = "6M"
    default_interval: str = "daily"        # daily | weekly | monthly
    # the tickers a new watchlist starts with (and Reset goes back to) - the list itself is saved in the
    # database ([lists]); empty = every ticker with data (at most WATCHLIST_MAX)
    watchlist: tuple[str, ...] = ()
    period_source: str = "auto"            # auto | tables | daily (see PERIOD_SOURCES)
    max_points: int = 5000                 # most recent trading days one chart request returns
    ticker_cache_seconds: int = 60         # the ticker list (a GROUP BY over the table) is cached
    # [auth]
    auth_mode: str = "none"                # none | basic
    username: str = "dummy_chart_viewer"
    password: str = field(default="", repr=False)   # from $STOCK_CHART_PASSWORD only
    # [logging]
    log_level: str = "INFO"
    log_file: Path | None = None
    # where the settings came from
    config_file: Path | None = None

    @property
    def lists_db(self) -> Path:
        """The database the lists are saved in."""
        return Path(self.lists_path) if self.lists_path else Path(self.sqlite_path)

    def validate(self) -> "ChartSettings":
        for key in ("daily_table", "weekly_table", "monthly_table"):
            if not _IDENTIFIER_RE.match(getattr(self, key)):
                raise ConfigError(f"[database] {key} '{getattr(self, key)}' is not a valid table name")
        if len({self.daily_table.lower(), self.weekly_table.lower(), self.monthly_table.lower()}) != 3:
            raise ConfigError("[database] daily_table, weekly_table and monthly_table must differ")
        if not 1 <= self.port <= 65535:
            raise ConfigError("[server] port must be between 1 and 65535")
        if self.default_range not in RANGES:
            raise ConfigError(f"[chart] default_range must be one of {', '.join(RANGES)}")
        if self.default_interval not in INTERVALS:
            raise ConfigError(f"[chart] default_interval must be one of {', '.join(INTERVALS)}")
        if self.period_source not in PERIOD_SOURCES:
            raise ConfigError(f"[chart] period_source must be one of {', '.join(PERIOD_SOURCES)}")
        bad = [t for t in self.watchlist if not _TICKER_RE.match(t)]
        if bad:
            raise ConfigError(f"[chart] watchlist: '{bad[0]}' is not a valid ticker")
        if len(self.watchlist) > WATCHLIST_MAX:
            raise ConfigError(f"[chart] watchlist: at most {WATCHLIST_MAX} tickers")
        if not 10 <= self.max_points <= 100_000:
            raise ConfigError("[chart] max_points must be between 10 and 100000")
        if self.ticker_cache_seconds < 0:
            raise ConfigError("[chart] ticker_cache_seconds must be >= 0")
        if self.auth_mode not in AUTH_MODES:
            raise ConfigError(f"[auth] mode must be one of {', '.join(AUTH_MODES)}")
        if self.auth_mode == "basic" and not (self.username and self.password):
            raise ConfigError("[auth] mode = basic needs [auth] username and the password in $STOCK_CHART_PASSWORD")
        if self.log_level.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise ConfigError("[logging] level must be DEBUG, INFO, WARNING or ERROR")
        return self


def parse_watchlist(text: str | None) -> tuple[str, ...]:
    """``"hd, JNJ; jpm hd"`` -> ``("HD", "JNJ", "JPM")`` (upper case, unique, in the given order)."""
    out: list[str] = []
    for item in (text or "").replace(";", ",").replace(" ", ",").split(","):
        item = item.strip().upper()
        if item and item not in out:
            out.append(item)
    return tuple(out)


def find_config_file(explicit: str | os.PathLike | None = None) -> Path | None:
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        return path
    candidates = [Path(os.environ[ENV_CONFIG])] if os.environ.get(ENV_CONFIG) else []
    candidates += [Path.cwd() / CONFIG_FILENAME, APP_ROOT / CONFIG_FILENAME]
    return next((c for c in candidates if c.is_file()), None)


def _int(section, key: str, default: int) -> int:
    try:
        return section.getint(key, fallback=default)
    except ValueError as exc:
        raise ConfigError(f"[{section.name}] {key}: expected a whole number ({exc})") from exc


def load_settings(config_path: str | os.PathLike | None = None) -> ChartSettings:
    """Settings from the config file (or defaults) and the environment, validated."""
    settings = ChartSettings()
    path = find_config_file(config_path)
    if path is not None:
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(path, encoding="utf-8")
        base = path.resolve().parent

        def resolve(value: str) -> Path:
            p = Path(os.path.expandvars(value.strip())).expanduser()
            return p if p.is_absolute() else (base / p).resolve()

        settings.config_file = path.resolve()
        if parser.has_section("database"):
            s = parser["database"]
            if s.get("sqlite_path", "").strip():
                settings.sqlite_path = resolve(s["sqlite_path"])
            settings.daily_table = s.get("daily_table", settings.daily_table).strip() or settings.daily_table
            settings.weekly_table = s.get("weekly_table", settings.weekly_table).strip() or settings.weekly_table
            settings.monthly_table = s.get("monthly_table", settings.monthly_table).strip() or settings.monthly_table
        if parser.has_section("lists"):
            s = parser["lists"]
            if s.get("sqlite_path", "").strip():
                settings.lists_path = resolve(s["sqlite_path"])
        if parser.has_section("server"):
            s = parser["server"]
            settings.host = s.get("host", settings.host).strip() or settings.host
            settings.port = _int(s, "port", settings.port)
        if parser.has_section("chart"):
            s = parser["chart"]
            settings.title = s.get("title", settings.title).strip() or settings.title
            settings.default_ticker = s.get("default_ticker", "").strip().upper()
            settings.default_range = s.get("default_range", settings.default_range).strip().upper() or "6M"
            settings.default_interval = (s.get("default_interval", settings.default_interval).strip().lower()
                                         or "daily")
            settings.period_source = s.get("period_source", settings.period_source).strip().lower() or "auto"
            settings.watchlist = parse_watchlist(s.get("watchlist", ""))
            settings.max_points = _int(s, "max_points", settings.max_points)
            settings.ticker_cache_seconds = _int(s, "ticker_cache_seconds", settings.ticker_cache_seconds)
        if parser.has_section("auth"):
            s = parser["auth"]
            settings.auth_mode = s.get("mode", "none").strip().lower() or "none"
            settings.username = s.get("username", settings.username).strip()
        if parser.has_section("logging"):
            s = parser["logging"]
            settings.log_level = s.get("level", "INFO").strip().upper() or "INFO"
            settings.log_file = resolve(s["file"]) if s.get("file", "").strip() else None
    env = os.environ
    if env.get("STOCK_CHART_DB"):
        settings.sqlite_path = Path(env["STOCK_CHART_DB"]).expanduser().resolve()
    if env.get("STOCK_CHART_LISTS_DB"):
        settings.lists_path = Path(env["STOCK_CHART_LISTS_DB"]).expanduser().resolve()
    if env.get("STOCK_CHART_HOST"):
        settings.host = env["STOCK_CHART_HOST"].strip()
    if env.get("STOCK_CHART_PORT"):
        try:
            settings.port = int(env["STOCK_CHART_PORT"])
        except ValueError as exc:
            raise ConfigError(f"$STOCK_CHART_PORT: expected a whole number ({exc})") from exc
    settings.password = env.get("STOCK_CHART_PASSWORD", "")
    return settings.validate()
