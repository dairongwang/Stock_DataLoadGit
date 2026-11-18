# External services and their stubs

Every dependency on the outside world sits behind a small interface and
has an in-process stand-in, selected by configuration. The production
code paths are the same with the stub and with the real service - only the
transport / sender differs.

| Service | Interface | Real implementation | Stub | Selected by |
|---------|-----------|---------------------|------|-------------|
| Card payments | `payments.PaymentProvider` (`start_checkout`, `verify_checkout`) over `httpclient.Transport` | `StripeCheckoutProvider` → api.stripe.com | `FakeGateway` answering the Stripe endpoints + `/fake-checkout/stripe/…` pages | `[portal] payment_gateway` |
| PayPal | same | `PayPalCheckoutProvider` → api-m(.sandbox).paypal.com | `FakeGateway` answering the PayPal endpoints + `/fake-checkout/paypal/…` | `[portal] payment_gateway` |
| Sign in with Google / Apple / Facebook | `oauth.OAuthProvider` (`authorize_url`, `exchange_code`, `profile`) over `Transport` | `GoogleProvider`, `AppleProvider`, `FacebookProvider` | `FakeIdentityGateway` + `/fake-oauth/<provider>/authorize` consent form (incl. Apple's form post) | `[oauth] gateway` |
| E-mail | `messaging.EmailSender.send(to, subject, body)` | `SmtpEmailSender` (smtplib, STARTTLS, login) | `ConsoleEmailSender` (prints to the console + log), `MemoryEmailSender` (`outbox` for tests) | `[mail] provider` |
| SMS | `messaging.SmsSender.send(to, text)` | `TwilioSmsSender` (REST, basic auth) | `ConsoleSmsSender`, `MemorySmsSender` | `[sms] provider` |
| EOD price API | `eod_api` transport `get(url, timeout)` | `UrllibGetter` → eodhd.com (or any `[eod_api] base_url`) | `FakeEodGateway` answering with `docs/SampleAPI_JasonData.txt` (JSON, or converted to CSV) and, for `exchange-symbol-list/NYSE`, with `docs/SampleAPI_NYSE_List.json` (other markets: an empty list); for `eod-bulk-last-day/US` (and `NYSE`) with one record per symbol of that list for the `date` asked for - the same invented prices as the single-stock calls, `[]` for days without data, `symbols=` narrows it, JSON or CSV; for `eod-fetch-market` with invented prices per symbol (`vary_by_symbol`) and simulated failures (`FaultPlan`: 5xx, 429 + Retry-After, timeouts, broken JSON, 404, empty answers, bad records, 401 / 402) | `[eod_api] gateway`, `eod-fetch --fake`, `eod-fetch-market --fake [--fake-faults SPEC]` |
| QR codes | `accounts.qr_svg()` | optional `qrcode` package | manual-entry key shown instead | package installed or not |

Rules:

* a **misconfigured** provider must not stop the application - the
  messaging factories return a `BrokenSender` whose `send()` reports the
  configuration problem; payment / OAuth factories raise a clear error at
  start-up when `live` lacks credentials;
* the user interface says when a stub is active ("test mode", "stub mode -
  the code was printed to the server console");
* the stubs are **deterministic** enough for tests (fixed subjects per
  provider + e-mail, inspectable `calls`, `fail_with` to simulate outages).

## Adding a provider

1. implement the interface (a payment provider: two methods; an OAuth
   provider: subclass `OAuthProvider` with the endpoints and `profile()`);
2. teach the fake gateway the new endpoints if you want stub support;
3. add the configuration keys to `config.py` (dataclass field, parser,
   template, `save_settings`) and the Settings page;
4. register it in the factory (`make_providers` / `make_oauth_providers`);
5. add tests: the flow through the fake gateway and the real client against
   mocked HTTP (`unittest.mock.patch("urllib.request.urlopen")`).
