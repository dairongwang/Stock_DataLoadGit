"""
The EOD API client (``eod_api.py``): the request URL from configuration, token redaction,
saving json / csv answers, HTTP errors (mocked ``urlopen``), the fake gateway and ``eod-fetch``.

No network; every token is a dummy.  :class:`FakeHttpResponse` is shared with the other
EOD test modules (``from .test_eod_api import FakeHttpResponse``).
"""

from __future__ import annotations

import io
import json
import os
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from stock_dataload import cli
from stock_dataload.config import ENV_EOD_API_TOKEN, save_settings
from stock_dataload.eod_api import (SAMPLE_RESPONSE, EodApiClient, EodApiError, FakeEodGateway, UrllibGetter,
                                    build_url, parse_params, redact, validate_market, validate_symbol)

from .helpers import TempProjectTestCase

SAMPLE = json.loads(SAMPLE_RESPONSE.read_text(encoding="utf-8"))


class FakeHttpResponse:
    """What ``urllib.request.urlopen`` returns - a context manager with status, headers and read()."""

    def __init__(self, body: bytes, status: int = 200, content_type: str = "application/json",
                 headers: dict | None = None):
        self.body = body
        self.status = status
        self.headers = {"Content-Type": content_type, **(headers or {})}

    def read(self, *args) -> bytes:
        return self.body

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


class UrlTests(unittest.TestCase):
    def test_build_url(self):
        self.assertEqual(build_url("https://eodhd.com/api/eod/", "MRK", "dummy", "json"),
                         "https://eodhd.com/api/eod/MRK?api_token=dummy&fmt=json")
        self.assertEqual(build_url("https://h/api/eod/{symbol}.US", "MRK", "dummy", "csv", {"from": "2025-01-01"}),
                         "https://h/api/eod/MRK.US?api_token=dummy&fmt=csv&from=2025-01-01")
        self.assertEqual(build_url("https://h/eod?x=1", "MRK", "", "", {"empty": "", "none": None}),
                         "https://h/eod/MRK?x=1")
        with self.assertRaises(EodApiError):
            build_url("ftp://h/", "MRK", "dummy", "json")

    def test_redact_and_validation(self):
        self.assertEqual(redact("https://h/MRK?api_token=dummy-secret-42&fmt=json"), "https://h/MRK?api_token=***&fmt=json")
        self.assertEqual(validate_symbol(" mrk.us "), "MRK.US")
        self.assertEqual(validate_market(" nyse "), "NYSE")
        for bad in ("", "MR K", "x" * 40, "a/b"):
            with self.assertRaises(EodApiError):
                validate_symbol(bad)
        with self.assertRaises(EodApiError):
            validate_market("NY SE")
        self.assertEqual(parse_params("period=d&order=a"), {"period": "d", "order": "a"})
        with self.assertRaises(EodApiError):
            parse_params("nonsense")


