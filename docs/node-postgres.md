---
layout: doc
title: Batch row lookups with node-postgres
seo_title: "PostgreSQL mget with Node.js and node-postgres | pg_local_cache"
description: Use pg_local_cache 2.0 from Node.js with a parameterized bigint array. Decode text[] results, preserve order and nulls, and compare with a prepared ANY query.
section: Node.js
permalink: /docs/node-postgres.html
last_modified_at: "2026-09-05"
---

# Batch row lookups with node-postgres

The extension exposes a SQL function. You do not need a new wire protocol or a
custom client library to use its SQL API.

Start the [demo](QUICKSTART.md), install the example's pinned dependency, and
run its integration assertions:

```bash
npm --prefix examples/node-postgres install --ignore-scripts
npm --prefix examples/node-postgres run demo
```

## Send one parameterized query

Given a connected node-postgres client or pool:

```js
const result = await client.query({
  name: 'items-mget',
  text: "SELECT local_cache.mget('public.items'::regclass, $1::bigint[]) AS rows",
  values: [[42, 7, 42, null, 999999]],
});
const rows = result.rows[0].rows.map(row =>
  row === null ? null : JSON.parse(row)
);
```

There is one result record containing a `text[]`. Each non-null element is a
serialized row, so the driver does not automatically decode it as a JSON
object. The returned positions match the input positions. Missing keys and
null inputs both produce `null`.

Keep the table name fixed in application code. The array is a query parameter,
not SQL assembled by joining IDs into a string. See node-postgres documentation
for [parameters and named prepared statements](https://node-postgres.com/features/queries).

The runnable helper rejects batches over 1,024 keys and returns `[]` without a
query for an empty batch. It uses safe integer demo IDs. PostgreSQL `bigint` and
numeric fields in JSON can exceed JavaScript's exact numeric range; choose a
lossless JSON parser or an explicit serialization contract before using such
values. Merely passing a key as a string does not fix precision in the returned
JSON payload.

## Compare with the existing batch query

The baseline uses:

```sql
SELECT id::text AS key, row_to_json(i)::text AS row
FROM public.items AS i
WHERE id = ANY($1::bigint[]);
```

`ANY` does not preserve input order or duplicate requested positions. The
example restores them on the client and supplies null for missing rows before
comparing results. Both that work and JSON parsing are included in the
[benchmark](BENCHMARKS.md).

The implementation and unit tests are in
[examples/node-postgres](https://github.com/profundium/pg_local_cache/tree/master/examples/node-postgres).
The helper takes an existing client rather than creating a pool per call.

## Transactions and application boundaries

Use one acquired client throughout a transaction. `mget` does not change that
rule. Reads after writes in the same transaction use PostgreSQL's source-table
path. The demo checks this with separate reader and writer connections; see
[cache invalidation](cache-invalidation.md).

This example does not patch an ORM, transparently intercept SELECT, provide an
application cache, or replace a connection pool. To decide where to integrate
it, measure a specific repeated whole-row lookup first. An endpoint dominated
by joins or network latency is a different problem.
