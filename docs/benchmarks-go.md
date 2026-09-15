---
layout: doc
title: "Go benchmarks: SQL and RESP"
description: Local pgx and RESP2 results on Apple M3 Max, with PostgreSQL CPU, memory and connection scaling.
section: Benchmarks
permalink: /docs/benchmarks-go.html
last_modified_at: "2026-09-16"
---

# Go benchmarks: SQL and RESP

[Overview](BENCHMARKS.md) · [Node.js](benchmarks-node.md) · [Go and RESP](benchmarks-go.md)

Go 1.27.1 with pgx 5.11.0 and a standard-library RESP2 client; `GOMAXPROCS=8`.
[Machine and measurement method](BENCHMARKS.md#test-environment).


Measured on 15 September 2026 with extension build `f03ed22`.
The Go client runs inside the Linux VM, in a separate container sharing
PostgreSQL's network namespace. Client CPU and memory are excluded from
server resource counters. RESP uses eight workers, a 1 GiB cache/buffer
budget and a 512-client limit.

Median **requests/s**, three five-second samples per case:

| Keys/request | Connections | Prepared SQL | SQL mget | RESP MGET |
|---:|---:|---:|---:|---:|
| 1 | 64 | 277,088 | 211,251 | 722,133 |
| 1 | 256 | 253,790 | 186,296 | 839,678 |
| 64 | 64 | 26,459 | 38,122 | 38,877 |
| 64 | 256 | 27,615 | 43,647 | 52,774 |

64-key SQL varied from 19–28k requests/s across server restarts despite
identical data, settings and index plans. The table uses the faster repeated
run; the cause of the variation remains unresolved.
[All 129 samples](../assets/benchmarks/2026-09-15-m3-max-resp.json) include
both runs, exact source revisions, binary hashes and query plans.

Read-only cache samples had 100% hits and no errors or connection-limit
rejections. The harness checks SQL and RESP connection headroom. A separate
256-connection, 64-key probe with 12 Go threads did not improve RESP;
SQL `mget` gained 6% over eight threads.

### Server resources

At **256 connections**, medians across the same samples:

| Keys/request | Path | Client CPU cores | Server CPU cores (% of VM) | Server µs/request | Sampled peak MiB |
|---:|---|---:|---:|---:|---:|
| 1 | SQL | 3.97 | 8.94 (63.9%) | 36.3 | 679.8 |
| 1 | SQL mget | 3.36 | 9.93 (70.9%) | 54.4 | 684.2 |
| 1 | RESP MGET | 5.98 | 6.11 (43.7%) | 7.8 | 263.7 |
| 64 | SQL | 3.72 | 9.45 (67.5%) | 348.3 | 689.8 |
| 64 | SQL mget | 5.12 | 4.99 (35.6%) | 116.0 | 707.7 |
| 64 | RESP MGET | 5.76 | 4.97 (35.5%) | 95.8 | 266.7 |

### Go client on macOS

Through Docker's published ports, at **64 connections**; medians of three
five-second samples, in requests/s:

| Keys/request, 64 connections | Prepared SQL | SQL mget | RESP MGET |
|---:|---:|---:|---:|
| 1 | 49,194 | 48,010 | 52,426 |
| 64 | 14,324 | 15,751 | 16,240 |

The VM and host cases differ in both client OS and network route. The
host-port results therefore cannot isolate PostgreSQL's throughput limit.
RESP also has a different session contract: workers use a configured database
role and do not inherit a caller's SQL transaction or snapshot. See the
[RESP reference](TECHNICAL.md#optional-resp2-endpoint).

## Reproduce

<details markdown="1">
<summary>Run the Go benchmark</summary>

From the repository root, with Docker, Node.js 20+ and Go 1.25+:

```bash
./examples/benchmark.sh go > go.json
```

The script builds a disposable PostgreSQL server and the Go client, runs each
case three times, records server resources, then removes its containers.
The client runs in the Docker VM, in a separate cgroup from PostgreSQL.
Current defaults: 4/64/256 connections, 1/16/64 keys and five seconds per sample.
Use `all` to run Node.js and Go against the same server with the
[common matrix](BENCHMARKS.md#run-the-same-comparison-on-every-client).
These defaults were unified after the historical measurements above;
use the recorded JSON revisions to reproduce the original harness.

Optional overrides: `CONNECTIONS`, `BATCHES`, `REPEATS`, `DURATION_SECONDS`
and `GOMAXPROCS`. The JSON records the running extension's build ID.

</details>
