---
layout: post
lang: en
translation_key: blog-cache-aside-late-fill
title: "PostgreSQL cache invalidation: the late-fill race"
description: Walk through a cache-aside race where an old read refills a deleted key after commit. Understand publication fencing, snapshots, rollback and request-local caches.
permalink: /blog/cache-aside-late-fill/
date: "2026-09-22"
last_modified_at: "2026-09-22"
topic: correctness
---

# Cache invalidation and the late-fill race {#cache-invalidation-and-the-late-fill-race}

“Delete the cache key after the database update” leaves one timing problem:
another request may already be loading the old value. Deletion removes the
entry that exists now; it does not cancel a result that is still in flight.

## Follow the two requests {#two-requests}

Assume PostgreSQL holds revision 0 and an application uses cache-aside reads.
This sequence can happen even when the writer invalidates after commit:

| Step | Reader A | Writer B |
|---|---|---|
| 1 | Misses the cache and reads revision 0 | |
| 2 | Pauses before storing the result | Updates the row to revision 1 |
| 3 | | Commits and deletes the cache key |
| 4 | Publishes its previously read revision 0 | |
| 5 | A later request reads the stale cached value | |

A TTL can limit how long that value remains eligible. It does not make step 4
correct. Moving deletion before commit creates a different interval in which
a reader can repopulate from the old committed database state. See the
[PostgreSQL and Redis guide](../docs/postgresql-redis-cache.md) for the boundary
between application cache-aside and database-local row caching.

## Validate publication as well as lookup {#publication}

A cache fill needs evidence that its result is still eligible when it is
published. In `pg_local_cache` 2.0, writes fence affected keys or relations;
fills carry generation information, and positive cached entries carry tuple
visibility information. A changed generation can reject an old fill. A read
that cannot safely use an entry falls back to PostgreSQL.

These checks are part of a specific PostgreSQL read path. They do not turn the
optional RESP endpoint into a general-purpose Redis server, and they do not
invalidate values that an application has already copied elsewhere.

## Test both rollback and commit {#test-transactions}

Use the [two-session test](../docs/cache-invalidation.md#test-with-two-sessions)
on a disposable database. Warm the row in one session. In another, update the
row while keeping the transaction open. Check three observations:

1. The writer can read its own change through the source-table path.
2. The other session still sees the committed value while the write is open.
3. After rollback, the original value remains; after commit, a new statement
   under `READ COMMITTED` sees the new revision.

The third condition refers to a new statement. A statement that started earlier
does not have to adopt a newer snapshot midway through execution. The
[transaction contract](../docs/TECHNICAL.md#transaction-consistency) also
describes the modes that bypass the cache. Use ordinary SQL for row locking.

## Check the next cache in the application {#application-cache}

A request-local DataLoader can still hold a value it loaded before a mutation.
Database invalidation cannot remove that JavaScript object. Clear or replace
the affected loader entry after a mutation according to the application's
authorization and result contract. Keep loaders scoped to requests.

The [batching guide](../docs/batch-primary-key-lookups.md#graphql-dataloader-and-n1-reads)
separates request memoization from the shared row cache. When diagnosing a
stale response, trace every storage point from the database snapshot to the
response object. Correctness at one boundary does not clear the others.
