---
layout: post
lang: en
translation_key: blog-row-cache-inside-postgresql
title: "Cache-aside without the race: a row cache inside PostgreSQL, measured"
description: "pg_local_cache 3.1.0 keeps hot rows in PostgreSQL shared memory, serves them over RESP and never caches a row older than the last commit. Results against Valkey and prepared SQL."
permalink: /blog/row-cache-inside-postgresql/
date: "2026-10-12"
last_modified_at: "2026-10-12"
topic: performance
---

# Cache-aside without the race {#cache-aside-without-the-race}

When primary-key reads get hot, the usual move is to put Redis or Valkey next
to PostgreSQL and use cache-aside: read the cache, on a miss read PostgreSQL
and fill the cache, and delete the key on every write. It works until a read
and a write overlap. A reader that missed can fetch the row just before a
writer commits; the writer's `DEL` arrives first, the reader's fill arrives
second, and the old row stays in the cache until something else removes it.
We walked through this [late-fill race](cache-aside-late-fill.md) earlier.

pg_local_cache moves the cache into PostgreSQL itself, so the cache and the
writes it must follow live in the same transaction system.

## How it stays correct {#how-it-stays-correct}

- Rows of an attached table are kept in bounded shared memory, keyed by primary
  key, and served by background workers over RESP2. Any Redis client can read
  them with `MGET`; the key is `CRUD:<db>.<schema>.<table>:<json primary key>`.
- Row triggers on the table publish an invalidation fence for each written key
  before the transaction becomes visible. Relation-wide changes such as
  `TRUNCATE` publish a fence for the whole table.
- A reader that misses takes a read token first. Its fill is stored only if no
  fence for that key, its table or the whole cache was published in between.
  A fill that raced a write is dropped instead of cached.
- Concurrent misses on one key are coalesced: one reader loads the row and the
  others use its result.
- PostgreSQL remains the source of truth. The cache is transient and can be
  dropped at any time.

## What it costs and what it buys {#results}

These numbers come from 3.1.0 on two 16-vCPU VMs (Xeon Gold 6354, 10 GbE) with
PostgreSQL 16 and a Go/pgx client. The [benchmark page](../docs/BENCHMARKS.md)
has every table, the raw JSONL and the reproduction steps.

**Reads while writing.** 64 readers, one key per read, uniform keys over 60k
rows, 30,000 `UPDATE`/s:

| Stack | Reads/s | Hit ratio | Stale entries |
| --- | --- | --- | --- |
| pg_local_cache | 161,919 | 0.837 | 0 |
| Valkey cache-aside | 42,294 | 0.577 | 0 |
| Prepared SQL | 106,949 | — | — |

With unlimited writers, Valkey cache-aside ended with stale entries (1 in the
mixed run; 2 and 1 in the 120-second probes). pg_local_cache ended every run
with none, including a one-hour soak of 252M `MGET` and 72M `UPDATE`.

**Write overhead.** Throughput relative to plain table writes was 3.6–8.2%
lower for `UPDATE` through pg_local_cache, and 28.9–63.7% lower for `UPDATE`
followed by Valkey `DEL`.

**Read throughput.** With the server pinned to one vCPU pair, pg_local_cache
served 210k reads/s against 180k for single-threaded Valkey; with two pairs,
337k against 266k for Valkey with I/O threads. At four pairs both reached a
client-bound plateau near 360k, with pg_local_cache using 5.2 server vCPU and
Valkey 8.0. Single-threaded Valkey used the least CPU per request. Prepared SQL
was far behind in the single-key tests.

## When it is the wrong tool {#when-not-to-use-it}

- It is not a Redis replacement: no TTLs, pub/sub or data structures, only
  whole-row reads by primary key.
- The hot working set has to fit the configured memory, and a row is limited to
  64 KiB of JSON over RESP.
- It serves one database on one writable primary.
- It needs `shared_preload_libraries` and a restart.

## Try it {#try-it}

pg_local_cache supports PostgreSQL 14–18 and is MIT-licensed. Install it from
the [GitHub releases](https://github.com/profundium/pg_local_cache/releases)
(deb and rpm), [PGXN](https://pgxn.org/dist/pg_local_cache/) or the Pigsty
repositories, then follow the [quick start](../docs/QUICKSTART.md).
