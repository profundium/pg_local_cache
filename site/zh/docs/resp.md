---
layout: doc
lang: zh
translation_key: resp
title: 通过 RESP 连接
description: 使用 redis-cli 或 Node.js 通过 RESP2 读取 PostgreSQL 行，包含认证、客户端配置、可运行示例和清理步骤。
section: RESP
permalink: /zh/docs/resp.html
last_modified_at: '2026-09-16'
---

# 通过 RESP 连接 {#connect-over-resp}

使用 RESP2 客户端的 `MGET` 读取缓存的 PostgreSQL 行。通过 RESP 配置启动[一次性演示](QUICKSTART.md)：

```bash
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml up --build --wait
```

这会在 `127.0.0.1:56379` 启用 RESP。重新创建演示会丢弃其数据。下方令牌是公开的，仅适用于本地演示。

## TLS 与 mTLS 客户端 {#tls-mtls-clients}

连接 loopback 之外的 RESP listener 时建议使用 TLS。RESP 原生 TLS 配置独立于 PostgreSQL 的 `ssl_*`
配置。以下示例使用 RESP2 和双向 TLS (mTLS)：`cache.example` 必须与服务器证书匹配，`./ca.crt`
必须信任该证书，客户端证书必须由 `pg_local_cache.tls_ca_file` 配置的 CA 签发。仅验证服务器的 TLS
连接可省略客户端证书和密钥选项。

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

响应以 JSON 返回行 42。缺失行返回 `nil`。[redis-cli](https://redis.io/docs/latest/develop/tools/cli/) 从 `REDISCLI_AUTH` 读取令牌。

## Node.js {#nodejs}

```bash
npm --prefix examples/node-postgres ci --ignore-scripts
npm --prefix examples/node-postgres run resp
```

示例使用官方 `@redis/client` 包，检查顺序、重复项、空输入位置和缺失行。辅助函数发送请求时省略空键，并在解码后恢复其位置。从应用连接：

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

将 `PGLC_RESP_TOKEN` 设置为服务器令牌。跨请求复用此连接。这些[客户端配置](https://github.com/redis/node-redis/blob/master/docs/client-configuration.md)选择 RESP2，并跳过 Redis 特有的客户端元数据命令。使用仅令牌认证，不要指定用户名或 Redis 数据库编号。

RESP 工作进程为所有客户端使用一个配置的 PostgreSQL 角色。如果需要在 SQL 事务内读取，请使用 [Node.js SQL](node-postgres.md) 或 [Go SQL](go.md)。支持的命令与限制见 [RESP 参考](TECHNICAL.md#optional-resp2-endpoint)。

监听器默认绑定到 loopback。loopback 之外建议使用 TLS；RESP TLS 使用独立配置，与 PostgreSQL 的 `ssl_*`
配置互不影响。TLS 关闭时，loopback 之外的明文 listener 必须显式设置
`pg_local_cache.allow_plaintext_network=on`，且仅限可信网络。演示仅在其容器网络内启用该项。参阅 [TLS 与
mTLS 客户端](#tls-mtls-clients)。 `pg_local_cache.enabled` 是 SIGHUP 紧急开关。每个 RESP
worker 都会在下一个命令边界异步应用重载，且会等当前执行的命令结束。`local_cache.health()` 中的 `cache_enabled`
字段报告调用它的 SQL 会话所见设置；它不表示所有 worker 都已应用该设置。若要在不重启的情况下关闭缓存读取，请执行 `ALTER SYSTEM SET pg_local_cache.enabled = off;` 和 `SELECT pg_reload_conf();`。关闭期间，RESP
会为每次读取直接查询源表。

## 与 SQL 比较 {#compare-with-sql}

[统一基准测试](BENCHMARKS.md#run-the-same-comparison-on-every-client)在 Node.js 和 Go 中使用相同的键与解码结果，运行 RESP `MGET` 与预备 SQL。已发布的 2.x SQL `mget` 测量属于历史数据；该测试路径已在 3.0.0 中移除。更广泛的应用缓存场景，请阅读 [PostgreSQL 与 Redis cache-aside 指南](postgresql-redis-cache.md)。

## 停止演示 {#stop-the-demo}

```bash
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml down
```
