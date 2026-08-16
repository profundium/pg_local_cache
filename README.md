# pg_local_cache: PostgreSQL row cache

**Transaction-aware, bounded shared-memory cache for PostgreSQL 14-18
primary-key row reads.**

`pg_local_cache` is an open-source PostgreSQL extension for repeated exact-key
lookups. It exposes an explicit SQL `mget` API and an optional RESP2 endpoint.
PostgreSQL remains the source of truth, and writes invalidate affected cache
entries transactionally.

[Documentation](https://profundium.github.io/pg_local_cache/) |
[Installation](docs/INSTALL_EXISTING.md) |
[Technical reference](docs/TECHNICAL.md) |
[Latest release](https://github.com/profundium/pg_local_cache/releases/latest)

## Why use pg_local_cache

- **Explicit:** only `local_cache.mget(...)` and optional RESP2 commands use the
  cache. Ordinary `SELECT` keeps the normal PostgreSQL planner and executor.
- **Transaction-aware:** a transaction reads its own writes from PostgreSQL;
  commit invalidates old entries and rollback publishes nothing.
- **Bounded:** shared-memory capacity is fixed at PostgreSQL startup.
- **Fail-safe:** unsafe state, malformed entries, and oversized rows fall back
  to an indexed source-table read.
- **Focused:** whole rows are cached by their complete primary key, not by
  arbitrary query text.

## Quick start

Supported binary target: **Linux amd64, PostgreSQL 14-18, glibc or musl**.

> **Restart required:** first activation adds `pg_local_cache` to
> `shared_preload_libraries` and restarts PostgreSQL once.

For a local cluster managed by `pg_ctl`:

```bash
curl -fsSL https://github.com/profundium/pg_local_cache/releases/latest/download/install-latest.sh | bash -s -- app
```

Replace `app` with the database name. The bootstrap pins one release, verifies
checksums, selects the matching binary, installs it, restarts PostgreSQL,
creates the extension, and checks health.

Use the [installation guide](docs/INSTALL_EXISTING.md) for systemd, Patroni,
Kubernetes, checksum-first installation, source builds, RESP, or rollback.

## Read whole rows by primary key

Attach a permanent table with a supported primary key:

```sql
SELECT local_cache.attach_table('public.items'::regclass);
```

Fetch ordered JSON rows:

```sql
SELECT local_cache.mget(
  'public.items'::regclass,
  ARRAY[42, 7, 42, NULL]::bigint[]
);
```

The result preserves order, duplicates, and `NULL` positions. Missing rows also
return `NULL`. A batch can contain at most 1,024 keys.

Composite keys use `text[][]` in primary-key column order:

```sql
SELECT local_cache.mget(
  'public.tenant_items'::regclass,
  ARRAY[['tenant-a', '42'], ['tenant-b', '7']]::text[][]
);
```

Grant only the required access:

```sql
GRANT SELECT ON public.items TO app_user;
GRANT USAGE ON SCHEMA local_cache TO app_user;
GRANT EXECUTE ON FUNCTION local_cache.mget(regclass, anyarray) TO app_user;
```

Writes remain ordinary PostgreSQL:

```sql
UPDATE public.items SET value = 'new' WHERE id = 42;
```

## Consistency contract

Each cache hit is checked against mapping, relation, transaction, row, and
snapshot state. `REPEATABLE READ`, `SERIALIZABLE`, recovery, parallel execution,
and transactions that wrote mapped data bypass the cache.

This design preserves read-your-writes and PostgreSQL visibility rules. The
cache accelerates eligible reads; it never becomes authoritative data storage.

## Workload fit

| Use pg_local_cache for | Keep using PostgreSQL directly for |
|---|---|
| Repeated exact primary-key reads | Joins, ranges, aggregates, and full scans |
| A hot row set that fits bounded memory | Arbitrary query-result caching |
| One writable PostgreSQL primary | Multi-primary or distributed coordination |
| Ordered whole-row JSON or limited RESP2 access | TTL, pub/sub, or a Redis replacement |

Mapped tables must be permanent heap tables with a valid primary key. RLS,
partitioning, inheritance, extension-owned tables, and unsupported key types
are rejected.

## Configure

Minimum SQL-only configuration:

```conf
shared_preload_libraries = 'pg_local_cache'
pg_local_cache.database = 'app'
pg_local_cache.cache_entries = 100000
pg_local_cache.port = 0
```

Keep existing `shared_preload_libraries` entries. Size cache entries, relation
states, clients, workers, and the extension memory budget together before the
restart.

Useful administration functions:

```sql
SELECT local_cache.health();
SELECT local_cache.stats();
SELECT local_cache.reconcile_table('public.items'::regclass);
SELECT local_cache.detach_table('public.items'::regclass);
```

## Optional RESP2 endpoint

RESP2 uses the same mappings and bounded cache. It supports authenticated,
bounded `MGET`, `SET`, `DEL`, and scoped invalidation. It is not a complete Redis
server.

Run workers under a dedicated PostgreSQL role. Keep the listener on loopback or
behind authenticated TLS, and prefer `pg_local_cache.auth_token_file` over an
inline token. Network clients do not inherit individual PostgreSQL ACLs.

## Build from source

Source builds use PostgreSQL's standard PGXS toolchain. Install the server
development headers for the target PostgreSQL version, then follow the
[source-build procedure](docs/INSTALL_EXISTING.md#build-from-source).

## Develop

```bash
make verify-static source-test
make docker-smoke
```

Local PGXS builds require PostgreSQL server headers. The Docker smoke test is
the portable PostgreSQL 14-18 verification path.

## Documentation

- [Documentation home](https://profundium.github.io/pg_local_cache/)
- [Install on an existing PostgreSQL server](docs/INSTALL_EXISTING.md)
- [Technical reference](docs/TECHNICAL.md)
- [Releases](https://github.com/profundium/pg_local_cache/releases)
- [Issues](https://github.com/profundium/pg_local_cache/issues)

License: [MIT](LICENSE).
