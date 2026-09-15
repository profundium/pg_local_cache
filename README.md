# pg_local_cache: PostgreSQL row cache

**Version 2.0: explicit SQL mget, bounded shared memory, and transaction-aware
invalidation for PostgreSQL 14-18.**

Cache whole rows by their complete primary key. PostgreSQL remains the source
of truth; ordinary writes invalidate affected entries. Ordinary `SELECT`
queries are not rewritten. An optional, limited RESP2 endpoint shares the cache.

[Documentation](https://profundium.github.io/pg_local_cache/) |
[Try locally](docs/QUICKSTART.md) |
[Benchmarks](docs/BENCHMARKS.md) |
[Installation](docs/INSTALL_EXISTING.md) |
[Technical reference](docs/TECHNICAL.md)

## Measured: 839,678 single-key RESP requests/s

**3.31× the prepared SQL throughput in one local comparison:** 839,678 vs
253,790 requests/s. Apple M3 Max, PostgreSQL 16, Go, 256 connections, warm
cache; median of three five-second samples on 15 September 2026. SQL `mget`
was slower for this case at 186,296 requests/s; batch results differ.
[Conditions and raw data](docs/benchmarks-go.md).

Run the [same SQL/RESP matrix on Node.js and Go](docs/BENCHMARKS.md#run-the-same-comparison-on-every-client):

```bash
./examples/benchmark.sh all > comparison.json
```

Requires Docker, Node.js 20+ and Go 1.25+. Measures a disposable demo, not your
existing database; use your own workload before choosing a cache.

## Try without changing an existing database

With Docker Compose, from this repository:

```bash
docker compose -f examples/compose.yaml up --build --wait
```

The demo builds the extension from your checkout. It binds PostgreSQL to
loopback port 55432, stores disposable data in tmpfs, and mounts no host database.
Follow the [quickstart](docs/QUICKSTART.md) to run queries and remove it.

With Node.js 20 or later, check the result contract and concurrent writes:

```bash
npm --prefix examples/node-postgres ci --ignore-scripts
npm --prefix examples/node-postgres run demo
```

## Read whole rows by primary key

Attach a permanent table with a supported primary key:

```sql
SELECT local_cache.attach_table('public.items'::regclass);
```

Fetch an ordered `text[]` of JSON rows:

```sql
SELECT local_cache.mget(
  'public.items'::regclass,
  ARRAY[42, 7, 42, NULL]::bigint[]
);
```

The result preserves order, duplicates, and `NULL` positions. Missing rows also
return `NULL`. A batch can contain at most 1,024 keys. Use `unnest(...)` to display
one array element per result row; the function itself does not return a set.

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

Connection examples: [Node.js](docs/node-postgres.md), [Go](docs/go.md),
[RESP](docs/resp.md). See [benchmark results](docs/BENCHMARKS.md) for throughput
and server resource use.

## Consistency and workload fit

Each cache hit is checked against mapping, relation, transaction, row, and
snapshot state. `REPEATABLE READ`, `SERIALIZABLE`, recovery, parallel execution,
and transactions that wrote mapped data bypass the cache. Oversized or unsafe
entries fall back to an indexed source-table read.

| Worth measuring | Keep using PostgreSQL directly for |
|---|---|
| Repeated complete primary-key reads | Joins, ranges, aggregates, and full scans |
| A hot set of whole rows that fits the cache | Arbitrary query-result caching |
| READ COMMITTED on one writable primary | RLS, partitioned, or inherited tables |
| An application that can call mget explicitly | Queries that need locking or have no measured benefit |

The extension is not a Redis replacement. It provides no TTL, pub/sub, or
distributed coordination. SQL mget still uses a PostgreSQL connection.

Choosing a design? Start with the [PostgreSQL caching guide](docs/postgresql-caching.md),
[PostgreSQL and Redis cache-aside](docs/postgresql-redis-cache.md), or
[batch primary-key lookups](docs/batch-primary-key-lookups.md).

## Install on an existing server

Supported binary target: **Linux amd64, PostgreSQL 14-18, glibc or musl**.
First activation adds `pg_local_cache` to `shared_preload_libraries` and requires
one controlled PostgreSQL restart.

For a local cluster managed by `pg_ctl`:

```bash
curl -fsSL https://github.com/profundium/pg_local_cache/releases/latest/download/install-latest.sh | bash -s -- app
```

Replace `app` with the database name. This command installs and restarts; it is
not the disposable demo. Use the [installation guide](docs/INSTALL_EXISTING.md)
for systemd, Patroni, Kubernetes, checksum-first installation, source builds,
RESP, or recovery.

Minimum SQL-only configuration:

```conf
shared_preload_libraries = 'pg_local_cache'
pg_local_cache.database = 'app'
pg_local_cache.role = 'local_cache_worker'
pg_local_cache.cache_entries = 16384
pg_local_cache.memory_budget_mb = 384
pg_local_cache.port = 0
```

Preserve existing preload entries. Size cache entries, relation states, clients,
workers, and the memory budget together before restart. Manual source installs
also need the [role and metadata grants](docs/INSTALL_EXISTING.md#initialize-a-source-installation)
before attaching a table, even when RESP is disabled.

Useful administration functions:

```sql
SELECT local_cache.health();
SELECT local_cache.stats();
SELECT local_cache.reconcile_table('public.items'::regclass);
SELECT local_cache.detach_table('public.items'::regclass);
```

## Optional RESP2 endpoint

RESP2 supports authenticated, bounded `MGET`, `SET`, `DEL`, and scoped
invalidation. Workers run under one configured PostgreSQL role; network clients
do not inherit individual PostgreSQL ACLs. Keep the listener on loopback or
behind authenticated TLS, and prefer `pg_local_cache.auth_token_file` over an
inline token. See the [technical reference](docs/TECHNICAL.md).

## Develop

Source builds use PostgreSQL's PGXS toolchain and the target server's headers.
Follow the [source-build procedure](docs/INSTALL_EXISTING.md#build-from-source).

```bash
make verify-static source-test
make docker-smoke
node --test examples/node-postgres/queries.test.mjs
```

[Report a workload](https://github.com/profundium/pg_local_cache/issues/new?template=workload.yml) |
[Releases](https://github.com/profundium/pg_local_cache/releases) |
[Contributing](CONTRIBUTING.md)

License: [MIT](LICENSE).
