---
layout: doc
lang: en
translation_key: go
title: Batch row lookups with Go and pgx
seo_title: "Batch PostgreSQL Row Lookups with Go and pgx"
description: Use pg_local_cache from Go with pgx, parameterized keys, and decoded JSON rows.
section: Go
permalink: /docs/go.html
last_modified_at: "2026-09-16"
---

# Batch row lookups with Go and pgx {#batch-row-lookups-with-go-and-pgx}

Start the [disposable database](QUICKSTART.md) first, then run:

```bash
go -C examples/go-pgx run ./demo
```

It uses `127.0.0.1:55432`, database `pglc_demo`, and the `demo` / `demo-only`
credentials. Set `PGLC_DEMO_PORT` when the quickstart uses another port.

The demo shows an ordinary prepared SQL fallback query:

```sql
SELECT id::text AS key, row_to_json(items)::text AS row
FROM public.items
WHERE id = ANY($1::bigint[]);
```

The example requests `42, 7, 42, NULL, 999999`, restores input order in Go,
and prints nulls for the null input and missing key. For cached reads, use
RESP `MGET`; the endpoint uses the configured worker role and is not part of an
application's SQL transaction.

## Compare rows, not just round trips {#compare-rows-not-just-round-trips}

An ordinary `WHERE id = ANY($1::bigint[])` query does not preserve requested
positions. Restore input order, duplicates and missing rows for ordinary SQL
results. For cached complete-row reads, use RESP `MGET`; the
[batch lookup guide](batch-primary-key-lookups.md) compares the contracts.

The benchmark uses persistent connections and prepared statements. Preparing
SQL does not cache its result rows: see the
[PostgreSQL caching guide](postgresql-caching.md). The
[common benchmark](BENCHMARKS.md#run-the-same-comparison-on-every-client) tests
Go and Node.js with the same keys, batch sizes, connection counts and duration
through prepared SQL and RESP `MGET`. RESP does not share a caller's SQL
transaction.

See the [quickstart](QUICKSTART.md) for setup and
[transaction checks](cache-invalidation.md) before adapting the read path.
