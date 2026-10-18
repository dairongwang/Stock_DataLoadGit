"""
Configuration handling.

Settings live in an INI file (``config.ini`` next to this package by default).
All relative paths in the file are resolved against the directory that
contains the config file, so the project can be moved around freely.

Lookup order for the config file:

1. explicit path passed to :func:`load_settings`
2. environment variable ``STOCK_DATALOAD_CONFIG``
3. ``config.ini`` in the current working directory
4. ``config.ini`` in the project root (parent of this package)
"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field
from pathlib import Path

ENV_CONFIG = "STOCK_DATALOAD_CONFIG"
CONFIG_FILENAME = "config.ini"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    """Raised for invalid or missing configuration."""


@dataclass
class Settings:
    # --- folders -----------------------------------------------------------
    base_dir: Path = PROJECT_ROOT
    conversion_dir: Path = PROJECT_ROOT / "data" / "conversion"  # source files waiting to be converted
    converted_dir: Path = PROJECT_ROOT / "data" / "converted"    # source files after successful conversion
    loading_dir: Path = PROJECT_ROOT / "data" / "loading"        # default-format files waiting to be loaded
    archive_dir: Path = PROJECT_ROOT / "data" / "archive"        # successfully loaded files
    error_dir: Path = PROJECT_ROOT / "data" / "error"            # files that failed to load
    skipped_dir: Path = PROJECT_ROOT / "data" / "skipped"        # deliveries dated on a no-trading day
    work_dir: Path = PROJECT_ROOT / "data" / "work"              # scratch space for the converter
    log_dir: Path = PROJECT_ROOT / "logs"

    # --- database (sqlite) ---------------------------------------------------
    sqlite_path: Path = PROJECT_ROOT / "data" / "stock_trade.db"
    stage_table: str = "Stock_Stage"
    batch_size: int = 5000
    truncate_before_load: bool = False
    # NOTE: re-loading a file always deletes its previous rows first (one copy
    # per file, guaranteed) - this is not configurable.
    # active (validated, typed) tables fed by the promotion procedures
    daily_table: str = "Stock_Daily"
    weekly_table: str = "Stock_Weekly"
    monthly_table: str = "Stock_Monthly"

    # --- promotion staging -> active tables (promotion.py) -----------------------
    # reject rows whose trade date is a day the market was closed (weekend / holiday)
    reject_market_closed_days: bool = True
    # reject rows whose open / close price lies outside the low..high range
    check_price_range: bool = True
    # reject rows dated before the stock's first / after its last trade date (Stock_Symbol table)
    check_trading_window: bool = True
    # weekly / monthly: flag aggregates with a trading day missing (No_Trading_Day + Stock_Symbol tables)
    check_completeness: bool = True

    # --- loader --------------------------------------------------------------
    file_patterns: tuple[str, ...] = ("*.txt",)
    market_from_filename_regex: str = r"^(?P<market>[A-Za-z]+)_\d{8}"
    default_market: str = ""
    # the trade date in the file name (NASDAQ_20250123.txt -> 2025-01-23); a file dated on a
    # no-trading day of its market (No_Trading_Day table) is skipped, not loaded
    date_from_filename_regex: str = r"(?P<date>\d{8})"
    skip_no_trading_days: bool = True
    skip_bad_rows: bool = False
    encoding: str = "utf-8-sig"

    # --- data quality ---------------------------------------------------------
    quality_checks_enabled: bool = True            # run the strategy-based checks after every load
    alert_missing_days: bool = True                # missing_market_day strategy on/off
    extra_market_holidays: tuple[str, ...] = ()    # ad-hoc market closures (YYYY-MM-DD)

    # --- converter / custom formats -----------------------------------------
    formats_file: Path = PROJECT_ROOT / "formats.json"
    # legacy [formats] section: name -> list of column names in source order
    named_formats: dict[str, list[str]] = field(default_factory=dict)

    # --- dashboard -------------------------------------------------------------
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8050
    # session-signing secret: generated once into this git-ignored file
    # (or taken from $STOCK_DATALOAD_SECRET) - never stored in the repository
    secret_key_file: Path = PROJECT_ROOT / "data" / ".secret_key"

    # where the settings came from (None = built-in defaults)
    config_file: Path | None = None

    # ---------------------------------------------------------------------
    @property
    def folders(self) -> dict[str, Path]:
        return {
            "conversion_dir": self.conversion_dir, "converted_dir": self.converted_dir,
            "loading_dir": self.loading_dir, "archive_dir": self.archive_dir,
            "error_dir": self.error_dir, "skipped_dir": self.skipped_dir, "work_dir": self.work_dir,
            "log_dir": self.log_dir,
        }

    def ensure_folders(self) -> None:
        for folder in self.folders.values():
            folder.mkdir(parents=True, exist_ok=True)
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)


def find_config_file(explicit: str | os.PathLike | None = None) -> Path | None:
    candidates: list[Path] = []
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise ConfigError(f"config file not found: {p}")
        return p
    env = os.environ.get(ENV_CONFIG)
    if env:
        candidates.append(Path(env))
    candidates.append(Path.cwd() / CONFIG_FILENAME)
    candidates.append(PROJECT_ROOT / CONFIG_FILENAME)
    for c in candidates:
        if c.is_file():
            return c
    return None


def _get_bool(section: configparser.SectionProxy, key: str, default: bool) -> bool:
    try:
        return section.getboolean(key, fallback=default)
    except ValueError as exc:
        raise ConfigError(f"[{section.name}] {key}: expected true/false ({exc})") from exc


def _get_int(section: configparser.SectionProxy, key: str, default: int) -> int:
    try:
        return section.getint(key, fallback=default)
    except ValueError as exc:
        raise ConfigError(f"[{section.name}] {key}: expected an integer ({exc})") from exc


def load_settings(config_path: str | os.PathLike | None = None) -> Settings:
    """Build a :class:`Settings` object from an INI file (or defaults)."""
    path = find_config_file(config_path)
    settings = Settings()
    if path is None:
        return settings

    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # keep case of format names
    parser.read(path, encoding="utf-8")
    base = path.resolve().parent

    def resolve(value: str) -> Path:
        p = Path(os.path.expandvars(value)).expanduser()
        return p if p.is_absolute() else (base / p).resolve()

    settings.config_file = path.resolve()
    settings.base_dir = base

    defaults = {
        "conversion_dir": "data/conversion", "converted_dir": "data/converted",
        "loading_dir": "data/loading", "archive_dir": "data/archive", "error_dir": "data/error",
        "skipped_dir": "data/skipped", "work_dir": "data/work", "log_dir": "logs",
    }
    section = parser["paths"] if parser.has_section("paths") else {}
    for key, default in defaults.items():
        setattr(settings, key, resolve(section.get(key, default) if section else default))

    settings.sqlite_path = base / "data" / "stock_trade.db"
    settings.formats_file = base / "formats.json"
    settings.secret_key_file = base / "data" / ".secret_key"
    if parser.has_section("database"):
        s = parser["database"]
        settings.sqlite_path = resolve(s.get("sqlite_path", "data/stock_trade.db"))
        settings.stage_table = s.get("stage_table", "Stock_Stage").strip()
        settings.batch_size = _get_int(s, "batch_size", 5000)
        settings.truncate_before_load = _get_bool(s, "truncate_before_load", False)
        settings.daily_table = s.get("daily_table", "Stock_Daily").strip() or "Stock_Daily"
        settings.weekly_table = s.get("weekly_table", "Stock_Weekly").strip() or "Stock_Weekly"
        settings.monthly_table = s.get("monthly_table", "Stock_Monthly").strip() or "Stock_Monthly"

    if parser.has_section("promotion"):
        s = parser["promotion"]
        settings.reject_market_closed_days = _get_bool(s, "reject_market_closed_days", True)
        settings.check_price_range = _get_bool(s, "check_price_range", True)
        settings.check_trading_window = _get_bool(s, "check_trading_window", True)
        settings.check_completeness = _get_bool(s, "check_completeness", True)

    if parser.has_section("quality"):
        s = parser["quality"]
        settings.quality_checks_enabled = _get_bool(s, "enabled", True)
        settings.alert_missing_days = _get_bool(s, "alert_missing_days", True)
        holidays = s.get("extra_holidays", "")
        settings.extra_market_holidays = tuple(
            h.strip() for h in holidays.replace(";", ",").split(",") if h.strip())

    if parser.has_section("loader"):
        s = parser["loader"]
        patterns = s.get("file_patterns", "*.txt")
        settings.file_patterns = tuple(p.strip() for p in patterns.replace(",", ";").split(";") if p.strip())
        settings.market_from_filename_regex = s.get("market_from_filename_regex", settings.market_from_filename_regex)
        settings.default_market = s.get("default_market", "").strip()
        settings.skip_bad_rows = _get_bool(s, "skip_bad_rows", False)
        settings.encoding = s.get("encoding", "utf-8-sig").strip() or "utf-8-sig"
        settings.date_from_filename_regex = s.get("date_from_filename_regex", settings.date_from_filename_regex)
        settings.skip_no_trading_days = _get_bool(s, "skip_no_trading_days", True)

    if parser.has_section("formats"):
        for name, cols in parser["formats"].items():
            if name == "file":
                settings.formats_file = resolve(cols)
            else:
                settings.named_formats[name.strip()] = [c.strip() for c in cols.split(",") if c.strip()]

    if parser.has_section("dashboard"):
        s = parser["dashboard"]
        settings.dashboard_host = s.get("host", settings.dashboard_host).strip() or settings.dashboard_host
        settings.dashboard_port = _get_int(s, "port", settings.dashboard_port)
        settings.secret_key_file = resolve(s.get("secret_key_file", "data/.secret_key"))

    if settings.batch_size <= 0:
        raise ConfigError("[database] batch_size must be > 0")
    if not settings.stage_table:
        raise ConfigError("[database] stage_table must not be empty")
    names = [settings.stage_table, settings.daily_table, settings.weekly_table, settings.monthly_table]
    if len({n.lower() for n in names}) != len(names):
        raise ConfigError("[database] stage_table, daily_table, weekly_table and monthly_table must differ")

    return settings


# ---------------------------------------------------------------------------
# writing the config back (used by the dashboard's Settings page)
# ---------------------------------------------------------------------------
_CONFIG_TEMPLATE = """\
; =====================================================================
;  stock_dataload configuration
;  Relative paths are resolved against the folder containing this file.
;  This file is (re)written by the dashboard's Settings page.
; =====================================================================

