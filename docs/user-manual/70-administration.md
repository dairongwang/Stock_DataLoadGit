# Administration: settings, users and the portal's customers

These pages are for the **admin** role.

## Settings

*Settings* edits `config.ini`: the folders, the database and table names,
loader options, the validation rules of the promotion, the portal (payment
methods, token packs, metering), sign-in providers, e-mail / SMS and the
account rules. *Save* validates everything, writes `config.ini` and reloads
the application - a wrong value is shown and nothing is saved. Secrets are
not entered here; they come from environment variables
([Operations › Configuration](../operations/10-configuration.md)).

## Users

| Action | How |
|--------|-----|
| add a user | *Users* → *New user*: user name, role, optional e-mail address, a password (tick *must change at first sign-in* for a temporary one) |
| change role / deactivate | *Edit*; a deactivated user cannot sign in, their history stays |
| reset a password | *Edit* → new password (the user can also use *Forgot your password* if they have an e-mail address) |
| reset 2FA | *Reset 2FA* removes all second factors and recovery codes of a locked-out user |
| delete | *Delete* - not possible for your own account or the last administrator |

Self-registered users (*Create account* on the sign-in page) start as
*viewer*; raise their role here. The same can be done on the command line:
`python -m stock_dataload user list | add | passwd | role | enable | disable | reset-2fa | delete`.

## Customers

*Customers* shows the customers of the public portal: accounts and guests,
their API keys with balances, recent orders (revenue) and API requests.

* **Grant** credits tokens to a key by hand - for refunds, trials or invoiced
  deals. It is recorded as a paid order of the `manual` provider with amount
  0, so the ledger stays complete.
* **Disable key** stops one key; **Disable customer** stops all their keys.

The portal has its own admin area with the same data (*Admin* on the portal,
for portal administrators with two-factor authentication). Command line:
`python -m stock_dataload customers list | orders | usage | stats | grant | new-key | ...`.
