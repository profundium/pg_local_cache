# Benchmark scenarios

`make benchmark` runs:

- SQL `mget` against direct and stock PostgreSQL baselines;
- one-key RESP `MGET` against Valkey and Redis;
- RESP response-width regression.

Use `make benchmark-test` before changing a harness. Environment variables
and defaults are defined next to their validation in `sql_only.py`,
`whole_row.py`, and `compose.yaml`.

Results go to `benchmark-results/`; release validation requires the exact
source revision, harness hash, image digests, resource limits, raw runs, and
passing gates.
