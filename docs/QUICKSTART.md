---
layout: doc
title: Try pg_local_cache locally
seo_title: "Try a PostgreSQL Row Cache Locally | pg_local_cache"
description: Run pg_local_cache 2.0 in disposable PostgreSQL, read sample rows, inspect cache hits, test updates, and remove the demo without changing an existing database.
section: Quickstart
permalink: /docs/QUICKSTART.html
last_modified_at: "2026-09-15"
---

# Try pg_local_cache locally

This demo builds pg_local_cache from your checkout in a separate PostgreSQL 16
server. It does not install into an existing PostgreSQL server.

You need Git, Docker, and Docker Compose with `up --wait` support. The image
builds from source.

## Start the database

```bash
git clone https://github.com/profundium/pg_local_cache.git
cd pg_local_cache
docker compose -f examples/compose.yaml up --build --wait
```

The demo binds PostgreSQL to `127.0.0.1:55432`, has no RESP listener or
persistent volume, and stores data in container-local tmpfs. Stopping the
container discards its data. `demo-only` is for this loopback demo; use your own
credentials in production.

If port 55432 is occupied, set `PGLC_DEMO_PORT` before starting Compose and keep
it set when running the Node.js example:

```bash
export PGLC_DEMO_PORT=55433
```

## Read as an application role

The setup creates 4,096 rows in `public.items`. Only that table is attached to
the cache. The `demo` role is not a superuser.

```bash
docker compose -f examples/compose.yaml exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 -U demo -d pglc_demo <<'SQL'
SELECT unnest(local_cache.mget(
  'public.items'::regclass,
  ARRAY[42, 7, 42, NULL, 999999]::bigint[]
));
SELECT unnest(local_cache.mget(
  'public.items'::regclass,
  ARRAY[42, 7, 42, NULL, 999999]::bigint[]
));
SQL
```

Both calls return the same ordered rows. The first and third positions refer to
row 42. The last two positions are SQL `NULL`: one input is null, and key 999999
does not exist. In psql, SQL nulls appear blank by default.

The function returns **`text[]`**. `unnest` above displays one array entry per
line.

Inspect counters as the database administrator:

```bash
docker compose -f examples/compose.yaml exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 -U postgres -d pglc_demo \
  -c 'SELECT local_cache.health();' \
  -c 'SELECT local_cache.stats();'
```

Look at `sql_cache_hits`, `sql_cache_misses`, `sql_cache_fills`, and
`sql_cache_bypasses`.

## Check commit and rollback

With Node.js 20 or later:

```bash
npm --prefix examples/node-postgres ci --ignore-scripts
npm --prefix examples/node-postgres run demo
```

The test opens separate reader and writer connections. It checks a warm hit,
input order, duplicate and missing keys, an uncommitted update, read-your-writes,
rollback, and a committed update. It exits nonzero on a failed assertion.

See the [two-session SQL walkthrough](cache-invalidation.md) or the
[Node.js query explanation](node-postgres.md).

## Connect your application

- [Node.js](node-postgres.md): use your existing `pg` connection or pool.
- [Go](go.md): connect with `pgx` and decode the returned rows.
- [RESP](resp.md): enable the optional endpoint and connect with a Redis client.

[Benchmark results](BENCHMARKS.md) include throughput and PostgreSQL resource use.

## Remove the demo

```bash
docker compose -f examples/compose.yaml down
```

The locally built Docker image remains available for another run. No host
PostgreSQL service needs to be restarted or restored.

For an existing database, follow the [installation guide](INSTALL_EXISTING.md).
That path has different privileges, configuration, and restart requirements.
