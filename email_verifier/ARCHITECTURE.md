# Chrissa Automates Email Verifier Architecture

## Purpose

This service verifies email addresses for the Chrissa Automates Contact Enricher. It is intentionally conservative: only strong SMTP-level evidence becomes `valid`; uncertain results stay `unknown` or `catch_all` and are kept out of the Ready to Email list.

## Upstream source preserved

Original open-source engine: `AfterShip/email-verifier`

Pinned source commit: `d8462fdd79d9aca5452bd220f9cd5224976fea49`

License: MIT, Copyright (c) 2020 AfterShip. The upstream LICENSE must remain with every copied or modified source snapshot.

We do not follow upstream `main` automatically. The pinned commit is deliberate so verifier behavior cannot change without review.

## How the engine works

For one email address the underlying verifier performs these checks in order:

1. Parse and validate email syntax.
2. Classify the domain/account metadata, including free-email provider, role account and disposable-domain checks.
3. Stop early if the domain is disposable.
4. Query DNS MX records for the email domain.
5. Open an SMTP connection to an available MX host when SMTP checking is enabled.
6. Send EHLO/HELO using the configured verifier hostname.
7. Send MAIL FROM using the configured verifier sender address.
8. Test a random recipient at the domain to detect catch-all behavior.
9. If the domain is not catch-all, test the requested recipient with RCPT TO.
10. Return structured reachability, MX, SMTP, disposable, role-account, free-domain and syntax data.

The verifier does not send an email message. SMTP is used only to ask the receiving mail server whether it will accept a recipient.

## Chrissa Automates custom wrapper

Our HTTP wrapper lives in `email_verifier/main.go`.

Custom behavior:

- `POST /v1/verify` accepts `{ "email": "..." }`.
- `GET /health` exposes health and engine information.
- SMTP verification is explicitly enabled.
- Domain typo suggestions are enabled.
- Sender identity comes from `VERIFIER_FROM_EMAIL`.
- EHLO hostname comes from `VERIFIER_HELLO_NAME`.
- SMTP connect timeout comes from `SMTP_CONNECT_TIMEOUT`.
- SMTP operation timeout comes from `SMTP_OPERATION_TIMEOUT`.
- Inputs are trimmed and lower-cased.
- Request bodies are size-limited.
- The API reports the pinned source commit in every response.

## Our verdict policy

`invalid`
- syntax is invalid, OR
- the domain has no MX records, OR
- the domain is disposable, OR
- the underlying reachability result is `no`.

`valid`
- the underlying reachability result is `yes`.

`catch_all`
- the SMTP check says the domain accepts random addresses, so the exact mailbox cannot be proven.

`unknown`
- SMTP/DNS/network/provider behavior does not give enough evidence for a safe valid/invalid decision.

The Contact Enricher treats only `valid` as Ready to Email. Catch-all and unknown results go to Review.

## Operational caveats

SMTP verification is inherently imperfect. Some mail providers block or tarpitting verification probes, some accept every RCPT command and reject later, and some infrastructure blocks outbound port 25. Therefore this project never promises zero bounces.

The sender identity matters. `VERIFIER_FROM_EMAIL` and `VERIFIER_HELLO_NAME` should use a domain we control because some receiving servers reject reserved/default sender identities before checking the requested recipient.

## Railway deployment

Railway service: `email-verifier`

The service is built from this repository and branch, using `Dockerfile.email-verifier`.

Expected environment variables:

- `VERIFIER_FROM_EMAIL`
- `VERIFIER_HELLO_NAME`
- `SMTP_CONNECT_TIMEOUT`
- `SMTP_OPERATION_TIMEOUT`

## Dependency independence plan

A complete source snapshot of the pinned AfterShip commit is stored under `third_party/aftership-email-verifier/` by the repository snapshot workflow. Once that snapshot exists, `Dockerfile.email-verifier` must use the local module copy instead of downloading `github.com/AfterShip/email-verifier` during every build.

This means an upstream deletion, archive, breaking release or repository rename must not prevent us from rebuilding the verifier.

## Safe upgrade procedure

Never update the verifier dependency just because upstream has a newer commit.

To upgrade:

1. Review upstream changes and license.
2. Test syntax, MX, catch-all, valid, invalid, disposable and provider-blocked cases.
3. Compare verdict distributions against the current pinned version.
4. Update the source snapshot.
5. Update the pinned source commit constant.
6. Deploy a canary.
7. Only then promote to production.
