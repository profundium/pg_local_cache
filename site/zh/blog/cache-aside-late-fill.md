---
layout: post
lang: zh
translation_key: blog-cache-aside-late-fill
title: PostgreSQL 缓存失效：延迟填充竞态
description: 逐步分析旧读取如何在提交后重新填入已删除的缓存键，并理解发布屏障、快照、回滚与请求本地缓存。
permalink: /zh/blog/cache-aside-late-fill/
date: '2026-09-22'
last_modified_at: '2026-09-22'
topic: correctness
---

# 缓存失效与延迟填充竞态 {#cache-invalidation-and-the-late-fill-race}

“数据库更新后删除缓存键”仍留下一个时序问题：另一个请求可能已经在加载旧值。删除移除的是当前存在的条目，不会取消仍在途中的结果。

## 跟踪两个请求 {#two-requests}

假设 PostgreSQL 保存 revision 0，应用使用 cache-aside 读取。即使写者在提交之后使缓存失效，也可能发生以下顺序：

| 步骤 | 读者 A | 写者 B |
|---|---|---|
| 1 | 缓存未命中，读取 revision 0 | |
| 2 | 在存储结果前暂停 | 将该行更新为 revision 1 |
| 3 | | 提交并删除缓存键 |
| 4 | 发布先前读取的 revision 0 | |
| 5 | 后续请求读到过期缓存值 | |

TTL 可以限制该值保持可用的时间，却无法使第 4 步变得正确。将删除移到提交之前，会产生另一个时间窗口，使读者能够用旧的已提交数据库状态重新填充缓存。[PostgreSQL 与 Redis 指南](../docs/postgresql-redis-cache.md)解释应用 cache-aside 与数据库本地行缓存的边界。

## 不仅检查查找，也检查发布 {#publication}

缓存填充需要证明：结果在发布时仍然符合使用条件。在 `pg_local_cache` 2.0 中，写入为受影响的键或关系设置屏障；填充携带代次信息，存在记录的缓存项携带元组可见性信息。代次变化可以拒绝旧填充。无法安全使用缓存项的读取会回退到 PostgreSQL。

这些检查属于一条特定的 PostgreSQL 读取路径，不会把可选 RESP 接口变成通用 Redis 服务器，也不会使应用已经复制到其他位置的值失效。

## 同时测试回滚与提交 {#test-transactions}

在一次性数据库上执行[双会话测试](../docs/cache-invalidation.md#test-with-two-sessions)。在一个会话预热该行，在另一个会话更新该行但保持事务打开。检查三个现象：

1. 写者通过源表路径能够读取自己的改动。
2. 写事务仍打开时，另一个会话仍看到已提交的值。
3. 回滚后保留原值；提交后，`READ COMMITTED` 下的新语句看到新 revision。

第三个条件指的是新语句。较早开始的语句，无需在执行途中采用更新的快照。[事务约定](../docs/TECHNICAL.md#transaction-consistency)还说明了哪些模式会绕过缓存。需要行锁时使用普通 SQL。

## 检查应用中的下一层缓存 {#application-cache}

请求本地的 DataLoader 可能仍保存修改前加载的值。数据库失效机制无法移除这个 JavaScript 对象。发生修改后，应按照应用授权与结果约定，清除或替换受影响的 loader 条目。将 loader 限定在单个请求范围内。

[批处理指南](../docs/batch-primary-key-lookups.md#graphql-dataloader-and-n1-reads)区分了请求记忆化与共享行缓存。排查过期响应时，应从数据库快照到响应对象，追踪每一个存储位置。某个边界上的正确性，不会自动清除其他位置的数据。
