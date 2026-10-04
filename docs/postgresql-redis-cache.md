---
layout: doc
lang: en
translation_key: postgresql-redis-cache
title: PostgreSQL and Redis cache-aside
seo_title: "PostgreSQL and Redis Cache-Aside: Invalidation and Race Conditions"
description: Compare application-managed Redis cache-aside with pg_local_cache RESP reads, including stale-fill races and write invalidation.
section: Guides
permalink: /docs/postgresql-redis-cache.html
last_modified_at: "2026-10-04"
---

# PostgreSQL and Redis cache-aside {#postgresql-and-redis-cache-aside}

This guide explains Redis cache-aside with PostgreSQL as the source of truth, its stale-fill race, and how `pg_local_cache` handles attached-row invalidation.

On a Redis cache miss, an application reads PostgreSQL, returns the row, and stores it under an application key. On a write, it commits PostgreSQL data and deletes the Redis key. A TTL bounds retention; it does not prove freshness. See the [Redis cache-aside guide](https://redis.io/docs/latest/develop/use-cases/cache-aside/).

## The invalidation race {#the-invalidation-race}

A reader can load an old PostgreSQL row, pause, then store it after a writer commits and deletes the key. The next reader sees stale data until expiry or another deletion.

Mitigations include rejecting fills with an old database version, serializing loads per key, or publishing committed changes through an outbox or CDC consumer. Each adds coordination. See [transaction-aware invalidation](cache-invalidation.md) for the corresponding late-fill boundary inside PostgreSQL.

## Where pg_local_cache fits {#where-pg_local_cache-fits}

`pg_local_cache` stores complete rows by primary key in bounded PostgreSQL shared memory. Applications request rows with authenticated RESP2 `MGET`; ordinary SQL and arbitrary query results do not use this cache. Attached-table triggers fence writes, and reads use PostgreSQL when cache eligibility checks fail.

Unlike Redis cache-aside, this path uses the database write path for invalidation and does not use TTLs or general Redis data structures. RESP workers use the configured PostgreSQL role in independent short transactions. Use [RESP clients](resp.md) and the [technical reference](TECHNICAL.md) for connection and security details.

Use Redis for shared application objects, TTL-managed freshness, or Redis data structures. Use `pg_local_cache` for repeated whole-row reads from one PostgreSQL database. Combining the layers requires separate keys, invalidation, and monitoring.
