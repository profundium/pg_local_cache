---
layout: doc
lang: zh
translation_key: postgresql-redis-cache
title: PostgreSQL 与 Redis 的 cache-aside 模式
seo_title: PostgreSQL 与 Redis 的 cache-aside 模式 | pg_local_cache
description: 以 PostgreSQL 为权威数据源，理解 Redis cache-aside 的过期读取竞态，并了解 pg_local_cache 的适用边界。
section: 指南
permalink: /zh/docs/postgresql-redis-cache.html
last_modified_at: '2026-09-16'
---

# PostgreSQL 与 Redis 的 cache-aside 模式 {#postgresql-and-redis-cache-aside}

Redis cache-aside 由应用协调读取与权威存储。未命中时，读取 PostgreSQL、返回结果并写入 Redis；写入时，更新 PostgreSQL 并使对应 Redis 键失效。[Redis 模式指南](https://redis.io/docs/latest/develop/use-cases/cache-aside/)描述了此流程，但该模式本身不会让应用缓存与 PostgreSQL 具有事务一致性。

对于以 `public.items.id` 为键的行，流程如下：

```text
GET item:42
miss -> SELECT * FROM public.items WHERE id = $1
     -> SET item:42 <serialized row> EX <ttl>
write -> UPDATE public.items ...
      -> COMMIT
      -> DEL item:42
```

使用参数化 SQL 和独立键命名空间。TTL 限制值在 Redis 中保留的时间，但不能证明其相对于 PostgreSQL 提交仍然新鲜。显式删除可以处理普通写入，却无法消除所有竞态。

## 失效竞态 {#the-invalidation-race}

考虑两个请求：读者 R1 在 Redis 未命中，从 PostgreSQL 读取旧行。写者 W 提交新行并删除 `item:42`。随后 R1 恢复执行，将旧值写入 Redis。下一个读者会读到过期数据，直到该键到期或另一次写入删除它。

可能的缓解方式包括加载完成后再次删除、保存数据库版本并拒绝更旧的值、按键串行化加载，或通过 outbox / CDC 消费者发布已提交变更。每种方案都增加了协调逻辑与失败情况。[缓存失效指南](cache-invalidation.md)展示了 PostgreSQL 内部类似的延迟填充问题。

## pg_local_cache 的适用位置 {#where-pg_local_cache-fits}

`pg_local_cache` 是范围更窄的 PostgreSQL 本地方案，用于按主键读取完整行。`local_cache.mget` 是显式 API；普通 `SELECT` 和任意形状查询都不会读取缓存。已关联表的触发器在数据库写入路径上为受影响的键或关系设置屏障。当事务或快照规则不允许命中时，读取可以回退到 PostgreSQL。先阅读[批量查找指南](batch-primary-key-lookups.md)与[技术约定](TECHNICAL.md)。

此扩展不提供通用 Redis 兼容性、Redis TTL 或分布式应用缓存协议。可选 RESP2 接口基于同一映射，提供有限且需认证的命令集，并具有自己的安全模型；它不提供 TLS。当问题是 PostgreSQL 本地、具有事务感知能力的整行读取时，可考虑此扩展。当多个应用实例需要共享对象、基于 TTL 的新鲜度或 Redis 数据结构时，应使用 Redis。组合使用两者时，各层都需要独立的键、失效处理和指标。

运行[快速开始](QUICKSTART.md)，与 [node-postgres 示例](node-postgres.md)中的普通客户端查询比较，并分别查看 SQL 与 RESP 计数器。[缓存选择指南](postgresql-caching.md)列出了 PostgreSQL 的其他选项。
