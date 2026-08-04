# pg_local_cache

`pg_local_cache` is a PostgreSQL 14–18 extension that turns attached tables into a
transaction-aware SQL key-value store. Applications keep using parameterized
`SELECT` through libpq, JDBC, Npgsql, psycopg, or an ORM. No cache server, token,
cache-specific driver, or proprietary SQL syntax is required.

Attached tables use transaction-aware invalidation. Source writes fence affected
entries before commit visibility, and rollback never exposes uncommitted row
data. PostgreSQL remains authoritative: a missing, unsafe, malformed, or
oversized entry runs the normal source plan.

The current implementation supports PostgreSQL 14–18 on Linux amd64 (glibc and
musl), one configured database, and one writable primary. It is a narrow
primary-key fast path, not a general query cache.

## Capabilities

| Capability | Behavior |
|---|---|
| Native tuple API | Ordinary primary-key `SELECT` returns the table row type and supports normal SQL projection. |
| Batch JSON API | `local_cache.get()` and `local_cache.mget()` provide ordered whole-row JSON for KV-style callers. |
| Whole rows | Each entry stores one versioned PostgreSQL composite row. |
| Transactional invalidation | `INSERT`, `UPDATE`, `DELETE`, and `TRUNCATE` fence affected entries before commit visibility. |
| Bounded extension memory | Entry capacity, client slots, and deterministic extension allocations are fixed at startup. |
| Optional RESP2 | Trusted internal clients can use authenticated whole-row `GET`, `SET`, and `DEL`. |
| Operations | SQL metrics, health checks, Prometheus rules, and a Grafana dashboard are included. |

## Docker quick start

Requirements: Docker with Compose v2 and OpenSSL.

```bash
git clone https://github.com/profundium/pg_local_cache.git
cd pg_local_cache

install -d -m 0700 secrets
openssl rand -base64 36 | tr -d '\n' > secrets/postgres_password
chmod 0600 secrets/postgres_password

docker compose -f compose.sql-only.yaml \
  up --detach --build --wait postgres
```

Open `psql`:

```bash
docker compose -f compose.sql-only.yaml \
  exec postgres psql --username postgres --dbname app
```

Create and attach a table:

```sql
CREATE TABLE public.items (
    id bigint PRIMARY KEY,
    value text NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    metadata jsonb
);

INSERT INTO public.items VALUES
    (1, 'hello', true, '{"source":"postgres"}');

SELECT local_cache.attach_table('public.items'::regclass);
```

Use ordinary PostgreSQL SQL. Select every column or only the columns needed by
the caller:

```sql
SELECT * FROM public.items WHERE id = $1::bigint;

SELECT value, metadata FROM public.items WHERE id = $1::bigint;
```

Supported exact-primary-key reads can use `Custom Scan (pg_local_cache_sql)`;
the row shape and zero-or-one-row semantics remain ordinary PostgreSQL:

```sql
EXPLAIN (ANALYZE, COSTS OFF)
SELECT * FROM public.items WHERE id = 1;

SELECT local_cache.health();
SELECT * FROM local_cache.metrics();
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
SELECT metadata FROM public.items WHERE id = 42::bigint;
```

Composite primary keys use normal SQL predicates, in any order:

```sql
SELECT * FROM public.tenant_items
WHERE item_id = 42::bigint AND tenant_id = 'tenant-a';
```

KV-style callers can opt into the JSON scalar and ordered batch functions:

```sql
SELECT local_cache.get('public.items'::regclass, 42::bigint);
SELECT local_cache.mget(
    'public.items'::regclass,
    ARRAY[42, 7, 42]::bigint[]
);
```

The functions are `SECURITY INVOKER`: the caller still needs `SELECT` on the
source table. Ordinary tuple reads need no `local_cache` schema or function
grant. Writes remain ordinary PostgreSQL DML, so a transaction can update a row
and immediately read its own value; commit invalidates the old entry and rollback
never publishes the new one.

The SQL fast path accepts:

- one attached permanent table without inheritance, partitioning, or RLS;
- equality predicates for every primary-key column, including composite keys;
- constants or external parameters;
- `SELECT *` or direct column projections, including aliases and reordered
  projections;
- no limit, or a constant `LIMIT 1`.

Unsupported query shapes use PostgreSQL's normal plan. So do
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

## Install on an existing server

Choose your PostgreSQL major and Linux libc, then download the exact latest
asset with `curl`—no GitHub CLI or tag lookup:

```bash
PG_MAJOR=18
LIBC=glibc # glibc or musl
BASE=https://github.com/profundium/pg_local_cache/releases/latest/download

curl -fLO "$BASE/pg_local_cache-pg${PG_MAJOR}-linux-${LIBC}-amd64.tar.gz"
curl -fLO "$BASE/SHA256SUMS"
sha256sum --check --ignore-missing --strict SHA256SUMS
tar -xzf "pg_local_cache-pg${PG_MAJOR}-linux-${LIBC}-amd64.tar.gz"
cd "pg_local_cache-"*-"pg${PG_MAJOR}-linux-${LIBC}-amd64"
sudo ./install.sh preflight --database app --mode sql-only
sudo ./install.sh install --database app --mode sql-only
```

Use `glibc` for Debian, Ubuntu, RHEL-family and similar systems; use `musl` for
Alpine. The installer rejects a PostgreSQL major, OS, architecture, or libc
mismatch before copying files. Use `pg_local_cache-source.tar.gz` and the target
server's PGXS when a packaged binary does not match.

The [existing-database install guide](docs/INSTALL_EXISTING.md) covers checksum
verification, read-only preflight, online staging, restart, HA, verification,
and rollback.

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

[CI run 30803546805](https://github.com/profundium/pg_local_cache/actions/runs/30803546805)
for [source `fe2d23c`](https://github.com/profundium/pg_local_cache/commit/fe2d23c87ddc7e523ada2951376ebcb7d8570fb1)
passed every current regression gate. Exact primary-key `SELECT` measured
1.84–1.94x stock PostgreSQL in the ordinary-SQL smoke. `local_cache.mget()`
measured 64,954 prepared and 66,156 unnamed-extended key ops/s: 10.30x and
10.39x the stock batch query in that profile.

These shared-runner smokes detect regressions; they are not capacity claims.
See the [benchmark methodology and exact commands](docs/BENCHMARKS.md), the
[scenario definitions](benchmarks/SCENARIOS.md), and the preserved
[evidence manifest](assets/benchmark-evidence/fe2d23c/README.md).

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
