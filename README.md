# pg_local_cache: PostgreSQL row cache

**Version 3.0.0: authenticated RESP row reads, bounded shared memory, and
transaction-aware invalidation for PostgreSQL 14-18.**

Cache whole rows by their complete primary key. PostgreSQL remains the source
of truth; ordinary writes invalidate affected entries. Ordinary `SELECT`
queries are not rewritten. The limited RESP2 endpoint exposes `MGET`, `SET`,
`DEL`, and scoped invalidation.

[Documentation](https://profundium.github.io/pg_local_cache/) |
[Try locally](docs/QUICKSTART.md) |
[Benchmarks](docs/BENCHMARKS.md) |
[Installation](docs/INSTALL_EXISTING.md) |
[Upgrade from 2.x](docs/UPGRADING.md) |
[Technical reference](docs/TECHNICAL.md)

## Measured: 839,678 single-key RESP requests/s

**3.31× the prepared SQL throughput in one local comparison:** 839,678 vs
253,790 requests/s. Apple M3 Max, PostgreSQL 16, Go, 256 connections, warm
cache; median of three five-second samples on 15 September 2026. The recorded
SQL `mget` comparison (186,296 requests/s) is from 2.x and was removed in 3.0.0;
batch results differ.
[Conditions and raw data](docs/benchmarks-go.md).

Run the [same prepared-SQL/RESP matrix on Node.js and Go](docs/BENCHMARKS.md#run-the-same-comparison-on-every-client):

```bash
./examples/benchmark.sh all > comparison.json
```

Requires Docker, Node.js 20+ and Go 1.25+. Runs against a disposable demo database.

## Try without changing an existing database

With Docker Compose, from this repository:

```bash
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml up --build --wait
```

The demo builds the extension from your checkout. It binds PostgreSQL to
loopback port 55432, stores disposable data in tmpfs, and mounts no host database.
Follow the [quickstart](docs/QUICKSTART.md) to run RESP reads and remove it.

With Node.js 20 or later, check the result contract and concurrent writes:

```bash
npm --prefix examples/node-postgres ci --ignore-scripts
npm --prefix examples/node-postgres run demo
```

## Read whole rows by primary key over RESP

Attach a permanent table with a supported primary key:

```sql
SELECT local_cache.attach_table('public.items'::regclass);
```

Connect with RESP2 and issue `MGET` using the mapped table's wire key:

```text
MGET CRUD:app.public.items:{"id":42} CRUD:app.public.items:{"id":7}
```

RESP `MGET` preserves key order and duplicates. Missing rows return null. The
listener's workers use one configured PostgreSQL role; they do not inherit the
network client's SQL privileges, transaction, or snapshot. See the
[RESP guide](docs/resp.md) for authentication and client examples.

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
| An application that can use authenticated RESP `MGET` | Queries that need locking or have no measured benefit |

The extension is not a Redis replacement. It provides no TTL, pub/sub, or
distributed coordination. RESP reads run through the configured worker role,
separately from application SQL sessions.

Related guides: [PostgreSQL caching](docs/postgresql-caching.md),
[PostgreSQL and Redis cache-aside](docs/postgresql-redis-cache.md), or
[batch primary-key lookups](docs/batch-primary-key-lookups.md).

## Install on an existing server

Release packages support PostgreSQL 14-18 on Linux for Debian/Ubuntu and
RHEL-family distributions. First activation adds `pg_local_cache` to
`shared_preload_libraries` and requires one controlled PostgreSQL restart. Use
the [installation guide](docs/INSTALL_EXISTING.md) for package verification,
PGXN and source installation, configuration, restart, upgrade, and uninstall.

Configure the RESP listener:

```conf
shared_preload_libraries = 'pg_local_cache'
pg_local_cache.database = 'app'
pg_local_cache.role = 'local_cache_worker'
pg_local_cache.port = 6380
pg_local_cache.bind_address = '127.0.0.1'
pg_local_cache.auth_token_file = '/secure/path/token'
pg_local_cache.cache_entries = 16384
pg_local_cache.memory_budget_mb = 384
```

Preserve existing preload entries. Size cache entries, relation states, clients,
workers, and the memory budget together before restart. Manual source installs
also need the [role and metadata grants](docs/INSTALL_EXISTING.md#initialize-a-source-installation)
before attaching a table; RESP reads use the configured worker role.

Useful administration functions:

```sql
SELECT local_cache.health();
SELECT local_cache.stats();
SELECT local_cache.reconcile_table('public.items'::regclass);
SELECT local_cache.detach_table('public.items'::regclass);
```

## RESP2 endpoint

RESP2 supports authenticated, bounded `MGET`, `SET`, `DEL`, and scoped
invalidation. Workers run under one configured PostgreSQL role; network clients
do not inherit individual PostgreSQL ACLs. Keep the listener on loopback unless
you deliberately enable plaintext network access on a trusted network. Native
TLS is planned for PR 3b. Prefer `pg_local_cache.auth_token_file` over an inline
token. See the [technical reference](docs/TECHNICAL.md).

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
