# Configuration keys

Defaults in parentheses.

| Section / key | Meaning |
|---------------|---------|
| `[paths] conversion_dir` (data/conversion) | source files waiting to be converted |
| `converted_dir` (data/converted) | sources after a successful conversion |
| `loading_dir` (data/loading) | default-format files waiting to be loaded |
| `archive_dir`, `error_dir`, `skipped_dir`, `work_dir`, `log_dir` | loaded / failed / skipped files, converter scratch, logs |
| `[database] sqlite_path` (data/stock_trade.db) | the database file |
| `stage_table` (Stock_Stage), `daily_table`, `weekly_table`, `monthly_table` | table names |
| `batch_size` (5000), `truncate_before_load` (false) | loader |
| `[promotion] reject_market_closed_days`, `check_price_range`, `check_trading_window`, `check_completeness` (all true) | validation switches |
| `[quality] enabled` (true), `alert_missing_days` (true), `extra_holidays` | data-quality checks; ad-hoc closures `YYYY-MM-DD, ...` |
| `[loader] file_patterns` (*.txt), `market_from_filename_regex`, `default_market`, `skip_bad_rows` (false), `encoding` (utf-8-sig), `date_from_filename_regex`, `skip_no_trading_days` (true) | |
| `[formats] file` (formats.json) | custom formats |
| `[dashboard] host` (127.0.0.1), `port` (8050), `secret_key_file` (data/.secret_key) | |
| `[portal] host` (127.0.0.1), `port` (8060), `public_url` | the public site |
| `payment_methods` (stripe, paypal), `payment_gateway` (fake) | methods on the purchase page; `live` or the built-in stub |
| `currency` (USD), `packs` (Starter:100:5.00, Standard:500:20.00, Pro:2000:60.00, Enterprise:10000:250.00) | |
| `tokens_per_query` (1), `max_tickers_per_request` (50) | metering |
| `stripe_secret_key`, `paypal_client_id`, `paypal_client_secret`, `paypal_environment` (sandbox) | live payments (secrets via environment) |
| `[oauth] providers` (empty; shipped: google, apple, facebook), `gateway` (fake) | sign-in buttons |
| `google_client_id`, `google_client_secret`, `facebook_app_id`, `facebook_app_secret`, `apple_client_id`, `apple_client_secret` | live providers |
| `[accounts] issuer` (Stock Data API) | name in authenticator apps / messages |
| `require_email_verification` (true), `admin_requires_2fa` (true) | portal |
| `dashboard_registration` (true), `dashboard_registration_role` (viewer) | dashboard *Create account* |
| `code_ttl_minutes` (10), `reset_ttl_minutes` (60) | validity |
| `[mail] provider` (console), `from_address`, `smtp_host`, `smtp_port` (587), `smtp_user`, `smtp_password`, `smtp_starttls` (true) | e-mail |
| `[sms] provider` (console), `twilio_account_sid`, `twilio_auth_token`, `twilio_from` | SMS |
| `[eod_api] base_url` (https://eodhd.com/api/eod/) | the EOD API address; the symbol is appended, or replaces `{symbol}` |
| `api_token` (dummy) | placeholder - `$STOCK_DATALOAD_EOD_API_TOKEN` wins |
| `fmt` (json), `extra_params` (empty) | `fmt=` of the request (json, csv, ...); more query parameters, e.g. `period=w&order=d` |
| `download_dir` (data/api_downloads), `file_name` ({symbol}_{timestamp}.{ext}) | where / how the answers are saved |
| `timeout` (30), `gateway` (live) | seconds; `live` or the built-in stub `fake` |
| `stage_table` (Stock_EOD_Stage), `load_to_stage` (true) | the staging table of the API data; whether `eod-fetch` loads into it |
| `symbol_list_url` (https://eodhd.com/api/exchange-symbol-list/), `symbol_list_fmt` (json) | exchange symbol lists: the market is appended, or replaces `{market}`; same `api_token` |
| `symbol_list_file_name` ({market}_symbols_{timestamp}.{ext}), `symbol_stage_table` (Stock_Symbol_Stage) | where / how lists are saved; their staging table |
| `bulk_url` (https://eodhd.com/api/eod-bulk-last-day/), `bulk_fmt` (json), `bulk_file_name` ({market}_bulk_{day}_{timestamp}.{ext}) | one trading day of every stock of a market (`eod-bulk-fetch`): the market is appended, or replaces `{market}`; same `api_token`; `{day}` = the trade date asked for (or `last`) |
| `retries` (3), `retry_backoff` (1) | `eod-fetch-market`: repeats of a failed call per stock; seconds before the first repeat, doubled each time (a `Retry-After` wins) |
| `request_pause` (0), `max_consecutive_failures` (20) | seconds between two stocks; a run stops after this many stocks failed in a row |
| `market_types` (Common Stock) | the symbol types of the exchange list `eod-fetch-market` fetches, comma separated (empty = every type) |
| `default_market` (empty) | `promote eod`: the market of tickers neither in `Stock_Symbol` nor in `Stock_Daily` (empty = reject them as `market_unknown`) |

Environment variables override the secrets: see
[Operations › Configuration](../operations/10-configuration.md).
