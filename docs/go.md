---
layout: doc
title: Batch row lookups with Go and pgx
seo_title: "Batch PostgreSQL Row Lookups with Go and pgx"
description: Use pg_local_cache from Go with pgx, parameterized keys, and decoded JSON rows.
section: Go
permalink: /docs/go.html
last_modified_at: "2026-09-15"
---

# Batch row lookups with Go and pgx

Start the [disposable database](QUICKSTART.md) first, then run:

```bash
go -C examples/go-pgx run ./demo
```

It uses `127.0.0.1:55432`, database `pglc_demo`, and the `demo` / `demo-only`
credentials. Set `PGLC_DEMO_PORT` when the quickstart uses another port.

The demo sends keys as a query parameter:

```sql
SELECT local_cache.mget('public.items'::regclass, $1::bigint[]);
```

`mget` returns `text[]`; each non-null element is a JSON row. The example
requests `42, 7, 42, NULL, 999999` and prints rows in input order. Duplicate
`42` stays in both positions; the null input and missing `999999` produce null
elements.

See the [quickstart](QUICKSTART.md) for setup and [benchmarks](BENCHMARKS.md)
for measurement methodology.
