---
layout: doc
lang: zh
translation_key: row-cache-vs-shared-buffers
title: PostgreSQL 行缓存与 shared_buffers 对比
seo_title: "PostgreSQL 行缓存与 shared_buffers 对比 | pg_local_cache"
description: 比较 PostgreSQL 页面缓存和 pg_local_cache 整行缓存：可省去的源数据工作、缓存成本，以及适合继续使用普通 SQL 的工作负载。
section: 读取路径
permalink: /zh/docs/row-cache-vs-shared-buffers.html
last_modified_at: "2026-10-04"
---

# PostgreSQL 行缓存与 shared_buffers 对比 {#postgresql-row-cache-vs-shared_buffers}

本指南比较 PostgreSQL 页面缓存和 `pg_local_cache` 整行缓存，并说明每种读取路径仍需执行哪些工作。

[`shared_buffers`](https://www.postgresql.org/docs/18/runtime-config-resource.html#GUC-SHARED-BUFFERS) 将数据库页面保留在内存中。热页面可以避免磁盘 I/O，但 PostgreSQL 仍会检查元组可见性、执行查询并构建结果。符合条件的 RESP `MGET` 命中在验证键、fence 代次和负载后，可直接返回已存储的整行负载。

请参阅[读取路径技术参考](TECHNICAL.md#read-path-and-safe-fallback)。

## PostgreSQL 会缓存 SELECT 结果吗？ {#does-postgresql-cache-select-results}

不会。PostgreSQL 页面缓存保存的是页面，而不是最终查询结果。[预备语句](https://www.postgresql.org/docs/18/sql-prepare.html)可以复用解析和规划过程，但 PostgreSQL 仍会执行语句。`pg_local_cache` 通过 RESP `MGET` 提供整行缓存，不缓存任意 `SELECT` 结果。请参阅[RESP 客户端](resp.md)。

## 比较工作量，而不只是存储介质 {#compare-the-work-not-just-the-storage-medium}

| 读取路径 | 仍需执行的工作 |
|---|---|
| 热页面上的预备主键 SQL | 协议处理、查询执行、可见性检查和结果转换 |
| 符合条件的 RESP `MGET` 命中 | 协议处理、键转换、缓存同步、资格检查和负载返回 |
| RESP 未命中或绕过缓存 | 缓存检查和源表读取；符合条件的行可能填充缓存 |

缓存命中可以避免重复执行源表查询和整行序列化，但仍需使用 PostgreSQL worker 并进行缓存同步。RESP 请求独立于调用方的 SQL 事务执行。

## 需要计入的成本 {#costs-to-include}

缓存行和映射状态会额外占用共享内存。对已附加表的写入会运行失效触发器。工作集超过缓存容量时，未命中和逐出可能增加。

在两条路径上使用相同的键集合、行结构、连接数和请求组合进行测量。将批量 SQL 与单行读取分开评估；批处理本身即使没有缓存，也能减少往返次数。

## 何时保持应用不变 {#when-to-leave-the-application-alone}

如果端到端延迟已可接受、应用只需要列投影，或主要开销来自连接查询、范围查询和聚合，请继续使用普通 SQL。需要行锁或需要与同一事务共享的读取应使用 SQL。

## 行缓存还是外部缓存？ {#row-cache-or-an-external-cache}

如果 PostgreSQL 仍是权威数据源，且应用反复按主键读取完整行，可使用 `pg_local_cache`。如果需要带 TTL 的应用状态、发布/订阅、分布式协调，或跨服务共享对象，请使用外部缓存。[技术参考](TECHNICAL.md)介绍了 RESP 的安全边界。
