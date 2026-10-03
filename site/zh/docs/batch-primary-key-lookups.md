---
layout: doc
lang: zh
translation_key: batch-primary-key-lookups
title: 批量查找 PostgreSQL 主键
seo_title: 批量查找 PostgreSQL 主键 | pg_local_cache
description: 用一次参数化 PostgreSQL 查询替代 N+1 主键读取，按需保留输入位置，并比较显式 pg_local_cache mget 路径。
section: 指南
permalink: /zh/docs/batch-primary-key-lookups.html
last_modified_at: '2026-09-16'
---

# 批量查找 PostgreSQL 主键 {#batch-postgresql-primary-key-lookups}

如果应用为每个 ID 分别发送查询，网络往返和查询开销可能比读取小行本身更昂贵。先尝试一次参数化语句：

```sql
SELECT id, value, revision
FROM public.items
WHERE id = ANY($1::bigint[]);
```

将 ID 作为数组参数传入。在语句中固定表名和列名，不要从 ID 字符串拼接 SQL。PostgreSQL 通过将左侧表达式与数组元素比较来求值 `ANY`，详见[行与数组比较文档](https://www.postgresql.org/docs/18/functions-comparisons.html#FUNCTIONS-COMPARISONS-ANY-SOME)。

## 明确结果约定 {#know-the-result-contract}

上面的查询返回集合，不保证输入顺序；重复 ID 通常只匹配一次表行。不存在的 ID 不产生行。输入 `NULL` 不匹配非空主键；值为 `NULL` 的数组或其中的 `NULL` 元素也遵循 PostgreSQL 的三值 `ANY` 规则。空数组返回零行。

如果调用者需要每个请求位置都有结果，应显式保留位置：

```sql
WITH requested AS (
  SELECT key, position
  FROM unnest($1::bigint[]) WITH ORDINALITY AS input(key, position)
)
SELECT requested.position,
       requested.key,
       CASE WHEN items.id IS NULL THEN NULL
            ELSE row_to_json(items)::text END AS row
FROM requested
LEFT JOIN public.items AS items ON items.id = requested.key
ORDER BY requested.position;
```

`WITH ORDINALITY` 保留重复项和 `NULL` 位置；左连接为缺失键返回空的 `row`。对需要明确位置对齐的客户端而言，这是有用的基线。[node-postgres 示例](node-postgres.md)介绍如何在客户端恢复同样的约定。

## 何时适合使用 mget {#when-mget-is-the-right-alternative}

对于按主键读取完整行的场景，`pg_local_cache` 提供显式且有界的批量 API：

```sql
SELECT local_cache.mget(
  'public.items'::regclass,
  $1::bigint[]
) AS rows;
```

返回的 `text[]` 保留输入顺序与重复项。输入 `NULL` 和缺失行产生位置对应的 `NULL` 元素。每次调用最多接受 1,024 个键；函数可能根据事务、快照、映射与行大小规则绕过缓存或未命中，并回退到 PostgreSQL，而不会改变结果约定。它返回序列化的整行，因此需要投影、连接、主键以外的过滤条件或不受此上限约束的批次时，应使用 `ANY` 或带 ordinality 的查询。

## GraphQL、DataLoader 与 N+1 读取 {#graphql-dataloader-and-n1-reads}

[DataLoader](https://github.com/graphql/dataloader#batching) 将独立加载合并成批次。其批处理函数必须按相同顺序，为每个输入键返回一个值；上方的位置恢复方式即使遇到缺失行也能满足这一约定。

DataLoader 的[请求内记忆化](https://github.com/graphql/dataloader#caching-per-request)与 PostgreSQL 共享行缓存不同。每个请求应创建自己的 loader，并在该请求中发生修改后清除受影响的条目。PostgreSQL 的失效机制无法清除已经存放在 JavaScript loader 中的值。保留应用授权检查；`pg_local_cache` 不支持 RLS 表。

运行[快速开始](QUICKSTART.md)，然后在[基准测试](BENCHMARKS.md)中比较两种读取路径。[技术参考](TECHNICAL.md#sql-mget-api)定义 API；[事务指南](cache-invalidation.md)说明写入行为。
