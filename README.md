# pg_local_cache

pg_local_cache is a PostgreSQL extension that serves complete rows by primary key over authenticated RESP2 MGET. It keeps PostgreSQL as the source of truth and stores bounded row entries in shared memory.

Version 3.1.0 replaces fixed-width row slots with compact descriptors and on-demand 64 KiB slabs, so byte capacity follows the configured memory budget and can hold millions of small keys when row sizes fit. It partitions cache locks, adds an allocation-free single-key hit path and cheaper write tracking, defers misses blocked by relation locks, and closes stale-read races with pre-commit fences.

![Architecture: RESP readers use a bounded shared cache; SQL writers invalidate entries at commit.](docs/diagrams/architecture.svg)

## Benchmarks

All figures below use final 3.1.0 build c431bcc.

- **Reads per pinned vCPU pair:** 1 pair: 210k requests/s for pg_local_cache vs 180k for best Valkey (io-threads=1); 2 pairs: 337k vs 266k (io-threads=4); 4 pairs: 361k vs 357k (io-threads=8), with 5.24 vs 7.95 server vCPU. Valkey io-threads=1 used the least CPU per request; prepared SQL was far behind in pinned single-key tests.
- **Wide MGET:** With all 16 vCPUs available, observed median MGET 16/64 throughput differences with Valkey io-threads=8 ranged from about −2.15% to +1.26%. Observed ranges did not overlap in any of the four 64-key comparisons, so these samples do not establish statistical equivalence; the runs were client/network-bound. With io-threads=1, random-key MGET 16 at 256 clients measured 116,130 vs 97,489 req/s (19.1% higher for pg_local_cache). pg_local_cache had higher p99 for random-key MGET 64 at 256 clients (43.52 vs 40.37 ms).
- **Writes:** pg_local_cache overhead was 3.6–8.2% for UPDATE and 3.7–5.5% for INSERT. UPDATE plus Valkey DEL was 28.9–63.7% below plain-table UPDATE throughput.
- **Stale probes:** pg_local_cache had 0 stale entries; Valkey cache-aside retained 2 Uniform and 1 Zipf stale entries after 120 seconds. The pg_local_cache Zipf reader hit a harness startup-equivalence failure, but its stale check still ran and found 0.
- **One-hour soak:** 252M MGET plus 72M UPDATE; worker RssAnon stayed flat, with maximum growth of 44 kB. [Full results, raw data, and method](docs/BENCHMARKS.md).
## Quickstart

```sh
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml up --build --wait
export REDISCLI_AUTH=DemoRespToken_0123456789abcdef0123456789
redis-cli -2 -p 56379 MGET 'CRUD:pglc_demo.public.items:{"id":42}'
```

Stop the demo with `docker compose -f examples/compose.yaml -f examples/compose.resp.yaml down`.

## Install

Install the 3.1.0 package for your PostgreSQL major version, or build from source: [installation guide](docs/INSTALL_EXISTING.md). From 3.0.0, replace the library and restart PostgreSQL, then run `ALTER EXTENSION pg_local_cache UPDATE;` in each database; the 3.1.0 SQL migration is a no-op. See [upgrade guide](docs/UPGRADING.md#upgrade-from-300).

## Fit

| Fits | Does not fit |
|---|---|
| Repeated reads of complete rows by primary key | Arbitrary SQL result caching |
| Bounded local cache with PostgreSQL as source of truth | Cross-database or distributed cache |
| RESP clients that can use MGET | SQL transaction reads through the RESP endpoint |

## Documentation

- [Try locally](docs/QUICKSTART.md)
- [RESP clients](docs/resp.md)
- [Benchmarks](docs/BENCHMARKS.md)
- [Technical reference](docs/TECHNICAL.md)
- [Upgrading to 3.1.0](docs/UPGRADING.md)
- [PostgreSQL caching guide](docs/postgresql-caching.md)
- [PostgreSQL and Redis](docs/postgresql-redis-cache.md)
- [Batch primary-key lookups](docs/batch-primary-key-lookups.md)
- [Row cache vs shared_buffers](docs/row-cache-vs-shared-buffers.md)
- [Cache invalidation](docs/cache-invalidation.md)

Using 2.x? Documentation for 2.0.4: https://github.com/profundium/pg_local_cache/tree/v2.0.4/docs

## License

MIT. See [LICENSE](LICENSE).
