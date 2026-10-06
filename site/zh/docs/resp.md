---
layout: doc
lang: zh
translation_key: resp
title: RESP 客户端
description: 使用 RESP2、MGET、令牌认证和原生 TLS，将 Redis 兼容客户端连接到 pg_local_cache。
section: RESP
permalink: /zh/docs/resp.html
redirect_from:
  - /zh/docs/go.html
  - /zh/docs/node-postgres.html
last_modified_at: "2026-10-04"
---

# RESP 客户端 {#resp-clients}

连接 Redis 兼容客户端，通过主键读取完整的 PostgreSQL 行。本页介绍键编码、身份验证、响应行为、错误和 TLS。

<a id="connect-over-resp"></a>

## 键与响应约定 {#key-and-response-contract}

RESP 键用于标识一个已附加的表和一个主键对象：

```text
CRUD:<db>.<schema>.<table>:<json pk>
CRUD:app.public.items:{"id":42}
CRUD:app.public.orders:{"tenant_id":7,"id":42}
```

`MGET key [key ...]` 按请求顺序返回 RESP2 数组。重复键保留各自位置。存在的行以 JSON bulk string 返回，不存在的行为 nil 元素。

| 限制 | 行为 |
|---|---|
| 每条命令最多 1,024 个键 | 在请求字节未超限时，1,025 个键的命令返回 `ERR MGET accepts at most 1024 keys`；达到 1,026 个键或更多时，解析器返回 `ERR invalid argument count`。 |
| 每个编码请求最多 65,536 字节 | 若输入缓冲区在完整请求解析前已填满，服务器会关闭连接。 |
| 每行 JSON 最大 65,536 字节 | 超大行无法通过 RESP 返回。 |
| 编码后的 MGET 响应最大 66,560 字节 | 聚合响应超出时返回 `ERR response exceeds limit`。 |

批次大小同时受键数量、编码请求字节数和响应字节数限制。每个编码请求请控制在 65,536 字节以内；键或行较大、接近任一字节上限时请拆分批次。

## AUTH {#auth}

发送其他命令前，先发送 `AUTH <token>`。客户端也可以发送 `AUTH <username> <token>`；用户名必须与 `pg_local_cache.role` 一致。所有 RESP 客户端共用该令牌，它不会为每个客户端选择不同的 PostgreSQL 权限。将令牌保存在权限为 `0400` 或 `0600` 的 `pg_local_cache.auth_token_file` 中；内联 `auth_token` 仅用于开发。非 loopback 监听器要求令牌至少为 32 字节。

## TLS {#tls}

原生 RESP TLS 使用 `pg_local_cache.tls_*` 设置，与 PostgreSQL 的 SQL TLS 分开。它要求 PostgreSQL 使用 OpenSSL 构建，并配置服务器证书和私钥。设置 `tls_ca_file` 后还会要求并验证客户端证书（mTLS）。如果只需验证服务器，请省略客户端证书选项。默认最低版本为 TLS 1.2。

监听器默认绑定到 loopback。关闭 TLS 时，非 loopback 明文监听器必须设置 `pg_local_cache.allow_plaintext_network=on`。监听器和安全设置请参阅[技术参考](TECHNICAL.md#optional-resp2-endpoint)。

## redis-cli {#redis-cli}

将 `PGLC_RESP_TOKEN` 设置为已配置的令牌。本地快速入门还提供了一个演示令牌。

```sh
export REDISCLI_AUTH="$PGLC_RESP_TOKEN"
redis-cli -2 -h 127.0.0.1 -p 56379 MGET 'CRUD:pglc_demo.public.items:{"id":42}'
redis-cli -2 --tls --cacert ./ca.crt --cert ./client.crt --key ./client.key -h cache.example -p 6380 MGET 'CRUD:app.public.items:{"id":42}'
```

## Go {#go}

安装 `github.com/redis/go-redis/v9`。设置 `PGLC_RESP_TOKEN`；TLS 示例使用 CA 和客户端证书文件。

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

TLS 变体：

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

使用 `npm install redis@^4` 安装。Redis v4 默认使用 RESP2。

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

TLS 变体：

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

使用 `python -m pip install redis` 安装。

```python
import os
import redis

client = redis.Redis(host="127.0.0.1", port=56379,
                     password=os.environ["PGLC_RESP_TOKEN"],
                     decode_responses=True, protocol=2)
print(client.execute_command('MGET', 'CRUD:pglc_demo.public.items:{"id":42}'))
client.close()
```

TLS 变体：

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

## 错误与边界 {#errors}

| 响应 | 原因 |
|---|---|
| `NOAUTH Authentication required` | 请先对连接进行身份验证。 |
| `WRONGPASS invalid authentication token` | 令牌或可选用户名不正确。 |
| `ERR MGET accepts at most 1024 keys` | 命令包含 1,025 个键；请拆分批次。 |
| `ERR invalid argument count` | 解析器因参数过多拒绝命令；MGET 达到 1,026 个键或更多时会超过参数上限。 |
| `ERR busy: relation locked, retry` | 关系锁定期间，延迟未命中队列已满；请重试请求。 |
| `ERR response exceeds limit` | 请缩小批次或行负载。 |
| `ERR PostgreSQL: …` | PostgreSQL 或 SPI 错误，包括 PostgreSQL 语句或锁超时及取消。 |
| `ERR MGET deadline exceeded` | 显式的 MGET 总期限已到，包括在队列中等待关系锁释放时；这与 PostgreSQL 语句或锁超时不同。 |
| `ERR KVik key targets a different database` | 使用 RESP 端点配置的数据库。 |
| `ERR unknown KVik table mapping` | 检查键中的模式和表是否已映射。 |
| `ERR key must use CRUD:database.schema.table:{primary-key-json}` | 使用完整的 CRUD 键格式。 |
| `ERR KVik key must end with a primary-key JSON object` | 在表范围后提供主键 JSON 对象。 |
| `ERR invalid CRUD cache scope` | 仅适用于 `INVALIDATE`：提供的范围不是受支持的 CRUD 范围。 |

RESP 使用独立连接，不参与调用方的 SQL 事务。SQL 事务请直接使用 PostgreSQL；参见[失效指南](cache-invalidation.md)。

## 清理演示环境 {#stop-the-demo}

```sh
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml down
```

2.x 中的 `local_cache.mget(regclass, anyarray)` 已在 3.0.0 中移除；请参阅[升级指南](UPGRADING.md)。
