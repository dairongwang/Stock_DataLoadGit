# Troubleshooting

## "No module named coverage" / "No module named flask"

The requirements were installed into a different interpreter than the one
running the command. Use the **same** `python` for both:

```bash
python -m pip install -r requirements.txt
python -m coverage report -m
```

Inside the project, activate the venv first (`.venv\Scripts\activate`) -
the prompt shows `(.venv)`. `python run_coverage.py` installs `coverage`
for the interpreter it runs with if it is missing, then runs the suite and
prints the report. If `pip install -r requirements.txt` itself fails,
check the first error line: a package without a wheel for your Python
version stops the whole installation; the optional extras (`playwright`,
`qrcode`) are therefore in `requirements-dev.txt`.

## I cannot sign in as admin

* The shipped database has **no user**. On the first start the dashboard
  prints a random one-time password for `admin` to the console - it is
  different every time; the value in any document is only an example.
* Missed it? `python -m stock_dataload user passwd admin` sets a new one;
  `python -m stock_dataload user list` shows the accounts.
* *"Please choose a new password"* after signing in: the account was created
  with *must change at first sign-in*.

## The verification / sign-in code never arrives

With `[mail] provider = console` (default) nothing is sent: the code is
**printed in the server console** where the dashboard / portal runs. For real
e-mail configure `[mail] provider = smtp`. Codes are valid 10 minutes; a new
one can be requested after 60 seconds; after 5 wrong attempts request a new
code.

## Locked out of two-factor authentication

Use a recovery code, or let an administrator reset the second factor:
dashboard *Users → Reset 2FA* (`user reset-2fa NAME`), portal *Admin →
Customers → Reset 2FA* (`customers reset-2fa EMAIL`).

## "Continue with Google" shows a strange form

`[oauth] gateway = fake` is active: the form is the built-in stub identity
provider. Configure the real client ids and `gateway = live` for production.

## The payment page says "Built-in stub of Stripe Checkout"

`payment_gateway = fake` - test mode. See [Deployment](20-deployment.md) to
go live.

## A delivery was skipped

Its file name carries a date on which the market was closed
(`No_Trading_Day`). Check `no-trading-days check MARKET DATE`; remove the
closure if the market did trade, or load with `--include-no-trading-days`.

## Rows were rejected by promote daily

Monitoring → *Data errors* lists them with the failing rule; fix them on
*Data correction* and promote again. Rows before a symbol's first trade date
are rejected on purpose - correct the date in *Symbols* if the listing
information is wrong.

## The test-suite asks for a password

It must not - the CLI tests patch the terminal check. If you see a prompt,
you are running an old copy of the tests; `git pull` and run
`python -m unittest discover -s tests -t .` again.

## The database is huge

`python -m stock_dataload db size`, then `db purge-stage --all --vacuum`
([Housekeeping](40-database-housekeeping.md)).
