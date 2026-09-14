---
layout: doc
title: PostgreSQL primary-key cache benchmarks
seo_title: "PostgreSQL Cache Benchmarks: SQL, RESP, Node.js and Go"
description: Apple M3 Max benchmarks of pg_local_cache over SQL and RESP with Go and Node.js, including connection scaling, PostgreSQL CPU, memory, and reproducible results.
section: Benchmarks
permalink: /docs/BENCHMARKS.html
last_modified_at: "2026-09-15"
---

# PostgreSQL primary-key cache benchmarks

Compare SQL `local_cache.mget` and RESP `MGET` with a prepared, batched
PostgreSQL query.
Every client below parses the returned JSON and restores input order,
duplicates, and missing positions. All paths return complete rows.

## SQL and RESP on the same Mac: 15 September 2026

With the Go client inside the Linux VM, one-key RESP `MGET` reached
**839,678 requests/s versus 253,790 for prepared SQL (3.31×)** at 256
connections. SQL `mget` was slower for one key. With 64 keys per request,
RESP and SQL `mget` were close at 64 connections; RESP was 21% faster at 256.

This uses the Apple M3 Max, PostgreSQL 16.15 and 4,096-row dataset described
below. Go 1.27.1 / pgx 5.11.0 and a small standard-library RESP2 client
decode every JSON row. Both build each request during timing. Connections,
authentication, preparation and warmup finish before timing; neither path
pipelines requests. The client runs in a separate container, sharing the
server's network namespace and CPU hardware, but **not its cgroup**.

The optional RESP overlay configures eight workers, a 1 GiB cache/buffer
budget, 1,024 entries, 512 RESP clients and 300 SQL connections.
The active extension build is `f03ed22169c3841c45f439bff5e1d693bb4808a5`;
the previous build is `53dbb3c43505034888019fbde808b2e167c22f1d`.

[Download all 129 samples](../assets/benchmarks/2026-09-15-m3-max-resp.json),
including machine details, client source and binary hashes, both server
images, resource snapshots, restart checks and prepared query plans.

### Throughput inside the VM

Numbers are median **requests/s** from three five-second samples per case.
A request returns either one or 64 complete rows, as indicated.

| Keys/request | Connections | Prepared SQL | SQL mget | RESP MGET |
|---:|---:|---:|---:|---:|
| 1 | 64 | 277,088 | 211,251 | 722,133 |
| 1 | 256 | 253,790 | 186,296 | 839,678 |
| 64 | 64 | 26,459 | 38,122 | 38,877 |
| 64 | 256 | 27,615 | 43,647 | 52,774 |

The initial optimized server boot delivered only 19–21k requests/s for
64-key SQL. Restarting the **same binary** restored 26–28k with identical
data, settings and prepared index plans. The table uses three repetitions
after that restart for 64-key cases; all earlier samples remain in the JSON.
The cause of this startup-dependent variation was not isolated. It is a
reason to avoid precise capacity claims from this shared laptop.

All timed cache reads hit: zero source reads, errors or connection-limit
rejections for RESP; zero misses, fills or bypasses for SQL `mget`.
Increasing connections from 64 to 256 helps RESP but slows single-key SQL.
The harness checks connection headroom before each run. A separate
`GOMAXPROCS=12` probe at 256 connections / 64 keys yielded 51,176 RESP
requests/s versus 53,120 with eight threads in the same server boot; SQL
`mget` rose from 44,057 to 46,877. More client threads did not improve RESP
in that probe; this does not establish a driver-independent ceiling.

### PostgreSQL resource cost

At **256 connections**, medians across the same reference samples:

| Keys/request | Path | Client CPU cores | Server CPU cores (% of VM) | Server µs/request | Sampled peak MiB |
|---:|---|---:|---:|---:|---:|
| 1 | SQL | 3.97 | 8.94 (63.9%) | 36.3 | 679.8 |
| 1 | SQL mget | 3.36 | 9.93 (70.9%) | 54.4 | 684.2 |
| 1 | RESP MGET | 5.98 | 6.11 (43.7%) | 7.8 | 263.7 |
| 64 | SQL | 3.72 | 9.45 (67.5%) | 348.3 | 689.8 |
| 64 | SQL mget | 5.12 | 4.99 (35.6%) | 116.0 | 707.7 |
| 64 | RESP MGET | 5.76 | 4.97 (35.5%) | 95.8 | 266.7 |

