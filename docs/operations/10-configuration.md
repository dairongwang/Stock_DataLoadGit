# Configuration

All settings live in **`config.ini`** in the project folder. Relative paths
in it are resolved against the folder that contains the file, so the
project can be moved around freely. Every key with its default is listed in
the [reference](../reference/40-configuration.md).

## Which file is used

1. `--config FILE` on the command line;
2. the environment variable `$STOCK_DATALOAD_CONFIG`;
3. `config.ini` in the current directory;
4. `config.ini` in the project root.

Without any file the built-in defaults apply (the same values as the
shipped `config.ini`).

## Sections

| Section | What it controls |
|---------|------------------|
| `[paths]` | the conversion, converted, loading, archive, error, skipped and work folders, the log folder |
| `[database]` | the SQLite file, the table names, batch size |
| `[promotion]`, `[quality]` | the validation rules of the promotion and the data-quality checks |
| `[loader]` | file patterns, market / date from the file name, skipping no-trading days |
| `[formats]` | the custom formats file (`formats.json`) |
| `[dashboard]` | host / port of the dashboard, the session secret file |
| `[portal]` | host / port / public URL of the portal, payment methods and gateway, token packs, metering |
| `[oauth]` | *Continue with Google / Apple / Facebook*: providers, live or fake gateway, client ids |
| `[accounts]` | e-mail verification, 2FA for portal admins, dashboard self-registration, code validity |
| `[mail]`, `[sms]` | how codes and links are sent: console / memory stubs, SMTP, Twilio |
| `[eod_api]` | end-of-day price downloads (`eod-fetch`): API address, token, format, download folder |

## Secrets belong in the environment

`config.ini` may be committed, secrets must not be. Every secret can be
given as an environment variable, which wins over the file:

| Variable | Replaces |
|----------|----------|
| `STOCK_DATALOAD_SECRET` | the session-signing secret (otherwise generated once into `data/.secret_key`) |
| `STOCK_DATALOAD_EOD_API_TOKEN` | `[eod_api] api_token` |
| `STOCK_DATALOAD_STRIPE_SECRET_KEY` | `[portal] stripe_secret_key` |
| `STOCK_DATALOAD_PAYPAL_CLIENT_SECRET` | `[portal] paypal_client_secret` |
| `STOCK_DATALOAD_GOOGLE_CLIENT_SECRET`, `STOCK_DATALOAD_FACEBOOK_APP_SECRET`, `STOCK_DATALOAD_APPLE_CLIENT_SECRET` | the `[oauth]` secrets |
| `STOCK_DATALOAD_SMTP_PASSWORD` | `[mail] smtp_password` |
| `STOCK_DATALOAD_TWILIO_AUTH_TOKEN` | `[sms] twilio_auth_token` |
| `STOCK_DATALOAD_ADMIN_PASSWORD` | the one-time password of the initial `admin` (first dashboard start) |
| `STOCK_DATALOAD_PASSWORD` | the password for `user add` / `user passwd` / `customers add-admin` without a prompt |

The shipped values are placeholders - the EOD API token is literally
`dummy` - so nothing real is ever stored in the repository.

## The EOD API section

```ini
[eod_api]
base_url = https://eodhd.com/api/eod/
api_token = dummy
fmt = json
extra_params =
download_dir = data/api_downloads
file_name = {symbol}_{timestamp}.{ext}
timeout = 30
gateway = live
stage_table = Stock_EOD_Stage
load_to_stage = true
default_market =
symbol_list_url = https://eodhd.com/api/exchange-symbol-list/
symbol_list_fmt = json
symbol_list_file_name = {market}_symbols_{timestamp}.{ext}
symbol_stage_table = Stock_Symbol_Stage
```

The symbol lists of a market (`eod-symbols-fetch NYSE`) use the same token:
`<symbol_list_url><MARKET>?api_token=<api_token>&fmt=<symbol_list_fmt>`.

The request is `<base_url><SYMBOL>?api_token=<api_token>&fmt=<fmt>` - with
the defaults `https://eodhd.com/api/eod/MRK?api_token=dummy&fmt=json`.

* **Another API address**: change `base_url`. If the symbol is not the last
  path segment, write `{symbol}` where it goes, e.g.
  `https://example.com/v2/prices/{symbol}/daily`.
* **Another token**: set `$STOCK_DATALOAD_EOD_API_TOKEN` (preferred) or
  `api_token`; `eod-fetch --token` overrides both for one call.
* **Another format**: `fmt = csv` (or any format the API offers); it is
  also the extension of the saved file. `eod-fetch --fmt csv` for one call.
* **More parameters** for every request: `extra_params = period=w&order=d`.
* **Offline**: `gateway = fake` answers every symbol with the sample response
  in `docs/SampleAPI_JasonData.txt` - no network, nothing real is called.
* **Staging**: every json / csv answer is loaded into `stage_table`
  (`Stock_EOD_Stage`); `load_to_stage = false` only saves the files.

Details: [Downloading prices from the EOD API](../user-manual/35-eod-api-downloads.md).

## The Settings page

Administrators edit most sections on the dashboard's **Settings** page; it
re-writes `config.ini` (comments included) and reloads the application.
Keys the page does not show (for example the whole `[eod_api]` section) are
kept as they are. Secrets given through environment variables are never
shown on the page.
