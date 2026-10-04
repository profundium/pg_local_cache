---
layout: doc
lang: en
translation_key: INSTALL_EXISTING
title: Install pg_local_cache on PostgreSQL 14-18
seo_title: Install pg_local_cache on PostgreSQL 14-18
description: Install a verified Debian or RPM package, use PGXN or PGXS, configure preload, restart PostgreSQL, and initialize, upgrade, or uninstall pg_local_cache.
section: Install
permalink: /docs/INSTALL_EXISTING.html
last_modified_at: "2026-10-04"
---

# Install pg_local_cache on an existing PostgreSQL server {#install-pg_local_cache-on-an-existing-postgresql-server}

This guide covers Linux with PostgreSQL 14-18. Installation and configuration
require a database superuser, and adding the extension to
`shared_preload_libraries` requires one PostgreSQL restart.

## 1. Requirements {#choose-an-installation-path}

Use the `pg_config` for the target server when building from source. Plan the
restart with the operator or service that owns the PostgreSQL cluster.

## 2. Install a package {#fast-binary-install}

Download the package and `SHA256SUMS` for your PostgreSQL major version and
architecture from the same [GitHub release](https://github.com/profundium/pg_local_cache/releases).

### Debian and Ubuntu {#controlled-binary-install}

Download `postgresql-<major>-pg-local-cache_<version>-1_<arch>.deb`. Packages
are built on Debian 12 and require glibc 2.36 or later.

The checksum manifest lists every release asset. Use `--ignore-missing` when
checking only the files you downloaded. Verify the checksum and build
provenance, then install:

```bash
sha256sum -c --ignore-missing SHA256SUMS
gh attestation verify <file>.deb --repo profundium/pg_local_cache
sudo apt install ./<file>.deb
```

### RHEL, Rocky Linux, and AlmaLinux 9

These RPMs require matching PostgreSQL from the PGDG Yum repository. Enable the
[PGDG repository](https://www.postgresql.org/download/linux/redhat/) for your EL
version and PostgreSQL major, then install `postgresql<major>-server` from PGDG
before installing the extension. The RPM installs under `/usr/pgsql-<major>`.
Distribution-native PostgreSQL packages use different package names, paths, and
service names; use [Build from source](#build-from-source) for those servers.

Download the matching `.rpm` and `SHA256SUMS` for your PostgreSQL major version
and architecture from the same release. Verify and install it:

```bash
sha256sum -c --ignore-missing SHA256SUMS
gh attestation verify <file>.rpm --repo profundium/pg_local_cache
sudo dnf install ./<file>.rpm
```

### PGXN

Install the PGXN client, the target PostgreSQL server development headers, a C
compiler, and GNU Make. Specify the target server's `pg_config`; PGXN otherwise
uses the first one on `PATH`. Its install phase needs root privileges. See the
[PGXN install options](https://pgxn.github.io/pgxnclient/usage.html#pgxn-install).

```bash
pgxn install --pg_config /path/to/pg_config --sudo sudo pg_local_cache
```

### Build from source {#build-from-source}

Install the target PostgreSQL server development headers, a C compiler, and GNU
Make. Build and install with that server's `pg_config`:

```bash
make PG_CONFIG=/path/to/pg_config && \
  sudo make PG_CONFIG=/path/to/pg_config install
```

## 3. Configure `postgresql.conf` {#configure-before-restart}

Keep existing entries in `shared_preload_libraries` and add `pg_local_cache`.
Replace `app` with the database served by the extension.

### Configure the RESP listener {#enable-optional-resp2}

Configure the RESP listener with a dedicated worker role and protected token
file:

```conf
shared_preload_libraries = 'pg_local_cache'
pg_local_cache.database = 'app'
pg_local_cache.role = 'local_cache_worker'
pg_local_cache.port = 6380
pg_local_cache.bind_address = '127.0.0.1'
pg_local_cache.auth_token_file = '/secure/path/token'
pg_local_cache.cache_entries = 16384
pg_local_cache.memory_budget_mb = 384
```

The default listener is loopback-only. For remote clients, use a local proxy or
sidecar, or explicitly enable plaintext on a trusted network with
`pg_local_cache.allow_plaintext_network = on`; native TLS is not available in
3.0.0.

Size cache entries, relation states, workers, clients, and
`memory_budget_mb` together. See the [technical reference](TECHNICAL.md#shared-memory-and-configuration)
for capacity and memory guidance.

## 4. Restart PostgreSQL

Use the service or operator that owns the cluster. On systemd:

```bash
# Debian and Ubuntu
sudo systemctl restart postgresql@<major>-main
# RHEL, Rocky Linux, and AlmaLinux
sudo systemctl restart postgresql-<major>
```

For Patroni, update the cluster configuration and restart through Patroni:

```bash
patronictl edit-config <cluster>
patronictl restart <cluster>
```

For Kubernetes, build a custom PostgreSQL image that includes the matching
package, then roll it out through your operator. CloudNativePG extension images
are planned.

## 5. Initialize {#initialize-a-source-installation}

Connect to the configured database as a database superuser. Create the
extension and dedicated worker role, then grant access to its metadata:

```sql
CREATE EXTENSION IF NOT EXISTS pg_local_cache;
CREATE ROLE local_cache_worker LOGIN NOINHERIT NOSUPERUSER
    NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT CONNECT ON DATABASE app TO local_cache_worker;
GRANT USAGE ON SCHEMA local_cache TO local_cache_worker;
GRANT SELECT ON TABLE local_cache.mapping TO local_cache_worker;
```

Keep the worker role separate from application table owners. Attach each
permanent table that has a supported primary key:

### Attach a table {#attach-a-table}

```sql
SELECT local_cache.attach_table('public.items'::regclass);
```

Check readiness and mapping convergence:

### Verify health {#verify-cold-fill-and-warm-hit}

```sql
SELECT local_cache.health();
```

## 6. Upgrade {#recover-a-failed-binary-install}

For a 2.x to 3.0 upgrade, follow the [upgrade guide](UPGRADING.md) first. In
short, migrate applications from SQL `mget` to RESP `MGET`, install the new
package, restart PostgreSQL through the correct service or operator, verify the
loaded library version, then update the extension in every database that has it:

```sql
ALTER EXTENSION pg_local_cache UPDATE;
```

## 7. Uninstall {#troubleshooting}

Detach every mapped table and drop the extension as a database superuser. Remove
`pg_local_cache` from `shared_preload_libraries`, restart PostgreSQL, then
remove the package with the matching package manager:

```sql
SELECT local_cache.detach_table('public.items'::regclass);
DROP EXTENSION pg_local_cache;
```

```bash
sudo apt remove postgresql-<major>-pg-local-cache
sudo dnf remove pg_local_cache_<major>
```

Next: see the [technical reference](TECHNICAL.md) for SQL behavior, memory
sizing, monitoring, and RESP security.
