---
layout: doc
title: Connect over RESP
description: Read PostgreSQL rows over RESP2 with redis-cli or Node.js. Includes authentication, client settings, runnable examples and cleanup.
section: RESP
permalink: /docs/resp.html
last_modified_at: "2026-09-15"
---

# Connect over RESP

Use `MGET` to read cached PostgreSQL rows with a RESP2 client.
Start the [disposable demo](QUICKSTART.md) with its RESP configuration:

```bash
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml up --build --wait
```

This enables RESP on `127.0.0.1:56379`. Recreating the demo discards its data.
The token below is public and only for this local demo.

## redis-cli

```bash
export REDISCLI_AUTH=DemoRespToken_0123456789abcdef0123456789
redis-cli -2 -p 56379 MGET 'CRUD:pglc_demo.public.items:{"id":42}'
```

The response contains row 42 as JSON. Missing rows return `nil`.
[redis-cli](https://redis.io/docs/latest/develop/tools/cli/) reads the token
from `REDISCLI_AUTH`.

## Node.js

```bash
npm --prefix examples/node-postgres ci --ignore-scripts
npm --prefix examples/node-postgres run resp
```

The example uses the official `@redis/client` package and checks duplicates and missing rows.
To connect from your application:

```js
import { createClient } from '@redis/client';

const client = createClient({
  url: 'redis://127.0.0.1:56379',
  password: process.env.PGLC_RESP_TOKEN,
  RESP: 2,
  disableClientInfo: true,
});
client.on('error', console.error);
await client.connect();

try {
  const values = await client.mGet(['CRUD:pglc_demo.public.items:{"id":42}']);
  const rows = values.map(value => value === null ? null : JSON.parse(value));
  console.log(rows);
} finally {
  await client.close();
}
```

Set `PGLC_RESP_TOKEN` to your server's token. Keep this connection open across
requests. These [client settings](https://github.com/redis/node-redis/blob/master/docs/client-configuration.md)
select RESP2 and skip Redis-specific client metadata commands.
Use token-only authentication, without a username or Redis database number.

RESP workers use the configured PostgreSQL role. For reads within a SQL
transaction, use [Node.js SQL](node-postgres.md) or [Go SQL](go.md).
See the [RESP reference](TECHNICAL.md#optional-resp2-endpoint) for commands and limits.

## Stop the demo

```bash
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml down
```
