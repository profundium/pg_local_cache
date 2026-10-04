---
layout: doc
lang: en
translation_key: QUICKSTART
title: Try pg_local_cache locally
seo_title: "Try a PostgreSQL Row Cache Locally | pg_local_cache"
description: Run pg_local_cache 3.0 in disposable PostgreSQL, read sample rows over RESP, inspect cache hits, test updates, and remove the demo without changing an existing database.
section: Quickstart
permalink: /docs/QUICKSTART.html
last_modified_at: "2026-10-04"
---

# Try pg_local_cache locally {#try-pg_local_cache-locally}

This demo builds pg_local_cache from your checkout in a separate PostgreSQL 16
server. It does not install into an existing PostgreSQL server. The read example
uses the RESP listener configured by the Compose overlay.

You need Git, Docker, and Docker Compose with `up --wait` support. The image
builds from source.

## Start the database {#start-the-database}

```bash
git clone https://github.com/profundium/pg_local_cache.git
cd pg_local_cache
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml up --build --wait
```

The demo binds PostgreSQL and RESP to loopback ports `55432` and `56379`, has no
persistent volume, and stores data in container-local tmpfs. The RESP overlay
binds inside the container network and explicitly enables its plaintext demo
listener. Stopping the container discards its data. `demo-only` and the public
RESP token are for this loopback demo; use your own credentials in production.

If port 55432 is occupied, set `PGLC_DEMO_PORT` before starting Compose and keep
it set when running the Node.js example:

```bash
export PGLC_DEMO_PORT=55433
```

## Read over RESP {#read-as-an-application-role}

The setup creates 4,096 rows in `public.items`. Only that table is attached to
the cache. The `demo` role is not a superuser.

```bash
export REDISCLI_AUTH=DemoRespToken_0123456789abcdef0123456789
redis-cli -2 -p 56379 MGET \
  'CRUD:pglc_demo.public.items:{"id":42}' \
  'CRUD:pglc_demo.public.items:{"id":7}' \
  'CRUD:pglc_demo.public.items:{"id":42}' \
  'CRUD:pglc_demo.public.items:{"id":999999}'
```

The response preserves key order and duplicates. The first and third positions
refer to row 42; the last position is a RESP null because key 999999 does not
exist. A RESP request omits null input keys; client helpers can restore those
positions when needed.

Inspect counters as the database administrator:

```bash
docker compose -f examples/compose.yaml exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 -U postgres -d pglc_demo \
  -c 'SELECT local_cache.health();' \
  -c 'SELECT local_cache.stats();'
```

On this fresh demo, `local_cache.health()` should report `ready: true`, and
repeating the reads should increase `cache_hits`. Inspect `cache_misses` and
`database_reads` in `local_cache.stats()`, and `cache_enabled` in
`local_cache.health()` if needed.

## Check commit and rollback {#check-commit-and-rollback}

With Node.js 20 or later:

```bash
npm --prefix examples/node-postgres ci --ignore-scripts
npm --prefix examples/node-postgres run demo
```

The test uses RESP for reads and PostgreSQL for writes. It checks a warm hit,
input order, duplicate and missing keys, cache invalidation, and visibility of
committed updates. RESP workers use a configured PostgreSQL role and do not
share an application's SQL transaction or snapshot.

See the [cache invalidation guide](cache-invalidation.md) or the
[Node.js query explanation](node-postgres.md).

## Connect your application {#connect-your-application}

- [Node.js](node-postgres.md): use the RESP client for cached reads and `pg` for SQL writes.
- [Go](go.md): use RESP for cached reads and `pgx` for SQL writes.
- [RESP](resp.md): connect with a Redis client.

Next, [compare the same SQL and RESP workload](BENCHMARKS.md#run-the-same-comparison-on-every-client).
For results or setup issues, open a
[workload report](https://github.com/profundium/pg_local_cache/issues/new?template=workload.yml)
with your environment and benchmark JSON or error log.

## Remove the demo {#remove-the-demo}

```bash
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml down
```

The locally built Docker image remains available for another run. No host
PostgreSQL service needs to be restarted or restored.

For an existing database, follow the [installation guide](INSTALL_EXISTING.md).
That path has different privileges, configuration, and restart requirements.
