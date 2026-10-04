---
layout: doc
lang: en
translation_key: cache-invalidation
title: Transaction-aware cache invalidation in PostgreSQL
seo_title: "PostgreSQL Cache Invalidation: Commit and Rollback | pg_local_cache"
description: Understand PostgreSQL trigger invalidation for RESP row reads, committed updates, source reads, and the separate SQL transaction boundary.
section: Cache invalidation
permalink: /docs/cache-invalidation.html
last_modified_at: "2026-09-16"
---

# Transaction-aware cache invalidation in PostgreSQL {#transaction-aware-cache-invalidation-in-postgresql}

Deleting a cache entry is not enough if an earlier read can refill it after
the deletion. Suppose a reader starts loading an old row, a writer commits a
new value and invalidates the key, and then that earlier loader publishes its
result. A cache needs to reject that late publication as well.

The implementation fences affected keys or relations on the database write
path. A fill carries generation information so it can be rejected after an
invalidation. Cached positive entries also carry tuple visibility information.
An ineligible entry falls back to a source-table read. See the
[technical reference](TECHNICAL.md#transaction-consistency) for the contract.

{% include diagrams/transaction.html id="invalidation-transaction" %}

## Check invalidation across SQL and RESP {#test-with-two-sessions}

Start the [local demo](QUICKSTART.md). Read row 42 over RESP and note its
revision. Then update the row in PostgreSQL and commit:

```sql
BEGIN;
UPDATE public.items SET revision = revision + 1 WHERE id = 42;
COMMIT;
```

The attached-table trigger invalidates the affected cached row at commit. The
next RESP read returns the committed revision. To observe rollback behavior,
begin another update and roll it back; RESP continues to return the last
committed revision.

RESP workers use the configured PostgreSQL role and do not share an application's
SQL transaction or snapshot. A read-your-writes check must use SQL in the same
application transaction, which follows PostgreSQL's normal source-table path.
The RESP endpoint is for separate worker-role reads.

The executable [Node.js test](https://github.com/profundium/pg_local_cache/blob/master/examples/node-postgres/demo.mjs)
checks RESP reads around PostgreSQL writes.

## Cases that deliberately bypass the cache {#cases-that-deliberately-bypass-the-cache}

`REPEATABLE READ`, `SERIALIZABLE`, recovery, parallel execution, and transactions
that have written mapped data use the source-table path. An oversized row may
be returned successfully without being cached. A cache hit rate near zero is
not necessarily a failed installation: check the workload and bypass counters.

When the application needs `SELECT ... FOR UPDATE`, use the ordinary PostgreSQL
operation; RESP `MGET` does not provide row locking or SQL-session semantics.

## Inspect the cause of a miss {#inspect-the-cause-of-a-miss}

Use `local_cache.stats()` and `local_cache.health()` as an administrator. Compare
counter snapshots before and after a controlled test. Cache counters describe
the RESP read path. After intentional DDL, follow the documented
`reconcile_table` or `reconcile_all` procedure instead of assuming a previously
attached mapping still describes the changed table.