Server CPU is the PostgreSQL container's cgroup CPU time; one core means
one CPU-second per second, and capacity is the VM's 14 visible CPUs.
Memory is sampled cgroup `memory.current`, including shared memory and
tmpfs, not process RSS. The client is measured separately. Resource windows
include the short reporting gap and monitoring work.

The raw file records block I/O, throttling, memory events and SQL session
state/wait snapshots. Network counters exclude loopback, so they **do not
measure the VM client's traffic**. The tmpfs dataset does not measure disk
performance. SQL baselines use `items_pkey`, with zero block reads in the
recorded plan checks.

### What changed in the extension

RESP MGET now parses and canonicalizes each key once, retaining the typed
values for a possible PostgreSQL lookup. It still validates the whole
request before reading, enforces one command deadline, and checks cached
payloads and invalidation state.

| Connections, 64 keys/request | RESP before | RESP after | Server µs/request, before → after |
|---:|---:|---:|---:|
| 64 | 36,018 | 38,877 | 107.9 → 88.3 |
| 256 | 49,149 | 52,774 | 114.5 → 95.8 |

Single-key throughput changed little: 843,382 → 839,678 requests/s at
256 connections. The 3.31× SQL comparison comes mainly from using the
existing RESP path, not from this small code change.

### Why the host measurements showed a small gain

The same Go code running on macOS through Docker's published ports gave:

| Keys/request, 64 connections | Prepared SQL | SQL mget | RESP MGET |
|---:|---:|---:|---:|
| 1 | 49,194 | 48,010 | 52,426 |
| 64 | 14,324 | 15,751 | 16,240 |

Moving the client into the VM changes both its runtime placement and the
network route. The large difference shows why the host-port run cannot
establish server capacity; it does not isolate port-forwarder cost alone.

