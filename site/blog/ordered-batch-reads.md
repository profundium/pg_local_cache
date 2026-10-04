---
layout: post
lang: en
translation_key: blog-ordered-batch-reads
title: "Batch PostgreSQL reads without losing order or missing keys"
description: Replace N+1 primary-key queries while preserving duplicate IDs, input order, NULL positions and missing rows. Compare ANY, WITH ORDINALITY and RESP MGET.
permalink: /blog/ordered-batch-reads/
date: "2026-09-22"
last_modified_at: "2026-10-04"
topic: application
---

# Batch reads need a result contract {#batch-reads-need-a-result-contract}

Replacing a loop of primary-key queries with one `ANY` query removes round
trips. It can also change the response shape. A caller might ask for
`[42, 7, 42, NULL, -1]` and expect five result positions. SQL set semantics do
not promise that alignment.

> **2026-10-04 release note:** The 2.x SQL row-cache API local_cache.mget(regclass, anyarray) was removed in 3.0.0; use RESP MGET for current whole-row reads.

## A set of rows is not a list of answers {#set-versus-list}

With `WHERE id = ANY($1::bigint[])`, a duplicate ID normally matches its table
row once. A missing ID contributes no row. An input `NULL` does not match a
non-null primary key, and the result has no guaranteed input order. Adding
`ORDER BY id` sorts by key; it still does not reproduce the requested positions.

If consumers need a set, that is fine. If they need a result for every input,
make the positions part of the query or restore them in the application.

## Keep positions explicit in SQL {#explicit-positions}

Start the [local demo](../docs/QUICKSTART.md) and run this in its `psql` session:

```sql
WITH requested AS (
  SELECT key, position
  FROM unnest(ARRAY[42, 7, 42, NULL, -1]::bigint[])
       WITH ORDINALITY AS input(key, position)
)
SELECT requested.position,
       requested.key,
       CASE WHEN items.id IS NULL THEN NULL
            ELSE row_to_json(items)::text END AS row
FROM requested
LEFT JOIN public.items AS items ON items.id = requested.key
ORDER BY requested.position;
```

The ordinality column distinguishes both occurrences of 42. The left join
retains all five positions, including the null input and any absent key.
For a missing key, `row` is SQL `NULL`. In application code, pass the array as a
parameter rather than concatenating IDs into SQL. The
[Node.js helper source](https://github.com/profundium/pg_local_cache/blob/master/examples/node-postgres/queries.mjs)
shows client-side result alignment.

## Compare the whole-row API {#whole-row-api}

Use RESP MGET to read complete rows from attached tables. Encode each primary key in a CRUD:<db>.<schema>.<table>:<json pk> key:

RESP MGET returns a RESP2 array in request order. Duplicate keys retain their positions; a missing row returns nil. RESP keys identify primary-key values, so they do not provide a SQL NULL array position. Use the WITH ORDINALITY SQL path above when every SQL input, including NULL, needs an aligned result. Each command accepts at most 1,024 keys, each JSON row is limited to 65,536 bytes, and the encoded reply to 66,560 bytes. RESP MGET does not replace projections, joins, row locks, or arbitrary query-result caching.

```bash
REDISCLI_AUTH=DemoRespToken_0123456789abcdef0123456789 redis-cli -2 -p 56379 MGET 'CRUD:pglc_demo.public.items:{"id":42}' \
  'CRUD:pglc_demo.public.items:{"id":7}' \
  'CRUD:pglc_demo.public.items:{"id":42}' \
  'CRUD:pglc_demo.public.items:{"id":-1}'
```

## Keep batching bounded and observable {#bounded-batches}

For more than 1,024 keys, split requests explicitly or retain an ordinary SQL
query. Chunking across statements can observe different `READ COMMITTED`
snapshots; choose transaction semantics deliberately. Larger batches also
increase response size and client decoding work, so “fewer queries” alone does
not prove a faster request.

For GraphQL, a DataLoader batch function must return one answer per input key
in the same order. Request-local memoization and PostgreSQL's shared cache are
separate layers; clear affected loader entries after mutations. See the
[full batching guide](../docs/batch-primary-key-lookups.md) and compare latency,
payloads and throughput with the [benchmark runner](../docs/BENCHMARKS.md).
