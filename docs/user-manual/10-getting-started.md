# Getting started

## Install and start

```bash
cd Stock_DataLoadGit
python -m venv .venv
.venv\Scripts\activate                 # macOS / Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
python -m stock_dataload init-db       # creates the tables and the no-trading days of this + next year
python -m stock_dataload dashboard     # http://127.0.0.1:8050
```

Use the **same interpreter** for installing and running (`python -m pip …`,
`python -m stock_dataload …`). If a command says *No module named flask* (or
*coverage*), the requirements were installed into a different Python - see
[Troubleshooting](../operations/50-troubleshooting.md).

## The first sign-in

The shipped database contains **no user**. When the dashboard starts with an
empty user table it creates the administrator **`admin`** and prints a
**random one-time password to the console**:

```
========================================================================
  Initial administrator created: username 'admin'
  One-time password: <random - copy it from YOUR console>
  It must be changed at the first login. Then add users on the 'Users' page.
========================================================================
```

The password is different on every machine and never stored - copy it from
the console window where the dashboard runs. If you missed it, stop the
dashboard and set a password yourself:

```bash
python -m stock_dataload user passwd admin            # prompts for the new password
python -m stock_dataload user add admin --role admin  # or create the administrator before the first start
```

## Creating your own account

On the sign-in page:

* **Create account** - user name, e-mail address, password. A 6-digit code
  is sent to the e-mail address; enter it on the next page to activate the
  account. You start as *viewer*; ask an administrator for more.
* **Continue with Google / Apple / Facebook** - signs you in with that
  identity. The first time, an account is created for the e-mail address
  the provider reports (or connected to your existing account with the same
  verified e-mail address).
* **Forgot your password?** - a single-use reset link is sent to the
  e-mail address of the account.

> In the default configuration e-mail is in **stub mode**: codes and links
> are printed to the *server console*, not sent. The pages tell you so.
> Administrators switch to a real mail server in `[mail]`
> ([Operations › Configuration](../operations/10-configuration.md)).

## Securing your account

On **My account** (top right) you can change the password, set your e-mail
address, enable **two-factor authentication** (authenticator app, e-mail
code, SMS code), save recovery codes and connect Google / Apple / Facebook.
Details: [Account security](80-account-security.md).

## Where things are

| Page | Purpose |
|------|---------|
| Overview | the pipeline: upload → convert → load, bulk-load a folder, recent loads, active tables |
| File formats | describe files that are not in the default layout |
| Active tables | promote staged rows, browse daily / weekly / monthly data |
| Trading calendar | no-trading days per market and year |
| Symbols | the stock symbol table (trading windows) |
| Monitoring | activity log, errors, data-quality findings, load history |
| Data correction | fix or add a staging row |
| Docs | this documentation |
| Settings, Users, Customers | administrators only |
