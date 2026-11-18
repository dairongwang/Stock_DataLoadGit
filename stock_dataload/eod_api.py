"""
Download **end-of-day prices** for a stock symbol from an EOD data API
(default: EODHD) and save the response in a local folder.

    python -m stock_dataload eod-fetch MRK                  # -> data/api_downloads/MRK_20251114_101500.json
    python -m stock_dataload eod-fetch MRK AAPL.US --fmt csv --from 2025-01-01

    from stock_dataload.eod_api import fetch_eod
    result = fetch_eod("MRK")                               # settings from config.ini
    print(result.path, result.records)

The request is built from configuration (``[eod_api]`` in ``config.ini``) so
every part can change without touching the code::

    <base_url><SYMBOL>?api_token=<api_token>&fmt=<fmt>[&<extra_params>][&from=..&to=..]
    https://eodhd.com/api/eod/MRK?api_token=dummy&fmt=json

``base_url``      the API address (default ``https://eodhd.com/api/eod/``); if it
                  contains ``{symbol}`` it is used as a template instead of
                  appending the symbol (e.g. ``https://host/api/eod/{symbol}.US``)
``api_token``     the token; ``$STOCK_DATALOAD_EOD_API_TOKEN`` wins over the file so
                  the real token never has to be written into ``config.ini``.
                  It is never logged - URLs in logs and messages show ``api_token=***``
``fmt``           ``json`` (default), ``csv`` or any other format the API offers;
                  it is also the extension of the saved file
``extra_params``  further query parameters for every request (``period=d&order=a``)
``download_dir``  where the responses are saved (default ``data/api_downloads``)
``file_name``     the file name template - ``{symbol}``, ``{fmt}``, ``{ext}``,
                  ``{date}``, ``{time}``, ``{timestamp}``
``gateway``       ``live`` (default) calls the API; ``fake`` is a built-in STUB that
                  answers with ``docs/SampleAPI_JasonData.txt`` (no network, no token)

Every argument of :func:`fetch_eod` / :meth:`EodApiClient.fetch` overrides the
configured value for one call (``fmt="csv"``, ``api_token=...``,
``base_url=...``, ``params={"from": "2025-01-01"}``).

The body is saved **exactly as received** (written to ``<name>.part`` first,
then renamed, so a half-written file never appears).  A JSON / CSV answer is
then loaded into the staging table ``Stock_EOD_Stage`` (``[eod_api]
stage_table``; see :mod:`eod_stage`) unless ``load_to_stage = false`` /
``load=False``.  A JSON answer is checked
to be valid JSON before it is saved; an HTTP error (401 bad token, 404 unknown
symbol, ...) raises :class:`EodApiError` and saves nothing.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import random
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping

from .config import ENV_EOD_API_TOKEN, PROJECT_ROOT, Settings, load_settings

log = logging.getLogger(__name__)

GATEWAY_LIVE = "live"
GATEWAY_FAKE = "fake"
#: the sample answer the fake gateway serves (a real EODHD response for MRK)
SAMPLE_RESPONSE = PROJECT_ROOT / "docs" / "SampleAPI_JasonData.txt"
#: the sample symbol list the fake gateway serves for NYSE (a real EODHD exchange-symbol-list answer)
SAMPLE_SYMBOL_LIST = PROJECT_ROOT / "docs" / "SampleAPI_NYSE_List.json"
#: the fields of a symbol-list record (fmt=csv: the same names as header)
SYMBOL_LIST_FIELDS = ("Code", "Name", "Country", "Exchange", "Currency", "Type", "Isin")

_SYMBOL_RE = re.compile(r"^[A-Za-z0-9.\-_^=]{1,32}$")
_MARKET_RE = re.compile(r"^[A-Za-z0-9.\-_]{1,20}$")
_TOKEN_RE = re.compile(r"(api_token=)[^&#\s]*", re.IGNORECASE)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
USER_AGENT = "stock_dataload-eod-client"


class EodApiError(Exception):
    """The request could not be made or the API refused it (nothing was saved).
    *status*: the HTTP status (None = no answer: network problem, timeout); *retry_after*: the seconds
    the API asked to wait (``Retry-After`` of a 429 / 503)."""

    def __init__(self, message: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def _retry_after(value: str | None) -> float | None:
    """``Retry-After: 30`` -> 30.0 (an HTTP date or garbage -> None)."""
    try:
        seconds = float((value or "").strip())
    except ValueError:
        return None
    return max(0.0, min(seconds, 3600.0))


def redact(text: str) -> str:
    """Hide the token in a URL / message: ``...?api_token=***&fmt=json``."""
    return _TOKEN_RE.sub(r"\1***", text or "")


def validate_symbol(symbol: str) -> str:
    """``" mrk "`` -> ``"MRK"``; exchange suffixes / indices / pairs stay (``BRK-B.US``, ``^GSPC.INDX``)."""
    cleaned = (symbol or "").strip()
    if not _SYMBOL_RE.match(cleaned):
        raise EodApiError(f"'{symbol}' is not a valid symbol (letters, digits and . - _ ^ = only, e.g. MRK or MRK.US)")
    return cleaned.upper()


def validate_market(market: str) -> str:
    """``" nyse "`` -> ``"NYSE"`` - a market / exchange code of the API (NYSE, NASDAQ, US, LSE, XETRA ...)."""
    cleaned = (market or "").strip()
    if not _MARKET_RE.match(cleaned):
        raise EodApiError(f"'{market}' is not a valid market code (letters, digits and . - _ only, e.g. NYSE, LSE)")
    return cleaned.upper()


def parse_params(text: str | None) -> dict[str, str]:
    """``"period=d&order=a"`` (or comma separated) -> ``{"period": "d", "order": "a"}``."""
    out: dict[str, str] = {}
    for part in re.split(r"[&,;]", text or ""):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise EodApiError(f"extra parameter '{part}' must look like name=value")
        key, value = part.split("=", 1)
        if not key.strip():
            raise EodApiError(f"extra parameter '{part}' has no name")
        out[key.strip()] = value.strip()
    return out


def build_url(base_url: str, symbol: str, api_token: str, fmt: str,
              params: Mapping[str, object] | None = None, placeholder: str = "symbol") -> str:
    """The request URL: ``<base_url><SYMBOL>?api_token=<token>&fmt=<fmt>[&more]``.

    A *base_url* with ``{symbol}`` is a template; otherwise the symbol is
    appended as the last path segment.  A query string already present in
    *base_url* is kept.  ``None`` / empty parameter values are left out."""
    base = (base_url or "").strip()
    if not base.lower().startswith(("http://", "https://")):
        raise EodApiError(f"the API address '{base_url}' must start with http:// or https://")
    encoded = urllib.parse.quote(symbol, safe=".-_^=")
    if "{" + placeholder + "}" in base:
        url = base.replace("{" + placeholder + "}", encoded)
    else:
        path, sep, query = base.partition("?")
        url = path.rstrip("/") + "/" + encoded + (sep + query if sep else "")
    query: dict[str, str] = {}
    if api_token:
        query["api_token"] = api_token
    if fmt:
        query["fmt"] = fmt
    for key, value in (params or {}).items():
        if value is not None and str(value) != "":
            query[str(key)] = str(value)
    if not query:
        return url
    return url + ("&" if "?" in url else "?") + urllib.parse.urlencode(query)


def _safe_file_part(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", text) or "_"


# ---------------------------------------------------------------------------
# transports: the real HTTP client and the fake gateway
# ---------------------------------------------------------------------------
@dataclass
class HttpResponse:
    status: int
    body: bytes
    content_type: str = ""


class UrllibGetter:
    """``GET`` with the standard library; raises :class:`EodApiError` (token redacted)."""

    def get(self, url: str, timeout: float) -> HttpResponse:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return HttpResponse(resp.status, resp.read(), resp.headers.get("Content-Type", ""))
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace").strip()
            except Exception:  # noqa: BLE001
                detail = ""
            hint = {401: " - check the API token", 403: " - the token may not allow this request",
                    404: " - unknown symbol or wrong API address", 429: " - too many requests, try again later"
                    }.get(exc.code, "")
            message = f"the API answered {exc.code} {exc.reason}{hint}"
            if detail:
                message += f": {detail[:200]}"
            wait = _retry_after(exc.headers.get("Retry-After")) if exc.headers is not None else None
            raise EodApiError(redact(message), exc.code, wait) from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            host = urllib.parse.urlsplit(url).netloc
            reason = getattr(exc, "reason", exc)
            raise EodApiError(redact(f"could not reach {host}: {reason}")) from None


#: the fault kinds of the fake gateway (see FaultPlan)
TRANSIENT_FAULTS = ("500", "502", "503", "504", "429", "timeout", "reset", "badjson", "truncated")
PERMANENT_FAULTS = ("400", "401", "402", "403", "404", "empty", "badrecord")
_FAULT_MESSAGES = {
    "400": "Bad Request", "401": "Unauthorized - check the API token", "402": "Payment Required - the API quota is used up",
    "403": "Forbidden - the token may not allow this request", "404": "Not Found - unknown symbol or wrong API address",
    "429": "Too Many Requests - too many requests, try again later", "500": "Internal Server Error",
    "502": "Bad Gateway", "503": "Service Unavailable", "504": "Gateway Timeout",
}


@dataclass
class FaultPlan:
    """What the fake gateway gets wrong - to validate error handling and retries without the API.

    Spec (comma separated)::

        MRK:503x2      the first 2 calls for MRK answer 503, then the call succeeds
        PFE:404        every call for PFE answers 404 (permanent faults repeat by default)
        HD:timeout     one timeout (transient faults happen once by default), then success
        *:401          every symbol: 401 (a bad token)
        rate=0.1       additionally 10% of all calls fail with a random transient fault ...
        seed=7         ... chosen reproducibly (seed)
        retry_after=2  the Retry-After (seconds) of the 429 answers

    Kinds: 500 502 503 504 429 timeout reset badjson truncated (transient) | 400 401 402 403 404 empty
    badrecord (permanent; ``empty`` = an empty list, ``badrecord`` = a record with a volume that is no number).
    The rules apply to the price calls (key: the symbol) and to the symbol-list calls (key: the market).
    """

    rules: dict[str, list] = field(default_factory=dict)        # SYMBOL or "*" -> [kind, times left (None = always)]
    rate: float = 0.0
    seed: int = 0
    retry_after: float | None = None
    attempts: dict[str, int] = field(default_factory=dict)     # calls per symbol so far

    @classmethod
    def parse(cls, spec: str | None) -> "FaultPlan":
        plan = cls()
        for item in (spec or "").split(","):
            item = item.strip()
            if not item:
                continue
            key, sep, value = item.partition("=")
            if sep and ":" not in item:
                try:
                    if key.strip() == "rate":
                        plan.rate = float(value)
                    elif key.strip() == "seed":
                        plan.seed = int(value)
                    elif key.strip() == "retry_after":
                        plan.retry_after = float(value)
                    else:
                        raise ValueError(key)
                except ValueError:
                    raise EodApiError(f"fake faults: '{item}' - use rate=0.1, seed=7 or retry_after=2") from None
                continue
            symbol, sep, kind = item.partition(":")
            m = re.fullmatch(r"([a-z0-9]+?)(?:x(\d+))?", kind.strip().lower())
            if not sep or not symbol.strip() or not m or m.group(1) not in TRANSIENT_FAULTS + PERMANENT_FAULTS:
                raise EodApiError(f"fake faults: '{item}' is not SYMBOL:KIND[xN] with KIND one of "
                                  f"{', '.join(TRANSIENT_FAULTS + PERMANENT_FAULTS)}")
            times = int(m.group(2)) if m.group(2) else (1 if m.group(1) in TRANSIENT_FAULTS else None)
            plan.rules[symbol.strip().upper()] = [m.group(1), times]
        if not 0 <= plan.rate <= 1:
            raise EodApiError("fake faults: rate must be between 0 and 1")
        return plan

    def next_fault(self, symbol: str) -> str | None:
        """The fault of the next call for *symbol* (None = answer normally)."""
        n = self.attempts[symbol] = self.attempts.get(symbol, 0) + 1
        rule = self.rules.get(symbol) or self.rules.get("*")
        if rule is not None and (rule[1] is None or rule[1] > 0):
            if rule[1] is not None:
                rule[1] -= 1
            return rule[0]
        if self.rate:
            rnd = random.Random(f"{self.seed}:{symbol}:{n}")
            if rnd.random() < self.rate:
                return rnd.choice(("503", "500", "timeout", "429", "badjson"))
        return None


class FakeEodGateway:
    """STUB of the EOD API (``gateway = fake``): answers every symbol with the sample
    response in ``docs/SampleAPI_JasonData.txt`` - as JSON, or converted to CSV
    (``Date,Open,High,Low,Close,Adjusted_close,Volume``) for ``fmt=csv``.
    An empty token answers 401, like the real API.  ``calls`` records the URLs.

    *vary_by_symbol*: every symbol gets prices of its own - the sample scaled by a factor derived
    from the symbol (MRK keeps the sample itself), some symbols with a shorter history - so a whole
    market can be fetched.  *faults*: a :class:`FaultPlan` (or its spec) of simulated failures."""

    def __init__(self, sample: Path = SAMPLE_RESPONSE, symbol_list: Path = SAMPLE_SYMBOL_LIST,
                 vary_by_symbol: bool = False, faults: "FaultPlan | str | None" = None):
        self.sample = Path(sample)
        self.symbol_list = Path(symbol_list)
        self.calls: list[str] = []
        self.fail_with: str | None = None
        self.vary_by_symbol = vary_by_symbol
        self.faults = faults if isinstance(faults, FaultPlan) else FaultPlan.parse(faults)
        self._sample_records: list[dict] | None = None

    def _records(self) -> list[dict]:
        if self._sample_records is None:
            try:
                self._sample_records = json.loads(self.sample.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise EodApiError(f"the fake gateway has no sample data ({self.sample}): {exc}") from None
        return [dict(r) for r in self._sample_records]

    @staticmethod
    def _variation(ticker: str, n_records: int) -> tuple[float, float, int] | None:
        """``(price factor, volume factor, first record)`` of a ticker - None for MRK (the sample itself)."""
        if ticker == "MRK":
            return None
        h = int(hashlib.sha256(ticker.encode("utf-8")).hexdigest()[:12], 16)
        factor = round(0.05 * (400 ** ((h % 10_000) / 10_000)), 4)          # log-uniform 0.05 .. 20
        vol = 0.02 + (h // 10_000 % 1000) / 200                               # 0.02 .. 5.02
        start = (h // 7) % (n_records // 2) if h % 8 == 0 and n_records >= 2 else 0
        return factor, vol, start

    @staticmethod
    def _scaled(record: dict, factor: float, vol: float) -> dict:
        return {"date": record["date"], **{k: round(record[k] * factor, 4) for k in
                                           ("open", "high", "low", "close", "adjusted_close")},
                "volume": int(record["volume"] * vol)}

    @classmethod
    def _varied(cls, records: list[dict], symbol: str) -> list[dict]:
        """The sample as the prices of *symbol*: scaled by a factor of the symbol (0.05 .. 20, MRK: 1),
        volume likewise; every 8th symbol has a shorter history (listed later)."""
        variation = cls._variation(symbol.split(".")[0], len(records))
        if variation is None:
            return records
        factor, vol, start = variation
        return [cls._scaled(r, factor, vol) for r in records[start:]]

    def _fault(self, kind: str, symbol: str, url: str) -> HttpResponse:
        """Raise / answer like the API does for the fault *kind*."""
        if kind in _FAULT_MESSAGES:
            status = int(kind)
            raise EodApiError(f"the API answered {status} {_FAULT_MESSAGES[kind]}", status,
                              self.faults.retry_after if status == 429 else None)
        if kind == "timeout":
            raise EodApiError(f"could not reach {urllib.parse.urlsplit(url).netloc}: timed out")
        if kind == "reset":
            raise EodApiError(f"could not reach {urllib.parse.urlsplit(url).netloc}: "
                              f"[Errno 104] Connection reset by peer")
        if kind == "badjson":
            return HttpResponse(200, b"<html><body>502 Bad Gateway (proxy)</body></html>", "text/html")
        if kind == "truncated":
            body = json.dumps(self._records(), separators=(",", ":")).encode("utf-8")
            return HttpResponse(200, body[: len(body) // 2], "application/json")
        if kind == "empty":
            return HttpResponse(200, b"[]", "application/json")
        if kind == "badrecord":
            records = self._records()[:3]
            records[1]["volume"] = "lots"                     # not a number: the staging table rejects it
            return HttpResponse(200, json.dumps(records).encode("utf-8"), "application/json")
        raise EodApiError(f"unknown fake fault {kind}")  # pragma: no cover

    def get(self, url: str, timeout: float) -> HttpResponse:
        self.calls.append(redact(url))
        if self.fail_with:
            raise EodApiError(self.fail_with)
        parts = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        if not query.get("api_token"):
            raise EodApiError("the API answered 401 Unauthorized - check the API token", 401)
        symbol = urllib.parse.unquote(parts.path.rstrip("/").rsplit("/", 1)[-1]).upper()
        fault = self.faults.next_fault(symbol)
        if fault is not None:
            return self._fault(fault, symbol, url)
        if "symbol-list" in parts.path:
            return self._symbol_list(symbol, query)
        if "eod-bulk-last-day" in parts.path:
            return self._bulk_last_day(symbol, query)
        records = self._records()
        if self.vary_by_symbol:
            records = self._varied(records, symbol)
        start, end = query.get("from"), query.get("to")
        records = [r for r in records if (not start or r["date"] >= start) and (not end or r["date"] <= end)]
        if query.get("order") == "d":
            records = list(reversed(records))
        fmt = (query.get("fmt") or "csv").lower()       # the real API answers CSV without fmt
        if fmt == "json":
            return HttpResponse(200, json.dumps(records, separators=(",", ":")).encode("utf-8"), "application/json")
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(["Date", "Open", "High", "Low", "Close", "Adjusted_close", "Volume"])
        for r in records:
            writer.writerow([r["date"], r["open"], r["high"], r["low"], r["close"], r["adjusted_close"], r["volume"]])
        return HttpResponse(200, buf.getvalue().encode("utf-8"), "text/csv")

    #: the markets the stub answers eod-bulk-last-day for (every symbol of the NYSE sample list)
    BULK_MARKETS = ("US", "NYSE")

    def _bulk_last_day(self, market: str, query: Mapping[str, str]) -> HttpResponse:
        """``eod-bulk-last-day/<MARKET>``: one record per symbol of the NYSE sample list (any type) for the
        day ``date`` (default: the last day of the sample), with the invented prices of ``vary_by_symbol``
        (MRK: the sample itself). A day without data (weekend, holiday, outside the sample) or another
        market answers ``[]``; ``symbols=MSFT,HD.US`` narrows the answer. Without fmt=json the answer is
        CSV (``Code,Ex,Date,Open,High,Low,Close,Adjusted_close,Volume``, as the API sends it)."""
        records = self._records()
        day = (query.get("date") or records[-1]["date"]).strip()
        index = next((i for i, r in enumerate(records) if r["date"] == day), None)
        try:
            listed = json.loads(self.symbol_list.read_text(encoding="utf-8")) if market in self.BULK_MARKETS else []
        except (OSError, ValueError) as exc:
            raise EodApiError(f"the fake gateway has no sample symbol list ({self.symbol_list}): {exc}") from None
        wanted = {s.strip().upper().split(".")[0] for s in (query.get("symbols") or "").split(",") if s.strip()}
        out = []
        if index is not None:
            for item in listed:
                code = str(item.get("Code") or "").strip()
                if not _SYMBOL_RE.match(code) or (wanted and code.upper() not in wanted):
                    continue
                variation = self._variation(code.upper(), len(records))
                if variation is None:
                    record = dict(records[index])
                else:
                    factor, vol, start = variation
                    if index < start:                       # listed later: no data on that day
                        continue
                    record = self._scaled(records[index], factor, vol)
                out.append({"code": code, "exchange_short_name": market, **record})
        if (query.get("fmt") or "csv").lower() == "json":
            return HttpResponse(200, json.dumps(out, separators=(",", ":")).encode("utf-8"), "application/json")
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(["Code", "Ex", "Date", "Open", "High", "Low", "Close", "Adjusted_close", "Volume"])
        for r in out:
            writer.writerow([r["code"], r["exchange_short_name"], r["date"], r["open"], r["high"], r["low"], r["close"],
                             r["adjusted_close"], r["volume"]])
        return HttpResponse(200, buf.getvalue().encode("utf-8"), "text/csv")

    def _symbol_list(self, market: str, query: Mapping[str, str]) -> HttpResponse:
        """``exchange-symbol-list/<MARKET>``: the NYSE sample for NYSE (optionally one ``type``), an empty
        list for every other market (the stub only knows NYSE).  Without fmt=json the answer is CSV."""
        try:
            records = json.loads(self.symbol_list.read_text(encoding="utf-8")) if market == "NYSE" else []
        except (OSError, ValueError) as exc:
            raise EodApiError(f"the fake gateway has no sample symbol list ({self.symbol_list}): {exc}") from None
        wanted = (query.get("type") or "").replace("_", " ").lower()
        if wanted:
            records = [r for r in records if (r.get("Type") or "").lower() == wanted]
        if (query.get("fmt") or "csv").lower() == "json":
            return HttpResponse(200, json.dumps(records, ensure_ascii=False).encode("utf-8"), "application/json")
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(SYMBOL_LIST_FIELDS)
        for r in records:
            writer.writerow(["" if r.get(f) is None else r.get(f) for f in SYMBOL_LIST_FIELDS])
        return HttpResponse(200, buf.getvalue().encode("utf-8"), "text/csv")


# ---------------------------------------------------------------------------
# the client
# ---------------------------------------------------------------------------
@dataclass
class EodFetchResult:
    symbol: str
    url: str                      # the request URL with the token redacted
    path: Path                    # the saved response
    fmt: str
    size: int                     # bytes saved
    records: int | None = None    # JSON list entries / CSV data lines (None if unknown)
    content_type: str = ""
    fetched_at: str = field(default_factory=lambda: datetime.now().replace(microsecond=0).isoformat(sep=" "))
    #: the load into the staging table (None when not loaded), see eod_stage.EodLoadResult
    stage: object | None = None

    @property
    def ok(self) -> bool:
        """Saved - and, if it was loaded, loaded without failure."""
        return self.stage is None or bool(getattr(self.stage, "ok", False))

    def __str__(self) -> str:
        what = f"{self.records:,} record(s), " if self.records is not None else ""
        return f"{self.symbol}: {what}{self.size:,} bytes ({self.fmt}) saved to {self.path}"


def _count_records(body: bytes, fmt: str) -> int | None:
    text = body.decode("utf-8-sig", errors="replace")
    if fmt == "json":
        data = json.loads(text)
        if isinstance(data, list):
            return len(data)
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return len(data["data"])
        return None
    if fmt == "csv":
        lines = [line for line in text.splitlines() if line.strip()]
        return max(0, len(lines) - 1)
    return None


class EodApiClient:
    """Builds the request from ``[eod_api]``, calls the API and saves the response."""

    def __init__(self, settings: Settings | None = None, transport=None, audit=None):
        self.settings = settings or load_settings()
        if transport is None:
            transport = FakeEodGateway() if self.settings.eod_api_gateway == GATEWAY_FAKE else UrllibGetter()
        self.transport = transport
        self.audit = audit

    # -- configuration --------------------------------------------------------
    def token(self, override: str | None = None) -> str:
        """The token to send: the argument, else ``$STOCK_DATALOAD_EOD_API_TOKEN``, else ``[eod_api] api_token``."""
        if override:
            return override.strip()
        return (os.environ.get(ENV_EOD_API_TOKEN) or self.settings.eod_api_token or "").strip()

    def url_for(self, symbol: str, fmt: str | None = None, api_token: str | None = None, base_url: str | None = None,
                params: Mapping[str, object] | None = None) -> str:
        """The full request URL (with the real token - do not log it; see :func:`redact`)."""
        fmt = (fmt or self.settings.eod_api_fmt or "json").strip().lower()
        if not fmt.isalnum():
            raise EodApiError(f"fmt '{fmt}' must be a single word such as json or csv")
        merged = parse_params(self.settings.eod_api_extra_params)
        merged.update({k: v for k, v in (params or {}).items() if v is not None})
        for key in ("from", "to"):
            if merged.get(key) and not _DATE_RE.match(str(merged[key])):
                raise EodApiError(f"'{key}' must be a date like 2025-01-31, got '{merged[key]}'")
        token = self.token(api_token)
        if not token:
            raise EodApiError(f"no API token: set [eod_api] api_token in config.ini or ${ENV_EOD_API_TOKEN}")
        return build_url(base_url or self.settings.eod_api_base_url, validate_symbol(symbol), token, fmt, merged)

    def target_path(self, symbol: str, fmt: str, output_dir: str | os.PathLike | None = None,
                    when: datetime | None = None) -> Path:
        return self._target(self.settings.eod_file_name, "file_name", {"symbol": symbol}, fmt, output_dir, when)

    def symbol_list_path(self, market: str, fmt: str, output_dir: str | os.PathLike | None = None,
                         when: datetime | None = None) -> Path:
        return self._target(self.settings.eod_symbol_list_file_name, "symbol_list_file_name", {"market": market},
                            fmt, output_dir, when)

    def _target(self, template: str, key: str, names: Mapping[str, str], fmt: str,
                output_dir: str | os.PathLike | None, when: datetime | None) -> Path:
        when = when or datetime.now()
        ext = fmt if fmt.isalnum() else "txt"
        values = {**{k: _safe_file_part(v) for k, v in names.items()}, "fmt": fmt, "ext": ext,
                  "date": when.strftime("%Y%m%d"), "time": when.strftime("%H%M%S"),
                  "timestamp": when.strftime("%Y%m%d_%H%M%S")}
        try:
            name = template.format(**values)
        except (KeyError, IndexError, ValueError) as exc:
            raise EodApiError(f"[eod_api] {key} '{template}' is not a valid template "
                              f"(placeholders: {', '.join('{' + k + '}' for k in values)}): {exc}") from None
        name = _safe_file_part(name)
        folder = Path(output_dir) if output_dir else self.settings.eod_download_dir
        return folder / name

    def _download(self, url: str, fmt: str, path: Path, overwrite: bool = True) -> tuple[bytes, int | None, str]:
        """GET *url*, check the answer (2xx; valid JSON for fmt=json) and save it to *path*
        (via ``.part``).  Returns ``(body, records, content type)``; raises :class:`EodApiError`."""
        response = self.transport.get(url, float(self.settings.eod_api_timeout))
        if not 200 <= response.status < 300:
            raise EodApiError(f"the API answered {response.status}", response.status)
        body = response.body or b""
        try:
            records = _count_records(body, fmt)
        except ValueError:
            snippet = body[:160].decode("utf-8", errors="replace").strip()
            raise EodApiError(f"the API did not answer with valid JSON: {redact(snippet)!r}") from None
        if path.exists() and not overwrite:
            raise EodApiError(f"{path} already exists")
        part = path.with_name(path.name + ".part")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            part.write_bytes(body)
            part.replace(path)
        except OSError as exc:
            part.unlink(missing_ok=True)
            raise EodApiError(f"could not save the response to {path}: {exc}") from None
        return body, records, response.content_type

    # -- the call ---------------------------------------------------------------
    def fetch(self, symbol: str, fmt: str | None = None, api_token: str | None = None, base_url: str | None = None,
              params: Mapping[str, object] | None = None, output_dir: str | os.PathLike | None = None,
              overwrite: bool = True, load: bool | None = None,
              skip_bad_rows: bool | None = None) -> EodFetchResult:
        """Call the API for *symbol*, save the response and - for json / csv, unless *load* is
        False (default: ``[eod_api] load_to_stage``) - load it into the staging table
        (``result.stage``)."""
        symbol = validate_symbol(symbol)
        fmt = (fmt or self.settings.eod_api_fmt or "json").strip().lower()
        url = self.url_for(symbol, fmt, api_token, base_url, params)
        shown = redact(url)
        will_load = (self.settings.eod_load_to_stage if load is None else load) and fmt in ("json", "csv")
        if will_load:                                 # a stock at its daily limit is not even called
            from .load_limits import LoadLimitError, audit_refusal
            try:
                self.stage_loader().ledger.check([symbol])
            except LoadLimitError as exc:
                audit_refusal(self.audit, "eod_api", exc, symbol=symbol)
                raise
        log.info("eod api: GET %s", shown)
        try:
            path = self.target_path(symbol, fmt, output_dir)
            body, records, content_type = self._download(url, fmt, path, overwrite)
        except EodApiError as exc:
            log.warning("eod api: %s failed: %s", symbol, exc)
            if self.audit is not None:
                self.audit.log("eod_api", "download_failed", f"{symbol}: {exc}", level="WARNING",
                               details={"symbol": symbol, "url": shown, "status": exc.status})
            raise
        result = EodFetchResult(symbol, shown, path, fmt, len(body), records, content_type)
        log.info("eod api: %s", result)
        if self.audit is not None:
            self.audit.log("eod_api", "downloaded", str(result), file_name=path.name,
                           details={"symbol": symbol, "url": shown, "bytes": len(body), "records": records})
        if will_load:
            result.stage = self.stage_loader().load_file(path, symbol, fmt, skip_bad_rows)
        return result

    def stage_loader(self):
        """The loader of the staging table (rejected records go to Data_Error)."""
        from .eod_stage import EodStageLoader
        from .quality import DataErrorRepository
        return EodStageLoader(self.settings, audit=self.audit, data_errors=DataErrorRepository(self.settings))

    # -- exchange symbol lists ---------------------------------------------------------
    def symbol_list_url_for(self, market: str, fmt: str | None = None, api_token: str | None = None,
                            base_url: str | None = None, params: Mapping[str, object] | None = None) -> str:
        """``<symbol_list_url><MARKET>?api_token=<token>&fmt=<fmt>[&params]`` (with the real token - do not log)."""
        fmt = (fmt or self.settings.eod_symbol_list_fmt or "json").strip().lower()
        if not fmt.isalnum():
            raise EodApiError(f"fmt '{fmt}' must be a single word such as json or csv")
        token = self.token(api_token)
        if not token:
            raise EodApiError(f"no API token: set [eod_api] api_token in config.ini or ${ENV_EOD_API_TOKEN}")
        return build_url(base_url or self.settings.eod_symbol_list_url, validate_market(market), token, fmt,
                         {k: v for k, v in (params or {}).items() if v is not None}, placeholder="market")

    def fetch_symbol_list(self, market: str, fmt: str | None = None, api_token: str | None = None,
                          base_url: str | None = None, params: Mapping[str, object] | None = None,
                          output_dir: str | os.PathLike | None = None, overwrite: bool = True,
                          load: bool | None = None, skip_bad_rows: bool | None = None) -> EodFetchResult:
        """Call the exchange-symbol-list API for *market* (NYSE, NASDAQ, LSE, ...), save the answer and -
        for json / csv, unless *load* is False (default ``[eod_api] load_to_stage``) - load it into the
        symbol staging table (``result.stage``).  *params* e.g. ``{"type": "common_stock"}``."""
        market = validate_market(market)
        fmt = (fmt or self.settings.eod_symbol_list_fmt or "json").strip().lower()
        url = self.symbol_list_url_for(market, fmt, api_token, base_url, params)
        shown = redact(url)
        log.info("eod api: GET %s", shown)
        try:
            path = self.symbol_list_path(market, fmt, output_dir)
            body, records, content_type = self._download(url, fmt, path, overwrite)
        except EodApiError as exc:
            log.warning("eod api: symbol list of %s failed: %s", market, exc)
            if self.audit is not None:
                self.audit.log("eod_api", "symbol_list_failed", f"{market}: {exc}", level="WARNING",
                               details={"market": market, "url": shown, "status": exc.status})
            raise
        result = EodFetchResult(market, shown, path, fmt, len(body), records, content_type)
        log.info("eod api: symbol list %s", result)
        if self.audit is not None:
            self.audit.log("eod_api", "symbol_list_downloaded", f"symbol list {result}", file_name=path.name,
                           details={"market": market, "url": shown, "bytes": len(body), "records": records})
        if (self.settings.eod_load_to_stage if load is None else load) and fmt in ("json", "csv"):
            result.stage = self.symbol_loader().load_file(path, market, fmt, skip_bad_rows)
        return result

    def symbol_loader(self):
        """The loader of the symbol staging table (rejected records go to Data_Error)."""
        from .eod_symbols import SymbolListLoader
        from .quality import DataErrorRepository
        return SymbolListLoader(self.settings, audit=self.audit, data_errors=DataErrorRepository(self.settings))

    def fetch_symbol_lists(self, markets: Iterable[str], **kwargs) -> list[tuple[str, EodFetchResult | None,
                                                                                EodApiError | None]]:
        """:meth:`fetch_symbol_list` for several markets; one failing market does not stop the others."""
        out: list[tuple[str, EodFetchResult | None, EodApiError | None]] = []
        for market in markets:
            try:
                out.append((market, self.fetch_symbol_list(market, **kwargs), None))
            except EodApiError as exc:
                out.append((market, None, exc))
        return out

    def fetch_many(self, symbols: Iterable[str], **kwargs) -> list[tuple[str, EodFetchResult | None, EodApiError | None]]:
        """:meth:`fetch` for several symbols; one failing symbol does not stop the others."""
        out: list[tuple[str, EodFetchResult | None, EodApiError | None]] = []
        for symbol in symbols:
            try:
                out.append((symbol, self.fetch(symbol, **kwargs), None))
            except EodApiError as exc:
                out.append((symbol, None, exc))
        return out


def fetch_symbol_list(market: str, settings: Settings | None = None, **kwargs) -> EodFetchResult:
    """One call: ``fetch_symbol_list("NYSE")`` gets the list of symbols traded on the market from the
    exchange-symbol-list API (``[eod_api] symbol_list_url``, token as configured), saves it in
    ``[eod_api] download_dir`` and loads it into the symbol staging table (``Stock_Symbol_Stage``).

    Keyword arguments override the configuration for this call: ``fmt``, ``api_token``, ``base_url``,
    ``params`` (e.g. ``{"type": "common_stock"}``), ``output_dir``, ``load``."""
    return EodApiClient(settings).fetch_symbol_list(market, **kwargs)


def fetch_eod(symbol: str, settings: Settings | None = None, **kwargs) -> EodFetchResult:
    """One call: ``fetch_eod("MRK")`` downloads the end-of-day prices of MRK with the
    configured API address / token / format and saves them in ``[eod_api] download_dir``.

    Keyword arguments override the configuration for this call: ``fmt``, ``api_token``,
    ``base_url``, ``params`` (e.g. ``{"from": "2025-01-01", "period": "w"}``), ``output_dir``,
    ``load`` (load into the staging table ``Stock_EOD_Stage``; default ``[eod_api] load_to_stage``)."""
    return EodApiClient(settings).fetch(symbol, **kwargs)
