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

Cache descriptors, indexes, dirty markers, relation states, counters, worker
generations, and RESP client slots are allocated at postmaster startup.
`cache_entries` is the hard global descriptor limit, split exactly across the
active partitions. Each 120-byte descriptor holds fence/version/lease state,
the key hash and length, and a 32-bit arena block reference; key and value
bytes live in the partition arena. Each partition indexes descriptors with
4-byte IDs in an open-addressed table sized to keep load at or below 0.5. A
separate bucket-sized scratch array rebuilds tombstoned indexes. Probes stop at
64 buckets, and rebuild starts after tombstones exceed one eighth of the table.

The partition-local arena assigns 64 KiB pages on demand to power-of-two block
classes from 256 bytes through 16 KiB. Positive blocks hold canonical key bytes
and the validated JSON payload (header, JSON and CRC); negative blocks hold
only key bytes. Empty pages return to that partition's free-page pool and can
be assigned to another class. Admission evicts eligible entries from the
requested class using the bounded sampling policy, skipping dirty entries and
active loads. If the requested class remains unavailable, the row is served
from PostgreSQL and is not cached. Index, descriptor or arena pressure never
turns a successful source read into an error.

Dirty keys without cache entries use a separate bounded marker table and key
arena. Markers are split across partitions, cannot evict cached values, and
block cache-entry creation for the same key while any writer holds the marker.
Marker or transaction-local dirty-set exhaustion widens fencing to the
relation, then global scope if relation state is unavailable.
By default, marker capacity scales with the configured cache: the entry limit
is `min(16384, max(1024, floor(cache_entries / 4)))`. The key-memory limit is
`min(16 MiB, max(1 MiB, floor(memory_budget_mb / 25) MiB))`; the actual key
arena also cannot exceed the marker entry limit. Set either marker option to a
positive value to override its automatic limit. Explicit limits retain their
supported ranges and remain part of the exact startup budget check.

The default `cache_entries` is derived at startup as the largest power of two
that leaves at least half of the default 384 MiB budget for arena pages after
exactly accounting for descriptors, both index arrays, markers and marker
keys, registry, partition metadata, alignment, and worker buffers. With the
default four workers and 64 client slots per worker, the result is 262,144:
30 MiB of descriptors, 4 MiB of bucket and scratch IDs, about 17.2 MiB for
markers and marker keys, about 122.2 MiB for worker memory, and at least 192
MiB for the arena. With root, registry, locks, page descriptors, and alignment,
that minimum layout uses about 366 MiB; the remaining roughly 18 MiB adds four
64 KiB pages per partition, for about 208 MiB of page capacity. Doubling the
descriptor count adds 34 MiB, exceeding the budget while preserving the 192
MiB arena reserve. `cache_entries` can be
raised to 16,777,216 when the configured budget and other components fit.
Startup reports a per-component breakdown and fails if they do not fit.

With 100,000 rows whose key plus approximately 150-byte JSON payload fits a
256-byte class, the arena needs about 24.4 MiB plus page slack. One million
such rows need about 244.2 MiB; a 512 MiB budget fits this with a smaller
worker configuration such as one RESP worker. The exact startup estimate is
authoritative for each configuration.

| Setting | Default | Meaning |
|---|---:|---|
| `pg_local_cache.database` | `postgres` | database served by the extension |
| `pg_local_cache.cache_entries` | `262144` | hard global maximum number of compact shared row-cache descriptors; derived from the 384 MiB default budget; range `128`–`16777216` |
| `pg_local_cache.dirty_marker_entries` | `-1` | maximum number of shared dirty-key markers; `-1` selects the automatic limit above; explicit range `128`–`1048576` |
| `pg_local_cache.dirty_marker_memory_mb` | `-1` | maximum memory for dirty-marker keys; `-1` selects the automatic limit above; explicit range `1`–`1024` MiB |
| `pg_local_cache.lock_partitions` | `64` | maximum cache lock partitions; small caches may use fewer; power of two from `16` to `256`; requires restart (`PGC_POSTMASTER`) |
| `pg_local_cache.relation_states` | `1024` | shared mapping-state capacity |
| `pg_local_cache.memory_budget_mb` | `384` | hard extension startup budget for shared cache storage and bounded RESP worker memory |
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
singleflight, worker, and RESP counters remain available. RESP fast-path stats
include `fast_path_hits`, `fast_path_fallbacks` (total), and per-reason counters
`fast_path_fallback_key_form`, `fast_path_fallback_mapping_shape`,
`fast_path_fallback_multi_key`, and `fast_path_fallback_cache_state`. The four
counters for the removed SQL read API were removed in 3.0.0.

The arena counters report its configured page capacity, live requested bytes,
and class slack. `arena_admission_rejections_total` counts rows left uncached
when no eligible block can be admitted. `dirty_marker_entries` is the active
marker count; `dirty_marker_highwater` records its peak, and
`dirty_marker_fallbacks_total` counts keyed publications widened to relation
or global fences because marker admission failed. `dirty_marker_entries_effective`
and `dirty_marker_memory_mb_effective` report resolved marker limits after
automatic sizing; `dirty_marker_memory_capacity_bytes` reports allocated key
storage, which can be lower when the entry limit is binding.

Next: use the [installation guide](INSTALL_EXISTING.md) for Debian and RPM
package verification, PGXS source builds, configuration, restarts, upgrades,
and uninstall. For breaking changes and rollback steps, see the
[2.x to 3.0 upgrade guide](UPGRADING.md).
