---
layout: post
lang: en
translation_key: blog-measure-postgresql-row-cache
title: "When a PostgreSQL row cache helps: measure the whole read"
description: Design a fair PostgreSQL row-cache comparison using prepared SQL, SQL mget and RESP MGET. Separate warm reads, misses, batch sizes, writes and client costs.
permalink: /blog/measure-postgresql-row-cache/
date: "2026-09-22"
last_modified_at: "2026-09-22"
topic: performance
---

# When a PostgreSQL row cache helps {#when-a-postgresql-row-cache-helps}

A database can serve every page from memory and still spend time executing
queries, checking visibility and constructing results. A row cache tries to
avoid part of that repeated work. It also adds key handling, cache checks and
serialization costs. The useful question is whether the complete application
request becomes cheaper for your workload.

`pg_local_cache` exposes an explicit `local_cache.mget` API. Ordinary `SELECT`
queries retain their normal PostgreSQL execution path. A warm `shared_buffers`
cache and a warm row cache are therefore different experimental conditions.

## Write down the result contract first {#result-contract}

Compare the same keys, columns and output shape. If the application needs only
two columns, comparing that SQL projection with serialized whole rows measures
different work. If callers expect duplicates, input order and a null result for
each missing key, include that alignment work in every client.

The [batch lookup guide](../docs/batch-primary-key-lookups.md) gives both an
`ANY` baseline and an ordered `WITH ORDINALITY` baseline. Neither requires the
extension. Establish the SQL baseline before adding a cache.

## Change one workload dimension at a time {#workload-dimensions}

| Experiment | What to hold fixed | What it reveals |
|---|---|---|
| Warm repeated reads | Keys, result shape, connections | Reuse of already populated entries |
| Cold or missing keys | Request distribution and batch size | Source-table and negative-result costs |
| Larger batches | Total requested keys and payload shape | Round-trip savings versus per-key work |
| Concurrent writes | Read/write mix and transaction boundaries | Invalidation, refill and visibility costs |
| Wider rows | Key distribution and client placement | Serialization, transport and row-size bypass |

An ineligible read can legitimately use the source table. Inspect counter
deltas around each experiment; a low hit rate alone does not diagnose a broken
installation. Keep SQL and RESP counters separate. The
[technical reference](../docs/TECHNICAL.md#health-and-monitoring) describes
`local_cache.stats()` and `local_cache.health()`.

## Use the shared runner, then inspect the evidence {#shared-runner}

After the [quickstart](../docs/QUICKSTART.md), run the repository's comparison:

```bash
./examples/benchmark.sh all > comparison.json
python3 scripts/benchmark_report.py comparison.json
```

The [benchmark guide](../docs/BENCHMARKS.md) lists prerequisites, workload
controls and metrics. Keep the raw JSON. Record the extension and harness
revisions, PostgreSQL version, machine, connection count and client placement.
Compare repeated runs, latency distributions and server resource use alongside
throughput. A short correctness smoke run is not a publishable speed result.

## Decide from the application boundary {#application-boundary}

SQL `mget` and RESP `MGET` use different transports and result handling. A gain
for one does not establish a gain for the other. The project's
[dated Go measurements](../docs/benchmarks-go.md) include a single-key case
where SQL `mget` was slower than prepared SQL. That is a reason to test, not a
universal prediction.

Keep ordinary SQL when joins, projections, locking or unsupported table shapes
are required, or when the cache brings no measured benefit. For repeated
whole-row reads by primary key, test the explicit API with the same client work
your application actually performs. Continue with the
[caching decision guide](../docs/postgresql-caching.md) and the
[invalidation experiment](../docs/cache-invalidation.md).
