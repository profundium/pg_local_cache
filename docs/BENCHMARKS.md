# Benchmarks

The repository keeps two focused harnesses:

- `benchmarks/sql_only.py`: `local_cache.mget` versus an indexed source
  query on mapped and stock PostgreSQL;
- `benchmarks/whole_row.py`: warm one-key RESP `MGET` versus Valkey and Redis, plus a
  response-width sweep.

There is no ordinary-`SELECT` cache lane because ordinary SQL is not
intercepted.

## Run checks

```bash
make benchmark-test
```

Run the container benchmark:

```bash
make benchmark
```

Results are written under `benchmark-results/`. CI runs the same harness with
fixed CPU/memory limits and uploads the raw JSON and rendered Markdown.

## SQL mget method

The SQL-only harness:

1. starts separate mapped and stock PostgreSQL servers;
2. creates identical schema, rows, login role, and indexes;
3. attaches only the mapped table;
4. proves cold miss/fill and a fully warm keyspace;
5. measures prepared and unnamed extended protocols;
6. validates returned JSON, operation counts, cache counters, and latency
   samples;
7. compares equal-width ordered batches.

`local_cache.mget` throughput counts resolved key positions, not SQL
statements. Direct and stock lanes use the indexed source query and must not
change SQL cache counters.

Minimum absolute gates default to 10,000 key operations/s for each protocol.
Relative gates are optional because hardware and container scheduling vary.

## RESP method

The whole-row harness sends identical authenticated one-key `MGET` frames and validates
every returned byte on pg_local_cache, Valkey, and Redis. It also repeats the
pg_local_cache measurement across configured row widths.

This is a warm-hit regression test, not a production capacity claim. It does
not establish write, miss, failover, network, or end-to-end application
capacity.

## Reproducibility

A releasable report records:

- exact source revision and harness SHA-256;
- image references and digests;
- PostgreSQL major and query-affecting settings;
- client/server CPU and memory limits;
- duration, repetitions, concurrency, jobs, pipeline depth, key count, and row
  width;
- raw runs, errors, counters, and latency samples;
- independent gate status.

`scripts/validate_benchmark_evidence.py` rejects missing, stale, malformed, or
failed reports before release.

The previous transparent-SQL evidence was removed with that feature. Publish
new performance numbers only from a report generated after the mget-only
change.
