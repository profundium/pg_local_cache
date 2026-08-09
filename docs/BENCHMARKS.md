---
layout: doc
title: PostgreSQL cache benchmarks
description: Reproducible, source-pinned measurements of ordinary PostgreSQL SELECT, SQL GET/MGET, and RESP GET, including both gains and observed trade-offs.
section: Benchmarks
permalink: /docs/BENCHMARKS.html
---

# pg_local_cache benchmarks

The published tables cover all read interfaces exercised by the current CI:

- SQL `local_cache.get()` and `local_cache.mget()` through the PostgreSQL
  protocol;
- ordinary exact-primary-key `SELECT` through the PostgreSQL protocol;
- ordinary single-column primary-key `IN`/`ANY` through the PostgreSQL
  protocol;
- whole-row RESP2 `GET` against the same PostgreSQL-backed rows.

The suites measure different operations and are reported separately.
Write, rollback, DDL, and invalidation semantics are verified by integration
tests instead of being inferred from read throughput.

A published number is meaningful only with its operation definition. In
particular, 32-key `key ops/s`, SQL `statements/s`, and RESP requests/s are not
interchangeable. Ratios below compare only the same command shape, protocol,
working set, and run.

## Evidence snapshots

Two source-pinned snapshots are kept because they exercise different public
interfaces:

