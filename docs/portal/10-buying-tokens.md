# Buying tokens

## Packs and price

The purchase page (*Buy tokens*) lists the packs - by default *Starter* 100
tokens, *Standard* 500, *Pro* 2,000, *Enterprise* 10,000 - with their
prices. One token pays for **one ticker for one year** of daily, weekly or
monthly data. Tokens never expire.

## Guest or account?

| | Guest checkout | With an account |
|-|----------------|-----------------|
| needs | an e-mail address | sign in (or create an account) |
| the tokens go to | a **new API key**, or an existing key you paste in (*top up*) | a new key or one of your keys (pick it) |
| later | find the receipt with **Find my order** (order reference + e-mail); rotate a lost key there | everything on the account page |

A guest can always create an account later and **claim** the purchases with
an order reference - the key and its tokens move to the account.

## Paying

Choose **Card (Stripe)** or **PayPal**, accept the terms and press
*Continue to payment*. You are taken to the provider's page, pay there,
and return to the **receipt**:

* the **API key** is shown **once** - copy it now. The portal stores only a
  fingerprint of the key; if you lose it, generate a new one (same balance)
  from the receipt or the account page;
* the order reference (`ORD-…`), pack, amount, payment and the key's balance.

If you cancel on the provider's page the order is cancelled and nothing is
charged. If the payment did not complete, the receipt says *pending* - just
start again.

> With the portal in **test mode** (the default, `payment_gateway = fake`)
> the provider pages are built-in stubs with *Pay* / *Cancel* buttons;
> nothing is charged. Live payments are described in
> [Operations › Deployment](../operations/20-deployment.md).
