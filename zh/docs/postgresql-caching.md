---
layout: doc
lang: zh
translation_key: postgresql-caching
title: PostgreSQL 缓存选择指南
seo_title: PostgreSQL 缓存选择指南 | pg_local_cache
description: 根据需要避免的重复工作，在 PostgreSQL 页缓存、预备 SQL、整行缓存、物化视图和外部缓存之间做出选择。
section: 指南
permalink: /zh/docs/postgresql-caching.html
last_modified_at: '2026-09-16'
---

# PostgreSQL 缓存选择指南 {#postgresql-caching-decision-guide}

“添加缓存”可能意味着多种不同改动。PostgreSQL 页缓存、预备 SQL、整行缓存、物化视图和 Redis 分别避免不同的读取工作。先识别请求中重复执行的工作，再使用[基准测试指南](BENCHMARKS.md)测量完整路径。

## 从重复工作开始 {#start-with-the-work-you-repeat}

| 需求 | 优先考虑 | 改变了什么 |
|---|---|---|
| 保持表页与索引页为热数据 | PostgreSQL `shared_buffers` 和操作系统缓存 | 减少存储读取；SQL 仍需执行 |
| 多次发送相同语句 | 预备语句 | 减少重复解析与规划；仍需执行 |
| 按主键返回完整行 | `pg_local_cache` SQL `mget` | 通过显式 API 复用符合条件的整行载荷 |
| 预计算连接或聚合 | 物化视图 | 读取持久化结果；刷新决定新鲜度 |
| 跨服务共享应用对象 | Redis 等外部缓存 | 应用管理键、TTL 和失效 |

### 页与预备 SQL {#pages-and-prepared-sql}

[`shared_buffers`](https://www.postgresql.org/docs/18/runtime-config-resource.html#GUC-SHARED-BUFFERS) 保存数据库页，而不是最终 `SELECT` 结果。热页可以避免存储 I/O，但 PostgreSQL 仍然要规划或执行查询、检查可见性并构造结果。[预备语句](https://www.postgresql.org/docs/18/sql-prepare.html)可在同一会话内避免重复解析与分析，但仍基于当前数据库状态执行，计划可能是通用计划或定制计划。

[行缓存比较](row-cache-vs-shared-buffers.md)展示各路径仍需完成的工作。

### 按主键缓存整行 {#whole-rows-by-primary-key}

`pg_local_cache` 在有界 PostgreSQL 共享内存中，按完整主键存储序列化的完整行。通过 `local_cache.mget('public.items'::regclass, $1::bigint[])` 访问；普通 `SELECT` 从不查询此缓存。未写入映射数据且符合条件的 `READ COMMITTED` 读取可以命中，而更严格的隔离级别、事务中的写入、恢复、并行执行或超大行会使用 PostgreSQL。在关联期间，不支持的表映射会被拒绝。这是一条特定读取路径，不是任意查询结果缓存。参阅[批量查找指南](batch-primary-key-lookups.md)、[技术约定](TECHNICAL.md)和[事务检查](cache-invalidation.md)。

### 视图与外部缓存 {#views-and-external-caches}

PostgreSQL [物化视图](https://www.postgresql.org/docs/18/rules-materializedviews.html)将查询结果持久化为关系，并按需刷新。适用于重复报表、聚合与连接，前提是刷新计划提供的新鲜度边界可以接受。它不能逐键替代行缓存。

Redis 等外部缓存适用于多进程或多服务共享的应用对象。应用负责键、序列化、TTL 和失效。参阅 [Redis cache-aside 指南](postgresql-redis-cache.md)。[快速开始](QUICKSTART.md)会在 `public.items` 上运行 `pg_local_cache`。