| Snapshot | Source and CI | Raw evidence | Scope |
|---|---|---|---|
| Ordinary SQL, 32-key `IN`, RESP GET | [`71b0aa3`](https://github.com/profundium/pg_local_cache/commit/71b0aa3a27c5c009b7ba08bbaa660147f078bde8), [run 31172234073](https://github.com/profundium/pg_local_cache/actions/runs/31172234073) | [`whole-row.json`](../assets/benchmark-evidence/71b0aa3/whole-row.json), [`whole-row.txt`](../assets/benchmark-evidence/71b0aa3/whole-row.txt) | Latest transparent SQL and RESP comparison; schema version 3. |
| SQL GET/MGET and scalar latency | [`fe2d23c`](https://github.com/profundium/pg_local_cache/commit/fe2d23c87ddc7e523ada2951376ebcb7d8570fb1), [run 30803546805](https://github.com/profundium/pg_local_cache/actions/runs/30803546805) | [`sql-only-benchmark-smoke.zip`](../assets/benchmark-evidence/fe2d23c/sql-only-benchmark-smoke.zip), [`comparison-smoke.zip`](../assets/benchmark-evidence/fe2d23c/comparison-smoke.zip) | Explicit JSON GET/MGET plus the earlier ordinary-SQL/RESP snapshot. |

### Latest ordinary SQL, `IN`/`ANY`, and RESP smoke (`71b0aa3`)

The latest green comparison used PostgreSQL 16.14, four clients, pipeline depth
eight, 128 deterministic rows per attached table, 256 cache entries, two CPU
cores per server target, and one timed second after explicit full-working-set
stabilization.

| Ordinary SQL lane | Mapped PostgreSQL | Stock PostgreSQL | Mapped/stock |
|---|---:|---:|---:|
| `SELECT *` by complete composite PK | 123,707 statements/s | 65,867 statements/s | 1.88x |
| Reordered direct-column projection | 118,679 statements/s | 63,952 statements/s | 1.86x |
| Reordered composite-PK predicates | 126,550 statements/s | 68,746 statements/s | 1.84x |

| Ordinary 32-key `SELECT IN` | Mapped PostgreSQL | Stock PostgreSQL | Mapped/stock |
|---|---:|---:|---:|
| Key throughput | 865,201 key ops/s | 328,282 key ops/s | 2.64x |
| Statement throughput | 27,038 statements/s | 10,259 statements/s | 2.64x |

| Warm RESP2 target | Ops/s | p99 | Relative to Redis |
|---|---:|---:|---:|
| pg_local_cache | 143,104 | 0.415 ms | 0.72x |
| Valkey 9.1.1 | 194,910 | 0.266 ms | 0.99x |
| Redis 8.8.1 | 197,522 | 0.269 ms | 1.00x |

The SQL lanes show the benefit of avoiding repeated PostgreSQL index and tuple
work for this warm primary-key profile. The RESP lane shows the opposite side
of the trade-off: dedicated Valkey and Redis were 1.36–1.38x faster at raw warm
`GET`. pg_local_cache is therefore positioned around transaction-aware source
integration and ordinary SQL compatibility, not maximum standalone cache
throughput.

The smoke had one short repetition, so it is a regression and correctness
signal. This is not a production capacity claim. Every timed mapped SQL key was
matched to one cache-hit counter increment with zero misses, fills, bypasses, or
failed batches.

## SQL GET/MGET results (separate snapshot)

This preserved profile measures only single-column `bigint[]` SQL MGET. It is
not evidence for composite SQL MGET or RESP MGET.

The strict throughput profile used 16 connections and a 32-key array. These
are the complete SQL commands after pgbench created the 32 key variables; both
protocol lanes used the same SQL text:

#### Stock PostgreSQL and mapped cache-off

```sql
SELECT pg_catalog.array_agg(
    pg_catalog.row_to_json(pglc_source)::text
    ORDER BY pglc_input.ordinality
)
FROM pg_catalog.unnest(ARRAY[:key_0, :key_1, :key_2, :key_3, :key_4, :key_5, :key_6, :key_7,
    :key_8, :key_9, :key_10, :key_11, :key_12, :key_13, :key_14, :key_15,
    :key_16, :key_17, :key_18, :key_19, :key_20, :key_21, :key_22, :key_23,
    :key_24, :key_25, :key_26, :key_27, :key_28, :key_29, :key_30, :key_31]::bigint[])
    WITH ORDINALITY AS pglc_input(id, ordinality)
LEFT JOIN "pglc_sql_bench_e407c3350a"."rows" AS pglc_source USING (id);
```

#### Mapped cache-on

```sql
SELECT local_cache.mget(
    'pglc_sql_bench_e407c3350a.rows'::regclass,
    ARRAY[:key_0, :key_1, :key_2, :key_3, :key_4, :key_5, :key_6, :key_7,
        :key_8, :key_9, :key_10, :key_11, :key_12, :key_13, :key_14, :key_15,
        :key_16, :key_17, :key_18, :key_19, :key_20, :key_21, :key_22, :key_23,
        :key_24, :key_25, :key_26, :key_27, :key_28, :key_29, :key_30, :key_31]::bigint[]
);
```

Latency was measured in a different scalar-key pass. These are its complete
commands; the p99 values below must not be attributed to the 32-key MGET call:

| Lane | Exact scalar latency command |
|---|---|
| Stock PostgreSQL and mapped cache-off | `SELECT pg_catalog.row_to_json(pglc_source)::text FROM "pglc_sql_bench_e407c3350a"."rows" AS pglc_source WHERE id = :key;` |
| Mapped cache-on | `SELECT local_cache.get('pglc_sql_bench_e407c3350a.rows'::regclass, (:key)::bigint);` |

| Protocol | Mode | c16/k32 key ops/s | vs stock | c16/k1 p50 | p95 | p99 |
|---|---|---:|---:|---:|---:|---:|
| Prepared | Stock PostgreSQL | 6,306 | 1.00x | 0.583 ms | 1.969 ms | 3.338 ms |
| Prepared | Mapped, cache off | 6,280 | 1.00x | 0.591 ms | 1.973 ms | 3.299 ms |
| Prepared | `local_cache.mget`, cache on | 64,954 | 10.30x | 0.815 ms | 1.940 ms | 3.505 ms |
| Unnamed extended | Stock PostgreSQL | 6,365 | 1.00x | 1.032 ms | 2.506 ms | 4.093 ms |
| Unnamed extended | Mapped, cache off | 6,257 | 0.98x | 1.043 ms | 2.411 ms | 3.620 ms |
| Unnamed extended | `local_cache.mget`, cache on | 66,156 | 10.39x | 1.110 ms | 2.452 ms | 3.847 ms |

Throughput is resolved key positions per second (`batch TPS × 32`), not SQL
statements per second. Both cached lanes passed the 10,000 key ops/s floor and
the `1.50x` cache/stock and cache/mapped-off gates. The scalar latency pass was
closed-loop, retained its raw samples, and had no configured p99 limit; its
status is `MEASURED`, not a latency pass/fail claim.

The runner exposed four logical AMD EPYC 9V74 CPUs and a 1 GiB client memory
limit. PostgreSQL 16.14 used 4,096 incompressible 3,000-byte rows, two seconds
of warmup, four pgbench jobs, and three rotated five-second repetitions. Every
timed cached key produced exactly one hit with zero misses, fills, or bypasses.

## Ordinary SQL and RESP results (latest snapshot)

### Ordinary SQL

Both PostgreSQL targets used the complete prepared command template shown
below; pgbench replaced `:key` with each measured key. Only the mapped server
loaded `pg_local_cache`; the stock server did not install or preload the
extension.

| Command template | Mapped cache ops/s | Stock PostgreSQL ops/s | Mapped/stock |
|---|---:|---:|---:|
| `SELECT * FROM public.pg_local_cache_whole_row_comparison WHERE tenant_id = 7 AND id = :key;` | 123,707 | 65,867 | 1.88x |
| `SELECT metadata, payload, enabled, amount, note, id, tenant_id FROM public.pg_local_cache_whole_row_comparison WHERE tenant_id = 7 AND id = :key;` | 118,679 | 63,952 | 1.86x |
| `SELECT payload, metadata, id, tenant_id FROM public.pg_local_cache_whole_row_comparison WHERE id = :key AND tenant_id = 7;` | 126,550 | 68,746 | 1.84x |

The mapped working set was filled and stabilized before measurement. Each
timed mapped operation produced one exact cache hit; misses, fills, bypasses,
and failed batches remained zero. CI gates the 10,000 mapped ops/s floor,
result integrity, and counter accounting. The mapped/stock ratios are displayed
for context and do not decide the gate.


### Ordinary SQL `IN` / `ANY`

New comparison runs add a separate single-column-primary-key table and send the
same prepared SQL text to mapped and stock PostgreSQL. The default report width
is 32 distinct keys per statement:

```sql
SELECT *
FROM public.pg_local_cache_whole_row_select_in_comparison
WHERE id IN ((:key_0)::bigint, (:key_1)::bigint, (:key_2)::bigint, (:key_3)::bigint, (:key_4)::bigint, (:key_5)::bigint, (:key_6)::bigint, (:key_7)::bigint, (:key_8)::bigint, (:key_9)::bigint, (:key_10)::bigint, (:key_11)::bigint, (:key_12)::bigint, (:key_13)::bigint, (:key_14)::bigint, (:key_15)::bigint, (:key_16)::bigint, (:key_17)::bigint, (:key_18)::bigint, (:key_19)::bigint, (:key_20)::bigint, (:key_21)::bigint, (:key_22)::bigint, (:key_23)::bigint, (:key_24)::bigint, (:key_25)::bigint, (:key_26)::bigint, (:key_27)::bigint, (:key_28)::bigint, (:key_29)::bigint, (:key_30)::bigint, (:key_31)::bigint);
```

The pgbench script chooses a contiguous unique key window for every statement,
so the number of returned keys is known exactly. The latest result was:

| Keys / statement | Mapped key ops/s | Stock key ops/s | Mapped statements/s | Stock statements/s | Mapped/stock |
|---:|---:|---:|---:|---:|---:|
| 32 | 865,201 | 328,282 | 27,038 | 10,259 | 2.64x |

`key ops/s` is `batch TPS × pipeline depth × keys per statement`; the report
also publishes SQL statements/s. Before timing, a complete-keyspace pass must
show exactly one hit per row and zero misses, fills, and bypasses. During the
mapped timing window, successful returned keys must equal the `sql_cache_hits`
delta exactly; any miss, fill, bypass, failed batch, or row-set mismatch fails
the lane.

This lane intentionally measures the all-hit transparent path. Correctness tests
separately prove that a mixed hit/miss array falls back as one PostgreSQL
statement and never combines partial cached rows with source rows. The width and
independent key-throughput floor are configured with
`PGLC_BENCH_ROW_SQL_IN_KEYS` and `PGLC_BENCH_ROW_SQL_IN_MIN_OPS`. The
configured width is capped at the transparent executor limit of 1,024 keys.

### RESP2 GET

The same RESP2 command bytes and expected row bytes were used for all three
targets. The command below is the concrete key for row `id=1` in the measured
key stream; subsequent operations changed only the `id` value.

| Target | Command | Ops/s | p50 | p95 | p99 | Errors |
|---|---|---:|---:|---:|---:|---:|
| pg_local_cache | `GET CRUD:benchmark.public.pg_local_cache_whole_row_comparison:{"id":1,"tenant_id":7}` | 143,104 | 0.176 ms | 0.325 ms | 0.415 ms | 0 |
| Valkey 9.1.1 | `GET CRUD:benchmark.public.pg_local_cache_whole_row_comparison:{"id":1,"tenant_id":7}` | 194,910 | 0.137 ms | 0.205 ms | 0.266 ms | 0 |
| Redis 8.8.1 | `GET CRUD:benchmark.public.pg_local_cache_whole_row_comparison:{"id":1,"tenant_id":7}` | 197,522 | 0.134 ms | 0.200 ms | 0.269 ms | 0 |

Valkey and Redis persistence was disabled because this lane measures warm cache
reads, not durable writes. Every response was decoded and compared byte-for-byte
with PostgreSQL's row JSON. Timed pg_local_cache operations produced zero cache
misses and zero source-table reads.

## Ordinary SQL and RESP workload

The raw report records the runner and container identities in addition to these
effective settings:

| Setting | Value |
|---|---|
| PostgreSQL | 16.14 on both targets |
| Runner CPU | AMD EPYC 7763; 4 logical CPUs visible |
| CPU quotas | 2 client CPUs; 2 CPUs per server target |
| Memory limits | 3 GiB client; 1 GiB per server target |
| Clients | 4 |
| Pipeline depth | 8 statements per batch |
| Ordinary `IN` width | 32 unique keys per statement in new schema-v3 runs |
| Keys per attached table / cache entries | 128 / 256 |
| Row text payload | 128 bytes |
| Timed repetitions | one 1-second smoke repetition |
| Timed warmup | 0 seconds after explicit full-working-set stabilization |

This short shared-runner run is useful as a correctness and regression smoke.
One repetition is not a capacity study, and the displayed relative ratios must
not be generalized to different rows, concurrency, hardware, or storage.

It does not measure write throughput, commit latency under invalidation load,
cold-miss latency, standby behavior, failover, application-to-database network
latency, or unsupported query shapes. Those require separate workload-specific
experiments.

## SQL GET/MGET methodology

`benchmarks/sql_only.py`, launched by `tests/docker_sql_only_smoke.sh`, creates
two PostgreSQL 16 servers:

- a stock server without the extension or preload;
- a mapped server with `pg_local_cache.port=0`, so no RESP listener, worker,
  client buffers, or token exists.

The mapped server is measured twice: once with `sql_cache=off` using the stock
batch command, and once with `sql_cache=on` using `local_cache.mget()`. All
three modes use the same schema, deterministic rows, LOGIN NOSUPERUSER role,
key stream, connections, jobs, duration, and PostgreSQL protocol. The SQL text
differs only because stock PostgreSQL has no `mget()` function.

Before timing, the harness byte-compares the first, middle, and last scalar
rows across stock, mapped cache-off, and cache-on. It proves one cold miss and
fill followed by a hit, then fills the complete 4,096-key working set. During
every cached timing window, successful key reads must equal the hit-counter
delta exactly while misses, fills, and bypasses remain zero. Direct runs must
leave all SQL-cache counters unchanged.

Prepared mode reuses a server-side prepared statement. Unnamed extended mode
sends Parse/Bind/Execute for every batch. The two protocols have independent
gates and are never averaged together.

## Ordinary SQL methodology

`benchmarks/whole_row.py` creates the same composite-primary-key table and
deterministic rows on two PostgreSQL 16 servers. It uses the same SQL template
and key on both targets with pgbench's prepared extended protocol. Each client
batch pipelines eight executions, so
`operations_per_second` counts completed row lookups rather than batches.

Before timing, the runner:

1. verifies source row counts and key ranges on both servers;
2. compares a result sample from mapped and stock PostgreSQL;
3. fills the mapped cache and repeats a full-keyspace pass until it observes no
   miss or database read;
4. resets counters immediately before each measured lane.

During each mapped scalar SQL lane, successful operations must equal the
`sql_cache_hits` delta exactly. Any miss, fill, safety bypass, failed batch, or
result mismatch fails the run instead of producing a publishable rate.

The `IN`/`ANY` lane uses a separate table with a single `bigint` primary key.
Mapped and stock servers receive identical 32-key `IN` statements through the
prepared protocol. The runner validates a sample row set on both servers, warms
the complete mapped keyspace until it observes an exact all-hit pass, excludes
warmup from measured counters, and records both key ops/s and statements/s.
Its independent default floor is 10,000 mapped key ops/s.

## RESP methodology

The whole-row RESP comparison loads PostgreSQL's exact `row_to_json` bytes into
Valkey and Redis. All three targets then receive the same client implementation,
key order, connection count, pipeline depth, CPU quota, Docker network, and
reply validation. Target order rotates when more than one repetition is used.

Connection and authentication setup happen before the timed interval. Latency
starts when a pipeline is sent and ends when every reply in that pipeline has
been decoded, so the percentiles include queueing behind earlier commands in
the pipeline. They are client-observed end-to-end values, not server execution
times.

## Run the current benchmarks

Run the SQL GET/MGET profile used for the published table with:

```bash
PGLC_SQL_ONLY_BENCH_DURATION=5 \
PGLC_SQL_ONLY_BENCH_WARMUP_SECONDS=2 \
PGLC_SQL_ONLY_BENCH_LATENCY_DURATION=5 \
PGLC_SQL_ONLY_BENCH_LATENCY_SAMPLE_RATE=0.10 \
PGLC_SQL_ONLY_BENCH_LATENCY_MIN_SAMPLES=2000 \
PGLC_SQL_ONLY_BENCH_REPETITIONS=3 \
PGLC_SQL_ONLY_BENCH_CONCURRENCY=16 \
PGLC_SQL_ONLY_BENCH_PIPELINE=32 \
PGLC_SQL_ONLY_BENCH_KEYS=4096 \
PGLC_SQL_ONLY_BENCH_PAYLOAD_BYTES=3000 \
PGLC_SQL_ONLY_BENCH_PREPARED_MIN_OPS=10000 \
PGLC_SQL_ONLY_BENCH_EXTENDED_MIN_OPS=10000 \
PGLC_SQL_ONLY_BENCH_MIN_CACHED_TO_DIRECT_RATIO=1.50 \
PGLC_SQL_ONLY_BENCH_MIN_CACHED_TO_STOCK_RATIO=1.50 \
PGLC_SQL_ONLY_BENCH_OUTPUT_DIR="$PWD/benchmark-results/sql-only" \
bash tests/docker_sql_only_smoke.sh
```

It writes `sql-only.json` with every repetition, gate, counter delta, and raw
latency sample plus the rendered `sql-only.md`.

Run ordinary SQL and RESP2 GET with:

```bash
bash benchmarks/run.sh
```

To reproduce the short CI profile exactly:

```bash
PGLC_BENCH_DURATION=1 \
PGLC_BENCH_WARMUP_SECONDS=0 \
PGLC_BENCH_REPETITIONS=1 \
PGLC_BENCH_CONCURRENCY=4 \
PGLC_BENCH_PIPELINE=8 \
PGLC_BENCH_KEYS=128 \
PGLC_BENCH_CACHE_ENTRIES=256 \
PGLC_BENCH_PG_LOCAL_CACHE_WORKERS=1 \
PGLC_BENCH_SERVER_CPUS=2 \
PGLC_BENCH_CLIENT_CPUS=2 \
PGLC_BENCH_SERVER_MEMORY=1g \
PGLC_BENCH_ROW_RESP_MIN_OPS=10000 \
PGLC_BENCH_ROW_SQL_MIN_OPS=10000 \
PGLC_BENCH_ROW_SQL_IN_KEYS=32 \
PGLC_BENCH_ROW_SQL_IN_MIN_OPS=10000 \
PGLC_BENCH_ROW_WIDTH_MIN_OPS=0 \
PGLC_BENCH_OUTPUT_DIR="$PWD/benchmark-results/comparison" \
bash benchmarks/run.sh
```

The comparison output directory receives:

- `whole-row.json`: schema version 3, source revision, exact scalar and
  32-key `IN` commands, environment, image identities, configuration, every
  repetition, counter deltas, and gates;
- `whole-row.md`: a rendered summary of the same run;
- a structured failure report if the runner stops before satisfying its
  correctness contract.

Use a clean Git commit: the runner records `-dirty` beside the source revision
when local changes are present.

## Publishing results

For a result intended as more than a CI smoke:

1. Pin PostgreSQL, Valkey, Redis, and client images by digest.
2. Place client and servers on separate physical CPU sets; do not rely only on
   container quotas.
3. Record CPU model, governor, memory, kernel, container runtime, storage, and
   swap policy.
4. Keep schema, payload bytes, key distribution, connection count, protocol,
   and command text identical within each comparison.
5. Use a meaningful warmup and at least three long repetitions; publish every
   repetition and its coefficient of variation.
6. Report throughput together with p50, p95, p99, sample count, and the latency
   semantics.
7. Retain raw reports, failures, counter deltas, harness checksums, and image
   identities under an immutable source revision.
8. Repeat the run on the intended HA, storage, connection-pool, row-width, and
   CPU profile before setting a service objective.

GitHub-hosted runners are useful for correctness and regression detection, but
variable CPU scheduling makes them unsuitable as the sole source of a capacity
claim.

For implementation limits that affect interpretation, see the
[technical reference]({{ '/docs/TECHNICAL.html' | relative_url }}) and the exact
[scenario definitions](https://github.com/profundium/pg_local_cache/blob/master/benchmarks/SCENARIOS.md).
