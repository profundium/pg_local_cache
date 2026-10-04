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

演示展示了普通的预备 SQL 回退查询：

```sql
SELECT id::text AS key, row_to_json(items)::text AS row
FROM public.items
WHERE id = ANY($1::bigint[]);
```

示例请求 `42, 7, 42, NULL, 999999`，在 Go 中恢复输入顺序，并为 null 输入和缺失键输出 null。缓存读取请使用 RESP `MGET`；该接口使用配置的 worker 角色，不属于应用的 SQL 事务。

## 比较行结果，而不仅是往返次数 {#compare-rows-not-just-round-trips}

普通的 `WHERE id = ANY($1::bigint[])` 查询不会保留请求位置。普通 SQL 结果需要自行恢复顺序、重复项和缺失行。读取缓存的完整行请使用 RESP `MGET`；[批量查找指南](batch-primary-key-lookups.md)比较两种契约。

基准测试使用持久连接和预备语句。预备 SQL 不会缓存结果行：参阅 [PostgreSQL 缓存指南](postgresql-caching.md)。[统一基准测试](BENCHMARKS.md#run-the-same-comparison-on-every-client)使用相同的键、批次大小、连接数和时长，通过预备 SQL 与 RESP `MGET` 测试 Go 和 Node.js。RESP 不共享调用方的 SQL 事务。

修改读取路径前，请阅读[快速开始](QUICKSTART.md)中的配置流程，并完成[事务检查](cache-invalidation.md)。
