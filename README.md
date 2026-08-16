# pg_local_cache

Transaction-aware, bounded cache for PostgreSQL 14–18 primary-key rows.

The public read API is intentionally small:

- SQL: `local_cache.mget(regclass, anyarray)`
- optional RESP2: `GET` and `MGET`

Ordinary `SELECT` is untouched and always uses PostgreSQL's normal planner.
PostgreSQL remains the source of truth; writes invalidate affected cache entries
transactionally.

## Install

Linux amd64, PostgreSQL 14–18, glibc or musl:

```bash
curl -fsSL https://github.com/profundium/pg_local_cache/releases/latest/download/install-latest.sh | bash -s -- app
```

Replace `app` with the database name. The command selects and verifies the
matching release, installs it, updates `shared_preload_libraries`, restarts
through `pg_ctl`, creates the extension, and verifies health.

Use [the production guide](docs/INSTALL_EXISTING.md) for systemd, Patroni,
Kubernetes, staged changes, rollback, RESP, or a checksum-first manual install.
Unsupported platforms can [build through PGXN/PGXS](PGXN.md).

## Use

Attach a permanent table with a primary key:

```sql
SELECT local_cache.attach_table('public.items'::regclass);
```

Read ordered JSON rows:

```sql
SELECT local_cache.mget(
  'public.items'::regclass,
  ARRAY[42, 7, 42, NULL]::bigint[]
);
```

The result preserves order, duplicates, and `NULL` positions. Missing rows
also return `NULL`. Batches are limited to 1,024 keys.

Composite keys use `text[][]` in primary-key column order:

```sql
SELECT local_cache.mget(
  'public.tenant_items'::regclass,
  ARRAY[['tenant-a', '42'], ['tenant-b', '7']]::text[][]
);
```

The caller needs `SELECT` on the source table and explicit access to the
function:

```sql
GRANT USAGE ON SCHEMA local_cache TO app_user;
GRANT EXECUTE ON FUNCTION local_cache.mget(regclass, anyarray) TO app_user;
```

Writes stay ordinary PostgreSQL:

```sql
UPDATE public.items SET value = 'new' WHERE id = 42;
```

A transaction reads its own writes from PostgreSQL. Committed changes
invalidate the old cache entry; rollback publishes nothing.

Administration:

```sql
SELECT local_cache.health();
SELECT local_cache.stats();
SELECT local_cache.reconcile_table('public.items'::regclass);
SELECT local_cache.detach_table('public.items'::regclass);
```

## Fit

Good fit:

- repeated exact-primary-key reads;
- a hot set that fits the configured cache;
- one writable PostgreSQL primary;
- callers that want ordered whole-row JSON or RESP.

Not a fit:

- joins, ranges, aggregates, full scans, or arbitrary query caching;
- TTL, pub/sub, distributed cache, or multi-primary coordination;
- tables with RLS, partitioning, inheritance, or unsupported key types.

A miss, unsafe transaction state, malformed entry, or oversized row falls back
to an indexed source-table read.

## Configure

The extension must be preloaded. Core settings are postmaster settings:

```conf
shared_preload_libraries = 'pg_local_cache'
pg_local_cache.database = 'app'
pg_local_cache.cache_entries = 100000
pg_local_cache.port = 0
```

`port = 0` is SQL-only mode. Enabling RESP also requires a dedicated
PostgreSQL role and an authentication token; keep it on loopback or behind TLS.
See [technical reference](docs/TECHNICAL.md).

## Try locally

```bash
docker compose --project-name pg_local_cache_demo -f compose.demo.yaml up -d --build --wait
```

Inspect:

```bash
docker compose --project-name pg_local_cache_demo -f compose.demo.yaml exec -T postgres psql -U postgres -d app -c "SELECT local_cache.mget('public.pg_local_cache_demo'::regclass, ARRAY[1,2]::bigint[]); SELECT local_cache.health();"
```

Remove:

```bash
docker compose --project-name pg_local_cache_demo -f compose.demo.yaml down -v
```

## Develop

```bash
make verify-static source-test
make docker-smoke
make benchmark-test
```

Local PGXS builds require PostgreSQL server headers. The Docker smoke test is
the portable PostgreSQL 14–18 verification path.

More:

- [production install](docs/INSTALL_EXISTING.md)
- [technical reference](docs/TECHNICAL.md)
- [benchmarks](docs/BENCHMARKS.md)
- [monitoring](monitoring/README.md)

License: [PostgreSQL License](LICENSE).
