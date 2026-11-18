# Deployment

The dashboard is an **internal** tool; the portal is the **public** site.
Both are Flask applications started by the CLI and meant to run behind a
reverse proxy that terminates TLS.

## Running the services

```bash
python -m stock_dataload init-db                         # once: tables + calendar
python -m stock_dataload dashboard --host 127.0.0.1      # staff, port 8050
python -m stock_dataload portal --host 127.0.0.1         # customers, port 8060
```

The built-in server is fine for a small number of users. For more load, run
the apps with any WSGI server, e.g.:

```bash
pip install waitress
waitress-serve --port 8060 --call stock_dataload.portal:create_app
```

## Publishing the portal

1. Put the portal behind a TLS-terminating proxy (nginx, Caddy, IIS) and
   forward `https://data.example.com` to `127.0.0.1:8060`.
2. Set `[portal] public_url = https://data.example.com` - it is used in links,
   e-mails, payment return URLs and the API documentation. With an
   `https://` URL the session cookie is marked *Secure*.
3. Keep the **dashboard** on the internal network only.
4. Set `$STOCK_DATALOAD_SECRET` (or keep `data/.secret_key` private).

## Going live with payments

`payment_gateway = fake` (the default) is test mode: the provider pages are
built-in stubs and nothing is charged. To take real payments:

1. **Stripe**: create a restricted or secret key, set
   `$STOCK_DATALOAD_STRIPE_SECRET_KEY`. Use a Stripe *test* key first.
2. **PayPal**: create a REST app, set `paypal_client_id` and
   `$STOCK_DATALOAD_PAYPAL_CLIENT_SECRET`; keep
   `paypal_environment = sandbox` until the flow works, then `live`.
3. Set `payment_gateway = live` and restart the portal. Missing credentials
   stop the start with a clear message.
4. Buy the smallest pack yourself; check the receipt, the key and the
   order on *Admin → Orders*.

## Going live with sign-in providers

`[oauth] gateway = fake` shows a stub consent page. For the real providers
register the redirect URI `<public_url>/oauth/<provider>/callback` with
Google, Apple and Facebook, fill in the client ids, put the secrets into the
environment (see [Configuration](10-configuration.md)) and set
`gateway = live`. Apple's client secret is a signed JWT valid at most six
months - renew it in time.

## Mail and SMS

The default `console` providers print codes to the server console. For
production set `[mail] provider = smtp` (+ host, port, user,
`$STOCK_DATALOAD_SMTP_PASSWORD`) and, if SMS codes are offered,
`[sms] provider = twilio` (+ account SID, sender number,
`$STOCK_DATALOAD_TWILIO_AUTH_TOKEN`).

## EOD API downloads

Set the real token in `$STOCK_DATALOAD_EOD_API_TOKEN` on the machine that
runs `eod-fetch` (a scheduled task, see [Scheduling](30-scheduling.md)); the
shipped `api_token = dummy` is only a placeholder. Keep `gateway = live`.

## The chart dashboard

`chart_dashboard/` (daily, weekly and monthly candlestick charts of
`Stock_Daily` / `Stock_Weekly` / `Stock_Monthly`) is deployed on its
own: `waitress-serve --listen=127.0.0.1:8070 wsgi:app` from that folder,
`STOCK_CHART_DB` pointing at the database (the stock tables are only read; the
chart's stock lists need write access - to that file, or to a file of their own
named by `STOCK_CHART_LISTS_DB`), health
check `GET /health`. Details in `chart_dashboard/README.md`.

## Checklist

- [ ] `public_url` set, TLS in front of the portal, dashboard not reachable from outside
- [ ] `$STOCK_DATALOAD_SECRET` set or `data/.secret_key` protected
- [ ] payments: live keys in the environment, `payment_gateway = live`, one real test purchase
- [ ] sign-in: redirect URIs registered, `[oauth] gateway = live`
- [ ] mail (and SMS) providers configured; a verification code arrives
- [ ] portal administrators created (`customers add-admin`) and their 2FA set up
- [ ] EOD API token in the environment; a test `eod-fetch` succeeded
- [ ] backups of `data/stock_trade.db` scheduled ([Housekeeping](40-database-housekeeping.md))
