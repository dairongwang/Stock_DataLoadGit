"""
Outbound **e-mail** and **text messages** (verification / sign-in codes, password-reset links).

Two small interfaces, each with real senders and in-process stubs::

    EmailSender.send(to, subject, body)      ConsoleEmailSender  STUB - printed to the console + the log
                                             MemoryEmailSender   STUB for tests - kept in .outbox / .last
                                             SmtpEmailSender     a real mail server (smtplib, STARTTLS, login)
    SmsSender.send(to, text)                 ConsoleSmsSender    STUB - printed to the console + the log
                                             MemorySmsSender     STUB for tests
                                             TwilioSmsSender     the Twilio REST API (basic auth)

The provider is chosen by ``[mail] provider`` (console | memory | smtp) and ``[sms] provider``
(console | memory | twilio).  :func:`make_email_sender` / :func:`make_sms_sender` raise
:class:`MessagingError` for a misconfigured provider; :func:`safe_email_sender` /
:func:`safe_sms_sender` return a :class:`BrokenSender` instead, so a wrong setting never stops the
application - the problem is reported when a message is sent.

Every sender has ``name`` (the provider) and ``stub`` (True = nothing leaves the machine - the
pages then say "stub mode").  The stubs record what they "sent" (``outbox``, ``last``) and can
simulate an outage: ``sender.fail_with = "mail server down"`` makes the next sends raise.

Secrets (the SMTP password, the Twilio auth token) come from ``$STOCK_DATALOAD_SMTP_PASSWORD`` /
``$STOCK_DATALOAD_TWILIO_AUTH_TOKEN`` or ``config.ini`` - they are never logged.
"""

from __future__ import annotations

import base64
import logging
import re
import smtplib
import ssl
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from .config import Settings
from .httpclient import HttpError, Transport, UrllibTransport

log = logging.getLogger(__name__)

CHANNEL_EMAIL = "email"
CHANNEL_SMS = "sms"

TWILIO_API = "https://api.twilio.com/2010-04-01"

_PHONE_SEPARATORS = re.compile(r"[\s().\-/]")
_CODE_RE = re.compile(r"\b(\d{6})\b")
_URL_RE = re.compile(r"https?://\S+")


