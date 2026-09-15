---
layout: doc
title: PostgreSQL caching decision guide
seo_title: "PostgreSQL Caching Decision Guide: Pages, Rows, Views, or Redis"
description: Choose PostgreSQL page caching, prepared SQL, whole-row caching, materialized views, or an external cache by the work you need to avoid.
section: Guides
permalink: /docs/postgresql-caching.html
last_modified_at: "2026-09-16"
---

# PostgreSQL caching decision guide

“Add a cache” describes several different changes. PostgreSQL page caching,
prepared SQL, a whole-row cache, a materialized view, and Redis avoid different
parts of a read. Choose from the repeated work in your request, then measure
the complete path with the [benchmark guide](BENCHMARKS.md).

## Start with the work you repeat

| Need | First option | What it changes |
|---|---|---|
| Keep table and index pages hot | PostgreSQL `shared_buffers` and the OS cache | Fewer storage reads; SQL still runs |
| Send the same statement many times | A prepared statement | Less repeated parse and plan work; execution still runs |
| Return complete rows by primary key | `pg_local_cache` SQL `mget` | Reuses eligible whole-row payloads through an explicit API |
| Precompute a join or aggregate | A materialized view | Reads persisted results; refresh defines freshness |
| Share application objects across services | An external cache such as Redis | Application-managed keys, TTLs, and invalidation |

### Pages and prepared SQL

[`shared_buffers`](https://www.postgresql.org/docs/18/runtime-config-resource.html#GUC-SHARED-BUFFERS)
holds database pages, not final `SELECT` results. A warm page can avoid storage
I/O, but PostgreSQL still plans or executes the query, checks visibility, and
constructs the result. A [prepared statement](https://www.postgresql.org/docs/18/sql-prepare.html)
can avoid repeated parse and analysis work in one session. It still executes
against the current database state, and its plan can be generic or custom.

These are often enough. Compare them first, especially when a query returns a
small projection or joins several tables. The [row cache comparison](row-cache-vs-shared-buffers.md)
shows the work that remains on each path.

### Whole rows by primary key

`pg_local_cache` stores serialized complete rows under complete primary keys in
bounded PostgreSQL shared memory. It is reached through
`local_cache.mget('public.items'::regclass, $1::bigint[])`; an ordinary
`SELECT` never consults it. Eligible clean `READ COMMITTED` reads may hit, while
stricter isolation modes, writes in the transaction, recovery, parallel
execution, or oversized rows use PostgreSQL. Unsupported table mappings are
rejected during attachment. This is a
specific read path, not an arbitrary query-result cache. See the
[batch lookup guide](batch-primary-key-lookups.md), [technical contract](TECHNICAL.md),
and [transaction checks](cache-invalidation.md).

### Views and external caches

PostgreSQL [materialized views](https://www.postgresql.org/docs/18/rules-materializedviews.html)
persist a query result in a relation and refresh it on demand. They suit
repeatable reports, aggregates, and joins where a refresh schedule is an
acceptable freshness boundary. They are not a per-key substitute for a row
cache.

An external cache such as Redis suits application objects shared by multiple
processes or services. The application owns keys, serialization, TTL, and
invalidation. Follow the [Redis cache-aside guide](postgresql-redis-cache.md)
before adding this coordination path. Use the [quickstart](QUICKSTART.md) to
test `pg_local_cache` on `public.items`; do not infer a speedup until your
workload's hit rate, row size, writes, and end-to-end latency support it.
