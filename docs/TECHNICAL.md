# Technical reference

## Boundary

`pg_local_cache` caches whole rows by complete primary key. It exposes one SQL
read function, `local_cache.mget`, plus an optional RESP2 endpoint.

It does not install planner or executor hooks. Ordinary SQL is normal
PostgreSQL and never reads this cache.

Supported source tables are permanent heap tables with a valid primary key and
without RLS, partitioning, inheritance, or extension ownership. Supported key
types are `smallint`, `integer`, `bigint`, `text`, `varchar`,
`char`, and `uuid`; collations must be deterministic.

## Mapping lifecycle

`local_cache.attach_table(regclass)`:

1. locks and validates the relation;
2. records namespace, relation OID, and ordered primary-key columns;
3. installs extension-owned statement, row, and truncate triggers;
4. reloads worker mappings.

DDL event triggers invalidate cached mapping metadata. Use
`reconcile_table` or `reconcile_all` after intentional schema changes.
`detach_table` removes the mapping and its triggers.

## SQL mget

Signature:

```sql
local_cache.mget(relation regclass, key_values anyarray) RETURNS text[]
```

Single-column keys use their native array type. Composite keys use a rectangular
`text[][]`, one key per row and one component per primary-key column.

Contract:

- maximum 1,024 keys;
- order and duplicates are preserved;
- input `NULL` and missing rows produce aligned `NULL`;
- composite components cannot be `NULL`;
- every component is parsed by its PostgreSQL type input function;
- the complete composite batch is validated before the first lookup;
- callers need source-table `SELECT`;
- the function is `SECURITY INVOKER`.

A prepared source query is cached per function instance, user, relation, and
mapping generation. Each key follows the same path:

1. canonicalize the complete primary key;
2. use shared cache only in a clean `READ COMMITTED` transaction on the
   writable primary;
3. validate cached payload checksum, row descriptor, source `xmin`, and
   snapshot visibility;
4. otherwise execute the indexed source-table query through SPI;
5. publish a positive or negative entry only after a latest-snapshot proof.

`REPEATABLE READ`, `SERIALIZABLE`, recovery, parallel execution, and a
transaction that has written mapped data bypass the cache and read PostgreSQL.

Rows larger than the cache payload limit still return from PostgreSQL but are
not cached.

## Consistency

Before a mapped write can commit, triggers fence the affected key or relation.
A cache fill carries mapping, global, relation, key, and loader generations, so
a stale loader cannot publish after invalidation or eviction.

Positive entries record the source tuple's `xmin` and a FullXID observation
horizon. Old or snapshot-ineligible entries fall back to PostgreSQL. Negative
entries are never treated as authoritative for an older active snapshot.

Rollback removes transaction-local dirty state without publishing new data.
Read-your-writes therefore comes from PostgreSQL, not from speculative cache
contents.

## Shared memory

The cache, relation states, counters, worker generations, and RESP client slots
are allocated at postmaster startup. Capacity is bounded; eviction samples a
bounded rotating set and prefers stale entries. Admission failure returns to
the source database instead of allocating unbounded memory.

Important settings:

| Setting | Default | Meaning |
|---|---:|---|
| `pg_local_cache.database` | `postgres` | database served by the extension |
| `pg_local_cache.cache_entries` | `16384` | shared row capacity |
| `pg_local_cache.relation_states` | `1024` | shared mapping-state capacity |
| `pg_local_cache.memory_budget_mb` | `384` | extension startup budget |
| `pg_local_cache.port` | `6380` | RESP port; `0` disables RESP |
| `pg_local_cache.bind_address` | `127.0.0.1` | RESP bind address |
| `pg_local_cache.workers` | `4` | RESP workers |
| `pg_local_cache.role` | `local_cache_worker` | RESP PostgreSQL role |
| `pg_local_cache.max_clients` | `256` | global RESP client limit |
| `pg_local_cache.max_clients_per_worker` | `64` | slots per worker |
| `pg_local_cache.idle_timeout_ms` | `300000` | idle/slow client deadline |
| `pg_local_cache.statement_timeout_ms` | `2000` | worker statement deadline |
| `pg_local_cache.lock_timeout_ms` | `250` | worker lock deadline |
| `pg_local_cache.singleflight_wait_ms` | `25` | same-key follower wait |
| `pg_local_cache.max_pipeline_commands` | `256` | commands per event-loop turn |
| `pg_local_cache.max_dirty_keys` | `4096` | transaction key fence bound |
| `pg_local_cache.auth_token_file` | empty | preferred RESP credential |
| `pg_local_cache.auth_token` | empty | development-only inline token |
| `pg_local_cache.allow_superuser` | `off` | development-only role override |

These are postmaster settings. Size them before restart; the installer preflight
checks the total plan.

## RESP2

RESP uses the same mappings and shared cache. The wire key is:

```text
CRUD:database.schema.table:{"pk_column":<json-scalar>,...}
```

Supported commands are authenticated bounded `MGET`, `SET`, `DEL`, and scoped
invalidation. RESP workers use one configured PostgreSQL
role; they do not inherit each network client's database ACLs.

The endpoint has no TLS. Bind to loopback or place it behind an authenticated
TLS proxy.

## Monitoring

`local_cache.health()` reports readiness and mapping convergence.
`local_cache.stats()` returns JSON counters. `local_cache.metrics()` exposes
the typed metrics row used by the exporter.

SQL cache counters now describe only explicit `mget` calls:

- `sql_cache_hits`
- `sql_cache_misses`
- `sql_cache_fills`
- `sql_cache_bypasses`

Database reads, invalidations, admission rejection, dirty-key fallback,
singleflight, worker, and RESP counters remain separate.

See [monitoring assets](../monitoring/README.md) and
[benchmark method](BENCHMARKS.md).
