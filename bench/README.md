# Two-node benchmark

Run `run_bench.py` and `write_bench.py` from a workstation with Python 3 and
OpenSSH. SSH config must provide aliases for the DB and load hosts. DB host runs
PostgreSQL 16, pg_local_cache RESP on 6380, and Valkey on 6379; load host runs
`/root/bench-client` and stores RESP token. The Go client targets DB's private
address. Scripts use Python standard library only.

Defaults: `PGLC_DB_HOST=pglc-db`, `PGLC_LOAD_HOST=pglc-load`,
`PGLC_DB_ADDR=192.168.0.4`, and `PGLC_TOKEN_FILE=/root/pglc.token` (read on load
host). `PGLC_NET_IF` selects DB NIC for read-run RX/TX; unset, script finds the
interface owning `PGLC_DB_ADDR` via `ip -o -4 addr`. Both hosts need `nproc`;
DB host also needs `mpstat`, `sar`, `ip`, and `systemctl`.

Optional `PGLC_SERVER_CPUS` accepts a CPU list such as `0-3` or `0,8`. Before
cases, scripts set runtime `AllowedCPUs` on PostgreSQL and Valkey, sample only
those CPUs, and reset both properties to empty on exit, including Ctrl-C.
Without pinning, DB cores = `mpstat` all-CPU busy percent × DB `nproc --all`.
Every JSONL row records `db_nproc` and `server_cpus`; pinned read rows also
record `requests_per_server_core`. Read rows include load-host `client_nproc`.

Record server versions/settings, kernel, memory, CPU governor, dataset size,
cache warm state, and run date with each output. Relevant settings:

- PostgreSQL: `shared_buffers`, `max_connections`, `wal_level`, `wal_sync_method`,
  `synchronous_commit`, `fsync`, `full_page_writes`, checkpoint and WAL limits.
- pg_local_cache: shared memory, worker count, RESP auth/TLS, and cache stats.
- Valkey: persistence, `maxmemory`, eviction policy, and output-buffer limits.

Run read matrix (defaults: modes `postgres-any,pg_local_cache,valkey`, batches
`1,16,64`, clients `16,64,256`, key spaces `0,100000`, 20 seconds, 3 repeats):

```sh
MODES=postgres-any,valkey python3 bench/run_bench.py read.jsonl
```

Run write overhead and mixed matrix:

```sh
python3 bench/write_bench.py writes.jsonl
```

`REPEATS`, `SECONDS_PER_RUN`, `BATCHES`, `CLIENTS`, `KEY_SPACES`,
`WRITE_SECONDS`, `MIXED_SECONDS`, `WRITE_CLIENTS`, `WRITE_STACKS`,
`MIXED_STACKS`, `MIXED_RATES`, and `MIXED_KEY_DISTS` select run sizes. Set
`RUN_WRITE_OVERHEAD=0` or `RUN_MIXED=0` to skip a write phase. Example smoke run:

```sh
REPEATS=1 WRITE_SECONDS=2 MIXED_SECONDS=3 WRITE_OPS=update WRITE_CLIENTS=8 \
  WRITE_STACKS=pglc MIXED_STACKS=pglc MIXED_RATES=none,unlimited \
  MIXED_KEY_DISTS=uniform python3 bench/write_bench.py smoke.jsonl
```

Both runners append one JSON object per case. Read rows contain throughput,
latency, client/server CPU, and network RX/TX. Write rows contain transaction
rate, latency, errors, DB CPU cores, and DB CPU microseconds per transaction;
mixed rows also contain read latency, cache hits/misses, writer rate, and stale
entry checks. Insert cases start at ID 100001 and clean up IDs above 100000.

To fill Valkey from the load host, run `python3 bench/fill_valkey.py [address]
[token_file]`. Positional values override `PGLC_DB_ADDR` and `PGLC_TOKEN_FILE`;
`KEY_SPACE` controls copied IDs (default 100000).
