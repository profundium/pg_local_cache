# pg_local_cache

`pg_local_cache` is a PostgreSQL 14–18 extension for repeated primary-key reads.
It keeps hot whole rows in bounded PostgreSQL shared memory and can transparently
accelerate supported ordinary `SELECT` statements after a table is attached.

PostgreSQL remains the source of truth. A cache miss, unsupported query shape,
unsafe transaction state, malformed entry, or oversized row runs the original
primary-key index plan. Source writes publish transaction-aware invalidation
fences before commit visibility, and rollback never exposes uncommitted data.

Applications keep their existing PostgreSQL driver or ORM. The transparent SQL
path needs no separate cache process, token, cache client, or proprietary query
syntax. Installation is not zero-touch: the extension uses
`shared_preload_libraries`, allocates a bounded cache at postmaster startup, and
requires one controlled restart for first activation.

## Product boundary

| Good fit | Not the product |
|---|---|
| Repeated exact-primary-key reads whose hot working set fits the configured cache. | A general query or result cache for joins, ranges, aggregates, arbitrary predicates, or full-table scans. |
| Applications that should keep ordinary PostgreSQL row types, ACLs, drivers, and ORM queries. | A universal Redis/Valkey replacement, distributed cache, pub/sub system, TTL store, or multi-primary coordination layer. |
| A single writable primary where transaction-aware invalidation is more valuable than maximum standalone cache throughput. | A way to avoid PostgreSQL operations: preload, restart planning, memory sizing, monitoring, and table attachment are still required. |

Unsupported or unsafe reads fall back to PostgreSQL rather than returning a
partial cached answer. Current packaged builds target PostgreSQL 14–18 on Linux
amd64 (glibc or musl), one configured database, and one writable primary.

## Capabilities

| Capability | Behavior |
|---|---|
| Transparent SQL fast path | Supported exact-primary-key and bounded single-column `IN`/`ANY` reads preserve ordinary row, projection, and ACL semantics. |
| Explicit JSON API | `local_cache.get()` and `local_cache.mget()` provide whole-row JSON for callers that want a cache-shaped API. |
| Source-plan fallback | Unsupported, unsafe, missing, malformed, or oversized entries execute PostgreSQL's retained source plan. |
| Whole rows | Each entry stores one versioned PostgreSQL composite row. |
| Transactional invalidation | `INSERT`, `UPDATE`, `DELETE`, and `TRUNCATE` fence affected entries before commit visibility. |
| Bounded extension memory | Entry capacity, client slots, and deterministic extension allocations are fixed at startup. |
| Optional RESP2 | Trusted internal clients can use authenticated whole-row `GET`, bounded `MGET`, `SET`, and `DEL`. |
| Operations | SQL metrics, health checks, Prometheus rules, and a Grafana dashboard are included. |

## Choose an install path

