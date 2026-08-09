---
layout: doc
title: PostgreSQL shared-memory cache technical reference
description: Product boundary, architecture, consistency, memory limits, SQL fast path, security and operations for pg_local_cache.
section: Technical
permalink: /docs/TECHNICAL.html
---

# pg_local_cache technical reference

This document describes the implementation boundaries that affect correctness,
capacity planning, security, and client compatibility. For task-oriented
instructions, use these guides:

- [README and Docker quick start](https://github.com/profundium/pg_local_cache#readme)
- [Install on an existing PostgreSQL server]({{ '/docs/INSTALL_EXISTING.html' | relative_url }})
- [Benchmark and latency methodology]({{ '/docs/BENCHMARKS.html' | relative_url }})
- [Monitoring and OOM signals]({{ '/docs/MONITORING.html' | relative_url }})
- [Benchmark scenario definitions](https://github.com/profundium/pg_local_cache/blob/master/benchmarks/SCENARIOS.md)

## Product boundary

`pg_local_cache` is an in-process PostgreSQL row cache, not a general query
result cache. It stores complete table rows addressed by a validated primary
key. The planner substitutes only narrow exact-key or bounded single-column
`IN`/`ANY` shapes and retains the original PostgreSQL index path as the fallback
child plan.

It does not cache joins, ranges, aggregates, arbitrary predicates, or full-table
results. It does not provide TTLs, pub/sub, clustering, multi-primary cache
coordination, or standby cache serving. The optional RESP endpoint exposes the
same rows for trusted internal clients, but the product is not positioned as a
raw-throughput replacement for Redis or Valkey.

The trade-off is operational: applications can keep ordinary PostgreSQL SQL,
row types, ACLs, drivers, and ORM mappings, but the database operator must load a
shared library at postmaster startup, reserve bounded shared memory, perform one
controlled restart for first activation, attach eligible tables, and monitor the
additional in-process state.

The extension and its GUC prefix are named `pg_local_cache`. User-facing SQL
objects are in the `local_cache` schema, and the default RESP worker role is
`local_cache_worker`. PostgreSQL reserves the `pg_` prefix for system schemas
and roles; these SQL names therefore omit it.

## Supported deployment

The current implementation supports PostgreSQL 14–18 on Linux amd64, one configured
database, and one writable primary. It is designed for attached, permanent
application tables with an immediate, valid, non-partial B-tree primary key.
It does not serve cache entries on standbys and does not coordinate multiple
writable primaries.

`local_cache.attach_table(regclass, boolean, text)` discovers the primary key,
records a whole-row mapping, installs extension-owned triggers, and grants the
configured worker role only the table privileges required by that mapping.
The supported primary-key types are `int2`, `int4`, `int8`, `text`, `varchar`,
`bpchar`, and `uuid`; composite keys may contain 1–16 columns.

The extension rejects temporary or unlogged tables, views, partitioned or
inherited tables, extension-owned or system tables, row-level security, partial
or expression primary keys, nondeterministic key collations, and non-default
primary-key operator classes.

## Architecture

Three PostgreSQL execution contexts share the same bounded memory region:

| Context | Responsibility |
|---|---|
| Application backend | The planner recognizes a supported SQL lookup. The executor reads shared memory or runs the original primary-key index child plan and may fill the cache. |
| Source-writing backend | Extension triggers collect changed keys. The transaction callback publishes invalidation fences before commit visibility and releases them at commit or abort. |
| Optional RESP background worker | The worker owns a TCP event loop. It reads mappings, performs source-table fallback and writes through SPI, and returns RESP2 replies. |

The ordinary SQL path does not run in a background worker. It executes in the
application's PostgreSQL backend under that session's table permissions and
snapshot. Likewise, DML invalidation is collected in the backend executing the
source transaction.

Mappings are stored in `local_cache.mapping` and published to shared memory by
generation. A worker accepts a mapping only after validating its relation OID,
primary key, triggers, ownership provenance, worker-role privileges, and row
shape. `local_cache.reconcile_table()` and `reconcile_all()` repeat those checks
and repair extension-owned triggers and grants before publishing a new mapping
generation.

## Shared memory and bounds

The cache is allocated at postmaster startup. Each key identifies a database,
relation, namespace, and canonical primary-key value. Each entry can hold a
positive value, a negative RESP result, invalidation state, version data, and a
bounded load lease.

Important compile-time bounds are:

| Item | Bound |
|---|---:|
| Encoded cache value | 8,192 bytes |
| Canonical key buffer | 1,024 bytes; the encoded key must be shorter |
| RESP request | 64 KiB |
| RESP response value | 64 KiB |
| Mappings per instance | 128 |
| Primary-key columns per mapping | 16 |
| RESP workers | 32 |
| Client slots per worker | 128 |

A whole-row entry always stores a native PostgreSQL composite value. A RESP
fill also embeds JSON when both representations fit; a SQL fill stores the
composite and renders JSON only if a later RESP read needs it. If the native
payload does not fit in 8 KiB, ordinary SQL uses the source plan. A RESP `GET`
can still read and return a wider source row when it fits the 64 KiB response
bound, but it will not create an oversized cache entry.

Eviction examines a bounded sample rather than scanning the entire cache.
Write-path relation and global invalidation advance version state instead of
rewriting every affected entry. Administrative invalidation calls may scan the
hash to report an affected-entry count, while correctness still depends on the
version change. Load leases are versioned; a late loader cannot overwrite a
newer generation after invalidation or eviction.

Entries do not have TTLs. The cache retains an entry until invalidation,
eviction, replacement, corruption detection, or an MVCC safety check retires
it.

### Memory budget and OOM boundary

`pg_local_cache.memory_budget_mb` is a startup limit for deterministic
extension allocations: shared hashes plus RESP client buffers and worker
state. Startup fails if the configured extension layout exceeds that budget.
`local_cache.metrics()` reports `shared_memory_bytes`, `worker_memory_bytes`,
`estimated_memory_bytes`, and `memory_budget_bytes` from the same model.

This budget does not include `shared_buffers`, backend processes, `work_mem`,
the operating system, exporters, or other containers. Set a cgroup or container
memory limit with headroom and monitor both the extension estimate and process
or container memory. The optional monitoring profile can collect cgroup OOM
events through cAdvisor; it requires host-level access and is disabled by
default.

RESP connection memory is bounded by the global client limit, per-worker
preallocated slots, fixed request/output buffers, idle timeout, output
backpressure handling, and slow-client disconnects. In SQL-only mode
`pg_local_cache.port=0` starts no RESP workers and allocates no RESP client
slots.

## SQL APIs

The canonical application path is ordinary PostgreSQL SQL over the existing
database connection:

```sql
SELECT * FROM public.items WHERE id = $1::bigint;
SELECT value FROM public.items WHERE id = $1::bigint;

SELECT * FROM public.items WHERE id IN (1, 7, 42);

SELECT id, value
FROM public.items
WHERE id = ANY($1::bigint[]);
```

This returns the table's normal tuple type with PostgreSQL's normal projection,
ACL, prepared-statement, scalar zero-or-one-row behavior, and batch row-set
semantics. No result-type witness,
column definition list, custom driver, or rewritten result decoder is involved.
The planner and executor fast path below is an implementation detail.

KV-style callers can opt into the JSON functions:

```sql
SELECT local_cache.get('public.items'::regclass, $1::bigint);
SELECT local_cache.mget('public.items'::regclass, $1::bigint[]);
SELECT local_cache.mget(
    'public.tenant_items'::regclass,
    ARRAY[['tenant-a', '42'], ['tenant-b', '7']]::text[][]
);
```

`get(regclass, anyelement)` returns complete-row JSON text and
`mget(regclass, anyarray)` returns `text[]`. For a single-column primary key,
duplicate keys and `NULL` elements are preserved; bind or cast the array element
to the actual key type.

For a composite primary key, `mget` accepts a two-dimensional `text[][]`. Each
inner row is one key in `attach_table()` column order. The result preserves key
order and duplicates and uses `NULL` for a missing row. Composite batches reject
`NULL` components and more than 1,024 keys before reading the cache or source.
Components are converted with their primary-key type's normal PostgreSQL input
function.

SQL `mget` and RESP `MGET` are the supported prewarm operation for a known key
set. They use the normal lookup/fill paths; there is no separate prewarm or pin
API.

`get(regclass, text[])` is the fallback for composite and heterogeneous primary
keys. Its components are in the order recorded by `attach_table()`. The function
converts each component with the primary-key type's normal PostgreSQL input
function.

All three functions are `SECURITY INVOKER`, enforce `SELECT` on the source
relation, and fail closed to a source lookup whenever the shared entry cannot be
used safely. After the current transaction writes an attached table, both the
ordinary SQL and function paths bypass the cache and use the transaction's own
snapshot. The normal pre-commit invalidation fence makes the new row refillable
only after commit; rollback does not publish it.

The caller needs `USAGE` on schema `local_cache`, `EXECUTE` on the selected
function overloads, and its normal source-table `SELECT` privilege. No RESP
listener, authentication token, or cache-specific client connection is involved.

## Planner and executor fast path

`pg_local_cache.sql_cache` is a `USERSET` GUC and defaults to `on`. The planner
adds `Custom Scan (pg_local_cache_sql)` only when all of these conditions hold:

- the transaction isolation level is `READ COMMITTED`;
- PostgreSQL is not in recovery or parallel execution;
- the current transaction has not modified an attached mapping;
- the query contains exactly one attached base table;
- scalar lookup uses primary-key equality against a constant or external
  parameter, with every PK column present exactly once;
- batch lookup uses equality `IN` / `= ANY(array)` on the complete
  single-column primary key; its array is a constant, external parameter, or an
  array built only from constants and external parameters;
- the primary key is backed by the validated immediate B-tree primary index;
- every selected expression is a direct column reference;
- the query has no joins, CTEs, subqueries, aggregates, windows, grouping,
  distinct, ordering, offset, row lock, or set operation;
- scalar lookup has no limit or constant `LIMIT 1`; batch lookup has no
  `LIMIT`.

Examples that can use the path:

```sql
SELECT * FROM public.items WHERE id = $1;

SELECT metadata, id, value
FROM public.items
WHERE id = $1
LIMIT 1;

SELECT *
FROM public.tenant_items
WHERE item_id = $1 AND tenant_id = $2;

SELECT * FROM public.items WHERE id IN (1, 7, 42);

SELECT id, value
FROM public.items
WHERE id = ANY($1::bigint[]);
```

Predicate order does not need to match composite-key order. Parameters must be
type-compatible with the key. The implementation accepts the same text/varchar
representation and lossless widening from `int2` to `int4`/`int8` or from
`int4` to `int8`; other cross-type forms use the normal planner.

The custom path retains the ordinary primary-key index path as its child.
Scalar execution follows this sequence:

1. Revalidate mapping generation, relation shape, triggers, isolation level,
   snapshot type, and transaction state.
2. Canonicalize primary-key parameters.
3. Read the versioned shared entry.
4. On a valid positive hit, verify the stored tuple's source transaction against
   the current MVCC snapshot and decode the composite row.
5. On a miss, negative RESP entry, unsafe snapshot, malformed payload, or stale
   row-shape fingerprint, execute the child index plan.
6. Fill only after a latest-snapshot visibility proof and only if the read token
   still matches all relevant generations.

For `IN`/`ANY`, the executor evaluates the array once, ignores `NULL` elements,
canonicalizes and deduplicates at most 1,024 keys, and copies at most 16 MiB of
validated composite data into query-local memory. It returns cached rows only
when every distinct key is a safe positive hit. One miss, negative entry,
snapshot rejection, malformed payload, or budget overflow switches the entire
statement to the retained PostgreSQL scalar-array index plan. The executor
never merges a partial cache result with source rows, so PostgreSQL's duplicate,
`NULL`, and missing-key semantics are preserved. Rows returned by a cold child
scan can refill their individual keys after the normal latest-snapshot proof.

Composite tuple `IN`, `ALL`, extra predicates, array queries with `LIMIT`, and
arrays containing unsupported expressions use normal PostgreSQL paths. Runtime safety failures
use the retained child plan. A missing row is fetched through that plan and
returns zero rows.
The SQL path does not trust negative RESP entries as authoritative for an
application snapshot.

Application roles need only their normal source-table privileges. They do not
need `USAGE` on `local_cache` to benefit from a cached `SELECT`. Attach rejects
RLS tables rather than attempting to share rows across policies.

## Consistency model

The source table remains authoritative. The cache provides a single-primary,
transaction-aware invalidation model rather than asynchronous invalidation from
WAL or a message bus.

### Source writes

Extension-owned `ENABLE ALWAYS` row triggers canonicalize the complete old and
new primary keys and collect them in backend-local transaction memory.
`TRUNCATE` and selected DDL collect relation or global invalidation. A primary
key update collects both keys.

At the pre-commit callback, before the source transaction can become visible,
the extension reserves shared dirty markers, advances versions, cancels
affected loads, and invalidates existing entries. Readers that overlap this
window may observe the previously committed row or the newly committed row,
but cannot publish a stale fill across the version fence. Commit releases the
dirty markers. Abort releases transaction state without publishing source
data. If abort follows publication of the pre-commit fence, affected entries
may remain invalid, but the cache cannot expose the uncommitted row.

The per-transaction key list is bounded by
`pg_local_cache.max_dirty_keys`. When that bound is reached, invalidation
widens to the relation instead of growing backend memory without limit. If the
required shared marker cannot be reserved, the transaction uses a global fence.
Both fallbacks preserve the commit boundary at the cost of a colder cache.

`PREPARE TRANSACTION` is rejected after a transaction modifies an attached
mapping. Read-only two-phase transactions are unaffected.

### Fills and snapshots

Every miss captures entry, relation, global, and configuration generations.
The fill is accepted only if those generations still match after the source
read. RESP same-key cold reads use a bounded single-flight lease: followers wait
up to `pg_local_cache.singleflight_wait_ms`, then read the table themselves if
necessary without overwriting the leader's generation. Ordinary SQL backends do
not wait on the RESP single-flight lease; a non-owner executes its child plan.

For ordinary SQL, a positive entry records the source tuple's `xmin` and the
observed full transaction ID. The executor checks snapshot membership before
returning the row. The full-XID observation horizon prevents treating a wrapped
32-bit transaction ID as current indefinitely. When that horizon is exceeded,
the exact entry is retired and the source plan runs. This is not TTL expiration.

`REPEATABLE READ` and `SERIALIZABLE` always use the ordinary plan. A
`READ COMMITTED` transaction that has written an attached mapping also bypasses
the cache for its remaining statements, preserving read-your-own-write behavior.

### Trigger and DDL provenance

Workers and the SQL path validate the managed statement, row, and truncate
triggers, including function OIDs, arguments, enable state, and extension
ownership. A mapping with missing or altered provenance is not served.

DDL event triggers reload or invalidate mappings when attached relations or
dependent type/output semantics change. Whole-row payloads include a tuple
descriptor fingerprint and CRC32C, so bytes saved for an old row shape are not
decoded as a new row shape. `DROP TABLE` forgets the mapping by relation OID;
recreating the same name does not attach the new table automatically.

## RESP2 wire API

The optional endpoint is KVik-inspired and uses the documented whole-row CRUD
key shape below. It has not been conformance-tested against Postgres Pro
Enterprise KVik. Client integrations should rely only on the surface
documented here.

The whole-row key format is:

```text
CRUD:database.schema.table:{"pk_column":<json-scalar>,...}
```

The JSON object must contain every primary-key field exactly once. Member order
is irrelevant; the worker parses values through PostgreSQL input functions and
canonicalizes them in primary-index order. Database, schema, and table names in
whole-row mappings cannot contain `.` or `:`.

| Command | Behavior |
|---|---|
| `AUTH token` | Authenticate with the shared token. |
| `AUTH username token` | Authenticate when `username` equals the configured worker role. |
| `GET CRUD:db.schema.table:{pk-json}` | Return complete row JSON; on a cache miss, read the source table. Return RESP null when the source row does not exist. |
| `MGET key [key ...]` | Return an ordered RESP2 array using GET semantics for up to 1,024 whole-row keys, including composite keys and keys from different mappings. |
| `SET CRUD:db.schema.table:{pk-json} row-json` | Perform a whole-row PostgreSQL upsert for a writable mapping and reply after commit. |
| `DEL CRUD:db.schema.table:{pk-json}` | Delete the source row for a writable mapping and reply after commit. |
| `INVALIDATE <scope>` | Invalidate an exact key, table, configured database, or the complete instance. |
| `STAT` / `STATS` | Return native statistics plus a subset of KVik-style counter aliases. |
| `PING`, `ECHO`, `HELLO 2`, `INFO`, `QUIT` | Supported connection and diagnostic subset. |
| `CLIENT`, `COMMAND`, `SELECT 0` | Minimal compatibility responses for common Redis clients. |

`MGET` validates every key before reading. Its response must fit the existing
64 KiB value plus protocol-overhead bound or the command returns one error and
queues no partial array. Existing `client_gets`, cache and database-read counters
are incremented per logical key, as if the same keys were read with `GET`. One
`pg_local_cache.statement_timeout_ms` deadline covers the complete command,
including all single-flight waits and source reads.

`SET` treats the wire key as authoritative. Primary-key fields may be omitted
from the row JSON; when present, they must match the wire key. The value is a
complete row, not a JSON patch. Omitted non-key fields
become `NULL`; column defaults do not fill them because the upsert supplies
every non-generated column. Unknown JSON fields are rejected. PostgreSQL casts,
generated-column rules, constraints, triggers, WAL, and commit still apply.
`SET` and `DEL` each execute as a separate PostgreSQL transaction.

If the TCP connection closes before a write reply arrives, the client cannot
infer whether the database transaction committed. Retrying a non-idempotent
operation without reading the source can repeat application effects.

### Compatibility boundary

| Area | pg_local_cache status | Boundary |
|---|---|---|
| `CRUD:database.schema.table:{pk-json}` keys | Implemented | Whole-row mappings only. |
| Composite primary keys | Implemented | 1–16 supported PK columns. |
| Source fallback on `GET` / `MGET` | Implemented | Missing rows may be stored as negative RESP entries. |
| Whole-row `SET` and `DEL` | Implemented | Mapping must be attached with `writable=true`; operations are PostgreSQL transactions. |
| Key/table/database/global invalidation | Implemented | Uses version fences in shared memory. |
| KVik-style statistic names | Partial aliases | Native counters remain authoritative; internal meanings are not claimed identical. |
| Common Redis client handshake | Partial | RESP2 subset only; unsupported commands return an error. |
| PostgreSQL per-user authentication and ACLs | Not provided on RESP | One shared token and worker role cover every mapping. |
| TLS | Not provided | Use loopback or an authenticated TLS proxy. |
| TTL and expiration commands | Not provided | Entries have no TTL. |
| Redis Cluster, Lua, Pub/Sub, transactions | Not provided | No `MULTI/WATCH`, scripting, or RESP3. |
| Multiple databases, standbys, multi-primary | Not provided | One configured database on one writable primary. |

## Row payload format

Whole-row cache values use a versioned binary header. All header integers are
big-endian; this is an internal shared-cache format, not the RESP wire format.

| Offset | Width | Field |
|---:|---:|---|
| 0 | 4 | Magic (`PGLC`) |
| 4 | 2 | Payload version |
| 6 | 2 | Flags |
| 8 | 4 | Composite type OID |
| 12 | 4 | Type modifier |
| 16 | 4 | Attribute count |
| 20 | 4 | Native composite length |
| 24 | 4 | JSON length |
| 28 | 4 | CRC32C checksum |
| 32 | 8 | Tuple-descriptor fingerprint |

Version 1 has a 40-byte header followed by the native composite bytes and,
when the `HAS_JSON` flag is set, stored JSON bytes. Decode checks the magic,
version, known flags, total lengths, checksum, type OID, typmod, attribute
count, descriptor fingerprint, and native tuple header before exposing a row.

## Configuration

Except for `pg_local_cache.sql_cache`, these GUCs are postmaster settings and
require a PostgreSQL restart.

| GUC | Default | Purpose |
|---|---:|---|
| `pg_local_cache.port` | `6380` | RESP listener port; `0` disables RESP workers. |
| `pg_local_cache.bind_address` | `127.0.0.1` | RESP listener address: `127.0.0.1` or `0.0.0.0`. |
| `pg_local_cache.workers` | `4` | RESP worker count when the listener is enabled. |
| `pg_local_cache.database` | `postgres` | Database served by mappings and RESP workers. |
| `pg_local_cache.role` | `local_cache_worker` | Dedicated RESP worker role. |
| `pg_local_cache.cache_entries` | `16384` | Shared cache entry capacity, 128–65,536. |
| `pg_local_cache.relation_states` | `1024` | Shared relation-state capacity, 128–8,192. |
| `pg_local_cache.max_clients` | `256` | Global RESP client limit, 1–4,096. |
| `pg_local_cache.max_clients_per_worker` | `64` | Preallocated slots per worker, 1–128. |
| `pg_local_cache.memory_budget_mb` | `384` | Deterministic extension-memory startup budget, 64–8,192 MiB. |
| `pg_local_cache.idle_timeout_ms` | `300000` | RESP idle/slow-client deadline. |
| `pg_local_cache.statement_timeout_ms` | `2000` | RESP worker database-operation deadline. |
| `pg_local_cache.lock_timeout_ms` | `250` | RESP worker lock-wait deadline. |
| `pg_local_cache.singleflight_wait_ms` | `25` | RESP follower wait for a same-key loader, 0–1,000 ms. |
| `pg_local_cache.max_pipeline_commands` | `256` | Per-client commands processed in one event-loop turn. |
| `pg_local_cache.max_dirty_keys` | `4096` | Per-transaction key bound before relation invalidation, 128–16,384. |
| `pg_local_cache.auth_token_file` | empty | Preferred RESP token source. |
| `pg_local_cache.auth_token` | empty | Inline development/test token; hidden from `SHOW ALL`. |
| `pg_local_cache.allow_superuser` | `off` | Permit a superuser RESP role for local development only. |
| `pg_local_cache.sql_cache` | `on` | Session-level ordinary-SQL fast-path switch; no restart required. |

`max_clients` must not exceed
`workers * max_clients_per_worker`. Enabling RESP also consumes PostgreSQL
background-worker slots; preserve capacity for replication, parallel queries,
and other extensions when setting `max_worker_processes`.

## Security boundary

The SQL and RESP surfaces have different identities:

- ordinary SQL executes as the connected PostgreSQL role, after PostgreSQL has
  checked its source-table privileges;
- administrative functions in `local_cache` are revoked from `PUBLIC` and must
  be granted to a deploy or monitoring role explicitly;
- RESP executes database operations as one configured worker role after shared
  token authentication;
- the RESP token grants access to every accepted mapping on the instance;
- RESP has no TLS and no per-client PostgreSQL authorization context.

Keep RESP disabled when it is unnecessary. When enabled, bind to loopback by
default, store the token in an OS-user-owned mode `0400` or `0600` regular file,
and place any remote access behind network isolation and authenticated TLS. Do
not use the inline token in a production configuration or pass tokens on a
command line that may be visible in process listings.

The worker role must be `LOGIN NOSUPERUSER NOINHERIT`; the extension refuses a
superuser worker unless `allow_superuser=on`. Attach and reconcile manage only
the grants required by configured mappings.

## Metrics and operations

The SQL monitoring surface is:

```sql
SELECT * FROM local_cache.metrics();
SELECT local_cache.health();
SELECT local_cache.stats();
```

`metrics()` returns a typed one-row snapshot. `health()` evaluates memory
budget, worker count, client limit, and mapping-generation readiness. `stats()`
returns the detailed JSON diagnostic snapshot. Counters reset at PostgreSQL
restart; rate and alert calculations must tolerate resets.

Watch at least:

- cache hit, miss, fill, bypass, eviction, and invalidation rates;
- estimated extension memory versus budget and external process/cgroup memory;
- active and peak clients, rejected connections, backpressure, and slow-client
  drops;
- dirty-key relation fallbacks;
- mapping reload failures and workers with incomplete mappings;
- worker restarts and health readiness.

The bundled Prometheus rules and Grafana dashboard are described in the
[monitoring guide]({{ '/docs/MONITORING.html' | relative_url }}).
They are templates; alert windows and memory thresholds must be calibrated
against the deployment.

## Operational limits

- Installing or changing shared-memory and worker settings requires a restart
  because the extension uses `shared_preload_libraries`.
- Attaching or reconciling a table takes `ShareRowExclusiveLock` while managed
  triggers and grants are validated. Use a bounded `lock_timeout` and retry.
- RESP writes are PostgreSQL transactions; their latency includes locks, WAL,
  constraints, triggers, and commit.
- A network failure before a RESP write reply leaves the commit outcome
  unknown to the client.
- Transparent `IN`/`ANY` is bounded to 1,024 elements and 16 MiB of
  query-local tuple copies; exceeding either bound executes PostgreSQL.
- The cache is not a durability layer. PostgreSQL remains the source of truth.
- Mappings are not automatically recreated for a new relation that reuses a
  dropped table's name.
- Arbitrary native extensions are unavailable on most managed PostgreSQL
  services unless the provider packages them explicitly.

For rollout and rollback procedures, including Patroni and operator-managed
clusters, follow the
[existing-database guide]({{ '/docs/INSTALL_EXISTING.html' | relative_url }}).
For performance claims, use the evidence rules in the
[benchmark guide]({{ '/docs/BENCHMARKS.html' | relative_url }}).
