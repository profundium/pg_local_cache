---
layout: doc
lang: en
translation_key: UPGRADING
title: Upgrade pg_local_cache from 2.x to 3.0.0
seo_title: "Upgrade pg_local_cache 2.x to 3.0.0"
description: Move applications from SQL mget to RESP MGET, install and upgrade the extension safely, understand dependency errors, and roll back to 2.0.4.
section: Install
permalink: /docs/UPGRADING.html
last_modified_at: "2026-10-04"
---

# Upgrade pg_local_cache from 2.x to 3.0.0 {#upgrade-pg_local_cache-from-2x-to-300}

Version 3.0.0 removes the SQL function `local_cache.mget(regclass, anyarray)`.
Use authenticated RESP `MGET` for cached whole-row lookups:

```text
MGET CRUD:app.public.items:{"id":42} CRUD:app.public.items:{"id":7}
```

RESP workers use the configured PostgreSQL role. They do not inherit the
application's SQL privileges, transaction, or snapshot. Keep SQL queries for
projections, joins, row locks, and reads that require application-session
semantics. The [RESP guide](resp.md) covers client setup and key encoding.

## Upgrade order {#upgrade-order}

1. Change applications to stop calling SQL `mget`; verify RESP `MGET` reads
   use a dedicated worker role and meet the required authorization model.
2. Install the 3.0.0 package or library for the running PostgreSQL major.
3. If the 2.x listener used a non-loopback `pg_local_cache.bind_address`,
   configure native RESP TLS and provide its certificate and key before
   restarting. Set `pg_local_cache.tls_ca_file` to require client certificates
   (mTLS). RESP TLS is independent of PostgreSQL `ssl_*` settings. If plaintext
   is required, set `pg_local_cache.allow_plaintext_network = on` explicitly
   and only on a trusted network. Without TLS or that explicit plaintext opt-in,
   RESP workers refuse to start.
4. Restart PostgreSQL so it loads the new shared library.
5. Verify the listener: `SELECT local_cache.health();` must show
   `workers_running = workers_configured`. Send RESP `PING` and expect `PONG`.
6. Verify the loaded library version:

   ```sql
   SELECT current_setting('pg_local_cache.binary_version');
   ```

   The result must be `3.0.0`.
7. In every database that has the extension installed, connect as a database
   superuser and run:

   ```sql
   ALTER EXTENSION pg_local_cache UPDATE;
   ```

The migration replaces `local_cache.metrics()` because its table result type
changed when the SQL read API counters were removed. Custom grants on
`local_cache.metrics()` are preserved. PostgreSQL refuses to drop the old
function while a user object depends on it. The migration deliberately uses no
`CASCADE`; drop or revise dependent views and functions yourself, then retry the
extension update. Other surviving C functions are replaced in place so their
OIDs, grants, and attached-table triggers remain valid.

## Roll back to 2.0.4 {#rollback-to-204}

There is no downgrade script. To return to 2.0.4, reinstall its package, restart
PostgreSQL so it loads the old library, detach every mapped table, then recreate
the extension in each database and attach the tables again:

```sql
SELECT local_cache.detach_table('public.items'::regclass);
DROP EXTENSION pg_local_cache;
CREATE EXTENSION pg_local_cache VERSION '2.0.4';
SELECT local_cache.attach_table('public.items'::regclass);
```

Repeat `detach_table` and `attach_table` for every mapped table. Save the
attached-table list and any custom extension grants before rollback so they can
be restored. User objects that depend on extension functions can block the drop
with PostgreSQL's normal dependency error; handle those dependencies explicitly.

## Library lookup path {#library-lookup-path}

The control file uses the bare library name `pg_local_cache` in
`module_pathname`. PostgreSQL resolves it through `dynamic_library_path` (whose
default includes `$libdir`). If your server overrides that setting, include the
directory where the package installed `pg_local_cache` before restarting.