| Goal | Start here |
|---|---|
| Try the extension locally | [Local demo](#local-demo): one isolated SQL-only PostgreSQL service with seeded data. |
| Install on a PostgreSQL host | [Verified binary release](#verified-binary-release), then follow the production [staging, restart and verification guide](docs/INSTALL_EXISTING.md). |
| Build through PGXN or local PGXS | [PGXN/source packaging](PGXN.md#install-from-pgxn); the existing-server guide still owns preload, restart and verification. |

## Local demo

Requirements: Docker with Compose v2. This demo uses trust authentication only
inside a private Compose network and publishes no host port. This demo is not a
production configuration.

```bash
git clone https://github.com/profundium/pg_local_cache.git
cd pg_local_cache
docker compose --project-name pg_local_cache_demo -f compose.demo.yaml \
  up --detach --build --wait
```

Inspect the seeded rows, mapping, cached plan and health:

```bash
docker compose --project-name pg_local_cache_demo -f compose.demo.yaml \
  exec -T postgres psql --no-psqlrc --username postgres --dbname app \
  -c "TABLE public.pg_local_cache_demo; EXPLAIN SELECT * FROM public.pg_local_cache_demo WHERE id = 1; SELECT local_cache.health();"
```

Remove only this demo's container, network and data volume:

```bash
docker compose --project-name pg_local_cache_demo -f compose.demo.yaml \
  down --volumes
```

## SQL API

`local_cache.attach_table()` discovers the complete primary key, records a
whole-row mapping, and installs extension-owned invalidation triggers:

```sql
BEGIN;
SET LOCAL lock_timeout = '2s';
SELECT local_cache.attach_table('public.items'::regclass);
COMMIT;
```

The canonical tuple API is an ordinary exact-primary-key query:

```sql
SELECT * FROM public.items WHERE id = 42::bigint;
SELECT value, metadata FROM public.items WHERE id = 42::bigint;
```

Composite primary keys use normal SQL predicates, in any order:

```sql
SELECT * FROM public.tenant_items
WHERE item_id = 42::bigint AND tenant_id = 'tenant-a';
```

For a single-column primary key, ordinary SQL can read a set of keys without
calling a cache function:

```sql
SELECT * FROM public.items WHERE id IN (42, 7, 99);

SELECT id, value
FROM public.items
WHERE id = ANY($1::bigint[]);
```

This remains a PostgreSQL row set: duplicate and `NULL` array elements do not
create duplicate rows, ordering is not implied, and a missing key contributes
no row. Use `local_cache.mget()` only when the caller needs an ordered JSON
array aligned with its input positions.

KV-style callers can opt into the JSON scalar and ordered batch functions:

```sql
SELECT local_cache.get('public.items'::regclass, 42::bigint);
SELECT local_cache.mget(
    'public.items'::regclass,
    ARRAY[42, 7, 42]::bigint[]
);

SELECT local_cache.mget(
    'public.tenant_items'::regclass,
    ARRAY[['tenant-a', '42'], ['tenant-b', '7']]::text[][]
);
```

For a single-column primary key, `mget` preserves input order, duplicates and
`NULL` positions; a missing row also produces an aligned `NULL` result.

The same `mget(regclass, anyarray)` function accepts composite keys as a
two-dimensional `text[][]`: each inner row contains one complete primary key in
`attach_table()` column order. Composite batches preserve order and duplicates,
return `NULL` for a missing row, reject `NULL` key components, and are limited to
1,024 keys. Each component is converted with its primary-key type's normal
PostgreSQL input function.

SQL `mget` and RESP `MGET` are also the supported way to warm a known set of
keys. There is no separate prewarm or pin API.

The functions are `SECURITY INVOKER`: the caller still needs `SELECT` on the
source table. Ordinary tuple reads need no `local_cache` schema or function
grant. Writes remain ordinary PostgreSQL DML, so a transaction can update a row
and immediately read its own value; commit invalidates the old entry and rollback
never publishes the new one.

The SQL fast path accepts:

- one attached permanent table without inheritance, partitioning, or RLS;
- equality predicates for every primary-key column, including composite keys;
- or `IN` / `= ANY(array)` on a single-column primary key;
- constants or external parameters, including an external array parameter;
- `SELECT *` or direct column projections, including aliases and reordered
  projections;
- no limit, or a constant `LIMIT 1` for scalar lookup; array lookup has no `LIMIT`.

The transparent array path is deliberately all-or-nothing. It accepts at most
1,024 input elements and 16 MiB of query-local copied tuple data. If any key is
missing, snapshot-ineligible, malformed, or outside those bounds, PostgreSQL
executes the original full `IN`/`ANY` index plan. Cached and source rows are
never merged partially. Composite tuple `IN`, additional filters, `ALL`, and
non-equality operators use PostgreSQL's normal plan. So do
`REPEATABLE READ`, `SERIALIZABLE`, recovery, and reads after the current
transaction writes an attached table. A nonexistent key returns the normal
empty SQL result after consulting the source table.

Application roles need only their normal source-table privileges to benefit
from a transparent cached `SELECT`. Administrative functions are separate:

```sql
SELECT local_cache.reconcile_table('public.items'::regclass);
SELECT local_cache.reconcile_all();
SELECT local_cache.detach_table('public.items'::regclass);
```

See the [technical reference](docs/TECHNICAL.md#planner-and-executor-fast-path)
for the exact planner, snapshot, and type rules.

## Verified binary release

Open the [latest stable release](https://github.com/profundium/pg_local_cache/releases/latest)
and copy its exact `vX.Y.Z` tag. The tag-bound helper selects and verifies the
right package; it never executes the installer.

```bash
(tag=vX.Y.Z; base="https://github.com/profundium/pg_local_cache/releases/download/${tag}"; tmp="$(mktemp -d)"; trap 'rm -rf -- "$tmp"' EXIT; curl -fsSL --proto '=https' --proto-redir '=https' --tlsv1.2 -o "$tmp/SHA256SUMS" "$base/SHA256SUMS" -o "$tmp/fetch-release.sh" "$base/fetch-release.sh" && (cd "$tmp" && awk '$2 == "fetch-release.sh"' SHA256SUMS > helper.sha256 && [ "$(wc -l < helper.sha256)" -eq 1 ] && sha256sum --check --strict helper.sha256) && bash "$tmp/fetch-release.sh" --release-tag "$tag" --output-directory "$(pwd -P)/pg_local_cache-package")
```

The helper validates the tag, detects PostgreSQL 14–18, Linux amd64 and
glibc/musl, verifies the selected archive against that tag's `SHA256SUMS`, and
safely extracts it. If the target is not a published binary platform, use the
[PGXN/source path](PGXN.md#install-from-pgxn).
The [manual release-asset route](docs/INSTALL_EXISTING.md#manual-release-download)
keeps both the archive and `SHA256SUMS` directly reachable.

Run preflight, then stage files and settings online. Keep the state-directory
path printed by `install`; staging is not activation:

```bash
sudo ./pg_local_cache-package/install.sh preflight --database app --mode sql-only
sudo ./pg_local_cache-package/install.sh install --database app --mode sql-only
```

Have the operator restart PostgreSQL with the cluster's existing orchestration.
Then use the exact printed state path for expectation-only activation and
verification:

```bash
sudo ./pg_local_cache-package/install.sh verify \
  --state-directory /var/lib/pg_local_cache/install-state/install-PRINTED-ID
```

The [existing-database install guide](docs/INSTALL_EXISTING.md) covers checksum
verification, read-only preflight, online staging, restart, recovery, upgrades,
existing Docker volumes, HA, managed services, verification and rollback.

The first installation requires one restart because
`shared_preload_libraries` is evaluated at postmaster startup. File staging and
configuration validation stay online. The installer's 30-second setting is a
warning target; actual interruption depends on shutdown, recovery, and client
reconnection.

## Optional RESP2 endpoint

SQL-only mode sets `pg_local_cache.port=0` and starts no RESP workers. RESP mode
uses the same shared cache and invalidation machinery, but has a separate
security boundary: one worker role and one shared token cover every accepted
mapping, with no TLS or per-client PostgreSQL ACL context.

Keep the listener on loopback or behind an authenticated TLS proxy. A whole-row
key has this form:

```text
CRUD:database.schema.table:{"pk_column":<json-scalar>,...}
```

`GET` returns the complete row as JSON and reads the source table on a cache
miss. Writable mappings expose PostgreSQL-backed `SET` and `DEL`. See the
[wire API and compatibility boundary](docs/TECHNICAL.md#resp2-wire-api) and the
[existing-server RESP setup](docs/INSTALL_EXISTING.md#optional-resp-mode).

## Benchmarks

The project reports different interfaces separately. A key resolved inside a
32-key batch is not presented as one SQL statement, and RESP throughput is not
used to imply ordinary-SQL throughput.

### Ordinary SQL and RESP comparative smoke

[CI run 31172234073](https://github.com/profundium/pg_local_cache/actions/runs/31172234073)
for [source `71b0aa3`](https://github.com/profundium/pg_local_cache/commit/71b0aa3a27c5c009b7ba08bbaa660147f078bde8)
passed every gate after full-working-set stabilization:

| Measured lane | pg_local_cache | Comparison target | Relative result |
|---|---:|---:|---:|
| Ordinary `SELECT *` by complete primary key | 123,707 statements/s | 65,867 stock PostgreSQL statements/s | 1.88x |
| Reordered direct-column projection | 118,679 statements/s | 63,952 stock PostgreSQL statements/s | 1.86x |
| Reordered composite-PK predicates | 126,550 statements/s | 68,746 stock PostgreSQL statements/s | 1.84x |
| Ordinary 32-key `SELECT ... IN (...)` | 865,201 key ops/s; 27,038 statements/s | 328,282 key ops/s; 10,259 statements/s | 2.64x |
| Warm RESP2 `GET` | 143,104 ops/s | 194,910 Valkey; 197,522 Redis ops/s | 0.72x Redis |

The RESP row is intentional rather than hidden: in this run, dedicated Valkey
and Redis were 1.36–1.38x faster on raw warm `GET`. The product's differentiator
is transaction-aware PostgreSQL integration and ordinary SQL compatibility, not
a claim to beat a dedicated in-memory server at its native protocol.

This was a one-second, one-repetition shared-runner regression smoke with four
clients, pipeline depth eight, 128 keys per attached table, 256 cache entries,
and two CPU cores per server target. It is evidence that the fast paths work and
remain faster in that exact profile, not a production capacity estimate.

### Explicit SQL GET/MGET profile

A separate [CI run 30803546805](https://github.com/profundium/pg_local_cache/actions/runs/30803546805)
for [source `fe2d23c`](https://github.com/profundium/pg_local_cache/commit/fe2d23c87ddc7e523ada2951376ebcb7d8570fb1)
measured `local_cache.mget()` at 64,954 prepared and 66,156 unnamed-extended
key ops/s: 10.30x and 10.39x the stock PostgreSQL batch query in that specific
32-key JSON workload. This historical measurement covers only single-column
`bigint[]` SQL MGET. It does not measure composite SQL MGET or RESP MGET and is
not directly comparable with ordinary `SELECT`, statements/s, or RESP `GET`.

See the [benchmark methodology and exact commands](docs/BENCHMARKS.md), the
[scenario definitions](benchmarks/SCENARIOS.md), the latest
[raw ordinary-SQL/RESP evidence](assets/benchmark-evidence/71b0aa3/README.md),
and the preserved [SQL GET/MGET evidence](assets/benchmark-evidence/fe2d23c/README.md).

## Monitoring

`local_cache.metrics()` exposes typed cache, memory, worker, client,
invalidation, backpressure, and mapping counters. The optional stack adds
postgres_exporter, Prometheus rules, container memory signals, and a provisioned
Grafana dashboard. Start with the [monitoring and OOM guide](docs/MONITORING.md).

## Releases

Download source, the platform-labelled binary, checksums, and CI evidence from
the [latest stable release](https://github.com/profundium/pg_local_cache/releases/latest).
Older reviewed versions remain available on the
[releases page](https://github.com/profundium/pg_local_cache/releases).

## Current limits

- PostgreSQL 14–18 on Linux amd64 (glibc or musl). PostgreSQL 14 support follows
  upstream through November 12, 2026.
- One configured database and one writable primary per extension instance.
- Permanent, non-partitioned tables with a supported primary key; no views,
  inheritance, or RLS.
- Encoded cache entries are limited to 8 KiB; oversized rows use PostgreSQL.
- Transparent single-column `IN`/`ANY` accepts at most 1,024 elements and 16 MiB
  of query-local tuple copies; larger or mixed-safe batches use PostgreSQL.
- Composite SQL `mget(text[][])` and RESP `MGET` accept at most 1,024 keys;
  RESP additionally rejects an encoded batch response above its fixed bound.
- At most 128 mappings and 16 primary-key columns per mapping.
- No TTL, Redis Cluster, Lua, Pub/Sub, multi-primary, or standby cache serving.
- RESP authentication is a shared-token boundary, not PostgreSQL user
  authentication.

## Documentation

- [Install on an existing PostgreSQL server](docs/INSTALL_EXISTING.md)
- [SQL, consistency, security, and configuration reference](docs/TECHNICAL.md)
- [Benchmarks and latency methodology](docs/BENCHMARKS.md)
- [Monitoring and OOM protection](docs/MONITORING.md)
- [Benchmark scenarios](benchmarks/SCENARIOS.md)
