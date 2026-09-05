---
layout: doc
title: Try pg_local_cache locally
seo_title: "Try a PostgreSQL Row Cache Locally | pg_local_cache"
description: Run pg_local_cache 2.0 in disposable PostgreSQL, read sample rows, inspect cache hits, test updates, and remove the demo without changing an existing database.
section: Quickstart
permalink: /docs/QUICKSTART.html
last_modified_at: "2026-09-05"
---

# Try pg_local_cache locally

This demo creates a separate PostgreSQL 16 server with pg_local_cache 2.0.1.
It does not install anything in your existing PostgreSQL server. The extension
source is pinned to commit `8569a937abb9ba1859ffb9c2a4dbc34f076fbe20`.

You need Git, Docker, and Docker Compose with `up --wait` support. The image is
built from source, not downloaded from an unlisted container registry. Binary
packages are tested on Linux amd64; a successful build on another architecture
is not evidence of equivalent performance or support.

## Start the database

```bash
git clone https://github.com/profundium/pg_local_cache.git
cd pg_local_cache
docker compose -f examples/compose.yaml up --build --wait
```

The demo binds PostgreSQL to `127.0.0.1:55432`. It has no RESP listener and no
persistent volume. Its data directory is a container-local tmpfs. Stopping the
container discards the data. The password `demo-only` is for this loopback-only
demo, not an example of production credential management.

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

Both calls return the same ordered rows. The first and third positions refer
to row 42. The last two positions are SQL `NULL`: one input is null, and key
999999 does not exist. In psql, SQL nulls appear blank by default.

The function returns **`text[]`**, not a set of rows and not `jsonb[]`.
`unnest` above is only for displaying one array entry per line.

Inspect counters as the database administrator:

```bash
docker compose -f examples/compose.yaml exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 -U postgres -d pglc_demo \
  -c 'SELECT local_cache.health();' \
  -c 'SELECT local_cache.stats();'
```

Look at `sql_cache_hits`, `sql_cache_misses`, `sql_cache_fills`, and
`sql_cache_bypasses`. A call succeeding is not proof that it used the cache.

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

## Compare with ordinary SQL

```bash
npm --prefix examples/node-postgres run --silent benchmark > benchmark.json
python3 scripts/benchmark_report.py benchmark.json
```

The benchmark resets the demo tables between samples. Do not use the demo to
store data you need. Read the [methodology and limitations](BENCHMARKS.md)
before interpreting the output.

## Remove the demo

```bash
docker compose -f examples/compose.yaml down
```

The locally built Docker image remains available for another run. No host
PostgreSQL service needs to be restarted or restored.

For an existing database, follow the [installation guide](INSTALL_EXISTING.md).
That path has different privileges, configuration, and restart requirements.
