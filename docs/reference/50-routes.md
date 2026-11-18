# Web routes

## Dashboard (:8050)

| Route | Role | Purpose |
|-------|------|---------|
| `/login`, `/login/verify`, `/logout`, `/register`, `/verify-email`, `/forgot-password`, `/reset-password/<token>` | public | sign-in, second factor, registration, password reset |
| `/oauth/<p>/start`, `/oauth/<p>/callback`, `/fake-oauth/<p>/authorize` | public | sign in with Google / Apple / Facebook (stub consent page when `gateway = fake`) |
| `/`, `/preview`, `/browse/<folder>`, `/files/...` | viewer | overview, previews, folders |
| `/convert`, `/convert-all`, `/load`, `/load-folder`, `/upload`, `/files/.../delete` | operator | pipeline actions |
| `/formats`, `/formats/new`, `/formats/<name>/edit`, `/formats/<name>/delete` | viewer / operator | formats |
| `/promote` (GET viewer, POST operator), `/active/<kind>` | | promotion, browsing |
| `/api-load`, `/api-load/runs/<run>` (viewer); `/api-load/start`, `/api-load/stop`, `/api-load/runs/<run>/resume` (POST, operator) | viewer / operator | API loading: EOD API → `Stock_EOD_Stage` in the background (stocks typed in, a whole market - at most 3 times a day per market -, a text file - at most 100 symbols, 5 loads a day; a stock at most 5 loads a day), progress, stop / resume, runs |
| `/api-load/schedules` (POST save), `/api-load/schedules/<id>/toggle|run|delete` (POST) | operator | the daily schedules of *One day, all stocks* |
| `/calendar`, `/calendar/update`, `/calendar/days/add|remove`, `/calendar/markets/add`, `/calendar/markets/<m>/toggle` | viewer / operator / admin | calendar |
| `/symbols`, `/symbols/save|delete|import` | viewer / operator | symbols |
| `/monitor`, `/monitor/logs|errors|data-errors|history` | viewer | monitoring |
| `/correct` | operator | data correction |
| `/account`, `/account/email`, `/account/security/...`, `/account/oauth/<p>/unlink` | signed in | own account, 2FA, connected accounts |
| `/docs`, `/docs/<path>` | viewer | this documentation (`?q=` searches) |
| `/settings`, `/users`, `/users/new`, `/users/<id>/edit|delete|reset-2fa`, `/customers`, `/customers/...` | admin | administration |
| `/api/status`, `/api/logs`, `/api/errors`, `/api/data-errors`, `/api/history`, `/api/corrections`, `/api/promotions`, `/api/monitor`, `/api/active/<kind>`, `/api/symbols`, `/api/no-trading-days`, `/api/formats/<name>`, `/api/api-load`, `/api/api-load/runs`, `/api/api-load/quota?market=M`, `/api/api-load/limits?symbols=A,B&market=M`, `/api/api-load/bulk-quota?market=M&date=D`, `/api/api-load/schedules`, `/api/customers` | viewer (customers: admin) | JSON |

## Portal (:8060)

| Route | Purpose |
|-------|---------|
| `/`, `/docs` | landing page, live API documentation |
| `/buy`, `/checkout/<ref>/return`, `/checkout/<ref>/cancel`, `/fake-checkout/<kind>/<id>` | token shop (stub payment pages when `payment_gateway = fake`) |
| `/orders/<ref>`, `/orders/lookup`, `/orders/<ref>/rotate-key` | receipts |
| `/signup`, `/verify-email`, `/login`, `/login/verify`, `/logout`, `/forgot-password`, `/reset-password/<token>` | accounts |
| `/oauth/<p>/start`, `/oauth/<p>/callback`, `/fake-oauth/<p>/authorize` | sign in with a provider |
| `/account`, `/account/keys...`, `/account/password`, `/account/security...`, `/account/security/oauth/<p>/unlink` | the customer's account |
| `/admin`, `/admin/customers`, `/admin/orders`, `/admin/usage`, `/admin/api/customers`, `/admin/customers/<id>/...`, `/admin/keys/<id>/...` | administrators with a second factor |
| `/api/v1/...` | the data API ([reference](20-api.md)) |
