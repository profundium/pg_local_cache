# pg_local_cache

pg_local_cache is a PostgreSQL extension that serves complete rows by primary key over authenticated RESP2 MGET. It keeps PostgreSQL as the source of truth and stores bounded row entries in shared memory.

Version 3.1.0 replaces fixed-width row slots with compact descriptors and on-demand 64 KiB slabs, so byte capacity follows the configured memory budget and can hold millions of small keys when row sizes fit. It partitions cache locks, adds an allocation-free single-key hit path and cheaper write tracking, defers misses blocked by relation locks, and closes stale-read races with pre-commit fences.

![Architecture: RESP readers use a bounded shared cache; SQL writers invalidate entries at commit.](docs/diagrams/architecture.svg)

## Benchmarks

<!-- BENCH:README -->

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
