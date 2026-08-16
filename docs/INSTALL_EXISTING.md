---
layout: doc
title: Install pg_local_cache on PostgreSQL 14-18
seo_title: Install pg_local_cache on PostgreSQL 14-18
description: Install the pg_local_cache PostgreSQL extension with verified Linux binaries or PGXS, then configure preload, restart, verify, and recover safely.
section: Install
permalink: /docs/INSTALL_EXISTING.html
---

# Install pg_local_cache on an existing PostgreSQL server

Install the extension with a verified Linux package or build it with PostgreSQL's
PGXS toolchain. Both paths require one controlled PostgreSQL restart before
`CREATE EXTENSION`.

> **Plan a maintenance window:** first activation changes
> `shared_preload_libraries`. Preserve its existing entries and restart the
> correct cluster only after preflight succeeds.

## Choose an installation path

| Path | Best for | Restart owner |
|---|---|---|
| Latest verified binary | Local Linux amd64 cluster | `pg_ctl` bootstrap |
| Fixed verified binary | Production and managed operations | systemd, `pg_ctl`, or external operator |
| PGXS source build | Unsupported platform or custom PostgreSQL installation | Your normal operations workflow |

Published binaries support PostgreSQL 14-18 on Linux amd64 with glibc or musl.

## Fast binary install

For a local cluster controlled by `pg_ctl`:

```bash
curl -fsSL https://github.com/profundium/pg_local_cache/releases/latest/download/install-latest.sh | bash -s -- app
```

Replace `app` with the database name. This enables SQL-only mode with
`pg_local_cache.port = 0`.

The bootstrap resolves one immutable release tag, verifies `fetch-release.sh`
against that release's `SHA256SUMS`, selects the matching PostgreSQL and libc
archive, verifies it, installs, restarts, creates the extension, and runs
`local_cache.health()`.

If `curl | bash` is outside your policy, inspect the script first:

```bash
curl -fsSLO https://github.com/profundium/pg_local_cache/releases/latest/download/install-latest.sh
less install-latest.sh
bash install-latest.sh app
```

## Controlled binary install

Download a fixed release with its published helper:

```bash
curl -fsSLO https://github.com/profundium/pg_local_cache/releases/download/vX.Y.Z/fetch-release.sh
bash fetch-release.sh --release-tag vX.Y.Z --output-directory ./pg_local_cache-package
```

Run preflight, then choose the restart owner explicitly:

```bash
sudo ./pg_local_cache-package/install.sh preflight --database app
sudo ./pg_local_cache-package/install.sh install \
  --database app \
  --restart-method systemd \
  --systemd-unit postgresql@16-main
```

Supported restart methods are `systemd`, `pg_ctl`, and `none`. Use `none` with
Patroni, a Kubernetes operator, or another external controller. Restart through
that controller, then verify:

```bash
sudo ./pg_local_cache-package/install.sh verify --database app
```

The installer prints a state directory. Keep it until verification succeeds;
it contains the online backup required by `recover`.

## Build from source

Use the same `pg_config` as the target PostgreSQL server. Install its server
development headers, a C compiler, and GNU Make first.

```bash
git clone --branch vX.Y.Z --depth 1 https://github.com/profundium/pg_local_cache.git
cd pg_local_cache
make PG_CONFIG=/usr/lib/postgresql/16/bin/pg_config
sudo make install PG_CONFIG=/usr/lib/postgresql/16/bin/pg_config
```

The checkout must be a clean, immutable revision so the build can record a
trustworthy build ID. Source installation copies extension files only. Continue
with preload configuration, restart, `CREATE EXTENSION`, and verification below.

## Configure before restart

Minimum SQL-only configuration:

```conf
shared_preload_libraries = 'pg_local_cache'
pg_local_cache.database = 'app'
pg_local_cache.cache_entries = 100000
pg_local_cache.port = 0
```

Size `cache_entries`, relation states, clients, workers, and
`memory_budget_mb` together. Binary installer preflight rejects inconsistent
plans. Source builds require the same capacity review before restart.

## Create the extension and attach a table

After PostgreSQL restarts:

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

Ordinary `SELECT` is not rewritten by the extension.

## Verify cold fill and warm hit

```sql
SELECT local_cache.invalidate('public.items');
SELECT local_cache.mget('public.items'::regclass, ARRAY[1]::bigint[]);
SELECT local_cache.mget('public.items'::regclass, ARRAY[1]::bigint[]);
SELECT local_cache.stats();
```

Confirm `local_cache.health()` is ready, the mapping has converged, and SQL cache
counters move as expected.

## Enable optional RESP2

RESP2 adds a listener, worker processes, a dedicated PostgreSQL role, and one
shared token:

```bash
sudo ./pg_local_cache-package/install.sh preflight \
  --database app \
  --mode resp \
  --token-file /secure/path/token

sudo ./pg_local_cache-package/install.sh install \
  --database app \
  --mode resp \
  --token-file /secure/path/token \
  --restart-method systemd \
  --systemd-unit postgresql@16-main
```

Keep the listener on `127.0.0.1` or behind authenticated TLS. RESP clients share
the configured worker role and do not receive per-client PostgreSQL ACL context.

## Recover a failed binary install

Use the state directory printed by the installer:

```bash
sudo ./pg_local_cache-package/install.sh recover \
  --state-directory /path/printed/by/install
```

Do not recover after a new postmaster has accepted traffic until you review the
recorded state and operational impact.

## Troubleshooting

- **Preload error:** confirm the target cluster's configuration and restart the
  correct postmaster.
- **Table rejected:** use a permanent, non-partitioned, non-RLS table with a
  supported primary key.
- **`mget` permission error:** grant source-table `SELECT`, schema `USAGE`, and
  function `EXECUTE`.
- **Cache bypasses:** inspect isolation level, current-transaction writes,
  recovery state, row size, and metrics.
- **Stale mapping after DDL:** run
  `local_cache.reconcile_table('public.items'::regclass)`.

Next: read the [technical reference](TECHNICAL.md) for SQL contracts, consistency,
memory sizing, monitoring, and RESP security.
