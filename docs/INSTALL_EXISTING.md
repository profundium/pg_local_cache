---
layout: doc
title: Install on an existing PostgreSQL server
description: Install pg_local_cache on PostgreSQL 14–18 Linux servers with direct latest downloads, preflight, rollback and one controlled restart.
section: Existing database
permalink: /docs/INSTALL_EXISTING.html
---

# Install pg_local_cache on an existing PostgreSQL server

This guide installs `pg_local_cache` without replacing the database cluster or
moving its data. It targets PostgreSQL 14–18 on Linux amd64 and one writable
primary.

The installation has two phases:

1. **Online preparation:** validate the cluster, copy the extension, create an
   isolated worker role, back up configuration and stage new settings.
2. **One controlled restart:** PostgreSQL allocates shared memory and registers
   the planner hooks and optional RESP workers, after which the extension is
   created and checked.

The first installation cannot be completely restartless.
`shared_preload_libraries` takes effect only at postmaster start, and
`pg_local_cache` uses it to reserve shared memory and install hooks. This is a
PostgreSQL constraint, not an installer choice. See the official PostgreSQL
[shared library preloading documentation](https://www.postgresql.org/docs/current/runtime-config-client.html#RUNTIME-CONFIG-CLIENT-PRELOAD).

All preparation is online. The installer treats 30 seconds as a warning target,
not an availability guarantee. Actual downtime depends on open sessions,
shutdown behavior, storage and recovery. The installer waits for readiness
without escalating to an immediate shutdown.
PostgreSQL itself cautions that startup recovery may exceed service-manager
timeouts in its [server startup documentation](https://www.postgresql.org/docs/16/server-start.html).

## Compatibility checklist

Use the standalone installer only when all of these are true:

- PostgreSQL server major version is 14, 15, 16, 17, or 18, and matches the
  selected archive;
- the database runs on Linux and you can write to the local PostgreSQL
  `pkglibdir` and extension directory;
- you can connect locally as a PostgreSQL superuser;
- you control `shared_preload_libraries` and the cluster restart;
- this node is the writable primary;
- one `pg_local_cache` instance serves one configured database.

Do not run the standalone configuration step against Patroni, a Kubernetes
operator or a managed service. Those systems own PostgreSQL configuration and
restart orchestration; use the dedicated sections below.

Binary archive names include the PostgreSQL major and libc, for example
`pg_local_cache-pg18-linux-glibc-amd64.tar.gz`. Use `glibc` on Debian, Ubuntu,
RHEL-family and similar systems, and `musl` on Alpine. There is no universal
Linux `.so`; use the source archive and local PGXS when the published binary
does not match the target's major, libc, or architecture.

### Package requirements

Both installation paths require local superuser access, `sha256sum`, `tar`,
and the target server's `pg_config`. You can download and verify the archive on
an administration host, then copy it to the server.

| Archive | Additional requirements |
|---|---|
| `pgN-linux-glibc-amd64` binary | PostgreSQL N on Linux amd64 with glibc; no compiler is needed. |
| `pgN-linux-musl-amd64` binary | PostgreSQL N on Linux amd64 with musl; no compiler is needed. |
| Source | GNU Make, a C compiler, matching PostgreSQL 14–18 PGXS, and server development headers. |

For Debian or Ubuntu with the PostgreSQL packages already configured, the
source toolchain is typically installed with:

```bash
sudo apt-get update
PG_MAJOR=18
sudo apt-get install --yes build-essential "postgresql-server-dev-${PG_MAJOR}"
```

Package names differ for PGDG, RPM-based distributions, and vendor builds. Use
the development package that supplies PGXS for the exact target server, then
confirm its path:

```bash
/usr/lib/postgresql/18/bin/pg_config --pgxs
```

## 1. Download and verify

Download the exact compatible asset through GitHub's `latest` redirect. This
needs only `curl`; it does not need GitHub CLI, an API token, or the current tag:

```bash
PG_MAJOR=18
LIBC=glibc # glibc or musl
BASE=https://github.com/profundium/pg_local_cache/releases/latest/download

curl -fLO "$BASE/pg_local_cache-pg${PG_MAJOR}-linux-${LIBC}-amd64.tar.gz"
curl -fLO "$BASE/SHA256SUMS"
```

For a local PGXS build, download
`$BASE/pg_local_cache-source.tar.gz` instead.

Verify the files you downloaded before extracting them. `--ignore-missing`
skips the other assets listed in the release checksum file:

```bash
sha256sum --check --ignore-missing --strict SHA256SUMS

tar -xzf "pg_local_cache-pg${PG_MAJOR}-linux-${LIBC}-amd64.tar.gz"
cd "pg_local_cache-"*-"pg${PG_MAJOR}-linux-${LIBC}-amd64"
```

For a source build, extract the source archive instead:

```bash
tar -xzf pg_local_cache-source.tar.gz
cd pg_local_cache-*-source
```

The source archive exposes `scripts/install-existing.sh`; the binary archive
places the same program at `./install.sh`.

The installer never accepts a RESP token value on the command line. It accepts
only a path, avoiding token disclosure through process listings and shell
history.

## 2. Run preflight

The default first deployment is SQL-only:

```bash
sudo ./install.sh preflight \
  --database app \
  --mode sql-only \
  --pg-config /usr/lib/postgresql/18/bin/pg_config
```

From a source archive, replace `./install.sh` with
`./scripts/install-existing.sh`.

Preflight is read-only. It checks:

- local and server PostgreSQL major versions;
- that the connection is superuser and the node is not in recovery;
- that local `pg_config` paths exactly match the connected server's
  `pg_catalog.pg_config` view;
- that the server's data directory exists on this host;
- existing errors in `pg_file_settings`;
- the effective preload list and worker-process budget, including valid
  pending-restart values already staged in PostgreSQL configuration files;
- role, sizing and token invariants.

The script connects as the configured operating-system PostgreSQL owner
(`postgres` by default), which works with the usual local peer authentication.
Use `--postgres-os-user` if the postmaster belongs to another OS account.
Normal libpq environment variables and `.pgpass` remain available; the
installer does not log them.

Inspect the cluster before the maintenance window as well:

```sql
SELECT version();
SHOW shared_preload_libraries;
SHOW max_worker_processes;
SHOW config_file;
SHOW data_directory;

SELECT pid, usename, state, xact_start, query_start
FROM pg_stat_activity
WHERE xact_start IS NOT NULL
ORDER BY xact_start;

SELECT *
FROM pg_file_settings
WHERE error IS NOT NULL;
```

## 3. Stage the SQL-only installation online

```bash
sudo ./install.sh install \
  --database app \
  --mode sql-only \
  --pg-config /usr/lib/postgresql/18/bin/pg_config
```

With `--restart-method none` (the default), this command does not interrupt the
server. It:

1. builds with the selected PGXS or validates the packaged binary;
2. atomically installs `.so`, control and versioned SQL files;
3. creates or strictly validates `local_cache_worker` as
   `LOGIN NOSUPERUSER NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION
   NOBYPASSRLS`;
4. grants that role `CONNECT` only to the configured database;
5. saves the exact current `postgresql.auto.conf` plus metadata under
   `/var/lib/pg_local_cache/install-state/`;
6. appends `pg_local_cache` to the effective `shared_preload_libraries`
   instead of replacing other active or already-staged libraries;
7. writes conservative SQL-only sizing (`port=0`, no RESP token/workers);
8. calls `pg_reload_conf()` only to parse the staged file and rejects any
   `pg_file_settings` error.

Reload validates the file but does not activate the extension. PostgreSQL
reports the relevant settings as pending restart.

Use `--dry-run` to execute preflight and print the mutation plan without
building, copying, changing a role or writing configuration:

```bash
sudo ./install.sh install --database app --mode sql-only --dry-run
```

If different extension files already exist, installation fails closed. Use
`--force` only for a deliberate, reviewed upgrade; upgrading a loaded native
library still requires a restart so old and new backends never mix code
generations.

## 4. Perform one restart

### Explicit operator restart

Use your existing service manager for the restart, drain, and connection-pool
handling:

```bash
sudo systemctl restart postgresql@18-main
```

Then activate and verify:

```bash
sudo ./install.sh verify --database app --mode sql-only
```

`verify` fails until the new preload setting is actually active. Once active,
it creates the extension, grants the technical role access to its mapping
catalog and requires `local_cache.health().ready=true`, port `0` and zero RESP
workers.

### Installer-controlled systemd restart

If the exact unit is known and your operational policy permits it:

```bash
sudo ./install.sh install \
  --database app \
  --mode sql-only \
  --restart-method systemd \
  --systemd-unit postgresql@18-main \
  --readiness-timeout 180 \
  --restart-goal-seconds 30
```

The script measures time until a SQL query succeeds. Exceeding 30 seconds is a
warning; failing to become ready within the larger readiness timeout is an
error. If the postmaster is confirmed stopped after a failed restart, the
installer atomically restores the exact pre-install `postgresql.auto.conf` and
attempts one rollback restart. If a postmaster is still running or recovering,
the installer leaves it alone and reports the state-backup path instead of
risking a second restart during recovery.

### Installer-controlled `pg_ctl`

For clusters managed directly through `pg_ctl`:

```bash
sudo ./install.sh install \
  --database app \
  --mode sql-only \
  --restart-method pg_ctl
```

Do not use this option when systemd, Patroni or an operator owns the
postmaster.

## 5. Attach an existing table

Run attach as the extension owner or a trusted deploy role. Use a short lock
timeout and retry outside the transaction if the table is busy:

```sql
BEGIN;
SET LOCAL lock_timeout = '2s';

SELECT local_cache.attach_table('public.items'::regclass);
COMMIT;
```

`attach_table` caches the whole row and discovers the complete primary key in
index-column order. It supports 1–16 PK columns. It acquires
`ShareRowExclusiveLock` while installing and validating extension-owned
triggers, so it can briefly conflict with DML and DDL.

Application users keep their normal source-table privileges:

```sql
GRANT SELECT, INSERT, UPDATE, DELETE ON public.items TO app_user;
```

The existing PostgreSQL driver continues issuing normal typed SQL:

```sql
SELECT * FROM public.items WHERE id = $1::bigint;
SELECT value FROM public.items WHERE id = $1::bigint;
```

No output column list or cache-specific function is required. `SELECT *`
returns the complete tuple; an ordinary projection returns only requested
columns. Grant the optional JSON functions only to applications that use them:

```sql
GRANT USAGE ON SCHEMA local_cache TO app_user;
GRANT EXECUTE ON FUNCTION local_cache.get(regclass, anyelement) TO app_user;
GRANT EXECUTE ON FUNCTION local_cache.mget(regclass, anyarray) TO app_user;
```

Use that same function for ordered single-column or composite batches:

```sql
SELECT local_cache.mget(
    'public.items'::regclass,
    ARRAY[42, 7, 42]::bigint[]
);
SELECT local_cache.mget(
    'public.tenant_items'::regclass,
    ARRAY[['tenant-a', '42'], ['tenant-b', '7']]::text[][]
);
```

These calls also prewarm the requested keys through the normal lookup/fill path;
no separate prewarm or pin function is needed.

Verify the transparent exact-PK fast path:

```sql
EXPLAIN (ANALYZE, COSTS OFF)
SELECT * FROM public.items WHERE id = 42 LIMIT 1;

SELECT local_cache.health();
SELECT * FROM local_cache.metrics();
```

A supported cold lookup reads the source table and fills the cache. For a
missing or unsafe entry, ordinary PostgreSQL reads the authoritative source
row. Composite primary keys use normal equality predicates for every key
column; see the technical reference for details.

## Optional RESP mode

Create a persistent token file before preflight. Use `/run` only when a secret
manager recreates the file on every boot. The example below uses
`/etc/pg_local_cache` for a standalone host.

```bash
sudo install -d -o postgres -g postgres -m 0700 /etc/pg_local_cache
openssl rand -base64 48 \
  | tr '+/' '-_' | tr -d '=[:space:]' \
  | sudo tee /etc/pg_local_cache/auth_token >/dev/null
sudo chown postgres:postgres /etc/pg_local_cache/auth_token
sudo chmod 0400 /etc/pg_local_cache/auth_token
```

Stage and restart:

```bash
sudo ./install.sh install \
  --database app \
  --mode resp \
  --bind-address 127.0.0.1 \
  --port 6380 \
  --workers 4 \
  --token-file /etc/pg_local_cache/auth_token \
  --restart-method systemd \
  --systemd-unit postgresql@18-main
```

The token must be a non-symlink regular file owned by and readable by the
PostgreSQL OS user, have mode exactly `0400` or `0600`, and contain 32–256
base64url characters. The installer preserves effective pending worker-budget
changes and adds only the positive RESP-worker delta when an active SQL-only
installation changes to RESP or an active RESP worker count increases.

Keep the RESP listener on loopback or a separately authenticated private
network. Its shared token grants access to every mapping; PostgreSQL
application-user ACL and RLS do not apply to RESP commands.

After `AUTH`, RESP clients may issue `MGET key [key ...]` for up to 1,024
whole-row keys. Keys may be composite or belong to different attached mappings;
the ordered response contains a bulk value or null per key and is rejected as a
single error if its encoded size exceeds the fixed response bound.

## Existing Docker volume

Build the `extension` target from exactly the matching PostgreSQL base image used by
the current container:

```bash
docker build \
  --target extension \
  --build-arg POSTGRES_IMAGE=postgres:16.14-bookworm \
  --tag company-postgres:16-pg-local-cache \
  .
```

This target keeps the official PostgreSQL entrypoint and adds only the native
extension files. Do not switch distributions or PostgreSQL majors under an
existing data volume.

While the old container is still online, create the worker role and stage
configuration through `ALTER SYSTEM`, preserving the existing preload list.
Then replace the container once with the new image and the same PGDATA volume.
A process restart without changing to the image containing the `.so` will not
work. Validate SQL readiness and `local_cache.health()` before admitting
traffic.

The repository's `runtime` image adds a custom entrypoint, secrets, and a
health check for a new volume. The `extension` target preserves the official
PostgreSQL entrypoint and is the intended base for an existing managed Docker
database.

## Patroni and HA

Do not use the standalone installer to write `ALTER SYSTEM` or restart an
individual Patroni member. Use this sequence:

1. install the identical binary and token file on every member;
2. add `pg_local_cache` and its GUCs through Patroni's dynamic configuration;
3. confirm `pending_restart` on all affected members;
4. restart replicas one at a time and verify each one;
5. perform a planned switchover;
6. restart and verify the former primary;
7. create the extension and attach tables on the new primary.

This reduces client-visible interruption to switchover/reconnect time, while
the extension still operates against one writable primary. Use Patroni's
official [`patronictl edit-config`, `restart` and `switchover`](https://patroni.readthedocs.io/en/latest/patronictl.html)
workflows and follow its
[configuration ownership and pending-restart model](https://patroni.readthedocs.io/en/master/patroni_configuration.html).

Apply the same principle to CloudNativePG or another operator: build a custom
image, place configuration in the operator-owned resource and request its
rolling update. Do not edit an operator-managed `postgresql.auto.conf`
directly.

## Managed PostgreSQL

Amazon RDS/Aurora, Cloud SQL, Azure Database for PostgreSQL, Supabase and
similar managed services normally prohibit arbitrary `.so` files and custom
`shared_preload_libraries` entries. `pg_local_cache` is unsupported there
unless the provider explicitly packages and permits this extension. The
installer does not attempt to bypass that boundary.

## Rollback and uninstall

For an immediate failed installation before any mapping is attached, restore
the exact `postgresql.auto.conf.before` from the state directory and perform
one restart. This exact restore is safe only if nobody has changed
`postgresql.auto.conf` since installation; otherwise remove only the
installer-written keys after reviewing the recorded `metadata.tsv`.

For an active installation:

1. stop new attach operations and drain application writes to mapped tables;
2. detach every mapping while the library is still preloaded;
3. `DROP EXTENSION pg_local_cache`;
4. remove only `pg_local_cache` from `shared_preload_libraries` and reset its
   GUCs, preserving every unrelated setting;
5. validate `pg_file_settings`, then restart once;
6. confirm no `pg_local_cache` workers or hooks remain;
7. only then remove `.so`, control and SQL files;
8. drop `local_cache_worker` only if it was installer-created and has no other
   dependencies.

Never remove the binary first. Existing mapping triggers call C functions and
would fail if the library disappeared while SQL objects remained.

## Post-install validation gate

Before routing production traffic:

- run the repository Docker suite before rollout and equivalent integration
  checks against the target cluster;
- run the dedicated SQL-only benchmark on the target CPU/storage profile;
- require prepared and unnamed-extended cached lanes to pass independently;
- verify every timed successful lookup increments `sql_cache_hits`, with no
  timed miss/fill/bypass;
- exercise a committed update, rollback, PK change and DDL/reconcile path;
- record actual restart and recovery time instead of assuming 30 seconds.

See [Benchmarks]({{ '/docs/BENCHMARKS.html' | relative_url }}) and the complete
[technical reference]({{ '/docs/TECHNICAL.html' | relative_url }}).