[paths]
; Source files in any supported format (csv, text, xlsx, json, xml) waiting to be converted
conversion_dir = {conversion_dir}
; Source files are moved here after a successful conversion
converted_dir = {converted_dir}
; THE LOADING FOLDER: files in the DEFAULT format (<ticker>,<date>,<open>,<high>,<low>,<close>,<vol>)
loading_dir = {loading_dir}
; Successfully loaded files
archive_dir = {archive_dir}
; Files that failed to load (+ .error.txt reports)
error_dir = {error_dir}
; Deliveries dated on a no-trading day (weekend / holiday) - skipped, not loaded
skipped_dir = {skipped_dir}
; Scratch folder for the converter
work_dir = {work_dir}
log_dir = {log_dir}

[database]
; SQLite database file (created on first use)
sqlite_path = {sqlite_path}
stage_table = {stage_table}
batch_size = {batch_size}
truncate_before_load = {truncate_before_load}
; re-loading a file always deletes its previous rows first (one copy per file) - not configurable
; active tables: validated, typed daily rows and the weekly / monthly aggregates (see 'promote')
daily_table = {daily_table}
weekly_table = {weekly_table}
monthly_table = {monthly_table}

[promotion]
; validation of the promotion staging -> daily table (rejected rows are recorded in Data_Error)
; reject rows dated on a day the market was closed (weekend / US holiday / extra_holidays)
reject_market_closed_days = {reject_market_closed_days}
; reject rows whose open or close price lies outside the low..high range
check_price_range = {check_price_range}
; reject rows dated before the stock's first / after its last trade date (Stock_Symbol table, see 'symbols')
check_trading_window = {check_trading_window}
; weekly / monthly: a week / month with a trading day missing is marked incomplete and recorded in Data_Error
check_completeness = {check_completeness}

