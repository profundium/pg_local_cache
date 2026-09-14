---
layout: doc
title: PostgreSQL primary-key cache benchmarks
seo_title: "PostgreSQL Row Cache Benchmarks: mget vs Batched SQL"
description: Local Apple M3 Max results for pg_local_cache versus prepared PostgreSQL ANY queries, with Node.js and pgbench connection sweeps, CPU measurements, and raw data.
section: Benchmarks
permalink: /docs/BENCHMARKS.html
last_modified_at: "2026-09-14"
---

# PostgreSQL primary-key cache benchmarks

Compare the explicit `local_cache.mget` API with one prepared, batched
PostgreSQL query. Both paths return the same ordered JavaScript objects,
including duplicate keys and null positions. Neither path sends a separate
network request for each key.

The [local Mac run](#local-mac-run-14-september-2026) below includes measured
results, machine details, and a connection sweep with Node.js and native
`pgbench`. It exposed a client-side limit for large Node.js batches. CI runs
remain correctness checks on shared runners, not production capacity estimates.

## Local Mac run: 14 September 2026

Measured on a shared development laptop, with the client and PostgreSQL VM
using the same CPU. Timestamps in the JSON are UTC; the local date is UTC+3.

| Component | Configuration |
|---|---|
| Host | MacBook Pro `Mac15,10`, Apple M3 Max, 14 CPU cores (10 performance + 4 efficiency), 36 GiB RAM |
| OS | macOS 26.5.2, build `25F84`, arm64 |
| Docker | Desktop 4.90.0; Engine 29.7.2; Linux `7.0.12-linuxkit`, aarch64 |
| Linux VM | 14 visible CPUs, 7.65 GiB RAM; no additional per-container CPU or RAM quota |
| Database | PostgreSQL 16.15, Debian bookworm; `max_connections=300`; `shared_buffers=128MB`; 256 MiB `/dev/shm` |
| Extension | Local 2.0.2 build with the SPI-plan lifetime fix, revision `67e57549c98726395e299b48f6092191a8153903` |
| Cache | 1,024 slots; configured memory budget 384 MiB |
| Storage | PostgreSQL data and WAL on the demo's tmpfs |
| Clients | Node.js 24.18.0, node-postgres 8.16.3; native macOS `pgbench` 18.6 |
| Connection | Prepared statements over `127.0.0.1:55432`, without TLS |

**These results use a local fix, not the unmodified 2.0.2 release.** Ten other
containers were running at the hardware snapshot; this was not an isolated
host. The [measurement JSON](../assets/benchmarks/2026-09-14-m3-max.json)
contains image IDs, source revisions, settings, query plans, all repetitions,
and the earlier attempts on the pinned 2.0.1 demo.

### Memory leak found during the run

Two long attempts on 2.0.1 slowed down and hit the 10-second statement timeout
while warming SQL queries for the third repetition. A separate check on one
persistent backend found a retained SPI plan per successful `mget` call.
PostgreSQL's [`SPI_keepplan`](https://www.postgresql.org/docs/16/spi-spi-keepplan.html)
keeps a plan until it is explicitly freed; the function state was disappearing
at query cleanup without releasing that plan.

| Repeated mget calls | Before fix: backend context bytes | After fix: backend context bytes |
|---:|---:|---:|
| 0 | 1,407,256 | 1,407,256 |
| 5,000 | 48,115,784 | 2,031,688 |
| 10,000 | 94,195,784 | 2,031,688 |
| 20,000 | 186,355,784 | 2,031,688 |

These are sums from `pg_backend_memory_contexts` after each group of calls,
not process RSS or shared-cache size. The fix frees the saved plan when the
function's memory context is reset or deleted, including query errors. The
regression test failed on 2.0.1 and passed with the fix.

Both failed attempts are retained in the JSON. The second includes 48
completed samples; the old runner had lost the first attempt's measurements.
The runner now preserves completed samples on query failure, still exits
nonzero, and the Markdown renderer rejects partial runs.

### Connection and client sweep

Each cell is median requests/s from three samples with a five-second duration
setting. A request contains the batch's number of keys. The fixed build was
measured at 4, 64, and 256 persistent connections, plus one admin connection.
The probe verified sufficient connection headroom and no other clients of the
demo database. The earlier 1–256 sweep on 2.0.1 is retained separately.

**One key per request**

| Connections | Node.js SQL | Node.js mget | pgbench SQL | pgbench mget |
|---|---:|---:|---:|---:|
| 4 | 10,559 | 10,855 | 10,467 | 10,243 |
| 64 | 51,314 | 49,950 | 49,909 | 49,875 |
| 256 | 70,173 | 60,848 | 72,999 | 68,328 |

**64 keys per request**

| Connections | Node.js SQL | Node.js mget | pgbench SQL | pgbench mget |
|---|---:|---:|---:|---:|
| 4 | 7,232 | 4,119 | 7,000 | 7,558 |
| 64 | 15,320 | 4,082 | 16,265 | 17,144 |
| 256 | 15,606 | 3,991 | 15,611 | 16,827 |

At batch 64 / 64 connections, Node.js `mget` used a median **1.03 client CPU
cores** and **0.60 PostgreSQL CPU cores**. Native `pgbench` used **0.59 client
cores** and **2.73 PostgreSQL cores**. Node.js throughput barely changed from
4 to 256 connections: this workload was limited by the client and decoding
path. Native throughput stopped improving between 64 and 256 connections.
The three native `mget` runs at 64 connections ranged from 16,657 to 17,293
requests/s; the Node.js runs ranged from 3,990 to 4,103.

For one-key reads, throughput was still increasing at 256 connections. Four
connections were too few, and this run does not establish the extension's
one-key throughput ceiling. All 72 fixed-build sweep samples completed
without query errors. Warm `mget` samples had zero misses, fills, or bypasses;
the prepared SQL baseline used `items_pkey` for both batch sizes in the
recorded query plans.

Node.js includes driver decoding, JSON parsing, and restoration of ordered
objects. Native `pgbench` receives the complete result but does not perform
that application work. Its throughput is a client-overhead diagnostic, not
an application speedup. See [measurement boundaries](#check-client-and-connection-limits)
for how CPU time and cache counters were collected.

### Application workloads

The full run uses 64 persistent clients and 50,000 requests per sample, with
three repetitions. Cold fill instead visits the 4,096 rows once per sample.
Both read paths return the same ordered JavaScript objects. The table reports
median requests/s and the minimum–maximum across repetitions.

| Workload | Keys/request | Prepared SQL requests/s | mget requests/s |
|---|---:|---:|---:|
| Warm reads | 1 | 48,612 (47,069–51,010) | 40,939 (39,579–46,171) |
| Warm reads | 16 | 29,755 (28,396–32,335) | 11,326 (10,691–12,394) |
| Warm reads | 64 | 11,191 (10,907–12,768) | 3,840 (3,570–4,065) |
| 5% updates | 1 | 48,136 (44,322–49,165) | 41,847 (41,793–46,322) |
| 5% updates | 16 | 30,563 (28,186–33,964) | 11,890 (11,042–13,039) |
| 5% updates | 64 | 13,784 (11,546–14,171) | 4,237 (3,998–4,319) |

| Write target | UPDATE requests/s |
|---|---:|
| Unattached copy | 44,423 (39,064–52,260) |
| Attached table | 43,901 (39,955–47,891) |

All 60 samples completed with the original 10-second statement timeout.
Prepared SQL was faster in these Node.js read workloads. The write ranges
overlap; three short repetitions do not establish a small trigger-overhead
percentage.

Individual read/write p50, p95, and p99 remain in the JSON. At batch 64, cold
fill has only 64 observations per repetition; its tail percentiles are too
sparse for a useful latency claim. Write timings use tmpfs, with `fsync`,
`full_page_writes`, and `synchronous_commit` enabled; they do not model writes
to persistent storage.

### Reproduce the local build

Use a clean checkout of the recorded harness revision (in the JSON). The
extension source at that revision matches the fix linked above. The optional
Compose overlay builds local source; the default demo remains pinned to 2.0.1.
Run the following from the repository root:

```bash
export PGLC_EXTENSION_REF=67e57549c98726395e299b48f6092191a8153903
export PGLC_DEMO_MAX_CONNECTIONS=300
npm --prefix examples/node-postgres ci --ignore-scripts
docker compose -f examples/compose.yaml -f examples/compose.local.yaml \
  up -d --build --wait
DURATION_SECONDS=5 REPEATS=3 CONNECTIONS=4,64,256 BATCHES=1,64 \
  node examples/node-postgres/connection-sweep.mjs > connection-sweep.json
CLIENTS=64 REQUESTS=50000 REPEATS=3 BATCHES=1,16,64 \
  npm --prefix examples/node-postgres run --silent benchmark > benchmark.json
python3 scripts/benchmark_report.py benchmark.json > benchmark.md
docker compose -f examples/compose.yaml -f examples/compose.local.yaml down
```

## Run

For a short correctness and comparison run, start the
[disposable demo](QUICKSTART.md), then run from the repository root:

```bash
npm --prefix examples/node-postgres ci --ignore-scripts
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

Both queries use named prepared statements through node-postgres. Read and
UPDATE statements are prepared on each connection before timing. The baseline
reads the same attached table through ordinary PostgreSQL; attachment does not
rewrite its SELECT. The JSON baseline matches the cache's whole-row contract.
If your application needs only two columns, also measure its existing projection
without whole-row JSON conversion.

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
just the rendered summary. Dependencies are pinned by the committed lockfile; retain
that file with results too.

Each sample includes elapsed time, completed requests/s, requested read keys/s,
read and write p50/p95/p99, observation counts, SQL cache-counter deltas,
Node.js process CPU time per elapsed second, and event-loop utilization.
Requested keys/s includes duplicates and missing keys; it is not a count of
unique rows returned. A mixed sample's requests/s includes both reads and writes.
The report keeps repetitions separate rather than averaging their percentiles.

These are client-observed timings over loopback TCP, including transfer, driver
decoding, and JSON parsing. They are not executor-only timings. The fixed number
of concurrent clients is a **closed-loop** load: a client sends its next request
after the previous one finishes. This does not model an independently arriving
production request stream or correct for coordinated omission.

## Check client and connection limits

The manual probe uses the same two SQL statements, prepared protocol, persistent
connections, and loopback TCP endpoint for Node.js and native
[`pgbench`](https://www.postgresql.org/docs/18/pgbench.html). Install `pgbench`
on the client host and put it on `PATH`; on this Mac it came from Homebrew's
`libpq`. Run with the disposable demo already started and no other clients
connected to it:

```bash
DURATION_SECONDS=5 REPEATS=3 CONNECTIONS=1,4,16,32,64 BATCHES=1,64 \
  node examples/node-postgres/connection-sweep.mjs > connection-sweep.json
```

For 128 and 256 connections, raise the **disposable demo's** limit first.
This recreates its container and resets its temporary data:

```bash
PGLC_DEMO_MAX_CONNECTIONS=300 \
  docker compose -f examples/compose.yaml up -d --wait --no-build
DURATION_SECONDS=5 REPEATS=3 CONNECTIONS=128,256 BATCHES=1,64 \
  node examples/node-postgres/connection-sweep.mjs > connection-sweep-high.json
```

Each client keeps one request in flight. `pgbench` uses up to eight worker
threads; Node.js uses one event loop. Both receive the complete response.
Node.js also decodes the text array, parses JSON, and restores the application
return shape. `pgbench` discards the result after libpq receives it, so its
throughput measures a different amount of client work. It is a diagnostic for
client overhead, not a drop-in application benchmark.

The probe repeatedly reads the same first 1 or 64 keys, whereas the main runner
cycles through 128 hot rows. It warms the cache, checks result parity before
timing, and requires cache hits with zero misses, fills, or bypasses during
every `mget` sample. Driver, query, and connection order alternate across three
repetitions. Node.js prepares each connection before timing; `pgbench` includes
its first prepare in the timed run and excludes initial connection time from
throughput. Neither reconnects per request or uses pipelining.
Node.js counter deltas also include one warm-up read per connection, so use
the explicit completed-request count for throughput, not cache hits.

The JSON includes client CPU consumption and PostgreSQL container CPU deltas
from cgroup v2. One CPU core means one CPU-second per elapsed second. Container
CPU covers the wider connection/setup/measurement window and background server
work; it excludes Docker Desktop's port forwarding and other VM processes.
Native client CPU comes from `/usr/bin/time -p` and includes startup and
connection setup. These short samples are approximate CPU measurements.

The probe checks connection headroom before starting. It does not change the
server configuration or dataset, and does not run in CI.

## What this does not establish

This small dataset does not establish behavior for large rows, a skewed
production key distribution, long transactions, network RTT, pool exhaustion,
replica reads, crash recovery, or sustained write-heavy traffic. The main runner
does not measure server CPU consumption; the connection sweep records
container CPU time. Neither measures peak resident memory; the configured
memory budget is not a measured RSS value. Docker Desktop also includes a VM.

Use a dedicated host for a reference result. Record CPU and memory limits,
PostgreSQL image ID, filesystem, container/VM details, and whether client and
server share CPU resources. Keep all repetitions, including slower ones. Check
query plans and cache counters before attributing any difference to the cache.

To share a result, open a
[workload report](https://github.com/profundium/pg_local_cache/issues/new?template=workload.yml)
with the JSON, configuration, and the workload you actually need to support.
