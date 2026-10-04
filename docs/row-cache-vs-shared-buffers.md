---
layout: doc
lang: en
translation_key: row-cache-vs-shared-buffers
title: PostgreSQL row cache vs shared_buffers
seo_title: "PostgreSQL Row Cache vs shared_buffers | pg_local_cache"
description: Compare PostgreSQL page caching with pg_local_cache whole-row caching: source work avoided, cache costs, and workloads that should keep ordinary SQL.
section: Read paths
permalink: /docs/row-cache-vs-shared-buffers.html
last_modified_at: "2026-10-04"
---

# PostgreSQL row cache vs shared_buffers {#postgresql-row-cache-vs-shared_buffers}

This guide compares PostgreSQL page caching with `pg_local_cache` whole-row caching and shows what each read path still does.

[`shared_buffers`](https://www.postgresql.org/docs/18/runtime-config-resource.html#GUC-SHARED-BUFFERS) keeps database pages in memory. A warm page can avoid storage I/O, but PostgreSQL still checks tuple visibility, executes the query, and builds the result. An eligible RESP `MGET` hit can return a stored whole-row payload after key, cache, and snapshot checks.

See the [technical read-path reference](TECHNICAL.md#read-path-and-safe-fallback).

## Does PostgreSQL cache SELECT results? {#does-postgresql-cache-select-results}

No. PostgreSQL page caches store pages, not final query results. A [prepared statement](https://www.postgresql.org/docs/18/sql-prepare.html) can reuse parse and planning work; PostgreSQL still executes it. `pg_local_cache` exposes whole-row caching through RESP `MGET`, not arbitrary `SELECT` caching. See [RESP clients](resp.md).

## Compare the work, not just the storage medium {#compare-the-work-not-just-the-storage-medium}

| Read path | Work remaining |
|---|---|
| Prepared primary-key SQL over warm pages | Protocol, query execution, visibility checks, and result conversion |
| Eligible RESP `MGET` hit | Protocol, key conversion, cache synchronization, eligibility checks, and payload return |
| RESP miss or bypass | Cache checks and a source-table read; eligible rows may fill the cache |

A hit avoids repeating source-table execution and whole-row serialization. It still uses a PostgreSQL worker and cache synchronization. RESP does not share the caller's SQL transaction or snapshot.

## Costs to include {#costs-to-include}

Rows and mapping state use additional shared memory. Writes to attached tables run invalidation triggers. A working set larger than cache capacity can increase misses and evictions.

Measure the same key set, row shape, connection count, and request mix on both paths. Keep batched SQL separate from single-row reads; batching alone can reduce round trips without a cache.

## When to leave the application alone {#when-to-leave-the-application-alone}

Keep ordinary SQL when its end-to-end latency is acceptable, when the application needs only a projection, or when joins, ranges, and aggregation dominate. Use SQL for row locks and reads that must share a transaction.

## Row cache or an external cache? {#row-cache-or-an-external-cache}

Use `pg_local_cache` when PostgreSQL remains authoritative and whole rows are repeatedly read by primary key. Use an external cache for TTL-based application state, pub/sub, distributed coordination, or shared objects across services. The [technical reference](TECHNICAL.md) describes the RESP security boundary.
