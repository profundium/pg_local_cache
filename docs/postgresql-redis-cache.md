---
layout: doc
lang: en
translation_key: postgresql-redis-cache
title: PostgreSQL and Redis cache-aside
seo_title: "PostgreSQL and Redis Cache-Aside: Invalidation and Race Conditions"
description: Use PostgreSQL as the source of truth with a Redis cache-aside path, understand stale-read races, and see where pg_local_cache fits.
section: Guides
permalink: /docs/postgresql-redis-cache.html
last_modified_at: "2026-09-16"
---

# PostgreSQL and Redis cache-aside {#postgresql-and-redis-cache-aside}

Redis cache-aside puts the application between a read and its authoritative
store. On a miss, read PostgreSQL, return that value, and write it to Redis;
on a write, update PostgreSQL and invalidate the corresponding Redis key. The
[Redis pattern guide](https://redis.io/docs/latest/develop/use-cases/cache-aside/)
describes this flow, but it does not make an application cache transactionally
consistent with PostgreSQL.

For a row keyed by `public.items.id`, the outline is:

```text
GET item:42
miss -> SELECT * FROM public.items WHERE id = $1
     -> SET item:42 <serialized row> EX <ttl>
write -> UPDATE public.items ...
      -> COMMIT
      -> DEL item:42
```

Use parameterized SQL and a key namespace. A TTL limits how long a stored value
remains in Redis; it does not prove freshness relative to a PostgreSQL commit.
Explicit deletion handles ordinary writes but does not remove every race.

## The invalidation race {#the-invalidation-race}

Consider two requests. Reader R1 misses Redis and reads the old row from
PostgreSQL. Writer W commits a new row and deletes `item:42`. R1 then resumes
and stores its old value in Redis. The next reader sees stale data until that
key expires or another write deletes it.

Possible mitigations include deleting again after a loader finishes, storing a
database version and rejecting older values, serializing loads per key, or
publishing committed changes through an outbox or CDC consumer. Each adds
coordination and failure cases. The
[cache invalidation guide](cache-invalidation.md) demonstrates the analogous
late-fill problem inside PostgreSQL.

## Where pg_local_cache fits {#where-pg_local_cache-fits}

`pg_local_cache` is a narrower PostgreSQL-local option for complete rows keyed
by primary key. `local_cache.mget` is explicit; a normal `SELECT` and an
arbitrary query shape never read the cache. Attached-table triggers fence
affected keys or relations on the database write path, and eligible reads can
fall back to PostgreSQL when transaction or snapshot rules disallow a cache
hit. Start with the [batch lookup guide](batch-primary-key-lookups.md) and
[technical contract](TECHNICAL.md).

This extension does not provide general Redis compatibility, Redis TTLs, or a
distributed application-cache protocol. Its optional RESP2 endpoint exposes a
limited authenticated command set over the same mappings and has its own
security model; it has no TLS. Use it when PostgreSQL-local transaction-aware
whole-row reads are the problem. Use Redis when several application instances
need shared objects, TTL-based freshness, or Redis data structures. Combining
both requires separate keys, invalidation, and metrics for each layer.

Run the [quickstart](QUICKSTART.md), compare against the ordinary client query
in the [node-postgres example](node-postgres.md), and inspect the separate
SQL and RESP counters. The [caching decision guide](postgresql-caching.md)
lists the other PostgreSQL options.
