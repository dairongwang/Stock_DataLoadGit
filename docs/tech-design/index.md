# Technical design

For developers: how the code is organised and why. Each page goes one level
deeper than the [user manual](../user-manual/index.md); the
[reference](../reference/index.md) has the exhaustive lists.

| Page | Covers |
|------|--------|
| [Architecture](10-architecture.md) | processes, packages, how the pieces talk, design principles |
| [Data model](20-data-model.md) | every table and its relationships |
| [Pipeline internals](30-pipeline.md) | readers / converter, loader transactions, the promotion procedures |
| [Accounts and security](40-accounts-and-security.md) | passwords, sessions, CSRF, 2FA (TOTP, codes), OAuth, roles |
| [Payments](50-payments.md) | Stripe / PayPal flows, verification, the fake gateway |
| [Data API](60-data-api.md) | metering, atomic charging, catalogue cache |
| [External services and stubs](70-external-services-and-stubs.md) | the transport abstraction and every stub |
| [Testing](80-testing.md) | the test-suite, what is mocked, how to add tests |
