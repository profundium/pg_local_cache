---
layout: doc
lang: zh
translation_key: node-postgres
title: "使用 node-postgres 批量查找行"
seo_title: 使用 node-postgres 批量查找行 | pg_local_cache
description: "在 Node.js 中使用经过身份验证的 RESP MGET 读取缓存行，使用 node-postgres 执行 SQL 写入和普通查询。"
section: Node.js
permalink: /zh/docs/node-postgres.html
last_modified_at: '2026-09-16'
---

# 使用 node-postgres 批量查找行 {#batch-row-lookups-with-node-postgres}

使用现有 node-postgres 连接或连接池，按主键读取行。

启动[演示](QUICKSTART.md)，安装依赖并运行集成断言：

```bash
npm --prefix examples/node-postgres ci --ignore-scripts
npm --prefix examples/node-postgres run demo
```

## 通过 RESP 读取 {#send-one-parameterized-query}

使用已连接的 `@redis/client` RESP 客户端：

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

RESP `MGET` 按键顺序返回 JSON 编码的行。辅助函数省略 null 输入键并恢复其位置；缺失键返回 null。

在应用代码中固定表名。将 ID 作为查询参数传递，不要拼接 SQL 字符串。参阅 node-postgres 关于[参数和具名预备语句](https://node-postgres.com/features/queries)的文档。

RESP 命令最多接受 1,024 个键。若所有输入均为 null，可运行的辅助函数会直接返回 `[]` 而不发送请求。PostgreSQL `bigint` 和 JSON 数值可能超出 JavaScript 的精确数字范围；请使用无损 JSON 解析器或明确的序列化契约。

## 与现有批量查询比较 {#compare-with-the-existing-batch-query}

基线使用：

```sql
SELECT id::text AS key, row_to_json(i)::text AS row
FROM public.items AS i
WHERE id = ANY($1::bigint[]);
```

`ANY` 不保留输入顺序或重复请求的位置。示例在客户端恢复这些位置，为缺失行填入 null，然后比较结果。

可运行实现位于 [examples/node-postgres](https://github.com/profundium/pg_local_cache/tree/master/examples/node-postgres)。辅助函数接收已有客户端，而不是每次调用都创建连接池。

## 事务与应用边界 {#transactions-and-application-boundaries}

整个事务应使用同一个已获取的客户端。在同一事务中先写后读时，读取使用 PostgreSQL 源表路径。演示通过独立的读写连接验证此行为；详见[缓存失效](cache-invalidation.md)。

## 预备语句与结果缓存 {#prepared-statements-and-result-caching}

具名 node-postgres 查询会在每个连接上复用预备语句，但不会缓存返回行。RESP `MGET` 使用扩展的共享整行缓存，但其 worker 角色和会话状态独立于应用的 SQL 连接。参阅[缓存决策指南](postgresql-caching.md)和[批量查找指南](batch-primary-key-lookups.md)。

RESP2 请使用 [Node.js RESP 示例](resp.md#nodejs)。[已记录的 Node.js 结果](benchmarks-node.md)包括批量读取和并发更新。[统一基准测试](BENCHMARKS.md#run-the-same-comparison-on-every-client)使用相同的预备 SQL 和 RESP 场景运行 Node.js 与 Go。
