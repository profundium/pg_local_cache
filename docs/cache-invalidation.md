---
layout: doc
title: Transaction-aware cache invalidation in PostgreSQL
seo_title: "PostgreSQL Cache Invalidation: Commit and Rollback | pg_local_cache"
description: Test pg_local_cache 2.0 invalidation with concurrent PostgreSQL sessions. Check uncommitted updates, read-your-writes, rollback, committed reads, and fallback rules.
section: Cache invalidation
permalink: /docs/cache-invalidation.html
last_modified_at: "2026-09-05"
---

# Transaction-aware cache invalidation in PostgreSQL

Deleting a cache entry is not enough if an earlier read can refill it after
the deletion. Suppose a reader starts loading an old row, a writer commits a
new value and invalidates the key, and then that earlier loader publishes its
result. A cache needs to reject that late publication as well.

The 2.0 implementation fences affected keys or relations on the database write
path. A fill carries generation information so it can be rejected after an
invalidation. Cached positive entries also carry tuple visibility information.
An ineligible entry falls back to a source-table read. See the
[technical reference](TECHNICAL.md#transaction-consistency) for the contract.

## Test with two sessions

Start the [local demo](QUICKSTART.md). Open this command in two terminals:

```bash
docker compose -f examples/compose.yaml exec postgres \
  psql -X -v ON_ERROR_STOP=1 -U demo -d pglc_demo
```

In session A, read row 42 and note its revision, then read it again to warm it:

```sql
SELECT (local_cache.mget('public.items'::regclass, ARRAY[42]::bigint[]))[1]::jsonb ->> 'revision';
SELECT (local_cache.mget('public.items'::regclass, ARRAY[42]::bigint[]))[1]::jsonb ->> 'revision';
```

In session B, update the row but leave the transaction open:

```sql
BEGIN;
UPDATE public.items SET revision = revision + 1 WHERE id = 42;
SELECT (local_cache.mget('public.items'::regclass, ARRAY[42]::bigint[]))[1]::jsonb ->> 'revision';
```

B sees its own increment. This read bypasses the cache. Repeat A's query while
B remains open: A must still see the committed revision, not B's uncommitted
value. In B, run `ROLLBACK`; another query in A must still return the original
revision.

Now run in B:

```sql
BEGIN;
UPDATE public.items SET revision = revision + 1 WHERE id = 42;
COMMIT;
```

A query started in A after that commit must return the incremented revision.
This is the relevant boundary: an older running statement is not required to
switch to a snapshot taken after it started. PostgreSQL documents that behavior
under [Read Committed](https://www.postgresql.org/docs/16/transaction-iso.html#XACT-READ-COMMITTED).

The executable [Node.js test](https://github.com/profundium/pg_local_cache/blob/master/examples/node-postgres/demo.mjs)
asserts these observations with separate connections. It is a regression example,
not an exhaustive proof of concurrent correctness. Broader integration tests
remain in the repository's `tests` directory.

## Cases that deliberately bypass the cache

`REPEATABLE READ`, `SERIALIZABLE`, recovery, parallel execution, and transactions
that have written mapped data use the source-table path. An oversized row may
be returned successfully without being cached. A cache hit rate near zero is
not necessarily a failed installation: check the workload and bypass counters.

Do not replace row locking with `mget`. When the application needs
`SELECT ... FOR UPDATE`, use the ordinary PostgreSQL operation. Likewise, a
successful example here says nothing about replica failover, a custom trigger
setup, or an untested extension combination.

## Inspect the cause of a miss

Use `local_cache.stats()` and `local_cache.health()` as an administrator. Compare
counter snapshots before and after a controlled test. Keep SQL `mget` counters
separate from RESP counters. After intentional DDL, follow the documented
`reconcile_table` or `reconcile_all` procedure instead of assuming a previously
attached mapping still describes the changed table.
