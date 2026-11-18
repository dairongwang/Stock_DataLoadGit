# Portal guide

The **portal** (`python -m stock_dataload portal`, http://127.0.0.1:8060) is
the public site for customers of the data API: the **token shop**, customer
**accounts** and the **data API** itself, plus a separate **admin area** for
portal administrators.

```
 buy tokens  ──►  API key (sdl_…) with a token balance  ──►  GET /api/v1/daily?ticker=AAPL&year=2023  ──►  tokens charged per ticker-year
```

* tokens are bought in **packs** with or without an account, by **card
  (Stripe)** or **PayPal**;
* a request costs tokens **per ticker that returns data** (default 1 per
  ticker and year); tickers / years without data are free;
* accounts are optional for buying, but let you keep several keys, see
  usage, and secure everything with two-factor authentication.

| Page | Covers |
|------|--------|
| [Buying tokens](10-buying-tokens.md) | packs, guest or account, paying with Stripe / PayPal, the receipt |
| [Using the data API](20-using-the-api.md) | the first request, what costs tokens, balance, lost keys |
| [Your customer account](30-customer-account.md) | sign-up, keys, orders, usage, security |
