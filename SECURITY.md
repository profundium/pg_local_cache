# Security Policy

## Supported versions

Security fixes target the latest `2.0.x` release. The `3.x` line will be
supported after its first release. Upgrade to the latest supported release
before reporting an issue as unresolved.

## Reporting a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/profundium/pg_local_cache/security/advisories/new)
or email **maxbronnikov10 <bronnikovmr@gmail.com>**. Please include the affected
version, PostgreSQL major version, a concise impact description, and a minimal
reproducer when possible. Do not include production secrets or customer data.

## Scope and response

Reports are in scope when they expose or corrupt data, bypass authorization,
compromise server availability, or enable code execution through the RESP
listener, SQL functions, shared memory, or installation and upgrade tooling.

Reports involving RESP TLS or mTLS are also in scope, including
certificate-validation bypasses, unexpected plaintext access, or private-key
permission checks that fail open. RESP TLS uses settings separate from
PostgreSQL `ssl_*` settings. For non-loopback listeners, prefer TLS; enable
`pg_local_cache.allow_plaintext_network` only for plaintext on a trusted
network.

We aim to acknowledge reports within 3 business days and provide an initial
triage within 10 business days. Confirmed active exploitation receives priority.
Response times are targets, not a service-level guarantee.

## Disclosure

We coordinate disclosure with the reporter and publish a fix and advisory
together when practical. We aim for a 90-day disclosure window from confirmation;
we may agree on a different date when exploitation risk, release readiness, or
reporter needs warrant it. Please allow maintainers time to investigate and
prepare a fix before publishing technical details.
