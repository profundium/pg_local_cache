---
layout: doc
lang: en
translation_key: batch-primary-key-lookups
title: Batch PostgreSQL primary-key lookups
seo_title: "Batch PostgreSQL Primary-Key Lookups with ANY and RESP MGET"
description: Replace N+1 primary-key reads with one parameterized PostgreSQL query, preserve input positions when needed, and compare authenticated RESP MGET.
section: Guides
permalink: /docs/batch-primary-key-lookups.html
last_modified_at: "2026-09-16"
---

# Batch PostgreSQL primary-key lookups {#batch-postgresql-primary-key-lookups}

If application code sends one query per ID, network round trips and query
overhead can dominate a small row read. First try one parameterized statement:

```sql
SELECT id, value, revision
FROM public.items
WHERE id = ANY($1::bigint[]);
```

Pass the IDs as an array parameter. Keep the table and columns fixed in the
statement; do not build SQL from ID strings. PostgreSQL evaluates `ANY` by
comparing the left expression with array elements, as described in its
[row and array comparison docs](https://www.postgresql.org/docs/18/functions-comparisons.html#FUNCTIONS-COMPARISONS-ANY-SOME).

## Know the result contract {#know-the-result-contract}

The query above returns a set. It does not promise the input order, and a
duplicate ID normally matches one table row once. Missing IDs produce no row.
An input `NULL` does not match a non-null primary key; a null array or null
elements also follow PostgreSQL's three-valued `ANY` rules. An empty array
returns no rows.

If the caller needs one result for every requested position, preserve the
positions explicitly:

```sql
WITH requested AS (
  SELECT key, position
  FROM unnest($1::bigint[]) WITH ORDINALITY AS input(key, position)
)
SELECT requested.position,
       requested.key,
       CASE WHEN items.id IS NULL THEN NULL
            ELSE row_to_json(items)::text END AS row
FROM requested
LEFT JOIN public.items AS items ON items.id = requested.key
ORDER BY requested.position;
```

`WITH ORDINALITY` retains duplicates and `NULL` positions; the left join
returns a null `row` for a missing key. This is a useful baseline for a client
that needs explicit alignment. See the [node-postgres example](node-postgres.md)
for client-side restoration of the same contract.

## When RESP `MGET` fits {#when-mget-is-the-right-alternative}

For complete rows by primary key, `pg_local_cache` offers an authenticated
RESP2 `MGET` command. Keys use the attached table's database, schema, table,
and primary-key values:

```text
MGET CRUD:app.public.items:{"id":42} CRUD:app.public.items:{"id":7}
```

The response keeps key order and duplicates; missing rows return null. Each
request accepts at most 1,024 keys and returns complete JSON rows. RESP workers
use the configured database role and do not share the caller's SQL transaction
or snapshot. Use SQL `ANY` or the ordinality query when you need a projection,
joins, filters beyond the key, or SQL transaction semantics.

## GraphQL, DataLoader, and N+1 reads {#graphql-dataloader-and-n1-reads}

[DataLoader](https://github.com/graphql/dataloader#batching) combines individual
loads into a batch. Its batch function must return one value per input key in
the same order; the restoration above provides that shape even for missing
rows.

DataLoader's [per-request memoization](https://github.com/graphql/dataloader#caching-per-request)
is separate from PostgreSQL's shared row cache. Create loaders for each
request, and clear affected loader entries after mutations in that request.
PostgreSQL invalidation cannot clear values already stored in a JavaScript
loader. Keep application authorization checks; `pg_local_cache` does not
support RLS tables.

Run the [quickstart](QUICKSTART.md), then compare both read paths in the
[benchmarks](BENCHMARKS.md). The [technical reference](TECHNICAL.md#optional-resp2-endpoint)
defines the RESP API; the [transaction guide](cache-invalidation.md) covers writes.
