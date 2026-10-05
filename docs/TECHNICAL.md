---
layout: doc
lang: en
translation_key: TECHNICAL
title: pg_local_cache technical reference
seo_title: pg_local_cache RESP API, consistency, memory, and configuration
description: Technical reference for pg_local_cache RESP reads, transaction-aware invalidation, bounded PostgreSQL shared memory, monitoring, and configuration.
section: Technical
permalink: /docs/TECHNICAL.html
---

# pg_local_cache technical reference {#pg_local_cache-technical-reference}

`pg_local_cache` caches whole rows by complete primary key in bounded PostgreSQL
shared memory. Applications read rows through the authenticated RESP2 endpoint.

> **Ordinary SQL stays ordinary:** the extension installs no planner or executor
> hooks. A normal `SELECT` always uses PostgreSQL and never reads this cache.

## Supported tables and keys {#supported-tables-and-keys}

Source tables must be permanent heap tables with a valid primary key and
without RLS, partitioning, inheritance, or extension ownership.

Supported key types:

- `smallint`, `integer`, and `bigint`;
- `text`, `varchar`, and `char` with deterministic collations;
- `uuid`;
- composite primary keys made only from those types.

Unsupported relations are rejected during attachment instead of producing an
unsafe partial mapping.

## Attach, reconcile, and detach tables {#attach-reconcile-and-detach-tables}

`local_cache.attach_table(regclass)` performs one guarded setup sequence:

1. lock and validate the relation;
2. record its namespace, relation OID, and ordered primary-key columns;
3. install extension-owned statement, row, and truncate triggers;
4. reload worker mappings.

DDL event triggers invalidate cached mapping metadata. Run
`local_cache.reconcile_table(...)` or `local_cache.reconcile_all()` after
intentional schema changes. `local_cache.detach_table(...)` removes the mapping
and its triggers.

## Read path and safe fallback {#read-path-and-safe-fallback}

Each RESP `MGET` key checks shared cache when caching is enabled. On each
cache miss, the worker reads the source row in its own short transaction. Rows
larger than the cache payload limit still return from PostgreSQL but are not
cached.

## Transaction consistency {#transaction-consistency}

Mapped-write triggers in any PostgreSQL session publish per-key or
per-relation dirty-writer fences and advance generations before the commit
becomes visible. Fenced entries bypass the cache until the write finishes;
generation checks prevent stale in-flight reads from publishing, so a committed
write cannot be followed by a stale cache hit.

RESP reads use `pg_local_cache.role`, not the client's PostgreSQL role, in
independent short transactions. They do not see the client's uncommitted changes,
share its snapshot, or participate in its transaction. Setting
`pg_local_cache.enabled = off` bypasses the cache.

## Shared memory and configuration {#shared-memory-and-configuration}

Cache entries, relation states, counters, worker generations, and RESP client
slots are allocated at postmaster startup. `cache_entries` is the hard global
maximum. Each partition has a 25% headroom cap to absorb hash variance below
that maximum. Eviction scans rotating buckets, finishes each sampled collision
chain so its tail is visited, and prefers stale entries; admission failure
returns to the source table.
Each active partition cap is `ceil(1.25 * cache_entries / active_partitions)`;
small caches reduce the active partition count to avoid dynahash's minimum
allocation per table. Headroom and per-partition rounding are included in the
startup memory estimate.

