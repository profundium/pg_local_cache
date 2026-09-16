---
layout: doc
title: PostgreSQL cache benchmarks
description: Measured pg_local_cache results with Node.js, Go and RESP on Apple M3 Max. Includes the machine, PostgreSQL CPU, memory and methodology.
section: Benchmarks
permalink: /docs/BENCHMARKS.html
last_modified_at: "2026-09-16"
---

# PostgreSQL cache benchmarks

Recorded on an Apple M3 Max with PostgreSQL 16. Each comparison uses the same
client, dataset and decoded row results for cached and ordinary SQL reads.

## Where the cache helped—and where it did not

- **Single-key SQL:** prepared SQL beat SQL `mget` in both published client
  setups. At 256 Go connections it returned 253,790 requests/s versus 186,296
  for `mget`. Adding a row cache did not improve this SQL workload.
- **64-key SQL batches:** Go at 256 connections returned 43,647 requests/s
  through `mget` versus 27,615 for prepared SQL, about 1.58× throughput.
  Node.js at 64 connections showed a smaller gain: 16,616 versus 15,577.
  Batch size and client overhead matter; check CPU and latency as well.
- **Single-key RESP:** Go at 256 connections reached 839,678 requests/s versus
  the 253,790 SQL baseline. RESP workers use a configured database role and
  do not share the caller's SQL transaction or snapshot.

The [Node.js measurements](benchmarks-node.md) use a macOS client and Docker
server; [Go and RESP measurements](benchmarks-go.md) put both inside the Docker
VM. Each page links raw repetitions, exact versions and server resource costs.
These separate setups do not rank languages. For connection examples, see
[Node.js](node-postgres.md), [Go](go.md) or [RESP](resp.md).

## Run the same comparison on every client

From the repository root, with Docker, Node.js 20+ and Go 1.25+:

```bash
./examples/benchmark.sh all > comparison.json
python3 scripts/benchmark_report.py comparison.json
```

The runner builds a disposable PostgreSQL server and runs this common matrix:

| Setting | Every client and read path |
|---|---|
| Clients | Node.js with node-postgres / node-redis; Go with pgx / standard-library RESP2 |
| Read paths | Prepared SQL `ANY`, SQL `mget`, RESP `MGET` |
| Keys per request | 1, 16, 64; the same fixed keys starting at 1 |
| Connections | 4, 64, 256; persistent, one outstanding request per connection |
| Samples | Three five-second repetitions per case; order rotates |
| Placement | Same separate Linux client container, sharing PostgreSQL's network namespace |
| Before timing | Connect, compare decoded rows, then warm each connection |
| Result contract | Full JSON rows, input order, duplicates, nulls, missing keys, empty and all-null input |
| Measurements | Requests/s, latency percentiles, client CPU, server CPU/memory, cache counters |

There are 162 samples by default, about 14 minutes of timed work plus setup.
The script removes its containers after success or failure. For a short
correctness run:

```bash
CONNECTIONS=4 BATCHES=1,16,64 REPEATS=1 DURATION_SECONDS=1 \
  ./examples/benchmark.sh all > smoke.json
```

Use `node` or `go` instead of `all` to select one client with the same defaults.
`CONNECTIONS`, `BATCHES`, `REPEATS`, `DURATION_SECONDS` and Go's `GOMAXPROCS`
can be overridden. Node.js uses one event-loop thread; Go defaults to eight
threads. Node's SQL mget wraps the returned array in JSON, while pgx decodes
the PostgreSQL text array. Those client costs stay inside timing.

Identical workloads do not make the protocols interchangeable: RESP workers
use their configured database role and do not join a caller's SQL transaction
or snapshot. See the [RESP contract](TECHNICAL.md#optional-resp2-endpoint).
The common comparison measures warm reads. Cold, mixed-read/write and
write-overhead diagnostics remain separate in `node-workload`.

The published 14–15 September results below predate this common launcher.
Their original environments and source revisions remain attached to the
data; they are not new measurements from the unified matrix.

## Test environment


| Component | Configuration |
|---|---|
| Host | MacBook Pro `Mac15,10`, Apple M3 Max: 10 performance + 4 efficiency cores, 36 GiB RAM |
| OS | macOS 26.5.2, build `25F84`, arm64 |
| Docker VM | Engine 29.7.2, Linux `7.0.12-linuxkit`, 14 CPUs, 7.65 GiB RAM; no container CPU or RAM quota |
| PostgreSQL | 16.15, Debian bookworm; 300 connections, 128 MiB shared buffers, 256 MiB `/dev/shm` |
| Data | 4,096 rows, 128-byte values; 1,024 cache entries; data and WAL on tmpfs |

The client and server share the Mac's CPUs with ten other development
containers. All clients encode requests and decode complete JSON rows,
preserving input order, duplicates and missing positions. SQL uses prepared
statements; connections, authentication and warmup are outside the timing.
There is no TLS or pipelining.

## Measurement method


Each connection waits for its response before sending another request:
a **closed-loop** workload, without correction for coordinated omission.
Query order rotates between repetitions; in-flight requests finish before
timing stops. Read-only comparisons use fixed keys already in the cache.
Recorded SQL plans use `items_pkey`, with zero shared-block reads.

Server CPU comes from the PostgreSQL container's cgroup counters. One core
means one CPU-second per elapsed second; capacity percentages divide by 14.
CPU µs/request divides server CPU time by completed requests. The sampling
window includes monitoring work and the short client-reporting gap.

Memory is cgroup `memory.current`, sampled every 500 ms plus endpoints.
Tables report the median of each repetition's sampled peak, including shared
memory, tmpfs and page cache; this is not process RSS. JSON files also contain
block I/O, throttling, memory events and SQL state/wait snapshots. Network
counters exclude loopback and therefore omit the VM client's traffic.

These short, warm-cache runs on a shared laptop are not production-capacity
estimates. Data and WAL use tmpfs with `fsync`, `full_page_writes` and
`synchronous_commit` enabled; disk performance is untested.

A failed run exits nonzero and preserves completed samples; the Markdown
renderer rejects partial results. For exact recorded code, use each JSON's
`harness_ref` and `extension_ref`. Reproduction commands are on the client
pages; results are written to JSON files such as `benchmark.json`.