[quality]
; run the strategy-based data quality checks after every load (findings -> Data_Error table)
enabled = {quality_enabled}
; alert when a known stock has no valid data for a market-open day
alert_missing_days = {alert_missing_days}
; ad-hoc market closures, comma separated YYYY-MM-DD (weekends + US holidays are built in)
extra_holidays = {extra_holidays}

[loader]
file_patterns = {file_patterns}
market_from_filename_regex = {market_from_filename_regex}
default_market = {default_market}
skip_bad_rows = {skip_bad_rows}
encoding = {encoding}
; the trade date in the file name (NASDAQ_20250123.txt -> 2025-01-23)
date_from_filename_regex = {date_from_filename_regex}
; skip files dated on a day their market was closed (No_Trading_Day table; see 'no-trading-days')
skip_no_trading_days = {skip_no_trading_days}

[formats]
; custom file formats are kept in this JSON file (edit via the dashboard)
file = {formats_file}

[dashboard]
host = {dashboard_host}
port = {dashboard_port}
; session secret (generated on first start; or set $STOCK_DATALOAD_SECRET). Keep out of version control.
secret_key_file = {secret_key_file}
"""


def _rel(path: Path, base: Path) -> str:
    """Paths inside the config folder are written relative (keeps the project portable)."""
    try:
        return Path(path).resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return str(Path(path).resolve())


def save_settings(settings: Settings, path: str | os.PathLike | None = None) -> Path:
    """Write *settings* to an INI file (default: where they were loaded from)."""
    target = Path(path) if path else (settings.config_file or (settings.base_dir / CONFIG_FILENAME))
    base = target.resolve().parent
    text = _CONFIG_TEMPLATE.format(
        conversion_dir=_rel(settings.conversion_dir, base), converted_dir=_rel(settings.converted_dir, base),
        loading_dir=_rel(settings.loading_dir, base), archive_dir=_rel(settings.archive_dir, base),
        error_dir=_rel(settings.error_dir, base), skipped_dir=_rel(settings.skipped_dir, base),
        work_dir=_rel(settings.work_dir, base),
        log_dir=_rel(settings.log_dir, base), sqlite_path=_rel(settings.sqlite_path, base),
        stage_table=settings.stage_table, batch_size=settings.batch_size,
        truncate_before_load=str(settings.truncate_before_load).lower(),
        daily_table=settings.daily_table, weekly_table=settings.weekly_table,
        monthly_table=settings.monthly_table,
        reject_market_closed_days=str(settings.reject_market_closed_days).lower(),
        check_price_range=str(settings.check_price_range).lower(),
        check_trading_window=str(settings.check_trading_window).lower(),
        check_completeness=str(settings.check_completeness).lower(),
        quality_enabled=str(settings.quality_checks_enabled).lower(),
        alert_missing_days=str(settings.alert_missing_days).lower(),
        extra_holidays=", ".join(settings.extra_market_holidays),
        file_patterns=";".join(settings.file_patterns),
        market_from_filename_regex=settings.market_from_filename_regex,
        default_market=settings.default_market, skip_bad_rows=str(settings.skip_bad_rows).lower(),
        date_from_filename_regex=settings.date_from_filename_regex,
        skip_no_trading_days=str(settings.skip_no_trading_days).lower(),
        encoding=settings.encoding, formats_file=_rel(settings.formats_file, base),
        dashboard_host=settings.dashboard_host, dashboard_port=settings.dashboard_port,
        secret_key_file=_rel(settings.secret_key_file, base),
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(target)
    settings.config_file = target.resolve()
    settings.base_dir = base
    return target
