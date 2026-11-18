"""
Account security and messaging: RFC 6238 TOTP test vectors, the e-mail / sms senders (stubs,
SMTP and Twilio against mocks, misconfigured providers), the two-factor service (codes, cooldown,
attempts, recovery codes, reset links), and the ``user`` / ``db keep-tickers`` commands.

No network, no credentials: every address, phone number (555-01xx, reserved for fiction),
token and password is a dummy.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from stock_dataload import cli
from stock_dataload.accounts import (MAX_ATTEMPTS, METHOD_EMAIL, METHOD_RECOVERY, METHOD_SMS, PRINCIPAL_STAFF,
                                     AccountError, CooldownError, Principal, TwoFactorService, format_secret, otpauth_uri,
                                     totp, verify_totp)
from stock_dataload.auth import UserStore
from stock_dataload.config import save_settings
from stock_dataload.messaging import (BrokenSender, ConsoleEmailSender, ConsoleSmsSender, MemoryEmailSender,
                                      MemorySmsSender, MessagingError, SmtpEmailSender, TwilioSmsSender, make_email_sender,
                                      make_sms_sender, mask_email, mask_phone, normalise_phone, safe_email_sender,
                                      safe_sms_sender)

from .helpers import TempProjectTestCase

#: RFC 6238 appendix B, SHA-1: the secret "12345678901234567890" in base32
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
RFC_VECTORS = ((59, "94287082"), (1111111109, "07081804"), (1111111111, "14050471"), (1234567890, "89005924"),
               (2000000000, "69279037"), (20000000000, "65353130"))
DUMMY_PHONE = "+1 555 010 1234"


class TotpTests(unittest.TestCase):
    def test_rfc6238_vectors(self):
        for at, expected in RFC_VECTORS:
            self.assertEqual(totp(RFC_SECRET, at=at, digits=8), expected)
            self.assertEqual(totp(RFC_SECRET, at=at), expected[-6:])

    def test_verification_window(self):
        code = totp(RFC_SECRET, at=1111111111)
        self.assertTrue(verify_totp(RFC_SECRET, code, at=1111111111))
        self.assertTrue(verify_totp(RFC_SECRET, code[:3] + " " + code[3:], at=1111111111 + 30))   # drift, blanks
        self.assertFalse(verify_totp(RFC_SECRET, code, at=1111111111 + 90))
        self.assertFalse(verify_totp(RFC_SECRET, "abcdef"))
        self.assertFalse(verify_totp("not base32!", "123456"))

    def test_uri_and_formatting(self):
        uri = otpauth_uri(RFC_SECRET, "dummy@example.com", "Stock Data API")
        self.assertTrue(uri.startswith("otpauth://totp/Stock%20Data%20API%3Adummy%40example.com?secret="))
        self.assertEqual(format_secret("ABCDEFGH"), "ABCD EFGH")


class MessagingTests(unittest.TestCase):
    def test_masking_and_phone_numbers(self):
        self.assertEqual(mask_email("jane.dummy@example.com"), "j******y@example.com")
        self.assertEqual(mask_email("jo@example.com"), "j*@example.com")
        self.assertEqual(mask_email(""), "")
        self.assertEqual(normalise_phone(DUMMY_PHONE), "+15550101234")
        self.assertEqual(normalise_phone("0015550101234"), "+15550101234")
        self.assertEqual(mask_phone("+15550101234"), "+1******1234")
        for bad in ("", "555 0101", "+1 abc", "+0123456789"):
            with self.assertRaises(MessagingError):
                normalise_phone(bad)

    def test_memory_senders_and_outages(self):
        mail = MemoryEmailSender()
        self.assertTrue(mail.stub)
        mail.send("dummy@example.com", "Your code 123456", "Hello,\n\n    654321\n\nhttps://portal.example/reset/x")
        self.assertEqual((mail.last.to, mail.last.code, mail.last.link), ("dummy@example.com", "654321",
                                                                         "https://portal.example/reset/x"))
        mail.fail_with = "mail server down"
        with self.assertRaisesRegex(MessagingError, "mail server down"):
            mail.send("dummy@example.com", "s", "b")
        self.assertEqual(len(mail.outbox), 1)
        with self.assertRaises(MessagingError):
            MemoryEmailSender().send("", "s", "b")
        sms = MemorySmsSender()
        sms.send(DUMMY_PHONE, "123456 is your code")
        self.assertEqual(sms.sent_to("+15550101234")[0].code, "123456")

    def test_console_senders_print(self):
        out = io.StringIO()
        ConsoleEmailSender(stream=out).send("dummy@example.com", "Subject line", "the body 246810")
        ConsoleSmsSender(stream=out).send(DUMMY_PHONE, "135790 is your code")
        text = out.getvalue()
        self.assertIn("E-MAIL (stub - not sent)", text)
        self.assertIn("the body 246810", text)
        self.assertIn("SMS (stub - not sent) to +15550101234", text)

    def test_factories(self):
        class S:                                                  # a minimal settings object
            mail_provider, sms_provider = "memory", "memory"
            smtp_host, smtp_port, smtp_user, smtp_password, mail_from, smtp_starttls = "", 587, "", "", "", True
            twilio_account_sid = twilio_auth_token = twilio_from = ""
        s = S()
        self.assertIsInstance(make_email_sender(s), MemoryEmailSender)
        self.assertIsInstance(make_sms_sender(s), MemorySmsSender)
        s.mail_provider, s.sms_provider = "smtp", "twilio"
        with self.assertRaises(MessagingError):
            make_email_sender(s)
        broken = safe_email_sender(s)
        self.assertIsInstance(broken, BrokenSender)
        self.assertFalse(broken.stub)
        with self.assertRaisesRegex(MessagingError, "not configured correctly"):
            broken.send("dummy@example.com", "s", "b")
        self.assertIsInstance(safe_sms_sender(s), BrokenSender)
        s.mail_provider = "pigeon"
        with self.assertRaises(MessagingError):
            make_email_sender(s)

    def test_smtp_sender_against_a_mocked_server(self):
        sender = SmtpEmailSender("smtp.example.com", 587, "dummy-user", "dummy-smtp-password", "no-reply@example.com")
        with mock.patch("smtplib.SMTP") as smtp:
            server = smtp.return_value.__enter__.return_value
            sender.send("dummy@example.com", "Hello", "Body")
        smtp.assert_called_once_with("smtp.example.com", 587, timeout=20.0)
        server.starttls.assert_called_once()
        server.login.assert_called_once_with("dummy-user", "dummy-smtp-password")
        message = server.send_message.call_args[0][0]
        self.assertEqual((message["To"], message["Subject"]), ("dummy@example.com", "Hello"))
        import smtplib
        with mock.patch("smtplib.SMTP", side_effect=smtplib.SMTPException("refused")):
            with self.assertRaisesRegex(MessagingError, "did not accept"):
                sender.send("dummy@example.com", "Hello", "Body")

    def test_twilio_sender_against_a_mocked_api(self):
        class Transport:
            def __init__(self):
                self.calls = []

            def request(self, method, url, data=None, headers=None, json_body=False):
                self.calls.append((method, url, data, headers))
                return {"sid": "SMdummy0001"}
        transport = Transport()
        sender = TwilioSmsSender("ACdummy0000", "dummy-twilio-token", "+1 555 010 9999", transport=transport)
        sender.send(DUMMY_PHONE, "123456 is your code")
        method, url, data, headers = transport.calls[0]
        self.assertEqual(url, "https://api.twilio.com/2010-04-01/Accounts/ACdummy0000/Messages.json")
        self.assertEqual(data, {"To": "+15550101234", "From": "+15550109999", "Body": "123456 is your code"})
        self.assertTrue(headers["Authorization"].startswith("Basic "))
        with self.assertRaises(MessagingError):
            TwilioSmsSender("", "dummy-twilio-token", "+15550109999")


class TwoFactorServiceTests(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.mail, self.sms = MemoryEmailSender(), MemorySmsSender()
        self.service = TwoFactorService(self.settings, email=self.mail, sms=self.sms)
        self.user = UserStore(self.settings).create_user("dummy_user", "Dummy-User-Pass-1", email="user.dummy@example.com")
        self.me = Principal(PRINCIPAL_STAFF, self.user.user_id, self.user.email, None, "Dummy")

    def test_email_codes_cooldown_and_attempts(self):
        masked = self.service.begin_code_method(self.me, METHOD_EMAIL)
        self.assertEqual(masked, "u******y@example.com")
        with self.assertRaises(CooldownError):
            self.service.begin_code_method(self.me, METHOD_EMAIL)
        self.assertFalse(self.service.confirm_code_method(PRINCIPAL_STAFF, self.user.user_id, METHOD_EMAIL, "000000"))
        self.assertTrue(self.service.confirm_code_method(PRINCIPAL_STAFF, self.user.user_id, METHOD_EMAIL,
                                                         self.mail.last.code))
        self.assertTrue(self.service.has_two_factor(PRINCIPAL_STAFF, self.user.user_id))
        self.service.send_login_code(PRINCIPAL_STAFF, self.user.user_id, METHOD_EMAIL)
        code = self.mail.last.code
        for _ in range(MAX_ATTEMPTS):
            self.assertFalse(self.service.verify_login(PRINCIPAL_STAFF, self.user.user_id, METHOD_EMAIL, "999999"))
        # after too many wrong entries even the right code is refused
        self.assertFalse(self.service.verify_login(PRINCIPAL_STAFF, self.user.user_id, METHOD_EMAIL, code))

    def test_sms_and_recovery_codes(self):
        self.service.begin_code_method(self.me, METHOD_SMS, DUMMY_PHONE)
        self.assertEqual(self.sms.last.to, "+15550101234")
        self.assertTrue(self.service.confirm_code_method(PRINCIPAL_STAFF, self.user.user_id, METHOD_SMS,
                                                         self.sms.last.code))
        codes = self.service.generate_recovery_codes(PRINCIPAL_STAFF, self.user.user_id)
        self.assertEqual(len(codes), 10)
        self.assertTrue(self.service.verify_login(PRINCIPAL_STAFF, self.user.user_id, METHOD_RECOVERY,
                                                  codes[0].upper().replace("-", " ")))
        self.assertFalse(self.service.verify_login(PRINCIPAL_STAFF, self.user.user_id, METHOD_RECOVERY, codes[0]))
        self.assertEqual(self.service.recovery_codes_left(PRINCIPAL_STAFF, self.user.user_id), 9)
        with self.assertRaises(AccountError):
            self.service.begin_code_method(self.me, METHOD_SMS, "not a number")

    def test_failed_sending_is_reported(self):
        self.mail.fail_with = "mail server down"
        with self.assertRaisesRegex(AccountError, "could not be sent"):
            self.service.send_email_verification(self.me)
        self.assertIsNone(self.service.pending_code(PRINCIPAL_STAFF, self.user.user_id, "verify_email"))

    def test_reset_links_are_single_use(self):
        self.service.send_password_reset(self.me, lambda token: f"https://dashboard.example/reset-password/{token}")
        token = self.mail.last.link.rsplit("/", 1)[1]
        self.assertEqual(self.service.peek_reset_token(token), (PRINCIPAL_STAFF, self.user.user_id))
        self.assertEqual(self.service.consume_reset_token(token), (PRINCIPAL_STAFF, self.user.user_id))
        self.assertIsNone(self.service.consume_reset_token(token))
        self.assertIsNone(self.service.peek_reset_token("dummy-not-a-token"))


class UserCliTests(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.settings.config_file = self.root / "config.ini"
        self.settings.mail_provider = self.settings.sms_provider = "memory"
        save_settings(self.settings, self.settings.config_file)
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("STOCK_DATALOAD_PASSWORD", None)

    def tearDown(self):
        self.env.stop()
        super().tearDown()

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), mock.patch("sys.stdin.isatty", return_value=False):
            rc = cli.main(["--config", str(self.settings.config_file), "-q", *args])
        return rc, out.getvalue(), err.getvalue()

    def test_user_commands(self):
        rc, out, _ = self.run_cli("user", "list")
        self.assertIn("no users", out)
        rc, out, err = self.run_cli("user", "add", "dummy_admin", "--role", "admin", "--password", "Dummy-Admin-Pass-1",
                                    "--email", "admin.dummy@example.com")
        self.assertEqual(rc, 0, err)
        rc, _, err = self.run_cli("user", "add", "dummy_nopass")                      # no prompt in tests
        self.assertEqual(rc, 1)
        self.assertIn("no password given", err)
        self.run_cli("user", "add", "dummy_op", "--role", "operator", "--password", "Dummy-Op-Pass-1")
        rc, out, _ = self.run_cli("user", "list")
        self.assertIn("dummy_admin", out)
        self.assertIn("admin.dummy@example.com", out)
        self.assertEqual(self.run_cli("user", "passwd", "dummy_op", "--password", "Dummy-Op-Pass-2", "--must-change")[0], 0)
        self.assertTrue(UserStore(self.settings).get_by_username("dummy_op").must_change_password)
        self.assertEqual(self.run_cli("user", "role", "dummy_op", "viewer")[0], 0)
        self.assertEqual(self.run_cli("user", "email", "dummy_op", "op.dummy@example.com")[0], 0)
        self.assertEqual(self.run_cli("user", "disable", "dummy_op")[0], 0)
        self.assertIsNone(UserStore(self.settings).authenticate("dummy_op", "Dummy-Op-Pass-2"))
        self.assertEqual(self.run_cli("user", "enable", "dummy_op")[0], 0)
        self.assertEqual(self.run_cli("user", "reset-2fa", "dummy_op")[0], 0)
        rc, _, err = self.run_cli("user", "delete", "dummy_admin")
        self.assertEqual(rc, 1)
        self.assertIn("last active administrator", err)
        self.assertEqual(self.run_cli("user", "delete", "dummy_op")[0], 0)
        rc, _, err = self.run_cli("user", "passwd", "nobody", "--password", "Dummy-Pass-123")
        self.assertIn("does not exist", err)

    def test_db_keep_tickers(self):
        from stock_dataload.eod_api import SAMPLE_RESPONSE
        from stock_dataload.eod_stage import EodStageLoader
        records = json.loads(SAMPLE_RESPONSE.read_text(encoding="utf-8"))[:5]
        loader = EodStageLoader(self.settings)
        for symbol in ("MRK", "PFE", "JPM", "IBM"):
            self.assertTrue(loader.load_text(symbol, json.dumps(records), source_file=f"test/{symbol}").ok)
        rc, out, _ = self.run_cli("db", "keep-tickers", "mrk,PFE")
        self.assertEqual(rc, 0)
        self.assertIn("delete         10 of         20 rows (e.g. IBM, JPM)", out)
        self.assertIn("dry run", out)
        rc, out, _ = self.run_cli("db", "keep-tickers", "MRK", "PFE", "--yes")
        self.assertEqual(rc, 0)
        self.assertIn("done: 10 rows deleted", out)
        with sqlite3.connect(self.settings.sqlite_path) as conn:
            left = {r[0] for r in conn.execute('SELECT DISTINCT "Ticker" FROM "Stock_EOD_Stage"')}
        self.assertEqual(left, {"MRK", "PFE"})
        self.assertEqual(self.run_cli("db", "keep-tickers", "BAD TICKER!")[0], 2)


if __name__ == "__main__":
    unittest.main()
