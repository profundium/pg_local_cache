---
layout: doc
lang: en
translation_key: TECHNICAL
title: pg_local_cache technical reference
seo_title: pg_local_cache RESP API, consistency, memory, and configuration
description: Reference for RESP reads, supported tables, transaction fences, TLS, shared memory, metrics, and PostgreSQL settings.
section: Technical
permalink: /docs/TECHNICAL.html
---

# pg_local_cache technical reference {#pg_local_cache-technical-reference}

Technical reference for the RESP2 endpoint, cache consistency, resource bounds, and security. See [quickstart](QUICKSTART.md) and [installation](INSTALL_EXISTING.md) for setup steps.

## Supported tables and keys {#supported-tables-and-keys}

Attach permanent heap tables with a valid primary key. Partitioned, inherited, row-level-security, temporary, foreign, and extension-owned tables are unsupported. Supported primary-key types are `smallint`, `integer`, `bigint`, `text`, `varchar`, `char` with deterministic collations, and `uuid`; composite keys may use these types, up to 16 columns.

DDL changes require mapping reconciliation. See [installation](INSTALL_EXISTING.md#attach-a-table).

## Read path and safe fallback {#read-path-and-safe-fallback}

![RESP MGET read path: cache hit, fenced source fill, and kill-switch bypass.](diagrams/read-path.svg)

Each RESP `MGET` key is validated and canonicalized before lookup. The allocation-free single-key fast path applies only to a table with a one-column primary key of a supported integer, `text`, or unbounded `varchar` type, unrestricted typmod, and key JSON supported by the scanner. Text keys require a UTF-8 database; integer keys remain eligible with other encodings. Other key shapes, multi-key requests, and unsupported cache states use the general path. On a miss, the worker reads the source table in a short transaction, then publishes a fill only if its read fence is still current. Missing rows return nil. Rows whose payload cannot fit shared cache may still return from PostgreSQL if their JSON fits the RESP value limit.

### Deferred misses and lock deadlines {#deferred-misses-and-lock-deadlines}

Before entering SPI for a cache miss, the worker tries to acquire the source
relation's `AccessShareLock` without waiting. If the lock is unavailable, it
releases that exact load claim, aborts the worker transaction, and places the
request in a bounded per-worker deferred-miss queue. It does not
enter SPI while waiting for the relation lock. The queue holds at most
`pg_local_cache.max_deferred_misses` requests per worker (default `8`) and
accounts at most 512 KiB of retained request bytes per worker. Request bytes
remain in the client's input buffer outside the per-command context. Each
client can have at most one deferred request. The setting range is `1`–`64`. If either limit
prevents enqueueing, `MGET` returns `-ERR busy: relation locked, retry` in
that client's response order.

A deferred request blocks later commands and responses from its client while
other clients on the same worker continue to run. Once the lock is available,
the worker retries the retained request and validates the captured
mapping generation again. Queue time uses the request's remaining
`statement_timeout` deadline; expiration returns `-ERR MGET deadline exceeded`
in order. This deferral covers the initial source-relation lock only. Waits on
catalog locks, child relations, or conflicting concurrent writes can still
occur and remain bounded by `lock_timeout` and `statement_timeout`. Source
query execution also remains subject to `statement_timeout`. Complete
isolation from blocked misses would require separate miss executors.
RESP `STAT` JSON reports `deferred_misses_total`,
`deferred_misses_current`, `deferred_timeouts_total`, and
`deferred_rejections_total` for the worker serving that connection.

## Transaction consistency {#transaction-consistency}

![Write invalidation: pre-commit fences protect committed writes; rollback before fence publication preserves prior entries.](diagrams/write-invalidation.svg)

Mapped-table row and statement triggers collect deduplicated dirty keys or a relation in transaction-local state. At pre-commit, keyed fences mark cached entries dirty; a separate marker protects a dirty key that has no cache entry and blocks a new fill while writers hold it. If marker or transaction-local key capacity is exhausted, fencing widens to the relation; if relation state is unavailable, it widens to a global fence. After commit, readers cannot use an old entry, and in-flight fills with stale generations are rejected. A rollback before fence publication discards the dirty state and leaves the prior entry valid. Cache entries, indexes, markers, and arenas use independent partition locks; global fences do not require a single global cache lock.

RESP source reads use `pg_local_cache.role` in short transactions independent of the caller's SQL transaction.

## Memory sizing and settings {#shared-memory-and-configuration}

The extension preallocates bounded shared cache, mapping, and worker/client state at PostgreSQL startup. `memory_budget_mb` limits the deterministic extension allocation. Admission failures and eviction do not allocate beyond configured capacity; reads fall back to PostgreSQL.

`cache_entries` is the global descriptor limit, divided across active
partitions. Keys and row JSON live separately in each partition's arena;
64 KiB pages are assigned on demand to block classes from 256 bytes through
16 KiB. Positive entries store validated JSON with a descriptor fingerprint
and CRC; negative entries store the key only. Eviction samples eligible entries
from the needed class. If space is unavailable, the row still returns from
PostgreSQL without cache admission.

`lock_partitions` sets the maximum number of independent cache partitions
(default `64`; power of two from `16` to `256`). Small caches reduce the partition count toward a target of 32 descriptors
per partition, subject to a 16-partition floor. Each partition owns its lock,
index, dirty markers, and arena.

Dirty keys without cache entries use a separate bounded marker table and key
arena. Markers cannot evict cached values and block fills for their key while a
writer holds them. Automatic marker limits are
`min(16384, max(1024, floor(cache_entries / 4)))` entries and
`min(16, max(1, floor(memory_budget_mb / 25)))` MiB of key memory; `-1` selects
automatic sizing. Startup includes all components in the memory-budget check
and reports a per-component breakdown on failure.

The built-in default is `262144` descriptors, derived against the default
384 MiB budget while reserving at least half for arena pages. The supported
range is `128`–`16777216`; the configured budget and worker/client settings
must still fit. `cache_entries` counts descriptors, not fixed byte-sized row
slots: actual row capacity depends on arena bytes and row size. With suitable
memory and small rows, the arena can hold millions of keys.

| Setting | Default | Range | Reload |
|---|---:|---|---|
| `pg_local_cache.enabled` | `on` | `on` / `off` | SIGHUP |
| `pg_local_cache.allow_plaintext_network` | `off` | `on` / `off` | Restart |
| `pg_local_cache.tls` | `off` | `on` / `off` | Restart |
| `pg_local_cache.tls_cert_file` | empty | PEM file path | Restart |
| `pg_local_cache.tls_key_file` | empty | PEM file path | Restart |
| `pg_local_cache.tls_ca_file` | empty | CA PEM file path | Restart |
| `pg_local_cache.tls_min_protocol_version` | `TLSv1.2` | `TLSv1.2` / `TLSv1.3` | Restart |
| `pg_local_cache.port` | `6380` | `0`–`65535`; `0` is for tests and diagnostics and serves no reads | Restart |
| `pg_local_cache.workers` | `4` | `1`–`32` | Restart |
| `pg_local_cache.cache_entries` | `262144` | `128`–`16777216` | Restart |
| `pg_local_cache.dirty_marker_entries` | `-1` | `-1` or `128`–`1048576` | Restart |
| `pg_local_cache.dirty_marker_memory_mb` | `-1` | `-1` or `1`–`1024` MiB | Restart |
| `pg_local_cache.lock_partitions` | `64` | maximum power of two, `16`–`256`; small caches may use fewer | Restart |
| `pg_local_cache.relation_states` | `1024` | `128`–`8192` | Restart |
| `pg_local_cache.max_clients` | `256` | `1`–`4096`; at most worker slots | Restart |
| `pg_local_cache.max_clients_per_worker` | `64` | `1`–`4096` | Restart |
| `pg_local_cache.memory_budget_mb` | `384` | `64`–`8192` MB | Restart |
| `pg_local_cache.idle_timeout_ms` | `300000` | `1000`–`86400000` | Restart |
| `pg_local_cache.statement_timeout_ms` | `2000` | `100`–`60000` | Restart |
| `pg_local_cache.lock_timeout_ms` | `250` | `10`–`60000` | Restart |
| `pg_local_cache.singleflight_wait_ms` | `25` | `0`–`1000` | Restart |
| `pg_local_cache.max_deferred_misses` | `8` | `1`–`64` per worker | Restart |
| `pg_local_cache.max_pipeline_commands` | `256` | `1`–`4096` | Restart |
| `pg_local_cache.max_dirty_keys` | `4096` | `128`–`16384` | Restart |
| `pg_local_cache.bind_address` | `127.0.0.1` | IPv4 address | Restart |
| `pg_local_cache.database` | `postgres` | Database name | Restart |
| `pg_local_cache.role` | `local_cache_worker` | PostgreSQL LOGIN role | Restart |
| `pg_local_cache.auth_token_file` | empty | PostgreSQL OS-user-owned mode `0400` or `0600` file | Restart |
| `pg_local_cache.auth_token` | empty | Inline token; development only | Restart |
| `pg_local_cache.allow_superuser` | `off` | `on` / `off`; development only | Restart |

All settings except `enabled` are postmaster settings and require restart. Client slots require `max_clients <= workers × max_clients_per_worker`.

Each RESP worker also checks its soft `RLIMIT_NOFILE`. It must be at least
`min(max_clients, max_clients_per_worker) + 33`; raise the process or container
`nofile` limit when increasing client capacity.

## RESP2 endpoint {#optional-resp2-endpoint}

The endpoint accepts RESP2. Keys use `CRUD:<db>.<schema>.<table>:<json pk>`. `MGET` preserves request order and duplicates; a missing row is a nil element. Each request accepts at most 1,024 keys, each JSON row is limited to 65,536 bytes, and the encoded reply is limited to 66,560 bytes.

Supported data commands are `MGET`, `SET`, and `DEL`; `AUTH` is required. The endpoint also supports `PING`, `ECHO`, `INFO`, `STAT`/`STATS`, scoped `INVALIDATE`, `HELLO 2`, `QUIT`, `CLIENT SETINFO`/`SETNAME`/`GETNAME`/`ID`, `COMMAND`, and `SELECT 0`. Unsupported commands return an error. RESP clients use database 0; database and table scope come from each cache key.

## TLS and security model {#security-model}

The listener binds to IPv4 loopback by default. RESP TLS uses extension-specific settings, not PostgreSQL `ssl_*`. It requires a PostgreSQL build with OpenSSL, a PEM server certificate and key, and a restart. Setting `tls_ca_file` enables required client-certificate verification (mTLS); minimum TLS version defaults to 1.2.

With TLS disabled, non-loopback plaintext requires `allow_plaintext_network=on` and a trusted network. Non-loopback listeners require a token of at least 32 bytes. Prefer a mode-restricted token file. All RESP clients share one configured PostgreSQL LOGIN role; PostgreSQL grants to each network client are not evaluated separately. Superuser workers are off by default and intended only for development.

## Cache kill switch {#cache-kill-switch}

`pg_local_cache.enabled` is a SIGHUP cache kill switch. When off, RESP reads bypass shared cache and read source tables; `SET` and `DEL` continue to write through PostgreSQL. Workers apply reloads asynchronously at command boundaries. `local_cache.health()` reports the calling SQL session's setting, not acknowledgement from every worker. Re-enabling advances the cache epoch before workers resume cache reads.

## Metrics and health {#health-and-monitoring}

`local_cache.health()` reports readiness, cache state, and mapping convergence. `local_cache.stats()` returns JSON counters; `local_cache.metrics()` returns the typed exporter row.

Metrics include cache hits, misses and negative hits; source reads and writes; invalidations and evictions; single-flight leaders, waiters, reuse and timeouts; active and peak clients; connection-limit rejections; authentication and protocol errors; output backpressure and slow-client drops; worker starts; dirty-key fallback; mapping reload failures and retries; TLS handshakes and failures. Gauges include entry and relation capacities, client and worker counts, mapping convergence, shared/worker/estimated memory, and the configured budget.

TLS counters `tls_handshakes_total` and `tls_handshake_failures_total` are
exposed in `stats()` and `metrics()`.

Database reads, invalidations, admission rejection, dirty-key fallback,
singleflight, worker, and RESP counters remain available. New `stats()` fields
include `fast_path_hits`, `fast_path_fallbacks`,
`fast_path_fallback_key_form`, `fast_path_fallback_mapping_shape`,
`fast_path_fallback_multi_key`, and `fast_path_fallback_cache_state`;
`cache_memory_capacity_bytes`, `cache_memory_used_bytes`,
`cache_fragmentation_bytes`, and `arena_admission_rejections_total`;
`dirty_marker_capacity`, `dirty_marker_entries`, `dirty_marker_highwater`,
`dirty_marker_fallbacks_total`, `dirty_marker_entries_effective`,
`dirty_marker_memory_mb_effective`, `dirty_marker_memory_capacity_bytes`, and
`dirty_key_limit_fallbacks`; plus `lock_partitions`, `max_clients_per_worker`,
and `client_slots`. RESP `STAT` adds worker-local `deferred_misses_total`,
`deferred_misses_current`, `deferred_timeouts_total`, and
`deferred_rejections_total`.

`cache_memory_capacity_bytes` reports arena page capacity,
`cache_memory_used_bytes` live requested bytes, and
`cache_fragmentation_bytes` class slack. `arena_admission_rejections_total`
counts rows left uncached when no eligible block can be admitted.
`dirty_marker_entries` is the active marker count;
`dirty_marker_highwater` records its peak, and
`dirty_marker_fallbacks_total` counts keyed publications widened to relation
or global fences because marker admission failed. Effective marker fields
report resolved automatic limits; allocated key storage can be lower when the
entry limit binds.

Next: [quickstart](QUICKSTART.md), [installation](INSTALL_EXISTING.md), and
[upgrading](UPGRADING.md). The upgrade guide covers settings and restart steps
for 3.0.0 to 3.1.0, plus the earlier 2.x migration.
