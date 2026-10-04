---
layout: doc
lang: en
translation_key: node-postgres
title: Batch row lookups with node-postgres
seo_title: "Batch PostgreSQL Row Lookups with node-postgres"
description: Use authenticated RESP MGET from Node.js for cached row reads and node-postgres for SQL writes and ordinary queries.
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

## Read through RESP {#send-one-parameterized-query}

Given a connected RESP client from `@redis/client`:

```js
const ids = [42, 7, 42, null, 999999];
const wireKeys = ids.filter(id => id !== null).map(id =>
  `CRUD:app.public.items:${JSON.stringify({ id })}`
);
const values = await client.mGet(wireKeys);
let position = 0;
const rows = ids.map(id => {
  if (id === null) return null;
  const value = values[position++];
  return value === null ? null : JSON.parse(value);
});
```

RESP `MGET` returns JSON-encoded rows in key order. The helper omits null input
keys and restores their positions; missing keys return null.

Keep the table name fixed in application code. Pass IDs as query parameters,
not SQL assembled from strings. See node-postgres documentation
for [parameters and named prepared statements](https://node-postgres.com/features/queries).

The RESP command accepts at most 1,024 keys. The runnable helper returns `[]`
without a request when all inputs are null. PostgreSQL `bigint` and numeric
fields in JSON can exceed JavaScript's exact numeric range; use a lossless JSON
parser or an explicit serialization contract for such values.

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
It does not cache returned rows. RESP `MGET` uses the extension's shared
whole-row cache, but its worker role and session state are separate from the
application's SQL connection. See the [caching decision guide](postgresql-caching.md)
and [batch lookup guide](batch-primary-key-lookups.md).

For RESP2, use the [Node.js RESP example](resp.md#nodejs).
[Recorded Node.js results](benchmarks-node.md) include batch reads and concurrent updates.
The [common benchmark](BENCHMARKS.md#run-the-same-comparison-on-every-client)
runs Node.js and Go through the same prepared SQL and RESP scenarios.
