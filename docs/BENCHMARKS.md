---
layout: doc
title: PostgreSQL primary-key cache benchmarks
seo_title: "PostgreSQL Row Cache Benchmarks: mget vs Batched SQL"
description: Reproduce pg_local_cache 2.0 benchmarks against a prepared PostgreSQL ANY query, including warm reads, cold fills, concurrent updates, latency, and write overhead.
section: Benchmarks
permalink: /docs/BENCHMARKS.html
last_modified_at: "2026-09-05"
---

# PostgreSQL primary-key cache benchmarks

Compare the explicit `local_cache.mget` API with one prepared, batched
PostgreSQL query. Both paths return the same ordered JavaScript objects,
including duplicate keys and null positions. Neither baseline sends a separate
network request for each key.

**No reference performance result is published yet.** The commands below
produce a measured report on your machine. CI runs are correctness checks on
shared runners, not production capacity estimates.

## Run

Start the [disposable demo](QUICKSTART.md), then run from the repository root:

```bash
npm --prefix examples/node-postgres install --ignore-scripts
CLIENTS=4 REQUESTS=2000 REPEATS=3 BATCHES=1,16,64 \
  npm --prefix examples/node-postgres run --silent benchmark > benchmark.json
python3 scripts/benchmark_report.py benchmark.json > benchmark.md
```

`REQUESTS` is the total number of requests per sample, not a per-client count.
Cold-fill samples instead visit all 4,096 demo rows exactly once. With a batch
of 64 that is only 64 latency observations; do not treat its p99 as a stable
tail estimate.

The runner uses only the loopback demo connection, checks the database and
table marker, and rejects a non-2.0 extension. It resets the demo rows between
samples. It does not accept an arbitrary production `DATABASE_URL`.

## Exact read queries

| Path | Query sent to PostgreSQL | Client work included in timing |
|---|---|---|
| SQL mget | `SELECT local_cache.mget('public.items'::regclass, $1::bigint[]) AS rows` | Decode the text array and parse each JSON row |
| Prepared SQL baseline | `SELECT id::text AS key, row_to_json(i)::text AS row FROM public.items AS i WHERE id = ANY($1::bigint[])` | Restore input order, duplicates, and missing positions, then parse each JSON row |

Both queries use named prepared statements through node-postgres. The baseline
reads the same attached table through ordinary PostgreSQL; attachment does not
rewrite its SELECT. The JSON baseline is chosen to match the cache's whole-row
contract. It is not a claim that JSON is the fastest format for every client.
An application that needs only two columns should also measure its existing
projection without whole-row JSON conversion.

Source: [queries.mjs](https://github.com/profundium/pg_local_cache/blob/master/examples/node-postgres/queries.mjs)
and [benchmark.mjs](https://github.com/profundium/pg_local_cache/blob/master/examples/node-postgres/benchmark.mjs).

## Workloads

| Sample | Dataset and operation | What to inspect |
|---|---|---|
| Warm reads | Repeated reads of 128 rows; 1,024 cache slots | Read latency, requested keys/s, and actual cache hits |
| Cold fill | Each of 4,096 rows visited once after cache invalidation | Miss/fill cost; the source pages are already warm |
| Mixed reads and writes | Every twentieth request is an UPDATE; remaining requests read the hot set | Separate read/write latency and cache-counter deltas |
| Writes, unattached | UPDATE a separate copy without cache triggers | Write baseline |
| Writes, attached | The same UPDATE against the attached table | Cost of cache invalidation on writes |

The update is `UPDATE <table> SET revision = revision + 1 WHERE id = $1`.
The runner selects from two fixed table names; no user-controlled identifier is
interpolated. Both tables begin each sample with the same rows and revisions.
Read-path order and write-table order alternate between repetitions.

## Recorded output

`benchmark.json` records extension and harness revisions separately, the
PostgreSQL and Node.js versions, client OS/architecture/CPU, visible CPU count,
cache settings, row counts, concurrency, and every sample. Retain the JSON, not
just the rendered summary. Dependency installation produces a lockfile; retain
that file with results too.

Each sample includes elapsed time, completed requests/s, requested read keys/s,
read and write p50/p95/p99, observation counts, and SQL cache-counter deltas.
Requested keys/s includes duplicates and missing keys; it is not a count of
unique rows returned. A mixed sample's requests/s includes both reads and writes.
The report keeps repetitions separate rather than averaging their percentiles.

These are client-observed timings over loopback TCP, including transfer, driver
decoding, and JSON parsing. They are not executor-only timings. The fixed number
of concurrent clients is a **closed-loop** load: a client sends its next request
after the previous one finishes. This does not model an independently arriving
production request stream or correct for coordinated omission.

## What this does not establish

This small dataset does not establish behavior for large rows, a skewed
production key distribution, long transactions, network RTT, pool exhaustion,
replica reads, crash recovery, or sustained write-heavy traffic. The runner does
not measure server CPU consumption or peak resident memory; the configured
memory budget is not a measured RSS value. Docker Desktop also includes a VM.

Use a dedicated host for a reference result. Record CPU and memory limits,
PostgreSQL image ID, filesystem, container/VM details, and whether client and
server share CPU resources. Keep all repetitions, including slower ones. Check
query plans and cache counters before attributing any difference to the cache.

To share a result, open a
[workload report](https://github.com/profundium/pg_local_cache/issues/new?template=workload.yml)
with the JSON, configuration, and the workload you actually need to support.
