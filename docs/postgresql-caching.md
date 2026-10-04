---
layout: doc
lang: en
translation_key: postgresql-caching
title: PostgreSQL caching decision guide
seo_title: "PostgreSQL Caching Decision Guide: Pages, Rows, Views, or Redis"
description: Compare PostgreSQL page caching, prepared SQL, whole-row caching, materialized views, and Redis by the work each read path avoids.
section: Guides
permalink: /docs/postgresql-caching.html
last_modified_at: "2026-10-04"
---

# PostgreSQL caching decision guide {#postgresql-caching-decision-guide}

This guide compares PostgreSQL page caching, prepared SQL, whole-row caching, materialized views, and Redis by the work each option avoids.

## Start with the work you repeat {#start-with-the-work-you-repeat}

| Need | Option | Work avoided |
|---|---|---|
| Keep table and index pages hot | PostgreSQL `shared_buffers` and OS cache | Fewer storage reads; SQL still runs |
| Send the same statement many times | Prepared statement | Less repeated parse and plan work; execution still runs |
| Read complete rows by primary key | Authenticated RESP `MGET` with `pg_local_cache` | Reuses eligible whole-row JSON |
| Reuse joins or aggregates | Materialized view | Reads persisted results; refresh defines freshness |
| Share application objects across services | Redis cache-aside | Application-managed keys, TTLs, and invalidation |

### Pages and prepared SQL {#pages-and-prepared-sql}

[`shared_buffers`](https://www.postgresql.org/docs/18/runtime-config-resource.html#GUC-SHARED-BUFFERS) caches database pages, not final `SELECT` results. PostgreSQL still checks visibility, executes the query, and builds each result. A [prepared statement](https://www.postgresql.org/docs/18/sql-prepare.html) reduces repeated parsing; it still executes against current database state.

See [row cache vs shared_buffers](row-cache-vs-shared-buffers.md) for work remaining on each path.

### Whole rows by primary key {#whole-rows-by-primary-key}

`pg_local_cache` stores complete rows in bounded PostgreSQL shared memory. An authenticated RESP `MGET` can return an eligible cached row; ordinary SQL never reads this cache. Misses and ineligible reads use the source table. The endpoint uses one configured database role and does not share the caller's SQL transaction or snapshot. See the [batch guide](batch-primary-key-lookups.md), [technical reference](TECHNICAL.md), and [invalidation guide](cache-invalidation.md).

### Views and external caches {#views-and-external-caches}

A PostgreSQL [materialized view](https://www.postgresql.org/docs/18/rules-materializedviews.html) stores a query result and refreshes on demand. It suits reports and aggregates where refresh time defines freshness.

Redis suits application objects shared across processes. The application owns keys, serialization, TTLs, and invalidation. See [PostgreSQL and Redis cache-aside](postgresql-redis-cache.md). Run the [quickstart](QUICKSTART.md) to try the row-cache path.