class MessagingError(Exception):
    """A message could not be sent (provider refused, not configured, network problem, bad address)."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def mask_email(email: str | None) -> str:
    """``jane.doe@example.com`` -> ``j******e@example.com`` (shown on pages: where the code went)."""
    text = (email or "").strip()
    if "@" not in text:
        return "*" * min(len(text), 8) if text else ""
    local, domain = text.rsplit("@", 1)
    if len(local) <= 2:
        masked = local[:1] + "*" * max(1, len(local) - 1)
    else:
        masked = local[0] + "*" * min(len(local) - 2, 6) + local[-1]
    return f"{masked}@{domain}"


def normalise_phone(phone: str | None) -> str:
    """A phone number in international (E.164) form: ``+1 (555) 010-1234`` -> ``+15550101234``;
    a leading ``00`` counts as ``+``.  :class:`MessagingError` when it is not one."""
    text = _PHONE_SEPARATORS.sub("", (phone or "").strip())
    if text.startswith("00"):
        text = "+" + text[2:]
    digits = text[1:]
    if not text.startswith("+") or not digits.isdigit() or not 8 <= len(digits) <= 15 or digits[0] == "0":
        raise MessagingError("enter the phone number in international format with the country code, "
                             "e.g. +1 555 010 1234")
    return text


def mask_phone(phone: str | None) -> str:
    """``+15550101234`` -> ``+1******1234``."""
    text = _PHONE_SEPARATORS.sub("", (phone or "").strip())
    if len(text) <= 4:
        return "*" * len(text)
    head = text[:2] if text.startswith("+") else text[:1]
    return head + "*" * (len(text) - len(head) - 4) + text[-4:]


@dataclass
class Message:
    """What a stub sender recorded."""

    channel: str                      # email | sms
    to: str
    subject: str
    body: str
    sent_at: str = field(default_factory=lambda: datetime.now().replace(microsecond=0).isoformat(sep=" "))

    @property
    def code(self) -> str | None:
        """The first 6-digit code of the message (tests, the console)."""
        m = _CODE_RE.search(self.body)
        return m.group(1) if m else None

    @property
    def link(self) -> str | None:
        """The first URL of the message (password-reset links)."""
        m = _URL_RE.search(self.body)
        return m.group(0) if m else None


# ---------------------------------------------------------------------------
# the interfaces
# ---------------------------------------------------------------------------
class EmailSender:
    """Sends one e-mail.  Raise :class:`MessagingError` when it cannot be sent."""

    name = "email"
    stub = False

    def send(self, to: str, subject: str, body: str) -> None:   # pragma: no cover - interface
        raise NotImplementedError


class SmsSender:
    """Sends one text message.  Raise :class:`MessagingError` when it cannot be sent."""

    name = "sms"
    stub = False

    def send(self, to: str, text: str) -> None:                 # pragma: no cover - interface
        raise NotImplementedError


class _Recorder:
    """The stubs: keep what was sent, optionally fail like a real provider would."""

    channel = CHANNEL_EMAIL

    def __init__(self, keep: int = 200):
        self.outbox: list[Message] = []
        self.keep = keep
        self.calls = 0
        #: set to a message (or an exception) to make the next sends fail - an outage in tests
        self.fail_with: str | BaseException | None = None
        self._lock = threading.Lock()

    def _record(self, to: str, subject: str, body: str) -> Message:
        with self._lock:
            self.calls += 1
            if self.fail_with is not None:
                if isinstance(self.fail_with, BaseException):
                    raise MessagingError(str(self.fail_with)) from self.fail_with
                raise MessagingError(str(self.fail_with))
            message = Message(self.channel, to, subject, body)
            self.outbox.append(message)
            del self.outbox[:-self.keep]
            return message

    @property
    def last(self) -> Message | None:
        return self.outbox[-1] if self.outbox else None

    def sent_to(self, to: str) -> list[Message]:
        wanted = (to or "").strip().lower()
        return [m for m in self.outbox if m.to.lower() == wanted]

    def clear(self) -> None:
        with self._lock:
            self.outbox.clear()


class MemoryEmailSender(_Recorder, EmailSender):
    """STUB for tests: nothing is sent, the messages are kept in :attr:`outbox`."""

    name = "memory"
    stub = True
    channel = CHANNEL_EMAIL

    def send(self, to: str, subject: str, body: str) -> None:
        if not (to or "").strip():
            raise MessagingError("no recipient address")
        self._record(to.strip(), subject, body)


class ConsoleEmailSender(MemoryEmailSender):
    """STUB for development: the e-mail is printed to the console (and the log) - nothing is sent."""

    name = "console"

    def __init__(self, stream=None, keep: int = 50):
        super().__init__(keep)
        self.stream = stream

    def send(self, to: str, subject: str, body: str) -> None:
        super().send(to, subject, body)
        banner = "-" * 72
        text = (f"\n{banner}\n E-MAIL (stub - not sent)\n To:      {to}\n Subject: {subject}\n{banner}\n"
                f"{body.rstrip()}\n{banner}\n")
        print(text, file=self.stream or sys.stdout, flush=True)
        log.info("e-mail (console stub) to %s: %s", mask_email(to), subject)


class MemorySmsSender(_Recorder, SmsSender):
    """STUB for tests: nothing is sent, the messages are kept in :attr:`outbox`."""

    name = "memory"
    stub = True
    channel = CHANNEL_SMS

    def send(self, to: str, text: str) -> None:
        self._record(normalise_phone(to), "", text)


class ConsoleSmsSender(MemorySmsSender):
    """STUB for development: the text message is printed to the console (and the log)."""

    name = "console"

    def __init__(self, stream=None, keep: int = 50):
        super().__init__(keep)
        self.stream = stream

    def send(self, to: str, text: str) -> None:
        super().send(to, text)
        number = normalise_phone(to)
        print(f"\n--- SMS (stub - not sent) to {number}: {text}\n", file=self.stream or sys.stdout, flush=True)
        log.info("sms (console stub) to %s", mask_phone(number))


class SmtpEmailSender(EmailSender):
    """A real mail server: ``smtplib`` with STARTTLS (port 587) or implicit TLS (port 465) and login."""

    name = "smtp"
    stub = False

    def __init__(self, host: str, port: int = 587, user: str = "", password: str = "", from_address: str = "",
                 starttls: bool = True, timeout: float = 20.0):
        if not (host or "").strip():
            raise MessagingError("[mail] provider = smtp needs smtp_host")
        if not (from_address or "").strip():
            raise MessagingError("[mail] provider = smtp needs from_address")
        self.host, self.port = host.strip(), int(port or 587)
        self.user, self.password = (user or "").strip(), password or ""
        self.from_address = from_address.strip()
        self.starttls, self.timeout = bool(starttls), float(timeout)

    def build(self, to: str, subject: str, body: str) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = self.from_address
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=self.from_address.rsplit("@", 1)[-1] or None)
        msg.set_content(body)
        return msg

    def send(self, to: str, subject: str, body: str) -> None:
        if not (to or "").strip():
            raise MessagingError("no recipient address")
        msg = self.build(to.strip(), subject, body)
        context = ssl.create_default_context()
        try:
            if self.port == 465:
                server = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=context)
            else:
                server = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
            with server as smtp:
                if self.port != 465 and self.starttls:
                    smtp.starttls(context=context)
                if self.user:
                    smtp.login(self.user, self.password)
                smtp.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            raise MessagingError(f"the mail server {self.host}:{self.port} did not accept the message: {exc}") from exc
        log.info("e-mail sent to %s via %s: %s", mask_email(to), self.host, subject)


class TwilioSmsSender(SmsSender):
    """The Twilio REST API (``POST /Accounts/<sid>/Messages.json``, basic auth)."""

    name = "twilio"
    stub = False

    def __init__(self, account_sid: str, auth_token: str, from_number: str, transport: Transport | None = None,
                 api_base: str = TWILIO_API):
        missing = [n for n, v in (("twilio_account_sid", account_sid), ("twilio_auth_token", auth_token),
                                  ("twilio_from", from_number)) if not (v or "").strip()]
        if missing:
            raise MessagingError(f"[sms] provider = twilio needs {', '.join(missing)} "
                                 f"(the token may come from $STOCK_DATALOAD_TWILIO_AUTH_TOKEN)")
        self.account_sid = account_sid.strip()
        self.auth_token = auth_token.strip()
        self.from_number = normalise_phone(from_number)
        self.transport = transport or UrllibTransport()
        self.api_base = api_base.rstrip("/")

    def send(self, to: str, text: str) -> None:
        number = normalise_phone(to)
        credentials = base64.b64encode(f"{self.account_sid}:{self.auth_token}".encode("utf-8")).decode("ascii")
        url = f"{self.api_base}/Accounts/{self.account_sid}/Messages.json"
        try:
            answer = self.transport.request("POST", url, {"To": number, "From": self.from_number, "Body": text},
                                            {"Authorization": f"Basic {credentials}"})
        except HttpError as exc:
            raise MessagingError(f"Twilio did not send the message: {exc}") from exc
        if isinstance(answer, dict) and answer.get("error_code"):
            raise MessagingError(f"Twilio did not send the message: {answer.get('error_message') or answer['error_code']}")
        log.info("sms sent to %s via Twilio (%s)", mask_phone(number), (answer or {}).get("sid", "-"))


class BrokenSender(EmailSender, SmsSender):
    """Stands in for a misconfigured provider: the application runs, every send reports the problem."""

    stub = False

    def __init__(self, name: str, reason: str):
        self.name = name
        self.reason = reason

    def send(self, to: str, *args) -> None:
        raise MessagingError(f"messages cannot be sent - {self.name} is not configured correctly: {self.reason}")


# ---------------------------------------------------------------------------
# factories
# ---------------------------------------------------------------------------
def make_email_sender(settings: Settings) -> EmailSender:
    """The sender of ``[mail] provider``; :class:`MessagingError` when it is misconfigured."""
    provider = (getattr(settings, "mail_provider", "console") or "console").lower()
    if provider == "console":
        return ConsoleEmailSender()
    if provider == "memory":
        return MemoryEmailSender()
    if provider == "smtp":
        return SmtpEmailSender(settings.smtp_host, settings.smtp_port, settings.smtp_user, settings.smtp_password,
                               settings.mail_from, settings.smtp_starttls)
    raise MessagingError(f"[mail] provider '{provider}' is unknown - use console, memory or smtp")


def make_sms_sender(settings: Settings) -> SmsSender:
    """The sender of ``[sms] provider``; :class:`MessagingError` when it is misconfigured."""
    provider = (getattr(settings, "sms_provider", "console") or "console").lower()
    if provider == "console":
        return ConsoleSmsSender()
    if provider == "memory":
        return MemorySmsSender()
    if provider == "twilio":
        return TwilioSmsSender(settings.twilio_account_sid, settings.twilio_auth_token, settings.twilio_from)
    raise MessagingError(f"[sms] provider '{provider}' is unknown - use console, memory or twilio")


def safe_email_sender(settings: Settings) -> EmailSender:
    """Like :func:`make_email_sender`, but a misconfiguration becomes a :class:`BrokenSender`."""
    try:
        return make_email_sender(settings)
    except MessagingError as exc:
        log.warning("e-mail is not available: %s", exc)
        return BrokenSender(getattr(settings, "mail_provider", "mail") or "mail", str(exc))


def safe_sms_sender(settings: Settings) -> SmsSender:
    """Like :func:`make_sms_sender`, but a misconfiguration becomes a :class:`BrokenSender`."""
    try:
        return make_sms_sender(settings)
    except MessagingError as exc:
        log.warning("text messages are not available: %s", exc)
        return BrokenSender(getattr(settings, "sms_provider", "sms") or "sms", str(exc))
