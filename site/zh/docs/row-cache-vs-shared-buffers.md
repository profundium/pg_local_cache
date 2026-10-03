---
layout: doc
lang: zh
translation_key: row-cache-vs-shared-buffers
title: PostgreSQL 行缓存与 shared_buffers 的比较
seo_title: PostgreSQL 行缓存与 shared_buffers 的比较 | pg_local_cache
description: 比较 PostgreSQL 页缓存与 pg_local_cache 2.0 整行缓存：缓存命中省去哪些工作、仍有哪些成本，以及何时不应增加缓存。
section: 读取路径
permalink: /zh/docs/row-cache-vs-shared-buffers.html
last_modified_at: '2026-09-16'
---

# PostgreSQL 行缓存与 shared_buffers 的比较 {#postgresql-row-cache-vs-shared_buffers}

PostgreSQL 的 [`shared_buffers`](https://www.postgresql.org/docs/16/runtime-config-resource.html#GUC-SHARED-BUFFERS) 保存数据库页。pg_local_cache 则另外按完整主键保存序列化后的整行载荷。页面已在内存中可以避免存储读取，但查询仍须从数据库元组生成结果。行缓存命中可在通过适用性与快照检查后直接返回已存载荷。

操作系统也可能缓存文件内容。因此，应以已预热的数据库作为基线。

{% include diagrams/read-path.html id="buffers-read" %}

## PostgreSQL 会缓存 SELECT 结果吗？ {#does-postgresql-cache-select-results}

`shared_buffers` 缓存查询用到的页，而不是最终结果集。[预备语句](https://www.postgresql.org/docs/18/sql-prepare.html)复用解析工作，也可能复用计划，但 PostgreSQL 仍然需要执行语句。pg_local_cache 通过显式 `mget` 调用添加整行缓存；它不会缓存任意 SELECT 结果或重写已有查询。[Node.js 示例](node-postgres.md)并列展示两种读取 API。

## 比较执行工作，而不仅是存储介质 {#compare-the-work-not-just-the-storage-medium}

| 读取方式 | 仍需执行的工作 |
|---|---|
| 基于热页的预备主键 SQL | 协议处理、计划执行、行可见性检查和结果转换 |
| 可用的 SQL mget 缓存命中 | 协议处理、SQL 函数执行、键转换、缓存同步、快照检查及返回已存载荷 |
| SQL mget 未命中或绕过缓存 | 函数检查加上源表查询；成功且符合条件的填充可以写入缓存 |

行缓存命中避免重复的源表执行和整行序列化。缓存检查与同步同样消耗 CPU，命中仍使用 PostgreSQL 连接与后端进程。此 SQL API 不会消除连接数上限或连接池排队。

## 必须计入的成本 {#costs-to-include}

即使源页已在内存中，缓存行仍占用额外共享内存。扩展还维护映射与失效状态。更新已关联的表会执行扩展触发器。当工作集超过容量时，看似优化读取的缓存，可能主要带来未命中与淘汰开销。

默认演示特意先用 1,024 个缓存槽位测试 128 行热数据，再首次遍历 4,096 行。[基准测试指南](BENCHMARKS.md)解释这两种情况，并单独测量已关联表的写入成本。

## 何时保持现有应用不变 {#when-to-leave-the-application-alone}

如果端到端延迟已经可接受、应用只需大行中的少量列，或主要工作是连接、范围查询与聚合，应保留已有查询。先将普通批量查询与当前逐键调用比较。批处理带来的收益不能证明缓存也有收益。

pg_local_cache 2.0 要求显式调用 `mget`、安装扩展并在启动时预加载。它拒绝启用 RLS、分区或继承的表。

## 行缓存还是外部缓存？ {#row-cache-or-an-external-cache}

对于仍以 PostgreSQL 为权威来源的数据，此设计将失效处理放在数据库写入路径上，避免在应用中维护 cache-aside 协议。它不提供通用 Redis 语义。可选 RESP2 接口只有有限命令集，并具有独立安全模型。

PostgreSQL 行缓存不能替代基于 TTL 的应用状态、发布订阅或分布式协调。参阅[技术约定](TECHNICAL.md)与[事务示例](cache-invalidation.md)。
