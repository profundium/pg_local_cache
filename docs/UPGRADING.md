---
layout: doc
lang: en
translation_key: UPGRADING
title: Upgrade pg_local_cache to 3.1.0
seo_title: "Upgrade pg_local_cache to 3.1.0"
description: Upgrade pg_local_cache from 3.0.0 or 2.x, review 3.1 capacity and worker settings, and verify the restarted extension.
section: Install
permalink: /docs/UPGRADING.html
last_modified_at: "2026-10-06"
---

# Upgrade pg_local_cache to 3.1.0 {#upgrade-pg_local_cache-from-2x-to-310}

Version 3.0.0 removed the SQL function `local_cache.mget(regclass, anyarray)`.
Use authenticated RESP `MGET` for cached whole-row lookups. RESP workers use
the configured PostgreSQL role; they do not inherit an application's SQL
privileges, transaction, or snapshot. Keep SQL for projections, joins, row
locks, and reads that require application-session semantics. The
[RESP guide](resp.md) covers client setup and key encoding.

## Upgrade from 3.0.0 {#upgrade-from-300}

Version 3.1.0 changes the shared library and shared-memory layout. Install the
3.1.0 package or library for the running PostgreSQL major and restart PostgreSQL
before using it. The `3.0.0--3.1.0` SQL migration is a no-op: SQL objects,
attached-table mappings, and triggers do not change. Run the extension update
to record version `3.1.0` in each database.

### Setting changes {#setting-changes}

| Setting | 3.1.0 behavior |
|---|---|
| `pg_local_cache.cache_entries` | Range is `128`–`16777216` descriptors. Built-in default is `262144`, derived against the default 384 MiB budget while reserving at least half for arena pages. Actual byte capacity comes from the slab arena and row sizes; startup rejects layouts that exceed the configured memory budget. |
| `pg_local_cache.lock_partitions` | Default `64`; power of two from `16` to `256`. Small caches use fewer partitions. |
| `pg_local_cache.dirty_marker_entries` | Default `-1` selects automatic sizing: `min(16384, max(1024, floor(cache_entries / 4)))`. Explicit range: `128`–`1048576`. |
| `pg_local_cache.dirty_marker_memory_mb` | Default `-1` selects automatic sizing: `min(16, max(1, floor(memory_budget_mb / 25)))` MiB. Explicit range: `1`–`1024` MiB. |
| `pg_local_cache.max_clients_per_worker` | Default `64`; range increased to `1`–`4096`. `max_clients` must not exceed `workers × max_clients_per_worker`. Each worker requires soft `RLIMIT_NOFILE >= min(max_clients, max_clients_per_worker) + 33`; raise the process/container `nofile` limit as needed. |
| `pg_local_cache.max_deferred_misses` | New setting. Default `8`; range `1`–`64` queued relation-locked requests per worker. |

No 3.0.0 settings were removed or renamed in 3.1.0. All these settings take
effect after restart.

`local_cache.stats()` adds `fast_path_hits`, `fast_path_fallbacks`,
`fast_path_fallback_key_form`, `fast_path_fallback_mapping_shape`,
`fast_path_fallback_multi_key`, and `fast_path_fallback_cache_state`;
`cache_memory_capacity_bytes`,
`cache_memory_used_bytes`, `cache_fragmentation_bytes`, and
`arena_admission_rejections_total`; `dirty_marker_capacity`,
`dirty_marker_entries`, `dirty_marker_highwater`,
`dirty_marker_fallbacks_total`, `dirty_marker_entries_effective`,
`dirty_marker_memory_mb_effective`, `dirty_marker_memory_capacity_bytes`, and
`dirty_key_limit_fallbacks`; plus `lock_partitions`, `max_clients_per_worker`,
and `client_slots`. RESP `STAT` adds worker-local `deferred_misses_total`,
`deferred_misses_current`, `deferred_timeouts_total`, and
`deferred_rejections_total`.

Upgrade steps:

1. Install the 3.1.0 package or library for the running PostgreSQL major.
2. Review the settings above. If increasing client slots, raise the PostgreSQL
   process/container `nofile` soft limit to meet the formula above.
3. Restart PostgreSQL so it loads the new shared library and allocates the new
   shared-memory layout.
4. In every database with the extension installed, connect as a database
   superuser and run:

   ```sql
   ALTER EXTENSION pg_local_cache UPDATE;
   ```

5. Verify the library and workers:

   ```sql
   SELECT current_setting('pg_local_cache.binary_version');
   SELECT local_cache.health();
   ```

   The binary version must be `3.1.0`; health must show
   `workers_running = workers_configured`.

## Upgrade from 2.x {#upgrade-from-2x}

Move applications off SQL `mget` before upgrading. Use RESP `MGET` with a
dedicated worker role and the required authorization model. When upgrading a
non-loopback 2.x listener, configure native RESP TLS and its certificate/key
before restart. Set `pg_local_cache.tls_ca_file` to require client certificates
(mTLS). RESP TLS is independent of PostgreSQL `ssl_*`. If plaintext is needed,
set `pg_local_cache.allow_plaintext_network = on` explicitly and only on a
trusted network. Without TLS or that opt-in, non-loopback RESP workers refuse
to start.

The 2.x-to-3.0 SQL migration removed the SQL read API and its counters, changing
the result type of `local_cache.metrics()`. Custom grants are preserved. A
user object depending on `local_cache.metrics()` can block the migration; it
uses no `CASCADE`, so revise or drop such dependencies and retry. These SQL
changes are part of the earlier 3.0 migration, not the no-op 3.1 migration.
Then follow the install, restart, extension-update, and verification steps
above. PostgreSQL applies the earlier 2.x-to-3.0 migration and then the no-op
3.0.0-to-3.1.0 migration.

## Roll back to 2.0.4 {#rollback-to-204}

There is no downgrade script. To return to 2.0.4, reinstall its package,
restart PostgreSQL so it loads the old library, detach every mapped table, then
recreate the extension in each database and attach the tables again:

```sql
SELECT local_cache.detach_table('public.items'::regclass);
DROP EXTENSION pg_local_cache;
CREATE EXTENSION pg_local_cache VERSION '2.0.4';
SELECT local_cache.attach_table('public.items'::regclass);
```

Repeat `detach_table` and `attach_table` for every mapped table. Save the
attached-table list and custom extension grants before rollback so they can be
restored. User objects that depend on extension functions can block the drop;
handle those dependencies explicitly.

## Library lookup path {#library-lookup-path}

The control file uses the bare library name `pg_local_cache` in
`module_pathname`. PostgreSQL resolves it through `dynamic_library_path` (whose
default includes `$libdir`). If your server overrides that setting, include
the directory where the package installed `pg_local_cache` before restarting.

Documentation for 2.x: https://github.com/profundium/pg_local_cache/tree/v2.0.4/docs