| Setting | Default | Meaning |
|---|---:|---|
| `pg_local_cache.database` | `postgres` | database served by the extension |
| `pg_local_cache.cache_entries` | `16384` | hard global maximum number of shared row-cache entries |
| `pg_local_cache.lock_partitions` | `64` | maximum cache lock partitions; small caches may use fewer; power of two from `16` to `256`; requires restart (`PGC_POSTMASTER`) |
| `pg_local_cache.relation_states` | `1024` | shared mapping-state capacity |
| `pg_local_cache.memory_budget_mb` | `384` | extension startup budget |
| `pg_local_cache.port` | `6380` | RESP port; `0` is for regression tests and diagnostics only, and serves no reads |
| `pg_local_cache.bind_address` | `127.0.0.1` | RESP bind address |
| `pg_local_cache.workers` | `4` | RESP workers |
| `pg_local_cache.role` | `local_cache_worker` | RESP PostgreSQL role |
| `pg_local_cache.max_clients` | `256` | global RESP client limit |
| `pg_local_cache.max_clients_per_worker` | `64` | slots per worker |
| `pg_local_cache.idle_timeout_ms` | `300000` | idle and slow-client deadline |
| `pg_local_cache.statement_timeout_ms` | `2000` | worker statement deadline |
| `pg_local_cache.lock_timeout_ms` | `250` | worker lock deadline |
| `pg_local_cache.singleflight_wait_ms` | `25` | same-key follower wait |
| `pg_local_cache.max_pipeline_commands` | `256` | commands per event-loop turn |
| `pg_local_cache.max_dirty_keys` | `4096` | transaction key-fence bound |
| `pg_local_cache.auth_token_file` | empty | preferred RESP credential |
| `pg_local_cache.auth_token` | empty | development-only inline token |
| `pg_local_cache.enabled` | `on` | SIGHUP cache kill switch; RESP reads go directly to source while off |
| `pg_local_cache.tls` | `off` | enable TLS on the RESP listener; requires PostgreSQL built with OpenSSL |
| `pg_local_cache.tls_cert_file` | empty | PEM server certificate/chain; required when TLS is on |
| `pg_local_cache.tls_key_file` | empty | PEM server private key; required when TLS is on |
| `pg_local_cache.tls_ca_file` | empty | trusted client CA; setting it enables mutual TLS |
| `pg_local_cache.tls_min_protocol_version` | `TLSv1.2` | minimum TLS version (`TLSv1.2` or `TLSv1.3`) |
| `pg_local_cache.allow_plaintext_network` | `off` | postmaster opt-in for plaintext listeners outside IPv4 loopback |
| `pg_local_cache.allow_superuser` | `off` | development-only role override |

Most settings are postmaster settings. See the
[installation guide](INSTALL_EXISTING.md) for package installation and restart
steps.

## RESP2 endpoint {#optional-resp2-endpoint}

RESP2 uses the same mappings and shared cache. Wire keys use this shape:

```text
CRUD:database.schema.table:{"pk_column":<json-scalar>,...}
```

Supported commands are authenticated, bounded `MGET`, `SET`, `DEL`, and scoped
invalidation. RESP workers use one configured PostgreSQL role; they do not
inherit each network client's database ACLs.

RESP TLS uses dedicated `pg_local_cache.tls_*` settings and is independent of
PostgreSQL `ssl_*` settings. PostgreSQL TLS on the SQL port does not secure
RESP, and RESP TLS does not change the SQL listener. Enable `pg_local_cache.tls`
and provide a server certificate and key; setting `pg_local_cache.tls_ca_file`
verifies client certificates and enables mutual TLS. The minimum protocol
defaults to `TLSv1.2` and can be raised to `TLSv1.3`. OpenSSL system cipher
defaults apply. The private key follows [PostgreSQL's server key file
rule](https://www.postgresql.org/docs/current/ssl-tcp.html). Prefer TLS beyond
loopback. With TLS off, plaintext on a non-loopback listener requires the
explicit `pg_local_cache.allow_plaintext_network = on` opt-in, limited to
trusted networks. A non-loopback listener still requires a token of at least 32
bytes; prefer a mode-restricted token file over an inline token.

`pg_local_cache.enabled` is a SIGHUP setting. Each RESP worker applies a reload
asynchronously at its next command boundary, after any command it is executing
finishes. `local_cache.health()` reports `cache_enabled` as seen by the SQL
session that calls it; it does not acknowledge that every worker has applied
the setting. Turning it on advances the global cache epoch before workers resume
cached reads. To disable cache lookups without restarting PostgreSQL:

```sql
ALTER SYSTEM SET pg_local_cache.enabled = off;
SELECT pg_reload_conf();
```

`SET` and `DEL` continue writing the mapped table while caching is disabled;
table-trigger invalidation also remains active. To re-enable cached reads:

```sql
ALTER SYSTEM SET pg_local_cache.enabled = on;
SELECT pg_reload_conf();
```

## Health and monitoring {#health-and-monitoring}

`local_cache.health()` reports readiness, `cache_enabled`, and mapping convergence.
`local_cache.stats()` returns JSON counters. `local_cache.metrics()` exposes the
typed metrics row used by the exporter.

TLS counters `tls_handshakes_total` and `tls_handshake_failures_total` are
exposed in `stats()` and `metrics()`.

Database reads, invalidations, admission rejection, dirty-key fallback,
singleflight, worker, and RESP counters remain available. The four counters for
the removed SQL read API were removed in 3.0.0.

Next: use the [installation guide](INSTALL_EXISTING.md) for Debian and RPM
package verification, PGXS source builds, configuration, restarts, upgrades,
and uninstall. For breaking changes and rollback steps, see the
[2.x to 3.0 upgrade guide](UPGRADING.md).
