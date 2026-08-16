# Install on an existing PostgreSQL server

Use a maintenance window: first activation changes
`shared_preload_libraries` and requires one PostgreSQL restart.

Supported binary target: Linux amd64, PostgreSQL 14–18, glibc or musl.

## Fast path

For a local cluster controlled by `pg_ctl`:

```bash
curl -fsSL https://github.com/profundium/pg_local_cache/releases/latest/download/install-latest.sh | bash -s -- app
```

Replace `app` with the database name. This is SQL-only mode
(`pg_local_cache.port = 0`).

The downloaded bootstrap resolves one release tag, verifies
`fetch-release.sh` against that release's `SHA256SUMS`, selects the matching
PostgreSQL/libc archive, verifies it, installs, restarts, creates the extension,
and runs the health check.

Review the bootstrap before running it if `curl | bash` is outside your
policy:

```bash
curl -fsSLO https://github.com/profundium/pg_local_cache/releases/latest/download/install-latest.sh
less install-latest.sh
bash install-latest.sh app
```

## Controlled install

Download a fixed release with the published helper:

```bash
curl -fsSLO https://github.com/profundium/pg_local_cache/releases/download/vX.Y.Z/fetch-release.sh
bash fetch-release.sh --release-tag vX.Y.Z --output-directory ./pg_local_cache-package
```

Then choose the restart owner explicitly:

```bash
sudo ./pg_local_cache-package/install.sh preflight --database app
sudo ./pg_local_cache-package/install.sh install --database app --restart-method systemd --systemd-unit postgresql@16-main
```

Other supported restart modes are `pg_ctl` and `none`. With `none`, restart
through Patroni, a Kubernetes operator, or your normal operations workflow,
then run:

```bash
sudo ./pg_local_cache-package/install.sh verify --database app
```

The installer prints its state directory. Keep it until verification succeeds;
it contains the online backup needed by `recover`:

```bash
sudo ./pg_local_cache-package/install.sh recover --state-directory /path/printed/by/install
```

Do not run recovery after a new postmaster has accepted traffic without first
reviewing the recorded state.

## Configure

Minimum SQL-only configuration:

```conf
shared_preload_libraries = 'pg_local_cache'
pg_local_cache.database = 'app'
pg_local_cache.cache_entries = 100000
pg_local_cache.port = 0
```

Size `cache_entries`, relation states, clients, workers, and the extension
memory budget together. The installer preflight rejects inconsistent plans.
Keep existing `shared_preload_libraries` entries.

## Create and attach

After restart:

```sql
CREATE EXTENSION IF NOT EXISTS pg_local_cache;
SELECT local_cache.attach_table('public.items'::regclass);
SELECT local_cache.health();
```

Grant an application only what it needs:

```sql
GRANT SELECT ON public.items TO app_user;
GRANT USAGE ON SCHEMA local_cache TO app_user;
GRANT EXECUTE ON FUNCTION local_cache.mget(regclass, anyarray) TO app_user;
```

Verify a cold fill and warm hit:

```sql
SELECT local_cache.invalidate('public.items');
SELECT local_cache.mget('public.items'::regclass, ARRAY[1]::bigint[]);
SELECT local_cache.mget('public.items'::regclass, ARRAY[1]::bigint[]);
SELECT local_cache.stats();
```

Ordinary `SELECT` is not rewritten by the extension.

## RESP mode

RESP is optional. It adds a listener, worker processes, a dedicated PostgreSQL
role, and one shared token. Use:

```bash
sudo ./pg_local_cache-package/install.sh preflight --database app --mode resp --token-file /secure/path/token
sudo ./pg_local_cache-package/install.sh install --database app --mode resp --token-file /secure/path/token --restart-method systemd --systemd-unit postgresql@16-main
```

Keep the listener on `127.0.0.1` or behind authenticated TLS. RESP has no
per-client PostgreSQL ACL context.

## Upgrade from 1.x

Version 2.0 has two breaking changes:

- ordinary `SELECT` uses PostgreSQL's planner; use `local_cache.mget` for
  explicit cached reads;
- both `local_cache.get` overloads (`anyelement` and `text[]`) are removed.

Before upgrading, move single-row reads to one-element `local_cache.mget` calls
and read array element `[1]`. Composite keys become a one-row `text[][]` input.
Remove `pg_local_cache.sql_cache` from PostgreSQL configuration.

Install the new files, restart the cluster, then update SQL objects:

```sql
ALTER EXTENSION pg_local_cache UPDATE;
SELECT local_cache.reconcile_all();
SELECT local_cache.health();
```

## Troubleshooting

- preload error: confirm the target cluster's config and restart it;
- table rejected: require a permanent, non-partitioned, non-RLS table with a
  supported primary key;
- `mget` permission error: grant source-table `SELECT`, schema `USAGE`, and
  function `EXECUTE`;
- cache bypasses: check transaction isolation, writes in the current
  transaction, recovery state, row size, and metrics;
- stale mapping after DDL: run `local_cache.reconcile_table(...)`.

See [technical reference](TECHNICAL.md) and
[monitoring](../monitoring/README.md).
