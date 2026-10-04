---
layout: doc
lang: zh
translation_key: postgresql-caching
title: PostgreSQL 缓存方案决策指南
seo_title: "PostgreSQL 缓存决策指南：页面、行、视图或 Redis"
description: 比较 PostgreSQL 页面缓存、预备 SQL、整行缓存、物化视图和 Redis，了解各读取路径能省去哪些工作。
section: 指南
permalink: /zh/docs/postgresql-caching.html
last_modified_at: "2026-10-04"
---

# PostgreSQL 缓存方案决策指南 {#postgresql-caching-decision-guide}

本指南比较 PostgreSQL 页面缓存、预备 SQL、整行缓存、物化视图和 Redis，说明每种方案能够省去哪些工作。

## 从重复的工作入手 {#start-with-the-work-you-repeat}

| 需求 | 方案 | 省去的工作 |
|---|---|---|
| 将表页和索引页保留在内存中 | PostgreSQL `shared_buffers` 和操作系统缓存 | 磁盘读取；SQL 仍会执行 |
| 在同一会话中重复执行一条语句 | 预备语句 | 重复解析和分析 |
| 按主键读取完整行 | 通过身份验证的 RESP `MGET`，使用 `pg_local_cache` | 符合条件的整行源数据读取 |
| 复用连接查询或聚合结果 | 物化视图 | 更新前无需重新计算已存储的查询结果 |
| 在多个服务间共享对象 | Redis cache-aside | 由应用管理的源数据读取 |

### 页面缓存与预备 SQL {#pages-and-prepared-sql}

[`shared_buffers`](https://www.postgresql.org/docs/18/runtime-config-resource.html#GUC-SHARED-BUFFERS) 缓存的是数据库页面，而不是最终的 `SELECT` 结果。PostgreSQL 仍会检查可见性、执行查询并构建每个结果。[预备语句](https://www.postgresql.org/docs/18/sql-prepare.html) 可以减少重复解析，但仍需基于数据库当前状态执行。

请参阅[行缓存与 shared_buffers 对比](row-cache-vs-shared-buffers.md)，了解每条路径仍需执行哪些工作。

### 按主键读取完整行 {#whole-rows-by-primary-key}

`pg_local_cache` 将完整行存储在 PostgreSQL 有界共享内存中。通过身份验证的 RESP `MGET` 可返回符合条件的缓存行；普通 SQL 不会读取此缓存。未命中或不符合条件的读取会访问源表。该端点使用一个已配置的数据库角色，且不会共享调用方的 SQL 事务或快照。请参阅[批量查询指南](batch-primary-key-lookups.md)、[技术参考](TECHNICAL.md)和[失效指南](cache-invalidation.md)。

### 视图与外部缓存 {#views-and-external-caches}

PostgreSQL [物化视图](https://www.postgresql.org/docs/18/rules-materializedviews.html)会存储查询结果，并按需刷新。适用于报告和聚合场景，刷新时间决定数据的新鲜度。

Redis 适合在多个进程间共享应用对象。键、序列化、TTL 和失效均由应用负责。请参阅[PostgreSQL 与 Redis cache-aside](postgresql-redis-cache.md)。可通过[快速入门](QUICKSTART.md)试用行缓存路径。
