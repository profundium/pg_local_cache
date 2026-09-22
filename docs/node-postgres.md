---
layout: doc
lang: en
translation_key: node-postgres
title: Batch row lookups with node-postgres
seo_title: "Batch PostgreSQL Row Lookups with node-postgres"
description: Use pg_local_cache 2.0 from Node.js with a parameterized bigint array and JSON transport. Preserve order and nulls, and compare with a prepared ANY query.
section: Node.js
permalink: /docs/node-postgres.html
last_modified_at: "2026-09-16"
---

# Batch row lookups with node-postgres {#batch-row-lookups-with-node-postgres}

Read rows by primary key using your existing node-postgres connection or pool.

Start the [demo](QUICKSTART.md), install dependencies, and run its integration
assertions:

```bash
npm --prefix examples/node-postgres ci --ignore-scripts
npm --prefix examples/node-postgres run demo
```

## Send one parameterized query {#send-one-parameterized-query}

Given a connected node-postgres client or pool:

```js
const result = await client.query({
  name: 'items-mget',
  text: "SELECT array_to_json(local_cache.mget('public.items'::regclass, $1::bigint[])) AS rows",
  values: [[42, 7, 42, null, 999999]],
});
const rows = result.rows[0].rows.map(row =>
  row === null ? null : JSON.parse(row)
);
```

`mget` returns `text[]`. `array_to_json` sends the outer array as JSON, so
node-postgres applies its JSON decoder. Each non-null element is a serialized
row and needs `JSON.parse`; positions match the input positions, and missing
keys or null inputs produce `null`.

Keep the table name fixed in application code. Pass IDs as query parameters,
not SQL assembled from strings. See node-postgres documentation
for [parameters and named prepared statements](https://node-postgres.com/features/queries).

The runnable helper rejects batches over 1,024 keys and returns `[]` without a
query for an empty batch. It uses safe integer demo IDs. PostgreSQL `bigint` and
numeric fields in JSON can exceed JavaScript's exact numeric range; use a
lossless JSON parser or an explicit serialization contract for such values.

## Compare with the existing batch query {#compare-with-the-existing-batch-query}

The baseline uses:

```sql
SELECT id::text AS key, row_to_json(i)::text AS row
FROM public.items AS i
WHERE id = ANY($1::bigint[]);
```

`ANY` does not preserve input order or duplicate requested positions. The
example restores them on the client and supplies null for missing rows before
comparing results.

The runnable implementation is in
[examples/node-postgres](https://github.com/profundium/pg_local_cache/tree/master/examples/node-postgres).
The helper takes an existing client rather than creating a pool per call.

## Transactions and application boundaries {#transactions-and-application-boundaries}

Use one acquired client throughout a transaction. Reads after writes in the same
transaction use PostgreSQL's source-table path. The demo checks this with
separate reader and writer connections; see
[cache invalidation](cache-invalidation.md).

## Prepared statements and result caching {#prepared-statements-and-result-caching}

A named node-postgres query reuses a prepared statement on each connection.
It does not cache returned rows. `local_cache.mget` adds a separate shared
whole-row cache inside PostgreSQL; the client still sends a query and decodes
its result. See the [caching decision guide](postgresql-caching.md) to compare
the layers and the [batch lookup guide](batch-primary-key-lookups.md) for a
SQL-only alternative that preserves requested positions.

For RESP2, use the [Node.js RESP example](resp.md#nodejs).
[Recorded Node.js results](benchmarks-node.md) include batch reads and concurrent updates.
The [common benchmark](BENCHMARKS.md#run-the-same-comparison-on-every-client)
runs Node.js and Go through the same SQL and RESP scenarios.