class ClientTests(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.settings.eod_api_token = "dummy-config-token"

    def test_url_from_configuration(self):
        client = EodApiClient(self.settings, transport=FakeEodGateway())
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ENV_EOD_API_TOKEN, None)
            self.assertEqual(client.url_for("mrk"), "https://eodhd.com/api/eod/MRK?api_token=dummy-config-token&fmt=json")
            with mock.patch.dict(os.environ, {ENV_EOD_API_TOKEN: "dummy-env-token"}):
                self.assertIn("api_token=dummy-env-token", client.url_for("MRK"))       # the environment wins
            self.assertIn("api_token=dummy-arg", client.url_for("MRK", api_token="dummy-arg"))
            self.settings.eod_api_extra_params = "period=d"
            self.assertIn("period=d", client.url_for("MRK"))
            with self.assertRaises(EodApiError):
                client.url_for("MRK", params={"from": "yesterday"})
            self.settings.eod_api_token = ""
            with self.assertRaisesRegex(EodApiError, "no API token"):
                client.url_for("MRK")

    def test_fake_gateway_json_saved_and_loaded(self):
        client = EodApiClient(self.settings, transport=FakeEodGateway())
        result = client.fetch("MRK")
        self.assertTrue(result.path.is_file())
        self.assertEqual(result.records, len(SAMPLE))
        self.assertIn("api_token=***", result.url)
        self.assertNotIn("dummy-config-token", result.url)
        self.assertEqual(result.stage.inserted, len(SAMPLE))
        self.assertTrue(result.ok)
        self.assertIn("saved to", str(result))

    def test_fake_gateway_csv_and_date_filter(self):
        client = EodApiClient(self.settings, transport=FakeEodGateway())
        result = client.fetch("MRK", fmt="csv", params={"from": SAMPLE[-5]["date"]}, load=False)
        self.assertEqual(result.records, 5)
        self.assertTrue(result.path.name.endswith(".csv"))
        self.assertIsNone(result.stage)

    def test_http_errors_are_reported_without_the_token(self):
        client = EodApiClient(self.settings, transport=UrllibGetter())
        refused = urllib.error.HTTPError("https://eodhd.com/api/eod/MRK", 401, "Unauthorized", {}, io.BytesIO(b"bad"))
        with mock.patch("urllib.request.urlopen", side_effect=refused):
            with self.assertRaises(EodApiError) as ctx:
                client.fetch("MRK")
        self.assertEqual(ctx.exception.status, 401)
        self.assertIn("check the API token", str(ctx.exception))
        self.assertNotIn("dummy-config-token", str(ctx.exception))
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no route")):
            with self.assertRaisesRegex(EodApiError, "could not reach eodhd.com"):
                client.fetch("MRK")
        with mock.patch("urllib.request.urlopen", return_value=FakeHttpResponse(b"<html>oops</html>")):
            with self.assertRaisesRegex(EodApiError, "valid JSON"):
                client.fetch("MRK")
        self.assertEqual(list(self.settings.eod_download_dir.glob("*.json")), [])      # nothing saved

    def test_live_client_saves_the_answer(self):
        client = EodApiClient(self.settings, transport=UrllibGetter())
        with mock.patch("urllib.request.urlopen", return_value=FakeHttpResponse(SAMPLE_RESPONSE.read_bytes())) as urlopen:
            result = client.fetch("MRK", load=False)
        self.assertEqual(urlopen.call_args[0][0].full_url,
                         "https://eodhd.com/api/eod/MRK?api_token=dummy-config-token&fmt=json")
        self.assertEqual(result.path.read_bytes(), SAMPLE_RESPONSE.read_bytes())

    def test_fetch_many_continues_after_a_failure(self):
        gateway = FakeEodGateway()
        client = EodApiClient(self.settings, transport=gateway)
        outcomes = client.fetch_many(["MRK", "BAD SYMBOL"], load=False)
        self.assertIsNotNone(outcomes[0][1])
        self.assertIsInstance(outcomes[1][2], EodApiError)
        gateway.fail_with = "simulated outage"
        with self.assertRaisesRegex(EodApiError, "simulated outage"):
            client.fetch("MRK")


class CliTests(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.settings.config_file = self.root / "config.ini"
        self.settings.eod_api_token = "dummy-cli-token"
        save_settings(self.settings, self.settings.config_file)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["--config", str(self.settings.config_file), "-q", *args])
        return rc, out.getvalue(), err.getvalue()

    def test_eod_fetch_with_the_fake(self):
        rc, out, err = self.run_cli("eod-fetch", "MRK", "--fake", "--no-load")
        self.assertEqual(rc, 0, err)
        self.assertIn(f"MRK: {len(SAMPLE):,} record(s)", out)
        rc, out, err = self.run_cli("eod-fetch", "MRK", "NOT A SYMBOL", "--fake", "--no-load")
        self.assertEqual(rc, 1)
        self.assertIn("ERROR NOT A SYMBOL", err)


if __name__ == "__main__":
    unittest.main()
