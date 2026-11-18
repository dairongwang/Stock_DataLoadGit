# Using the data API

The data API delivers **daily**, **weekly** and **monthly** stock data per
ticker and year as JSON or CSV. Every request is made with an **API key**
(`sdl_…`) - you get one with your first purchase
([Buying tokens](10-buying-tokens.md)). The full list of endpoints and
fields is in the [API reference](../reference/20-api.md); the portal's
*API docs* page shows the same with live examples for your site address.

## The first request

Send the key in the `Authorization` header (or as `X-API-Key`, or as the
query parameter `api_key` - the header is preferred because URLs end up in
logs):

```bash
curl -H "Authorization: Bearer sdl_dummyKeyForTheDocs0000000000000" \
     "http://127.0.0.1:8060/api/v1/daily?ticker=MRK&year=2025"
```

```python
import urllib.request, json

req = urllib.request.Request("http://127.0.0.1:8060/api/v1/daily?ticker=MRK,PFE&year=2025",
                             headers={"Authorization": "Bearer sdl_dummyKeyForTheDocs0000000000000"})
with urllib.request.urlopen(req) as resp:
    data = json.load(resp)
print(data["tokens_charged"], data["tokens_remaining"], len(data["records"]))
```

The answer lists the tickers **with** and **without** data, the rows
(`records`) and what the request cost. Add `format=csv` for a CSV file
instead of JSON.

| Parameter | Meaning |
|-----------|---------|
| `ticker` | one or more symbols, comma separated or repeated (`ticker=MRK&ticker=PFE`) |
| `year` | the calendar year (daily, monthly) or ISO year (weekly) |
| `market` | optional - only rows of this market, e.g. `NYSE` |
| `format` | `json` (default) or `csv` |

## What costs tokens

* A request costs **one token per ticker that returns data** for the year
  (the portal may be configured differently - the *API docs* page shows
  the current price).
* Tickers or years **without data are free** - ask for several tickers at
  once and pay only for the ones that exist.
* It is **all or nothing**: if the key does not have enough tokens for the
  whole request, the answer is `402` and nothing is charged.
* The catalogue (`/api/v1/catalog`), the ticker list (`/api/v1/tickers`),
  the balance and the usage are **free**.

Check before you buy:

```bash
curl "http://127.0.0.1:8060/api/v1/catalog"                    # data sets, years, markets, packs
curl "http://127.0.0.1:8060/api/v1/tickers?year=2025&q=M"      # tickers with data
```

## Balance and usage

```bash
curl -H "Authorization: Bearer sdl_dummyKeyForTheDocs0000000000000" http://127.0.0.1:8060/api/v1/balance
curl -H "Authorization: Bearer sdl_dummyKeyForTheDocs0000000000000" "http://127.0.0.1:8060/api/v1/usage?limit=20"
```

Every metered answer also carries the headers `X-Tokens-Charged` and
`X-Tokens-Remaining`. When the balance runs low, buy another pack and put
it on the same key (*top up*) - the key does not change.

## Errors

| Status | Meaning | What to do |
|--------|---------|------------|
| 400 | a parameter is wrong (ticker, year, format) | the `error` text says which |
| 401 | no key, unknown or revoked key, disabled account | check the key; rotate it if it leaked |
| 402 | not enough tokens - nothing was charged | top up the key |
| 404 | unknown data set | use `daily`, `weekly` or `monthly` |
| 503 | the data is temporarily unavailable | try again later |

## Lost or leaked keys

The portal stores only a fingerprint of each key, so a lost key cannot be
shown again. **Rotate** it instead - on your account page, or on the
receipt (*Find my order* with the order reference and e-mail address) if
you bought as a guest. The new key keeps the balance; the old one stops
working immediately. A key you no longer need can be **revoked** on the
account page.

> The key `sdl_dummyKeyForTheDocs0000000000000` in the examples is a
> placeholder - it does not exist.
