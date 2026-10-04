---
layout: doc
lang: en
translation_key: resp
title: Connect over RESP
description: Read PostgreSQL rows over RESP2 with redis-cli or Node.js. Includes authentication, client settings, runnable examples and cleanup.
section: RESP
permalink: /docs/resp.html
last_modified_at: "2026-10-04"
---

# Connect over RESP {#connect-over-resp}

Use `MGET` to read cached PostgreSQL rows with a RESP2 client.
Start the [disposable demo](QUICKSTART.md) with its RESP configuration:

```bash
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml up --build --wait
```

This enables RESP on `127.0.0.1:56379`. Recreating the demo discards its data.
The token below is public and only for this local demo.

## TLS and mTLS clients {#tls-mtls-clients}

Prefer TLS for RESP access beyond loopback. Native RESP TLS uses settings
dedicated to the extension and is independent of PostgreSQL `ssl_*` settings.
These examples use RESP2 and mutual TLS: `cache.example` must match the server
certificate, `./ca.crt` must trust that certificate, and the client certificate
must chain to the CA configured in `pg_local_cache.tls_ca_file`. For
server-authenticated TLS without mTLS, omit the client certificate and key
options.

**redis-cli**

```bash
export REDISCLI_AUTH="$PGLC_RESP_TOKEN"
redis-cli -2 --tls --cacert ./ca.crt --cert ./client.crt --key ./client.key -h cache.example -p 6380 MGET 'CRUD:app.public.items:{"id":42}'
```

**go-redis**

```go
package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"log"
	"os"

	"github.com/redis/go-redis/v9"
)

func main() {
	caPEM, err := os.ReadFile("./ca.crt")
	if err != nil {
		log.Fatal(err)
	}
	roots := x509.NewCertPool()
	if ok := roots.AppendCertsFromPEM(caPEM); !ok {
		log.Fatal("no CA certificates found")
	}
	clientCert, err := tls.LoadX509KeyPair("./client.crt", "./client.key")
	if err != nil {
		log.Fatal(err)
	}

	client := redis.NewClient(&redis.Options{
		Addr:            "cache.example:6380",
		Password:        os.Getenv("PGLC_RESP_TOKEN"),
		Protocol:        2,
		DisableIdentity: true,
		TLSConfig: &tls.Config{
			RootCAs:      roots,
			MinVersion:   tls.VersionTLS12,
			ServerName:   "cache.example",
			Certificates: []tls.Certificate{clientCert},
		},
	})
	defer client.Close()

	rows, err := client.MGet(context.Background(), `CRUD:app.public.items:{"id":42}`).Result()
	if err != nil {
		log.Fatal(err)
	}
	log.Printf("%v", rows)
}
```

**node-redis**

```js
import { readFileSync } from 'node:fs';
import { createClient } from '@redis/client';

const client = createClient({
  socket: {
    host: 'cache.example',
    port: 6380,
    tls: true,
    servername: 'cache.example',
    ca: readFileSync('./ca.crt'),
    cert: readFileSync('./client.crt'),
    key: readFileSync('./client.key'),
  },
  password: process.env.PGLC_RESP_TOKEN,
  RESP: 2,
  disableClientInfo: true,
});
client.on('error', console.error);
await client.connect();

try {
  const values = await client.mGet(['CRUD:app.public.items:{"id":42}']);
  const rows = values.map(value => value === null ? null : JSON.parse(value));
  console.log(rows);
} finally {
  await client.close();
}
```

## redis-cli {#redis-cli}

```bash
export REDISCLI_AUTH=DemoRespToken_0123456789abcdef0123456789
redis-cli -2 -p 56379 MGET 'CRUD:pglc_demo.public.items:{"id":42}'
```

The response contains row 42 as JSON. Missing rows return `nil`.
[redis-cli](https://redis.io/docs/latest/develop/tools/cli/) reads the token
from `REDISCLI_AUTH`.

## Node.js {#nodejs}

```bash
npm --prefix examples/node-postgres ci --ignore-scripts
npm --prefix examples/node-postgres run resp
```

The example uses the official `@redis/client` package and checks order,
duplicates, null input positions and missing rows. Its helper omits null keys
on the wire and restores their positions after decoding.
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

RESP workers use one configured PostgreSQL role for all clients. For reads
within a SQL transaction, use [Node.js SQL](node-postgres.md) or [Go SQL](go.md).
See the [RESP reference](TECHNICAL.md#optional-resp2-endpoint) for commands and limits.

The listener binds to loopback by default. Prefer TLS beyond loopback; RESP TLS
uses extension-specific settings independent of PostgreSQL `ssl_*` settings.
With TLS off, a non-loopback plaintext listener requires the explicit
`pg_local_cache.allow_plaintext_network=on` opt-in, limited to trusted networks.
The demo enables it only inside its container network. See [TLS and mTLS
clients](#tls-mtls-clients). `pg_local_cache.enabled` is a SIGHUP kill switch.
Each RESP worker applies a reload asynchronously at its next command boundary,
after any command it is executing finishes. `local_cache.health()` reports
`cache_enabled` as seen by the SQL session that calls it; it does not
acknowledge that every worker has applied the setting. To disable cache reads
without a restart, run `ALTER SYSTEM SET pg_local_cache.enabled = off;` and
`SELECT pg_reload_conf();`. While disabled, each read goes directly to the
source table.

## Compare with SQL {#compare-with-sql}

The [common benchmark](BENCHMARKS.md#run-the-same-comparison-on-every-client)
runs RESP `MGET` and prepared SQL with the same keys and decoded results in both
Node.js and Go. Published SQL `mget` measurements from 2.x are historical; that
lane was removed in 3.0.0.
For broader application caching, read the
[PostgreSQL and Redis cache-aside guide](postgresql-redis-cache.md).

## Stop the demo {#stop-the-demo}

```bash
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml down
```
