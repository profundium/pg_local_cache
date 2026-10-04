---
layout: doc
lang: en
translation_key: cache-invalidation
title: Transaction-aware cache invalidation in PostgreSQL
seo_title: "PostgreSQL Cache Invalidation: Commit and Rollback | pg_local_cache"
description: See how PostgreSQL triggers fence RESP row reads across commit, rollback, and concurrent cache fills.
section: Cache invalidation
permalink: /docs/cache-invalidation.html
last_modified_at: "2026-10-04"
---

# Transaction-aware cache invalidation in PostgreSQL {#transaction-aware-cache-invalidation-in-postgresql}

This guide explains how attached-table triggers prevent a committed PostgreSQL write from being followed by a stale RESP cache hit.

![Write invalidation: committed updates publish a fence; rollback before fence publication leaves the entry valid.](diagrams/write-invalidation.svg)

A trigger records dirty keys or a dirty relation inside the writer transaction. At commit, the extension publishes invalidation fences and advances generations. A fill that started before the fence cannot publish stale data. Rollback before fence publication discards the transaction's dirty state, so prior entries remain valid. If the transaction aborts after publication, invalidation is not undone and affected entries remain invalid.

See the [technical consistency reference](TECHNICAL.md#transaction-consistency) for the full read-path contract.

## Check invalidation across SQL and RESP {#test-with-two-sessions}

Start the [local demo](QUICKSTART.md), then read the same key from RESP and PostgreSQL:

```text
MGET CRUD:pglc_demo.public.items:{"id":42}
```

In a separate SQL session, update and commit:

```sql
BEGIN;
UPDATE public.items SET revision = revision + 1 WHERE id = 42;
COMMIT;
```

The next RESP command returns the committed revision. If the writer rolls back instead, RESP continues to return the last committed revision.

RESP uses the configured PostgreSQL role in an independent short transaction. It does not share the application's role, transaction, or snapshot. Use SQL in the application transaction for read-your-writes and `SELECT ... FOR UPDATE`.

## Cases that bypass the cache {#cases-that-deliberately-bypass-the-cache}

With `pg_local_cache.enabled` off, RESP `MGET` skips cache lookup and fill and reads the source table. An active dirty fence for a key, relation, or globally also blocks cache hits and fills, so reads use the source table. RESP workers start after recovery finishes; recovery is not a separate bypass condition. A row too large for one cache entry may still be returned from PostgreSQL if its JSON fits the RESP value limit, without being stored.

## Inspect the cause of a miss {#inspect-the-cause-of-a-miss}

Compare `local_cache.stats()` and `local_cache.health()` before and after a controlled workload. Check bypass, miss, invalidation, and mapping-reload counters. See the [technical metrics list](TECHNICAL.md#health-and-monitoring).
