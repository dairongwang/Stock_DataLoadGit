"""
A minimal JSON-over-HTTP transport (standard library) shared by the payment
providers and the OAuth providers.  Everything external goes through
``Transport.request`` so the real thing (:class:`UrllibTransport`) can be
swapped for an in-process stub in development and tests.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping


class HttpError(Exception):
    """The request failed (network problem or a non-2xx answer with its message)."""

    def __init__(self, message: str, status: int | None = None, payload: dict | None = None):
        super().__init__(message)
        self.status = status
        self.payload = payload or {}


class Transport:
    def request(self, method: str, url: str, data: Mapping[str, Any] | None = None,
                headers: Mapping[str, str] | None = None, json_body: bool = False) -> dict:
        raise NotImplementedError


class UrllibTransport(Transport):
    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout

    def request(self, method, url, data=None, headers=None, json_body=False) -> dict:
        body = None
        hdrs = dict(headers or {})
        if data is not None:
            if json_body:
                body = json.dumps(data).encode("utf-8")
                hdrs.setdefault("Content-Type", "application/json")
            else:
                body = urllib.parse.urlencode(data).encode("utf-8")
                hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        hdrs.setdefault("Accept", "application/json")
        req = urllib.request.Request(url, data=body, method=method)
        for k, v in hdrs.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            payload: dict = {}
            try:
                payload = json.loads(exc.read().decode("utf-8"))
                message = ((payload.get("error") or {}).get("message") if isinstance(payload.get("error"), dict) else None) \
                    or payload.get("message") or payload.get("error_description") or payload.get("error") or str(exc)
            except Exception:  # noqa: BLE001
                message = str(exc)
            raise HttpError(str(message), exc.code, payload) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise HttpError(f"could not reach {urllib.parse.urlsplit(url).netloc}: {exc}") from exc
