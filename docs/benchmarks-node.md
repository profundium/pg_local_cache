---
layout: doc
title: Node.js benchmarks
description: "Local node-postgres results on Apple M3 Max: batch reads, concurrent updates, PostgreSQL CPU and memory."
section: Benchmarks
permalink: /docs/benchmarks-node.html
last_modified_at: "2026-09-16"
---

# Node.js benchmarks

[Overview](BENCHMARKS.md) · [Node.js](benchmarks-node.md) · [Go and RESP](benchmarks-go.md)

Node.js 24.18.0 with node-postgres 8.16.3.
[Machine and measurement method](BENCHMARKS.md#test-environment).


Measured on 14 September 2026 with extension build `67e5754`, a 384 MiB cache
budget and clients on macOS through Docker's published SQL port.

At **64 keys per request**, median requests/s from three 10-second samples:

| Connections | Prepared SQL | SQL mget |
|---:|---:|---:|
| 4 | 7,023 | 7,528 |
| 64 | 15,577 | 16,616 |
| 256 | 15,459 | 16,574 |

At 64 connections, `mget` used **170 µs of server CPU per request**, versus
286 µs for SQL. Server CPU averaged 2.80 versus 4.43 cores; sampled peak
memory was 215.0 versus 205.5 MiB. Client CPU was 0.84 versus 0.87 cores.

For **one key** at 64 connections, SQL was faster: 55,409 versus 53,646
requests/s. The batch result does not apply to single-key reads.

[Raw measurements](../assets/benchmarks/2026-09-14-m3-max-clients.json)
include latency percentiles, resource samples and source revisions.

### Reads mixed with writes

The Node.js application runner uses **64 connections**, **50,000 requests
per sample** and three repetitions. Reads cycle through 128 hot rows;
5% of operations in the mixed workload update rows.

| Workload | Keys/request | Prepared SQL requests/s | mget JSON requests/s |
|---|---:|---:|---:|
| Warm reads | 1 | 56,104 (52,246–56,364) | 52,842 (52,494–53,399) |
| Warm reads | 16 | 37,391 (36,996–37,938) | 38,425 (38,340–38,617) |
| Warm reads | 64 | 15,178 (15,109–15,488) | 16,294 (16,131–16,368) |
| 5% updates | 1 | 55,768 (54,273–56,527) | 52,621 (51,432–53,216) |
| 5% updates | 16 | 38,669 (38,429–38,718) | 37,604 (37,481–37,916) |
| 5% updates | 64 | 16,049 (16,008–16,063) | 16,814 (16,771–17,229) |

Values are median requests/s with minimum–maximum in parentheses. Mixed
samples count reads and writes together. The JSON's `application_run` includes
cold-fill and write-overhead cases. Cold fill at batch 64 has only 64 latency
observations, too few for a useful p99 estimate.

## Query setup

```sql
SELECT array_to_json(local_cache.mget('public.items'::regclass, $1::bigint[])) AS rows;
```

Connections and named prepared statements are reused. JSON decoding and
restoring input positions are included in request time. See the
[Node.js example](node-postgres.md).

## Reproduce

<details markdown="1">
<summary>Run the Node.js benchmarks</summary>

From the repository root, with Docker and Node.js 20+:

```bash
./examples/benchmark.sh node > node.json
```

Current defaults: 4/64/256 connections, 1/16/64 keys, three five-second samples
per case. Node.js now runs all three paths: prepared SQL, SQL `mget`, and RESP
`MGET`, inside the Docker VM. The script creates a disposable server and
separate client container, records resources, then removes both.
Optional overrides: `CONNECTIONS`, `BATCHES`, `REPEATS`, `DURATION_SECONDS`.
Use `all` to include Go in the [same matrix](BENCHMARKS.md#run-the-same-comparison-on-every-client).
For the recorded host-based setup, use the revisions in the measurements JSON.

For reads mixed with writes:

```bash
BATCHES=1,16,64 ./examples/benchmark.sh node-workload > benchmark.json
python3 scripts/benchmark_report.py benchmark.json
```

Defaults: 64 connections, 50,000 requests per sample and three repetitions.
The runner resets its demo tables between samples. `CLIENTS`, `REQUESTS`,
`BATCHES` and `REPEATS` are optional overrides.

</details>
