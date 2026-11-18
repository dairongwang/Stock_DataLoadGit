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


#: token packs sold on the portal: (name, tokens, price in cents)
DEFAULT_TOKEN_PACKS: tuple[tuple[str, int, int], ...] = (
    ("Starter", 100, 500),
    ("Standard", 500, 2000),
    ("Pro", 2000, 6000),
    ("Enterprise", 10000, 25000),
)

PAYMENT_METHODS = ("stripe", "paypal")
PAYMENT_GATEWAYS = ("live", "fake")
OAUTH_PROVIDERS = ("google", "apple", "facebook")
OAUTH_GATEWAYS = ("live", "fake")


def parse_list(text: str, allowed: tuple[str, ...], what: str) -> tuple[str, ...]:
    """``"stripe, paypal"`` -> ``("stripe", "paypal")``, validated against *allowed* (empty -> ())."""
    out: list[str] = []
    for item in (text or "").replace(";", ",").split(","):
        item = item.strip().lower()
        if not item:
            continue
        if item not in allowed:
            raise ConfigError(f"{what}: '{item}' is not one of {', '.join(allowed)}")
        if item not in out:
            out.append(item)
    return tuple(out)
MAIL_PROVIDERS = ("console", "memory", "smtp")
SMS_PROVIDERS = ("console", "memory", "twilio")
EOD_API_GATEWAYS = ("live", "fake")
#: the end-of-day data API (``[eod_api]``, see eod_api.py)
DEFAULT_EOD_API_BASE_URL = "https://eodhd.com/api/eod/"
DEFAULT_SYMBOL_LIST_URL = "https://eodhd.com/api/exchange-symbol-list/"
DEFAULT_BULK_URL = "https://eodhd.com/api/eod-bulk-last-day/"
ENV_EOD_API_TOKEN = "STOCK_DATALOAD_EOD_API_TOKEN"


def parse_token_packs(text: str) -> tuple[tuple[str, int, int], ...]:
    """``Starter:100:5.00, Pro:2000:60`` -> (("Starter", 100, 500), ("Pro", 2000, 6000))."""
    packs: list[tuple[str, int, int]] = []
    names: set[str] = set()
    for item in (text or "").replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        parts = [p.strip() for p in item.split(":")]
        if len(parts) != 3 or not parts[0]:
            raise ConfigError(f"[portal] packs: expected name:tokens:price, got '{item}'")
        name, tokens_text, price_text = parts
        try:
            tokens = int(tokens_text)
            cents = int(round(float(price_text) * 100))
        except ValueError as exc:
            raise ConfigError(f"[portal] packs: '{item}' - tokens must be an integer and the price a number") from exc
        if tokens <= 0 or cents < 0:
            raise ConfigError(f"[portal] packs: '{item}' - tokens must be > 0 and the price >= 0")
        if name.lower() in names:
            raise ConfigError(f"[portal] packs: pack '{name}' is listed twice")
        names.add(name.lower())
        packs.append((name, tokens, cents))
    if not packs:
        raise ConfigError("[portal] packs: at least one pack is required")
    return tuple(packs)


