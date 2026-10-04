---
layout: doc
lang: en
translation_key: resp
title: RESP clients
description: Connect Redis-compatible clients to pg_local_cache with RESP2 MGET, token authentication, and native TLS.
section: RESP
permalink: /docs/resp.html
redirect_from:
  - /docs/go.html
  - /docs/node-postgres.html
last_modified_at: "2026-10-04"
---

# RESP clients {#resp-clients}

Connect Redis-compatible clients to read complete PostgreSQL rows by primary key. This page covers key encoding, authentication, response behavior, errors, and TLS.

<a id="connect-over-resp"></a>

## Key and response contract {#key-and-response-contract}

RESP keys identify an attached table and a primary-key object:

```text
CRUD:<db>.<schema>.<table>:<json pk>
CRUD:app.public.items:{"id":42}
CRUD:app.public.orders:{"tenant_id":7,"id":42}
```

`MGET key [key ...]` returns a RESP2 array in request order. Duplicate keys keep their positions. Existing rows are JSON bulk strings; missing rows are nil elements.

| Limit | Behavior |
|---|---|
| 1,024 keys per command | Larger batches return `ERR MGET accepts at most 1024 keys`. |
| 65,536 bytes per JSON row | A larger row cannot be returned over RESP. |
| 66,560 bytes per encoded MGET reply | Larger aggregate replies return `ERR response exceeds limit`. |

Batch size is bounded by both key count and reply bytes. Split batches when a large row can approach the response limit.

## AUTH {#auth}

Send `AUTH <token>` before other commands. Clients may also send `AUTH <username> <token>`; username must match `pg_local_cache.role`. The token is shared across RESP clients and does not select PostgreSQL privileges per client. Store it in `pg_local_cache.auth_token_file` with mode `0400` or `0600`; reserve inline `auth_token` for development. Non-loopback listeners require a token of at least 32 bytes.

## TLS {#tls}

Native RESP TLS uses `pg_local_cache.tls_*` settings and is separate from PostgreSQL's SQL TLS. TLS requires an OpenSSL-enabled PostgreSQL build, a server certificate, and a private key. Setting `tls_ca_file` also requires and verifies client certificates (mTLS). Omit client certificate options for server-authenticated TLS. The default minimum is TLS 1.2.

