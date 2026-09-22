---
layout: doc
lang: zh
translation_key: go
title: 使用 Go 与 pgx 批量查找行
seo_title: 使用 Go 与 pgx 批量查找行 | pg_local_cache
description: 通过 pgx、参数化键和解码后的 JSON 行，在 Go 中使用 pg_local_cache。
section: Go
permalink: /zh/docs/go.html
last_modified_at: '2026-09-16'
---

# 使用 Go 与 pgx 批量查找行 {#batch-row-lookups-with-go-and-pgx}

先启动[一次性数据库](QUICKSTART.md)，然后运行：

```bash
go -C examples/go-pgx run ./demo
```

示例使用 `127.0.0.1:55432`、数据库 `pglc_demo`，以及 `demo` / `demo-only` 凭据。如果快速开始使用其他端口，请设置 `PGLC_DEMO_PORT`。

演示将键作为查询参数发送：

```sql
SELECT local_cache.mget('public.items'::regclass, $1::bigint[]);
```

`mget` 返回 `text[]`；每个非空元素都是 JSON 行。示例请求 `42, 7, 42, NULL, 999999`，并按输入顺序输出结果。重复的 `42` 保留在两个位置；空输入与不存在的 `999999` 产生空元素。

## 比较行结果，而不仅是往返次数 {#compare-rows-not-just-round-trips}

普通 `WHERE id = ANY($1::bigint[])` 查询不保留请求位置。与 `mget` 比较前，应恢复输入顺序、重复项和缺失行；[批量查找指南](batch-primary-key-lookups.md)介绍了客户端和 SQL 两种方案。

基准测试使用持久连接与预备语句。预备 SQL 不会缓存结果行：参阅 [PostgreSQL 缓存指南](postgresql-caching.md)。[统一基准测试](BENCHMARKS.md#run-the-same-comparison-on-every-client)通过 SQL 与 RESP，以相同键、批次大小、连接数和时长测试 Go 与 Node.js。RESP 不共享调用者的 SQL 事务。

修改读取路径前，请阅读[快速开始](QUICKSTART.md)中的配置流程，并完成[事务检查](cache-invalidation.md)。