def format_token_packs(packs) -> str:
    return ", ".join(f"{name}:{tokens}:{cents / 100:.2f}" for name, tokens, cents in packs)


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

    # --- customer portal + data API (portal.py / dataapi.py) ---------------------
    # the public site: token shop, customer accounts and the token-metered data API
    portal_host: str = "127.0.0.1"
    portal_port: int = 8060
    # public base URL used in links / the API documentation (empty = the request's host)
    portal_public_url: str = ""
    # payment methods offered on the purchase page: stripe (card via Stripe Checkout), paypal
    payment_methods: tuple[str, ...] = ("stripe", "paypal")
    # live = the real Stripe / PayPal APIs | fake = the built-in stub of both (development / tests)
    payment_gateway: str = "fake"
    currency: str = "USD"
    # token packs on sale: (name, tokens, price in cents)
    token_packs: tuple[tuple[str, int, int], ...] = DEFAULT_TOKEN_PACKS
    # tokens charged per ticker and year returned by the data API
    tokens_per_query: int = 1
    # how many tickers one data API request may ask for
    max_tickers_per_request: int = 50
    # Stripe secret key - prefer $STOCK_DATALOAD_STRIPE_SECRET_KEY over the config file
    stripe_secret_key: str = ""
    # PayPal REST app credentials (secret: prefer $STOCK_DATALOAD_PAYPAL_CLIENT_SECRET); sandbox | live
    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    paypal_environment: str = "sandbox"

    # --- sign in with Google / Apple / Facebook (oauth.py) --------------------------------
    # providers offered on the sign-in page (empty = none)
    oauth_providers: tuple[str, ...] = ()
    # live = the real identity providers | fake = the built-in stub identity provider (development / tests)
    oauth_gateway: str = "fake"
    google_client_id: str = ""
    google_client_secret: str = ""       # or $STOCK_DATALOAD_GOOGLE_CLIENT_SECRET
    facebook_app_id: str = ""
    facebook_app_secret: str = ""        # or $STOCK_DATALOAD_FACEBOOK_APP_SECRET
    apple_client_id: str = ""            # the Services ID
    apple_client_secret: str = ""        # the signed JWT client secret, or $STOCK_DATALOAD_APPLE_CLIENT_SECRET

    # --- accounts: e-mail verification, two-factor authentication, password reset ---
    # name shown in authenticator apps and messages
    issuer: str = "Stock Data API"
    # a new portal account must confirm its e-mail address (code) before it can sign in
    require_email_verification: bool = True
    # portal administrators must have two-factor authentication before the admin pages open
    admin_requires_2fa: bool = True
    # the dashboard's sign-in page offers "Create account" (new users get dashboard_registration_role)
    dashboard_registration: bool = True
    dashboard_registration_role: str = "viewer"
    # validity of e-mail / sms codes and of password-reset links
    code_ttl_minutes: int = 10
    reset_ttl_minutes: int = 60

    # --- outbound messages (messaging.py) ---------------------------------------------
    # mail: console (STUB - printed to the console) | memory (STUB for tests) | smtp
    mail_provider: str = "console"
    mail_from: str = "no-reply@localhost"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""          # or $STOCK_DATALOAD_SMTP_PASSWORD
    smtp_starttls: bool = True
    # sms: console (STUB) | memory (STUB for tests) | twilio
    sms_provider: str = "console"
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""      # or $STOCK_DATALOAD_TWILIO_AUTH_TOKEN
    twilio_from: str = ""

    # --- end-of-day data API downloads (eod_api.py) -----------------------------------
    # the request is <base_url><SYMBOL>?api_token=<token>&fmt=<fmt>; a base_url containing
    # "{symbol}" is used as a template instead (e.g. https://host/api/eod/{symbol}.US)
    eod_api_base_url: str = DEFAULT_EOD_API_BASE_URL
    # placeholder - put the real token in $STOCK_DATALOAD_EOD_API_TOKEN (it wins over this value)
    eod_api_token: str = "dummy"
    # response format asked for with fmt=... (json, csv, ...)
    eod_api_fmt: str = "json"
    # further query parameters sent with every request, e.g. "period=d&order=a"
    eod_api_extra_params: str = ""
    # where the responses are saved; file name template ({symbol} {fmt} {ext} {date} {time} {timestamp})
    eod_download_dir: Path = PROJECT_ROOT / "data" / "api_downloads"
    eod_file_name: str = "{symbol}_{timestamp}.{ext}"
    eod_api_timeout: int = 30
    # live = call the API | fake = built-in STUB answering with docs/SampleAPI_JasonData.txt (no network)
    eod_api_gateway: str = "live"
    # the staging table of the API data (eod_stage.py) and whether eod-fetch loads into it
    eod_stage_table: str = "Stock_EOD_Stage"
    eod_load_to_stage: bool = True
    # promote eod: the market of symbols neither in Stock_Symbol nor in the daily table (empty = reject them)
    eod_default_market: str = ""
    # exchange symbol lists (eod_symbols.py): <symbol_list_url><MARKET>?api_token=<token>&fmt=<fmt>;
    # a symbol_list_url containing "{market}" is used as a template. Same token as the price calls.
    eod_symbol_list_url: str = DEFAULT_SYMBOL_LIST_URL
    eod_symbol_list_fmt: str = "json"
    eod_symbol_list_file_name: str = "{market}_symbols_{timestamp}.{ext}"
    eod_symbol_stage_table: str = "Stock_Symbol_Stage"
    # one trading day of every stock of a market (eod_bulk.py, eod-bulk-fetch):
    # <bulk_url><MARKET>?api_token=<token>&fmt=<bulk_fmt>&date=YYYY-MM-DD ("{market}" = template); same token
    eod_bulk_url: str = DEFAULT_BULK_URL
    eod_bulk_fmt: str = "json"
    # file name of the saved answer: {market} {day} (the trade date asked for, or "last") {fmt} {ext} {date} {time} {timestamp}
    eod_bulk_file_name: str = "{market}_bulk_{day}_{timestamp}.{ext}"
    # whole markets (eod_market.py, eod-fetch-market): one API call per stock, loaded straight into the stage table
    # a failed call (network, timeout, 429, 5xx, unreadable answer) is repeated up to `retries` times per stock,
    # waiting retry_backoff * 2^(n-1) seconds (a Retry-After of the API wins)
    eod_api_retries: int = 3
    eod_api_retry_backoff: float = 1.0
    # seconds to wait between two stocks (respect the API's rate limit)
    eod_request_pause: float = 0.0
    # the symbol types of the exchange list that are fetched (empty = every type)
    eod_market_types: tuple[str, ...] = ("Common Stock",)
    # stop the run after this many stocks failed in a row (the API is probably down)
    eod_max_consecutive_failures: int = 20

    # where the settings came from (None = built-in defaults)
    config_file: Path | None = None

    # ---------------------------------------------------------------------
    @property
    def folders(self) -> dict[str, Path]:
        return {
            "conversion_dir": self.conversion_dir, "converted_dir": self.converted_dir,
            "loading_dir": self.loading_dir, "archive_dir": self.archive_dir,
            "error_dir": self.error_dir, "skipped_dir": self.skipped_dir, "work_dir": self.work_dir,
            "log_dir": self.log_dir, "eod_download_dir": self.eod_download_dir,
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


def _get_float(section: configparser.SectionProxy, key: str, default: float) -> float:
    try:
        return section.getfloat(key, fallback=default)
    except ValueError as exc:
        raise ConfigError(f"[{section.name}] {key}: expected a number ({exc})") from exc


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
    settings.eod_download_dir = base / "data" / "api_downloads"
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

    if parser.has_section("portal"):
        s = parser["portal"]
        settings.portal_host = s.get("host", settings.portal_host).strip() or settings.portal_host
        settings.portal_port = _get_int(s, "port", settings.portal_port)
        settings.portal_public_url = s.get("public_url", "").strip().rstrip("/")
        settings.payment_methods = parse_list(s.get("payment_methods", "stripe, paypal"), PAYMENT_METHODS,
                                              "[portal] payment_methods")
        settings.payment_gateway = s.get("payment_gateway", "fake").strip().lower() or "fake"
        settings.currency = s.get("currency", "USD").strip().upper() or "USD"
        packs = s.get("packs", "").strip()
        if packs:
            settings.token_packs = parse_token_packs(packs)
        settings.tokens_per_query = _get_int(s, "tokens_per_query", 1)
        settings.max_tickers_per_request = _get_int(s, "max_tickers_per_request", 50)
        settings.stripe_secret_key = s.get("stripe_secret_key", "").strip()
        settings.paypal_client_id = s.get("paypal_client_id", "").strip()
        settings.paypal_client_secret = s.get("paypal_client_secret", "").strip()
        settings.paypal_environment = s.get("paypal_environment", "sandbox").strip().lower() or "sandbox"
    env_stripe = os.environ.get("STOCK_DATALOAD_STRIPE_SECRET_KEY")
    if env_stripe:
        settings.stripe_secret_key = env_stripe
    env_paypal = os.environ.get("STOCK_DATALOAD_PAYPAL_CLIENT_SECRET")
    if env_paypal:
        settings.paypal_client_secret = env_paypal

    if parser.has_section("oauth"):
        s = parser["oauth"]
        settings.oauth_providers = parse_list(s.get("providers", ""), OAUTH_PROVIDERS, "[oauth] providers")
        settings.oauth_gateway = s.get("gateway", "fake").strip().lower() or "fake"
        settings.google_client_id = s.get("google_client_id", "").strip()
        settings.google_client_secret = s.get("google_client_secret", "").strip()
        settings.facebook_app_id = s.get("facebook_app_id", "").strip()
        settings.facebook_app_secret = s.get("facebook_app_secret", "").strip()
        settings.apple_client_id = s.get("apple_client_id", "").strip()
        settings.apple_client_secret = s.get("apple_client_secret", "").strip()
    for attr, env in (("google_client_secret", "STOCK_DATALOAD_GOOGLE_CLIENT_SECRET"),
                      ("facebook_app_secret", "STOCK_DATALOAD_FACEBOOK_APP_SECRET"),
                      ("apple_client_secret", "STOCK_DATALOAD_APPLE_CLIENT_SECRET")):
        if os.environ.get(env):
            setattr(settings, attr, os.environ[env])

    if parser.has_section("accounts"):
        s = parser["accounts"]
        settings.issuer = s.get("issuer", settings.issuer).strip() or settings.issuer
        settings.require_email_verification = _get_bool(s, "require_email_verification", True)
        settings.admin_requires_2fa = _get_bool(s, "admin_requires_2fa", True)
        settings.dashboard_registration = _get_bool(s, "dashboard_registration", True)
        settings.dashboard_registration_role = s.get("dashboard_registration_role", "viewer").strip().lower() or "viewer"
        settings.code_ttl_minutes = _get_int(s, "code_ttl_minutes", 10)
        settings.reset_ttl_minutes = _get_int(s, "reset_ttl_minutes", 60)

    if parser.has_section("mail"):
        s = parser["mail"]
        settings.mail_provider = s.get("provider", "console").strip().lower() or "console"
        settings.mail_from = s.get("from_address", settings.mail_from).strip() or settings.mail_from
        settings.smtp_host = s.get("smtp_host", "").strip()
        settings.smtp_port = _get_int(s, "smtp_port", 587)
        settings.smtp_user = s.get("smtp_user", "").strip()
        settings.smtp_password = s.get("smtp_password", "").strip()
        settings.smtp_starttls = _get_bool(s, "smtp_starttls", True)
    env_smtp = os.environ.get("STOCK_DATALOAD_SMTP_PASSWORD")
    if env_smtp:
        settings.smtp_password = env_smtp

    if parser.has_section("sms"):
        s = parser["sms"]
        settings.sms_provider = s.get("provider", "console").strip().lower() or "console"
        settings.twilio_account_sid = s.get("twilio_account_sid", "").strip()
        settings.twilio_auth_token = s.get("twilio_auth_token", "").strip()
        settings.twilio_from = s.get("twilio_from", "").strip()
    env_twilio = os.environ.get("STOCK_DATALOAD_TWILIO_AUTH_TOKEN")
    if env_twilio:
        settings.twilio_auth_token = env_twilio

    if parser.has_section("eod_api"):
        s = parser["eod_api"]
        settings.eod_api_base_url = s.get("base_url", DEFAULT_EOD_API_BASE_URL).strip() or DEFAULT_EOD_API_BASE_URL
        # an empty token stays empty (the environment variable may supply it)
        settings.eod_api_token = s.get("api_token", settings.eod_api_token).strip()
        settings.eod_api_fmt = s.get("fmt", "json").strip().lower() or "json"
        settings.eod_api_extra_params = s.get("extra_params", "").strip()
        settings.eod_download_dir = resolve(s.get("download_dir", "data/api_downloads").strip() or "data/api_downloads")
        settings.eod_file_name = s.get("file_name", settings.eod_file_name).strip() or settings.eod_file_name
        settings.eod_api_timeout = _get_int(s, "timeout", 30)
        settings.eod_api_gateway = s.get("gateway", "live").strip().lower() or "live"
        settings.eod_stage_table = s.get("stage_table", "Stock_EOD_Stage").strip() or "Stock_EOD_Stage"
        settings.eod_load_to_stage = _get_bool(s, "load_to_stage", True)
        settings.eod_default_market = s.get("default_market", "").strip().upper()
        settings.eod_symbol_list_url = s.get("symbol_list_url", DEFAULT_SYMBOL_LIST_URL).strip() or DEFAULT_SYMBOL_LIST_URL
        settings.eod_symbol_list_fmt = s.get("symbol_list_fmt", "json").strip().lower() or "json"
        settings.eod_symbol_list_file_name = (s.get("symbol_list_file_name", settings.eod_symbol_list_file_name).strip()
                                              or settings.eod_symbol_list_file_name)
        settings.eod_symbol_stage_table = s.get("symbol_stage_table", "Stock_Symbol_Stage").strip() or "Stock_Symbol_Stage"
        settings.eod_bulk_url = s.get("bulk_url", DEFAULT_BULK_URL).strip() or DEFAULT_BULK_URL
        settings.eod_bulk_fmt = s.get("bulk_fmt", "json").strip().lower() or "json"
        settings.eod_bulk_file_name = s.get("bulk_file_name", settings.eod_bulk_file_name).strip() or settings.eod_bulk_file_name
        settings.eod_api_retries = _get_int(s, "retries", settings.eod_api_retries)
        settings.eod_api_retry_backoff = _get_float(s, "retry_backoff", settings.eod_api_retry_backoff)
        settings.eod_request_pause = _get_float(s, "request_pause", settings.eod_request_pause)
        if "market_types" in s:
            settings.eod_market_types = tuple(t.strip() for t in s.get("market_types", "").split(",") if t.strip())
        settings.eod_max_consecutive_failures = _get_int(s, "max_consecutive_failures",
                                                         settings.eod_max_consecutive_failures)

    if settings.batch_size <= 0:
        raise ConfigError("[database] batch_size must be > 0")
    if settings.payment_gateway not in PAYMENT_GATEWAYS:
        raise ConfigError(f"[portal] payment_gateway must be one of {', '.join(PAYMENT_GATEWAYS)}")
    if not settings.payment_methods:
        raise ConfigError("[portal] payment_methods must list at least one of " + ", ".join(PAYMENT_METHODS))
    if settings.paypal_environment not in ("sandbox", "live"):
        raise ConfigError("[portal] paypal_environment must be sandbox or live")
    if settings.oauth_gateway not in OAUTH_GATEWAYS:
        raise ConfigError(f"[oauth] gateway must be one of {', '.join(OAUTH_GATEWAYS)}")
    if settings.tokens_per_query <= 0:
        raise ConfigError("[portal] tokens_per_query must be > 0")
    if settings.max_tickers_per_request <= 0:
        raise ConfigError("[portal] max_tickers_per_request must be > 0")
    if settings.mail_provider not in MAIL_PROVIDERS:
        raise ConfigError(f"[mail] provider must be one of {', '.join(MAIL_PROVIDERS)}")
    if settings.sms_provider not in SMS_PROVIDERS:
        raise ConfigError(f"[sms] provider must be one of {', '.join(SMS_PROVIDERS)}")
    if settings.code_ttl_minutes <= 0 or settings.reset_ttl_minutes <= 0:
        raise ConfigError("[accounts] code_ttl_minutes and reset_ttl_minutes must be > 0")
    if settings.dashboard_registration_role not in ("viewer", "operator"):
        raise ConfigError("[accounts] dashboard_registration_role must be viewer or operator")
    if not settings.eod_api_base_url.lower().startswith(("http://", "https://")):
        raise ConfigError("[eod_api] base_url must start with http:// or https://")
    if not settings.eod_api_fmt.isalnum():
        raise ConfigError("[eod_api] fmt must be a single word such as json or csv")
    if settings.eod_api_timeout <= 0:
        raise ConfigError("[eod_api] timeout must be > 0 seconds")
    if settings.eod_api_gateway not in EOD_API_GATEWAYS:
        raise ConfigError(f"[eod_api] gateway must be one of {', '.join(EOD_API_GATEWAYS)}")
    if not settings.stage_table:
        raise ConfigError("[database] stage_table must not be empty")
    names = [settings.stage_table, settings.daily_table, settings.weekly_table, settings.monthly_table]
    if len({n.lower() for n in names}) != len(names):
        raise ConfigError("[database] stage_table, daily_table, weekly_table and monthly_table must differ")
    if settings.eod_stage_table.lower() in {n.lower() for n in names}:
        raise ConfigError("[eod_api] stage_table must differ from the [database] table names")
    if settings.eod_symbol_stage_table.lower() in {n.lower() for n in names + [settings.eod_stage_table, "stock_symbol"]}:
        raise ConfigError("[eod_api] symbol_stage_table must differ from the other table names")
    if not settings.eod_symbol_list_url.lower().startswith(("http://", "https://")):
        raise ConfigError("[eod_api] symbol_list_url must start with http:// or https://")
    if not settings.eod_symbol_list_fmt.isalnum():
        raise ConfigError("[eod_api] symbol_list_fmt must be a single word such as json or csv")
    if not settings.eod_bulk_url.lower().startswith(("http://", "https://")):
        raise ConfigError("[eod_api] bulk_url must start with http:// or https://")
    if settings.eod_bulk_fmt not in ("json", "csv"):
        raise ConfigError("[eod_api] bulk_fmt must be json or csv")
    if not 0 <= settings.eod_api_retries <= 10:
        raise ConfigError("[eod_api] retries must be between 0 and 10")
    if not 0 <= settings.eod_api_retry_backoff <= 300 or not 0 <= settings.eod_request_pause <= 60:
        raise ConfigError("[eod_api] retry_backoff must be 0..300 and request_pause 0..60 seconds")
    if settings.eod_max_consecutive_failures < 1:
        raise ConfigError("[eod_api] max_consecutive_failures must be at least 1")

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

[portal]
; the public site: token shop, customer accounts and the token-metered data API
; (python -m stock_dataload portal). Bind to 0.0.0.0 / put it behind a TLS proxy to publish it.
host = {portal_host}
port = {portal_port}
; public base URL used in links and the API documentation (empty = the address of the request)
public_url = {portal_public_url}
; payment methods offered on the purchase page: stripe (card via Stripe Checkout), paypal
payment_methods = {payment_methods}
; live = the real Stripe / PayPal APIs | fake = the built-in STUB of both APIs (development / tests; nothing is charged)
payment_gateway = {payment_gateway}
currency = {currency}
; token packs on sale: name:tokens:price, comma separated
packs = {token_packs}
; tokens charged per ticker and year returned by the data API
tokens_per_query = {tokens_per_query}
; how many tickers one data API request may ask for
max_tickers_per_request = {max_tickers_per_request}
; Stripe: the secret key (or $STOCK_DATALOAD_STRIPE_SECRET_KEY)
stripe_secret_key = {stripe_secret_key}
; PayPal: REST app credentials (secret or $STOCK_DATALOAD_PAYPAL_CLIENT_SECRET); environment sandbox | live
paypal_client_id = {paypal_client_id}
paypal_client_secret = {paypal_client_secret}
paypal_environment = {paypal_environment}

[oauth]
; sign in with Google / Apple / Facebook on the portal: providers offered (comma separated; empty = none)
providers = {oauth_providers}
; live = the real identity providers | fake = the built-in STUB identity provider (development / tests)
gateway = {oauth_gateway}
; redirect URIs to register with the providers: <public_url>/oauth/<provider>/callback
google_client_id = {google_client_id}
google_client_secret = {google_client_secret}
facebook_app_id = {facebook_app_id}
facebook_app_secret = {facebook_app_secret}
; Apple: the Services ID and the signed client-secret JWT (see README)
apple_client_id = {apple_client_id}
apple_client_secret = {apple_client_secret}

[accounts]
; e-mail verification, two-factor authentication (authenticator app / e-mail / sms) and password reset
; name shown in authenticator apps and in messages
issuer = {issuer}
; a new portal account must confirm its e-mail address with a code before it can sign in
require_email_verification = {require_email_verification}
; portal administrators must have two-factor authentication before the admin pages open
admin_requires_2fa = {admin_requires_2fa}
; the dashboard's sign-in page offers "Create account"; new users get this role (viewer | operator)
dashboard_registration = {dashboard_registration}
dashboard_registration_role = {dashboard_registration_role}
; validity of e-mail / sms codes and of password-reset links, in minutes
code_ttl_minutes = {code_ttl_minutes}
reset_ttl_minutes = {reset_ttl_minutes}

[mail]
; how e-mails (verification / sign-in codes, password-reset links) are sent:
; console = STUB, printed to the console and the log | memory = STUB for tests | smtp = a real mail server
provider = {mail_provider}
from_address = {mail_from}
smtp_host = {smtp_host}
smtp_port = {smtp_port}
smtp_user = {smtp_user}
; or set $STOCK_DATALOAD_SMTP_PASSWORD
smtp_password = {smtp_password}
smtp_starttls = {smtp_starttls}

[sms]
; how text messages (sign-in codes) are sent: console = STUB | memory = STUB for tests | twilio
provider = {sms_provider}
twilio_account_sid = {twilio_account_sid}
; or set $STOCK_DATALOAD_TWILIO_AUTH_TOKEN
twilio_auth_token = {twilio_auth_token}
twilio_from = {twilio_from}

[eod_api]
; end-of-day price downloads: python -m stock_dataload eod-fetch MRK [AAPL ...]
; request = <base_url><SYMBOL>?api_token=<api_token>&fmt=<fmt>[&<extra_params>]
; (a base_url containing {{symbol}} is used as a template, e.g. https://host/api/eod/{{symbol}}.US)
base_url = {eod_api_base_url}
; placeholder token - set the real one in $STOCK_DATALOAD_EOD_API_TOKEN (it wins over this value)
api_token = {eod_api_token}
; response format passed as fmt=...: json | csv | ... (also the extension of the saved file)
fmt = {eod_api_fmt}
; more query parameters for every request, e.g. period=d&order=a&from=2025-01-01
extra_params = {eod_api_extra_params}
; the responses are saved here, one file per symbol and request
download_dir = {eod_download_dir}
; file name template: {{symbol}} {{fmt}} {{ext}} {{date}} {{time}} {{timestamp}}
file_name = {eod_file_name}
; seconds to wait for the API
timeout = {eod_api_timeout}
; live = call the API | fake = built-in STUB answering with docs/SampleAPI_JasonData.txt (no network)
gateway = {eod_api_gateway}
; staging table of the API data: date, open, high, low, close, adjusted_close, volume per symbol and day
stage_table = {eod_stage_table}
; eod-fetch loads every json / csv response into stage_table right after saving it (eod-load loads saved files)
load_to_stage = {eod_load_to_stage}
; promote eod (Stock_EOD_Stage -> daily table): the market of symbols that are neither in the symbol table
; nor in the daily table yet, e.g. NYSE (empty = such rows are rejected as market_unknown)
default_market = {eod_default_market}
; exchange symbol lists: python -m stock_dataload eod-symbols-fetch NYSE [LSE ...] (same api_token)
; request = <symbol_list_url><MARKET>?api_token=<api_token>&fmt=<symbol_list_fmt> ({{market}} = template)
symbol_list_url = {eod_symbol_list_url}
symbol_list_fmt = {eod_symbol_list_fmt}
; file name template: {{market}} {{fmt}} {{ext}} {{date}} {{time}} {{timestamp}}
symbol_list_file_name = {eod_symbol_list_file_name}
; staging table of the lists: Code, Name, Country, Exchange, Currency, Type, Isin per market and code
symbol_stage_table = {eod_symbol_stage_table}
; one trading day of EVERY stock of a market in one call: python -m stock_dataload eod-bulk-fetch US --date 2026-10-12
; request = <bulk_url><MARKET>?api_token=<api_token>&fmt=<bulk_fmt>&date=YYYY-MM-DD ({{market}} = template), loaded
; into stage_table; counts as a whole-market load (at most 3 a day per market)
bulk_url = {eod_bulk_url}
; json | csv
bulk_fmt = {eod_bulk_fmt}
; the saved answer: {{market}} {{day}} (the trade date asked for, or "last") {{fmt}} {{ext}} {{date}} {{time}} {{timestamp}}
bulk_file_name = {eod_bulk_file_name}
; whole markets: python -m stock_dataload eod-fetch-market NYSE - one call per stock of the market's symbol list,
; each answer loaded straight into stage_table (no file is saved)
; a failed call (network, timeout, 429, 5xx, unreadable answer) is repeated up to this many times per stock
retries = {eod_api_retries}
; seconds before the first repeat; doubled for every further one (a Retry-After of the API wins)
retry_backoff = {eod_api_retry_backoff}
; seconds to wait between two stocks (the API's rate limit)
request_pause = {eod_request_pause}
; the symbol types of the exchange list that are fetched, comma separated (empty = every type, incl. ETF, FUND ...)
market_types = {eod_market_types}
; stop a run after this many stocks failed in a row (the API is probably down); continue later with --resume
max_consecutive_failures = {eod_max_consecutive_failures}
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
        portal_host=settings.portal_host, portal_port=settings.portal_port,
        portal_public_url=settings.portal_public_url, payment_methods=", ".join(settings.payment_methods),
        payment_gateway=settings.payment_gateway,
        currency=settings.currency, token_packs=format_token_packs(settings.token_packs),
        tokens_per_query=settings.tokens_per_query, max_tickers_per_request=settings.max_tickers_per_request,
        stripe_secret_key=settings.stripe_secret_key, paypal_client_id=settings.paypal_client_id,
        paypal_client_secret=settings.paypal_client_secret, paypal_environment=settings.paypal_environment,
        oauth_providers=", ".join(settings.oauth_providers), oauth_gateway=settings.oauth_gateway,
        google_client_id=settings.google_client_id, google_client_secret=settings.google_client_secret,
        facebook_app_id=settings.facebook_app_id, facebook_app_secret=settings.facebook_app_secret,
        apple_client_id=settings.apple_client_id, apple_client_secret=settings.apple_client_secret,
        issuer=settings.issuer, require_email_verification=str(settings.require_email_verification).lower(),
        admin_requires_2fa=str(settings.admin_requires_2fa).lower(), code_ttl_minutes=settings.code_ttl_minutes,
        dashboard_registration=str(settings.dashboard_registration).lower(),
        dashboard_registration_role=settings.dashboard_registration_role,
        reset_ttl_minutes=settings.reset_ttl_minutes,
        mail_provider=settings.mail_provider, mail_from=settings.mail_from, smtp_host=settings.smtp_host,
        smtp_port=settings.smtp_port, smtp_user=settings.smtp_user, smtp_password=settings.smtp_password,
        smtp_starttls=str(settings.smtp_starttls).lower(),
        sms_provider=settings.sms_provider, twilio_account_sid=settings.twilio_account_sid,
        twilio_auth_token=settings.twilio_auth_token, twilio_from=settings.twilio_from,
        eod_api_base_url=settings.eod_api_base_url, eod_api_token=settings.eod_api_token,
        eod_api_fmt=settings.eod_api_fmt, eod_api_extra_params=settings.eod_api_extra_params,
        eod_download_dir=_rel(settings.eod_download_dir, base),
        eod_file_name=settings.eod_file_name,
        eod_api_timeout=settings.eod_api_timeout, eod_api_gateway=settings.eod_api_gateway,
        eod_stage_table=settings.eod_stage_table, eod_load_to_stage=str(settings.eod_load_to_stage).lower(),
        eod_default_market=settings.eod_default_market,
        eod_symbol_list_url=settings.eod_symbol_list_url, eod_symbol_list_fmt=settings.eod_symbol_list_fmt,
        eod_symbol_list_file_name=settings.eod_symbol_list_file_name,
        eod_symbol_stage_table=settings.eod_symbol_stage_table,
        eod_bulk_url=settings.eod_bulk_url, eod_bulk_fmt=settings.eod_bulk_fmt,
        eod_bulk_file_name=settings.eod_bulk_file_name,
        eod_api_retries=settings.eod_api_retries, eod_api_retry_backoff=f"{settings.eod_api_retry_backoff:g}",
        eod_request_pause=f"{settings.eod_request_pause:g}", eod_market_types=", ".join(settings.eod_market_types),
        eod_max_consecutive_failures=settings.eod_max_consecutive_failures,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(target)
    settings.config_file = target.resolve()
    settings.base_dir = base
    return target