The listener binds to loopback by default. With TLS off, a non-loopback plaintext listener requires `pg_local_cache.allow_plaintext_network=on`. Use the [technical reference](TECHNICAL.md#optional-resp2-endpoint) for listener and security settings.

## redis-cli {#redis-cli}

Set `PGLC_RESP_TOKEN` to the configured token. The local quickstart also provides a demo token.

```sh
export REDISCLI_AUTH="$PGLC_RESP_TOKEN"
redis-cli -2 -h 127.0.0.1 -p 56379 MGET 'CRUD:pglc_demo.public.items:{"id":42}'
redis-cli -2 --tls --cacert ./ca.crt --cert ./client.crt --key ./client.key -h cache.example -p 6380 MGET 'CRUD:app.public.items:{"id":42}'
```

## Go {#go}

Install `github.com/redis/go-redis/v9`. Set `PGLC_RESP_TOKEN`; the TLS form uses CA and client certificate files.

```go
package main

import (
	"context"
	"fmt"
	"os"

	"github.com/redis/go-redis/v9"
)

func main() {
	client := redis.NewClient(&redis.Options{
		Addr: "127.0.0.1:56379", Password: os.Getenv("PGLC_RESP_TOKEN"), Protocol: 2,
	})
	defer client.Close()
	rows, err := client.MGet(context.Background(), `CRUD:pglc_demo.public.items:{"id":42}`).Result()
	if err != nil {
		panic(err)
	}
	fmt.Printf("%v\n", rows)
}
```

TLS variant:

```go
package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"os"

	"github.com/redis/go-redis/v9"
)

func main() {
	ca, err := os.ReadFile("./ca.crt")
	if err != nil { panic(err) }
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(ca) { panic("invalid CA") }
	cert, err := tls.LoadX509KeyPair("./client.crt", "./client.key")
	if err != nil { panic(err) }
	client := redis.NewClient(&redis.Options{
		Addr: "cache.example:6380", Password: os.Getenv("PGLC_RESP_TOKEN"), Protocol: 2,
		TLSConfig: &tls.Config{MinVersion: tls.VersionTLS12, ServerName: "cache.example", RootCAs: roots, Certificates: []tls.Certificate{cert}},
	})
	defer client.Close()
	rows, err := client.MGet(context.Background(), `CRUD:app.public.items:{"id":42}`).Result()
	if err != nil { panic(err) }
	fmt.Printf("%v\n", rows)
}
```

## Node.js (redis v4) {#nodejs}

Install with `npm install redis@^4`. Redis v4 uses RESP2 by default.

```js
import { createClient } from 'redis';

const client = createClient({
  socket: { host: '127.0.0.1', port: 56379 },
  password: process.env.PGLC_RESP_TOKEN,
});
client.on('error', console.error);
await client.connect();
try {
  console.log(await client.mGet(['CRUD:pglc_demo.public.items:{"id":42}']));
} finally {
  await client.quit();
}
```

TLS variant:

```js
import { readFileSync } from 'node:fs';
import { createClient } from 'redis';

const client = createClient({
  socket: {
    host: 'cache.example', port: 6380, tls: true, servername: 'cache.example',
    ca: [readFileSync('./ca.crt')],
    cert: readFileSync('./client.crt'), key: readFileSync('./client.key'),
  },
  password: process.env.PGLC_RESP_TOKEN,
});
client.on('error', console.error);
await client.connect();
try {
  console.log(await client.mGet(['CRUD:app.public.items:{"id":42}']));
} finally {
  await client.quit();
}
```

## Python (redis-py) {#python}

Install with `python -m pip install redis`.

```python
import os
import redis

client = redis.Redis(host="127.0.0.1", port=56379,
                     password=os.environ["PGLC_RESP_TOKEN"],
                     decode_responses=True, protocol=2)
print(client.execute_command('MGET', 'CRUD:pglc_demo.public.items:{"id":42}'))
client.close()
```

TLS variant:

```python
import os
import redis

client = redis.Redis(host="cache.example", port=6380,
                     password=os.environ["PGLC_RESP_TOKEN"],
                     decode_responses=True, protocol=2, ssl=True,
                     ssl_ca_certs="./ca.crt", ssl_certfile="./client.crt",
                     ssl_keyfile="./client.key", ssl_cert_reqs="required",
                     ssl_check_hostname=True)
print(client.execute_command('MGET', 'CRUD:app.public.items:{"id":42}'))
client.close()
```

## Errors and boundaries {#errors}

| Reply | Cause |
|---|---|
| `NOAUTH Authentication required` | Authenticate the connection first. |
| `WRONGPASS invalid authentication token` | Token or optional username is incorrect. |
| `ERR MGET accepts at most 1024 keys` | Split the batch. |
| `ERR response exceeds limit` | Reduce batch size or row payload size. |
| `ERR MGET deadline exceeded` | Source reads and same-key waits exceeded the command deadline. |
| `ERR KVik key targets a different database` | Use the database configured for the RESP endpoint. |
| `ERR unknown KVik table mapping` | Check that the key names an attached schema and table. |
| `ERR key must use CRUD:database.schema.table:{primary-key-json}` | Use the complete CRUD key format. |
| `ERR KVik key must end with a primary-key JSON object` | End the table scope with a primary-key JSON object. |
| `ERR invalid CRUD cache scope` | `INVALIDATE` only: the supplied scope is not a supported CRUD scope. |

RESP is independent of the caller's SQL connection, role, transaction, and snapshot. For SQL transactions, use PostgreSQL directly; see the [invalidation guide](cache-invalidation.md).

## Demo cleanup {#stop-the-demo}

```sh
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml down
```

2.x SQL `local_cache.mget(regclass, anyarray)` was removed in 3.0.0; see the [upgrade guide](UPGRADING.md).
