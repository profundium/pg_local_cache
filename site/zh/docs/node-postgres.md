---
layout: doc
lang: zh
translation_key: node-postgres
title: 使用 node-postgres 批量查找行
seo_title: 使用 node-postgres 批量查找行 | pg_local_cache
description: 通过参数化 bigint 数组和 JSON 传输在 Node.js 中使用 pg_local_cache 2.0，保留顺序与空值，并与预备 ANY 查询比较。
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

## 发送一个参数化查询 {#send-one-parameterized-query}

对于已经连接的 node-postgres 客户端或连接池：

```js
const result = await client.query({
  name: 'items-mget',
  text: "SELECT array_to_json(local_cache.mget('public.items'::regclass, $1::bigint[])) AS rows",
  values: [[42, 7, 42, null, 999999]],
});
const rows = result.rows[0].rows.map(row =>
  row === null ? null : JSON.parse(row)
);
```

`mget` 返回 `text[]`。`array_to_json` 将外层数组作为 JSON 发送，node-postgres 会调用其 JSON 解码器。每个非空元素都是序列化的行，需要 `JSON.parse`；位置与输入一一对应，缺失键或空输入产生 `null`。

在应用代码中固定表名。将 ID 作为查询参数传入，不要拼接 SQL 字符串。参阅 node-postgres 的[参数与具名预备语句](https://node-postgres.com/features/queries)文档。

可运行的辅助函数拒绝超过 1,024 个键的批次；空批次直接返回 `[]`，不发起查询。演示 ID 都是安全整数。PostgreSQL `bigint` 以及 JSON 中的数值字段可能超出 JavaScript 的精确数值范围；对于这类值，请使用无损 JSON 解析器或明确的序列化约定。

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

具名 node-postgres 查询会在每个连接上复用预备语句，但不会缓存返回行。`local_cache.mget` 在 PostgreSQL 内添加独立的共享整行缓存；客户端仍然发送查询并解码结果。参阅[缓存选择指南](postgresql-caching.md)比较这些层次；[批量查找指南](batch-primary-key-lookups.md)提供保留请求位置的纯 SQL 替代方案。

RESP2 可使用 [Node.js RESP 示例](resp.md#nodejs)。[已记录的 Node.js 结果](benchmarks-node.md)包括批量读取与并发更新。[统一基准测试](BENCHMARKS.md#run-the-same-comparison-on-every-client)让 Node.js 和 Go 执行相同的 SQL 与 RESP 场景。
