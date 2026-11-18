# Payments

## Providers

Two providers, both using the provider's **hosted payment page** so the
portal never handles card data (`payments.py`):

| Provider | Start | Return / verification |
|----------|-------|-----------------------|
| **Stripe Checkout** | `POST /v1/checkout/sessions` (mode `payment`, one line item with the pack, `client_reference_id` = order reference, success URL with `{CHECKOUT_SESSION_ID}`) → redirect to `session.url` | `GET /v1/checkout/sessions/{id}`: `payment_status == paid`, `client_reference_id` == order, `amount_total` / `currency` == order; the payment intent id becomes the order's provider reference |
| **PayPal Checkout** (Orders v2) | `POST /v1/oauth2/token` (client credentials, cached, refreshed on 401) then `POST /v2/checkout/orders` (intent `CAPTURE`, `reference_id` = order reference, amount as decimal string) → redirect to the `approve` link | `POST /v2/checkout/orders/{id}/capture`: status `COMPLETED`, `reference_id` == order, captured amount / currency == order; the capture id becomes the provider reference |

Both implement `start_checkout(order, success_url, cancel_url) -> (url, provider_ref)`
and `verify_checkout(order, return_args) -> PaymentResult`. The portal
stores the provider reference on the order at start and refuses a return
whose session / PayPal order id does not match it.

## Order life cycle

```
 buy page ──► Api_Order pending ──► start_checkout ──► customer on Stripe / PayPal
                                                             │ pays            │ cancels
                                          /checkout/<ref>/return           /checkout/<ref>/cancel
                                                             │                     │
                               verify_checkout ok? ──► complete_order (paid,   fail_order (cancelled)
                                                       tokens credited, key
                                                       created if needed)
```

`complete_order` is idempotent (a second return does not credit twice) and,
if the key chosen at purchase time was revoked meanwhile, credits a fresh
key instead of losing the payment. Manual grants are orders of provider
`manual` with amount 0.

## The fake gateway

`payments.FakeGateway` implements the transport interface and answers the
Stripe and PayPal endpoints in memory (`fake.stripe.local`,
`fake.paypal.local`). It hands out checkout URLs pointing at the portal's
own pages `/fake-checkout/<stripe|paypal>/<id>`, where *Pay* calls
`gateway.approve()` and *Cancel* `gateway.cancel()`, then redirects to the
success / cancel URL exactly like the real providers. `fail_with` simulates
an outage. `make_providers(settings, gateway)` wires the real provider
classes to it when `payment_gateway = fake`, so the provider code under test
is the production code.

## Going live

`payment_gateway = live`, `$STOCK_DATALOAD_STRIPE_SECRET_KEY`,
`paypal_client_id` + `$STOCK_DATALOAD_PAYPAL_CLIENT_SECRET`,
`paypal_environment = sandbox` for PayPal's test system first, `live`
afterwards; `[portal] public_url` must be the address customers return to.
Checklist: [Operations › Deployment](../operations/20-deployment.md).
