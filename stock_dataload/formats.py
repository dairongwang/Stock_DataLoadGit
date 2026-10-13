"""
Definition of the *default* stock data file format and helpers to recognise
column names coming from other providers.

Default format (what the loader expects in the loading folder)::

    <ticker>,<date>,<open>,<high>,<low>,<close>,<vol>
    AACG,20250123,0.9,0.9229,0.89,0.9128,2800

* comma delimited, one header line, no text qualifier
* date as YYYYMMDD
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence

# The canonical column sequence.  Order matters - this IS the default format.
DEFAULT_COLUMNS: tuple[str, ...] = ("ticker", "date", "open", "high", "low", "close", "vol")

DEFAULT_DELIMITER = ","
DEFAULT_DATE_FORMAT = "%Y%m%d"

#: Header line written by the converter / expected by the loader.
DEFAULT_HEADER_FIELDS: tuple[str, ...] = tuple(f"<{c}>" for c in DEFAULT_COLUMNS)
DEFAULT_HEADER_LINE = DEFAULT_DELIMITER.join(DEFAULT_HEADER_FIELDS)

#: Marker a user can put in an explicit column list to ignore a source column.
SKIP_COLUMN = "skip"

# Alternative spellings of the canonical columns seen in files from various
# sources (Yahoo, stooq, brokers, ...).  Keys are *normalised* names (see
# normalise_name()).  Extend this table to teach the converter new headers.
COLUMN_ALIASES: dict[str, str] = {
    # ticker
    "ticker": "ticker", "symbol": "ticker", "sym": "ticker", "stock": "ticker",
    "code": "ticker", "name": "ticker", "instrument": "ticker", "security": "ticker",
    # date
    "date": "date", "tradedate": "date", "tradingdate": "date", "dt": "date",
    "day": "date", "timestamp": "date", "time": "date", "period": "date",
    # open
    "open": "open", "o": "open", "openprice": "open", "openingprice": "open", "px_open": "open",
    # high
    "high": "high", "h": "high", "highprice": "high", "max": "high", "px_high": "high",
    # low
    "low": "low", "l": "low", "lowprice": "low", "min": "low", "px_low": "low",
    # close
    "close": "close", "c": "close", "closeprice": "close", "closingprice": "close",
    "last": "close", "px_close": "close", "px_last": "close",
    # vol
    "vol": "vol", "volume": "vol", "v": "vol", "qty": "vol", "quantity": "vol",
    "shares": "vol", "sharestraded": "vol", "tradedvolume": "vol",
}

_NORMALISE_RE = re.compile(r"[^a-z0-9_]")


def normalise_name(name: str) -> str:
    """Normalise a header field for alias look-up.

    ``"<Ticker>"`` / ``" Ticker "`` / ``"TICKER"`` / ``"Trade Date"`` all
    become ``"ticker"`` / ``"tradedate"``.
    """
    cleaned = name.strip().strip("<>[]\"'").strip().lower()
    cleaned = cleaned.replace("-", "").replace(" ", "").replace(".", "")
    return _NORMALISE_RE.sub("", cleaned)


def resolve_column(name: str) -> str | None:
    """Map an arbitrary header field to one of ``DEFAULT_COLUMNS`` (or None)."""
    if name is None:
        return None
    raw = name.strip().lower()
    if raw == SKIP_COLUMN or raw in ("", "_", "-", "ignore", "none"):
        return SKIP_COLUMN
    found = COLUMN_ALIASES.get(normalise_name(name))
    if found is None and "." in raw:
        # flattened JSON/XML keys such as "prices.open" or "quote.symbol"
        found = COLUMN_ALIASES.get(normalise_name(raw.rsplit(".", 1)[-1]))
    return found


def is_default_header(fields: Sequence[str]) -> bool:
    """True if *fields* is exactly the default column sequence.

    Angle brackets, case and surrounding whitespace are ignored so that
    ``ticker,date,...`` and ``<ticker>,<date>,...`` are both accepted.
    """
    if len(fields) != len(DEFAULT_COLUMNS):
        return False
    return tuple(normalise_name(f) for f in fields) == DEFAULT_COLUMNS


def describe_header(fields: Iterable[str]) -> str:
    return DEFAULT_DELIMITER.join(str(f) for f in fields)


# ---------------------------------------------------------------------------
# Row level validation rules for the default format
# ---------------------------------------------------------------------------
_DATE_RE = re.compile(r"^\d{8}$")
_NUMBER_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


def validate_default_row(row: Sequence[str]) -> str | None:
    """Return an error message if *row* is not a valid default-format row.

    Rules are intentionally light - this is a *staging* load.  We only check
    shape and that the numeric columns are numeric, so obviously broken lines
    are caught before they hit the database.
    """
    if len(row) != len(DEFAULT_COLUMNS):
        return f"expected {len(DEFAULT_COLUMNS)} columns, found {len(row)}"
    ticker, date, open_, high, low, close, vol = (v.strip() for v in row)
    if not ticker:
        return "ticker is empty"
    if not _DATE_RE.match(date):
        return f"date '{date}' is not in YYYYMMDD format"
    for label, value in (("open", open_), ("high", high), ("low", low), ("close", close)):
        if value and not _NUMBER_RE.match(value):
            return f"{label} '{value}' is not numeric"
    if vol and not _NUMBER_RE.match(vol):
        return f"vol '{vol}' is not numeric"
    return None
