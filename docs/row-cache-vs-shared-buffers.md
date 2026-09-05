---
layout: doc
title: PostgreSQL row cache vs shared_buffers
seo_title: "PostgreSQL Row Cache vs shared_buffers | pg_local_cache"
description: Compare PostgreSQL page caching with pg_local_cache 2.0 whole-row caching. See what a row-cache hit avoids, what it still costs, and when not to add another cache.
section: Read paths
permalink: /docs/row-cache-vs-shared-buffers.html
last_modified_at: "2026-09-05"
---

# PostgreSQL row cache vs shared_buffers

A warm database is the relevant baseline. A second cache is not justified just
because data fits in memory.

PostgreSQL's [`shared_buffers`](https://www.postgresql.org/docs/16/runtime-config-resource.html#GUC-SHARED-BUFFERS)
contains database pages. The operating system may also cache file contents.
A page already in memory can avoid a storage read, but a query still has to
produce a result from the database's tuples.

pg_local_cache separately stores a serialized whole-row payload under the
complete primary key. It is not a replacement for PostgreSQL's buffer manager.

## Compare the work, not just the storage medium

| Read | Work remaining |
|---|---|
| Prepared primary-key SQL over warm pages | Protocol handling, plan execution, row visibility checks, and result conversion |
| Eligible SQL mget cache hit | Protocol handling, SQL function execution, key conversion, cache synchronization, snapshot checks, and returning the stored payload |
| SQL mget miss or bypass | The function's checks plus a source-table query; a successful eligible fill can populate the cache |

The expected opportunity is avoiding repeated source-table execution and
whole-row serialization. That is a mechanism to test, not a guaranteed
speedup. Cache checks and synchronization also consume CPU, and a hit still
uses a PostgreSQL connection and backend. This SQL API does not eliminate
connection limits or connection-pool queueing.

## Costs to include

A cached row takes additional shared memory even when its source page is
already in memory. The extension also maintains mapping and invalidation
state. Updates to attached tables run the extension's triggers. When the
working set exceeds capacity, an apparent read optimization can become mostly
miss and eviction overhead.

The default demo deliberately compares a 128-row hot set with 1,024 cache slots,
then a first pass over 4,096 rows. The [benchmark guide](BENCHMARKS.md) explains
both cases and measures attached-table writes separately.

## When to leave the application alone

Keep the existing query when its end-to-end latency is already acceptable,
when the application needs only a small projection of a large row, or when
joins, ranges, and aggregation dominate. First compare an ordinary batched
query with the application's current per-key calls. A gain from batching is
not evidence of a gain from caching.

pg_local_cache 2.0 also requires explicit `mget` calls, extension installation,
and a startup preload. It rejects RLS, partitioned, and inherited tables. Those
constraints are part of the decision, not setup details to discover afterwards.

## Row cache or an external cache?

For data that remains authoritative in PostgreSQL, this design keeps
invalidation on the database write path and avoids maintaining an application
cache-aside protocol. It does not provide general Redis semantics. The optional
RESP2 endpoint has a limited command set and a separate security model.

Choose based on the operations and failure behavior you need. A PostgreSQL
row cache cannot stand in for TTL-based application state, pub/sub, or
distributed coordination. See the [technical contract](TECHNICAL.md) and
[transaction examples](cache-invalidation.md), then measure your read path.
