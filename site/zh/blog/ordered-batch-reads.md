---
layout: post
lang: zh
translation_key: blog-ordered-batch-reads
title: 批量读取 PostgreSQL：保留顺序与缺失键
description: 替代 N+1 主键查询，同时保留重复 ID、输入顺序、NULL 位置和缺失行，比较 ANY、WITH ORDINALITY 与 SQL mget。
permalink: /zh/blog/ordered-batch-reads/
date: '2026-09-22'
last_modified_at: '2026-09-22'
topic: application
---

# 批量读取需要结果约定 {#batch-reads-need-a-result-contract}

用一次 `ANY` 查询替代逐个主键查询，可以减少往返，但也可能改变响应形状。调用者可能请求 `[42, 7, 42, NULL, -1]`，并期待五个结果位置。SQL 集合语义并不保证这种对齐。

## 行集合不等于答案列表 {#set-versus-list}

对于 `WHERE id = ANY($1::bigint[])`，重复 ID 通常只匹配一次表行；缺失 ID 不产生行。输入 `NULL` 不匹配非空主键，结果也不保证输入顺序。加上 `ORDER BY id` 只会按键排序，仍无法还原请求位置。

如果使用方需要集合，这没有问题。如果每个输入都需要一个结果，就应将位置纳入查询，或在应用中恢复位置。

## 在 SQL 中显式保留位置 {#explicit-positions}

启动[本地演示](../docs/QUICKSTART.md)，在其 `psql` 会话中运行：

```sql
WITH requested AS (
  SELECT key, position
  FROM unnest(ARRAY[42, 7, 42, NULL, -1]::bigint[])
       WITH ORDINALITY AS input(key, position)
)
SELECT requested.position,
       requested.key,
       CASE WHEN items.id IS NULL THEN NULL
            ELSE row_to_json(items)::text END AS row
FROM requested
LEFT JOIN public.items AS items ON items.id = requested.key
ORDER BY requested.position;
```

ordinality 列能区分两次出现的 42。左连接保留全部五个位置，包括空输入和任何不存在的键。对于缺失键，`row` 是 SQL `NULL`。在应用代码中应将数组作为参数传入，而不是把 ID 拼接进 SQL。[Node.js 示例](../docs/node-postgres.md)展示了客户端位置对齐。

## 比较整行 API {#whole-row-api}

对于已关联的表，对应的显式缓存调用如下：

```sql
SELECT local_cache.mget(
  'public.items'::regclass,
  ARRAY[42, 7, 42, NULL, -1]::bigint[]
) AS rows;
```

它返回 `text[]`，保留输入顺序与重复项。缺失键和空输入产生位置对应的 SQL `NULL`；每个非空元素都是序列化的完整行。缓存未命中或被绕过时会读取 PostgreSQL。此 API 每次调用最多接受 1,024 个键，不能替代投影、连接、行锁或任意查询结果缓存。

## 保持批次有界并可观测 {#bounded-batches}

超过 1,024 个键时，应显式拆分请求或保留普通 SQL 查询。跨语句分块可能观察到不同的 `READ COMMITTED` 快照，因此必须有意识地选择事务语义。更大批次还会增加响应大小和客户端解码工作，所以“查询更少”本身不能证明请求更快。

对于 GraphQL，DataLoader 批处理函数必须按输入顺序为每个键返回一个答案。请求本地记忆化与 PostgreSQL 共享缓存是两层不同机制；修改后应清除受影响的 loader 条目。参阅[完整批处理指南](../docs/batch-primary-key-lookups.md)，并使用[基准测试脚本](../docs/BENCHMARKS.md)比较延迟、载荷和吞吐量。