[KVik](https://postgrespro.ru/docs/enterprise/current/proxima) also offers a
RESP path. This benchmark tests pg_local_cache only; it does not reproduce
KVik's [30× conference claim](https://pgconf.ru/talk/3118665).
Use SQL `mget` when reads belong to a SQL transaction. RESP workers use their
configured database role and do not inherit a caller's SQL session or snapshot;
see the [technical contract](TECHNICAL.md#optional-resp2-endpoint).

### Reproduce the SQL/RESP comparison

From this branch, on an Apple Silicon Mac with Docker and Go 1.25 or newer:

```bash
npm --prefix examples/node-postgres ci --ignore-scripts
go -C examples/go-pgx build -o /tmp/pglc-go-pgx .
GOOS=linux GOARCH=arm64 CGO_ENABLED=0 \
  go -C examples/go-pgx build -o /tmp/pglc-go-pgx-linux .
export PGLC_EXTENSION_REF="$(git rev-parse HEAD)"
export PGLC_DEMO_PORT=55433
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml \
  up -d --build --wait
docker run -d --rm --name pglc-go-client \
  --network container:pglc-demo-postgres-1 \
  --mount type=bind,src=/tmp/pglc-go-pgx-linux,dst=/tmp/pglc-go-pgx,readonly \
  pglc-demo-postgres sleep infinity

COMPARE_RESP=1 PGLC_PGX_CONTAINER=pglc-go-client GOMAXPROCS=8 \
  CONNECTIONS=64,256 BATCHES=1,64 REPEATS=3 DURATION_SECONDS=5 \
  node examples/node-postgres/client-comparison.mjs > resp-vm.json
COMPARE_RESP=1 PGLC_PGX_BIN=/tmp/pglc-go-pgx GOMAXPROCS=8 \
  CONNECTIONS=64 BATCHES=1,64 REPEATS=3 DURATION_SECONDS=5 \
  node examples/node-postgres/client-comparison.mjs > resp-host.json

docker stop pglc-go-client
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml down
```

The public token and password belong to this disposable demo; both published
ports bind to loopback. Recreate the client container after rebuilding its binary or
recreating PostgreSQL. Keep all repetitions and rerun SQL controls after a
server restart if throughput shifts. These timed comparisons do not run in CI.

## Local Mac run: 14 September 2026

Wrapping the outer `mget` array in JSON raised Node.js throughput from
**3,949 to 16,616 requests/s (4.2×)** at 64 keys and 64 connections.
It was **7% faster than prepared SQL** and used **41% less PostgreSQL
CPU per request** in this run.

| Component | Configuration |
|---|---|
| Host | MacBook Pro `Mac15,10`, Apple M3 Max, 14 cores (10 performance + 4 efficiency), 36 GiB RAM |
| OS | macOS 26.5.2, build `25F84`, arm64 |
| Docker | Engine 29.7.2; Linux `7.0.12-linuxkit`, aarch64 |
| Linux VM | 14 visible CPUs, 7.65 GiB RAM; no additional container CPU or RAM quota |
| Database | PostgreSQL 16.15, Debian bookworm; `max_connections=300`; `shared_buffers=128MB`; 256 MiB `/dev/shm` |
| Extension | Local 2.0.2 source build, revision `67e57549c98726395e299b48f6092191a8153903` |
| Cache | 1,024 entries; configured memory budget 384 MiB |
| Data | 4,096 rows, 128-byte values; PostgreSQL data and WAL on tmpfs |
| Node client | Node.js 24.18.0, node-postgres 8.16.3; one event loop |
| Go client | Go 1.27.1, pgx 5.11.0; `GOMAXPROCS=8` |
| Connection | Persistent prepared statements over `127.0.0.1:55432`, without TLS or pipelining |

The client and PostgreSQL VM share the Mac CPU. Ten other development
containers were running; this was not an isolated host. The extension uses
the recorded local source revision, not the unmodified 2.0.2 release.

[Download the measurements](../assets/benchmarks/2026-09-14-m3-max-clients.json):
all 60 driver-comparison samples, the 60 application samples, resource
snapshots, machine details, versions, exact SQL, and source revisions.
Times in the JSON are UTC; the local date is UTC+3.

### Connections and client decoding

The table shows median **requests/s** from three 10-second samples per case,
with **64 keys per request**. Each variant runs in a separate process, after
the previous variant has disconnected. Connections and prepared statements
are ready before timing. In-flight requests finish before the sample ends.

| Connections | Node SQL | Node mget text[] | Node mget JSON | Go SQL | Go mget |
|---:|---:|---:|---:|---:|---:|
| 4 | 7,023 | 4,046 | 7,528 | 6,268 | 6,767 |
| 64 | 15,577 | 3,949 | 16,616 | 14,511 | 15,528 |
| 256 | 15,459 | 3,942 | 16,574 | 14,073 | 15,973 |

`Node mget text[]` uses the previous query shape and node-postgres's text-array
parser. `Node mget JSON` wraps that same call in `array_to_json`, letting
node-postgres decode the outer array with `JSON.parse`. Each inner JSON row
is still parsed on the client. This moves some conversion work to PostgreSQL;
the resource table below includes that cost.

Go uses [pgx](https://github.com/jackc/pgx) with its binary `text[]` result
decoder and Go's `encoding/json`, returning maps in requested order. It uses
the unwrapped `mget` query. The SQL baseline returns text columns in both
languages. Differences in wire format, allocation, and JSON decoding are
part of this application-client comparison. No pgbench results are used here.
Median client CPU at 64 connections: Node / SQL: 0.87 cores, Node / mget text[]: 1.05 cores, Node / mget JSON: 0.84 cores, Go / SQL: 2.18 cores, Go / mget: 2.36 cores. Node event-loop
utilization fell from 100% with the text-array parser to
83% with JSON transport.

Four connections leave throughput below the 64-connection results. Compare
64 and 256 connections in the table before increasing a pool: more sessions
also require more backend memory. Every timed `mget` sample had exactly
`requests × batch` cache hits and zero misses, fills, or bypasses.
The recorded prepared SQL plans use `items_pkey` for both batch sizes,
with zero shared-block reads during the plan checks.

### PostgreSQL CPU, memory, and I/O

For **64 keys / 64 connections**, the table gives medians across repetitions;
throughput also includes the minimum–maximum. CPU cost is measured CPU time
divided by completed requests. It helps distinguish throughput improvements
from changes that simply consume more server CPU.

| Client / query | Requests/s, median (range) | Server CPU cores | CPU capacity % | CPU µs/request | Sampled peak MiB |
|---|---:|---:|---:|---:|---:|
| Node / SQL | 15,577 (14,960–15,848) | 4.43 | 31.6% | 286 | 205.5 |
| Node / mget text[] | 3,949 (3,942–3,959) | 0.56 | 4.0% | 141 | 209.1 |
| Node / mget JSON | 16,616 (16,572–16,696) | 2.80 | 20.0% | 170 | 215.0 |
| Go / SQL | 14,511 (14,355–14,546) | 3.90 | 27.8% | 271 | 209.8 |
| Go / mget | 15,528 (15,525–15,611) | 2.31 | 16.5% | 150 | 210.0 |

One CPU core means one CPU-second per elapsed second. Capacity percentages
divide by the VM's 14 available CPUs; 100% means all 14 CPUs, not one core.
The client shares those physical cores with the VM, so this is not a claim
that PostgreSQL has an otherwise idle, dedicated 14-core machine.

Memory is the PostgreSQL container's **cgroup `memory.current`**, sampled
every 500 ms plus endpoints. It includes anonymous memory, shared memory,
tmpfs, and page cache. The reported peak is the median of each repetition's
sampled peak, not process RSS or an exact instantaneous maximum. The JSON
also records anonymous/shared/file memory separately, CPU throttling, memory
events, network bytes, block I/O, and `pg_stat_activity` state/wait snapshots.

Largest block-I/O delta among the 60 driver samples: **0 bytes read,
0 bytes written**. There was no cgroup CPU throttling or memory-event
increment. Warm reads and tmpfs do not test persistent storage performance.
Median outbound traffic at 64 connections: Node / SQL: 170.6 MiB/s, Node / mget text[]: 41.8 MiB/s, Node / mget JSON: 176.0 MiB/s, Go / SQL: 158.3 MiB/s, Go / mget: 157.9 MiB/s.

Resource snapshots bracket the timed client loop but also include the short
IPC/reporting gap and monitoring work. CPU includes the sampler's container
processes and background PostgreSQL work; it excludes Docker's host-side
port forwarding. Client CPU is measured separately in each client process.

### One key per request

The same five variants were measured at **64 connections**, three 10-second
samples each. This tests a small response; it does not establish a single-key
throughput ceiling.

| Client / query | Requests/s, median (range) | Server CPU cores |
|---|---:|---:|
| Node / SQL | 55,409 (55,366–55,535) | 3.15 |
| Node / mget text[] | 52,198 (52,171–52,251) | 3.95 |
| Node / mget JSON | 53,646 (53,617–53,938) | 4.06 |
| Go / SQL | 48,716 (48,551–48,962) | 3.09 |
| Go / mget | 47,925 (47,333–48,010) | 3.89 |

For these one-key samples, `mget` does not improve throughput and consumes
more PostgreSQL CPU than the SQL baseline. The 64-key gain should not be
applied to single-key lookups.

### Application workloads

The main Node.js runner uses the JSON query, **64 persistent clients** and
**50,000 requests per sample**, with three repetitions. It cycles through
128 hot rows. Unlike the fixed-key driver probe, it also checks cold fill,
5% updates, and writes to attached/unattached tables.

| Workload | Keys/request | Prepared SQL requests/s | mget JSON requests/s |
|---|---:|---:|---:|
| Warm reads | 1 | 56,104 (52,246–56,364) | 52,842 (52,494–53,399) |
| Warm reads | 16 | 37,391 (36,996–37,938) | 38,425 (38,340–38,617) |
| Warm reads | 64 | 15,178 (15,109–15,488) | 16,294 (16,131–16,368) |
| 5% updates | 1 | 55,768 (54,273–56,527) | 52,621 (51,432–53,216) |
| 5% updates | 16 | 38,669 (38,429–38,718) | 37,604 (37,481–37,916) |
| 5% updates | 64 | 16,049 (16,008–16,063) | 16,814 (16,771–17,229) |

The table reports median requests/s and minimum–maximum. Per-repetition
latencies, CPU, memory and I/O are in `application_run` in the JSON.
Mixed samples count reads and writes together. Cold fill visits all 4,096
rows once; at batch 64 it has only 64 latency observations, so its p99 is
too sparse for a useful tail claim. Write timings use tmpfs with `fsync`,
`full_page_writes`, and `synchronous_commit` enabled.

## Reproduce the 14 September measurements

Use a clean checkout of the recorded harness revision from the JSON.
That historical harness uses the local Compose overlay; today's quickstart
builds the checkout directly. To reproduce the recorded harness, run from
the repository root:

```bash
export PGLC_EXTENSION_REF=67e57549c98726395e299b48f6092191a8153903
export PGLC_DEMO_MAX_CONNECTIONS=300
npm --prefix examples/node-postgres ci --ignore-scripts
go -C examples/go-pgx build -o /tmp/pglc-go-pgx .
docker compose -f examples/compose.yaml -f examples/compose.local.yaml \
  up -d --build --wait

GOMAXPROCS=8 DURATION_SECONDS=10 REPEATS=3 CONNECTIONS=4,64,256 BATCHES=64 \
  node examples/node-postgres/client-comparison.mjs > clients-64.json
GOMAXPROCS=8 DURATION_SECONDS=10 REPEATS=3 CONNECTIONS=64 BATCHES=1 \
  node examples/node-postgres/client-comparison.mjs > clients-1.json
SERVER_RESOURCES=1 CLIENTS=64 REQUESTS=50000 REPEATS=3 BATCHES=1,16,64 \
  node examples/node-postgres/benchmark.mjs > benchmark.json

python3 scripts/benchmark_report.py benchmark.json > benchmark.md
docker compose -f examples/compose.yaml -f examples/compose.local.yaml down
```

Go requires 1.25 or newer; the recorded run used 1.27.1. Driver versions are
pinned in their lock/module files. `PGLC_PGX_BIN` can override
the Go binary path. The probe checks the demo marker, row count, extension
version and connection headroom, and rejects other active database clients.
It samples the demo's cgroup v2 counters through Docker. Keep the clients on
the host, using the same loopback endpoint. Run no other benchmark at the
same time. These long comparisons do not run in CI.

For a shorter correctness run, start the [demo](QUICKSTART.md), then use:

```bash
CLIENTS=4 REQUESTS=2000 REPEATS=3 BATCHES=1,16,64 \
  npm --prefix examples/node-postgres run --silent benchmark > benchmark.json
```

Set `SERVER_RESOURCES=1` to collect PostgreSQL container resources too.
The runner resets only the disposable demo tables between application
samples and does not accept a production `DATABASE_URL`. `REQUESTS` is the
total per sample, not a per-client count. A query failure preserves completed
samples, exits nonzero, and makes the Markdown renderer reject that partial run.

## Exact read queries

Node.js uses:

```sql
SELECT array_to_json(local_cache.mget('public.items'::regclass, $1::bigint[])) AS rows;
```

Go and the previous Node text-array path use:

```sql
SELECT local_cache.mget('public.items'::regclass, $1::bigint[]) AS rows;
```

Both SQL baselines use:

```sql
SELECT id::text AS key, row_to_json(i)::text AS row
FROM public.items AS i WHERE id = ANY($1::bigint[]);
```

The baseline restores input order, duplicates, and missing positions before
returning parsed rows. Attachment does not rewrite its SELECT. If an
application needs only two columns, measure that existing projection too;
this comparison uses complete JSON rows to match the cache contract.

## What this does not establish

This is a **closed-loop** workload: each client sends its next request after
the previous response has been processed. It does not model independently
arriving traffic or correct for coordinated omission. Three short repetitions
on a shared laptop do not establish production capacity or small stable
percentage differences. Keep individual percentiles separate; do not average
them across repetitions.

The small dataset does not establish behavior for large rows, production key
skew, long transactions, network RTT, pool exhaustion, replica reads, crash
recovery, or sustained writes. Use a dedicated host for reference results.
Record CPU/memory limits, filesystem, image ID, client/server placement and
exact queries. Keep every repetition, including slower ones, and inspect
cache counters before attributing a difference to the cache.

The [earlier run](../assets/benchmarks/2026-09-14-m3-max.json) remains available
with its original query shapes and source revisions. Its client workloads
differ from this comparison; use the current tables to evaluate the JSON path.
To share a workload, open a
[workload report](https://github.com/profundium/pg_local_cache/issues/new?template=workload.yml)
with the JSON and configuration.
