---
layout: doc
lang: zh
translation_key: postgresql-redis-cache
title: PostgreSQL 与 Redis cache-aside
seo_title: "PostgreSQL 与 Redis cache-aside：失效与竞争条件"
description: 比较由应用管理的 Redis cache-aside 与 pg_local_cache RESP 读取，包括过期填充竞争和写入失效。
section: 指南
permalink: /zh/docs/postgresql-redis-cache.html
last_modified_at: "2026-10-04"
---

# PostgreSQL 与 Redis cache-aside {#postgresql-and-redis-cache-aside}

本指南介绍以 PostgreSQL 为事实来源的 Redis cache-aside、过期缓存填充导致的竞争条件，以及 `pg_local_cache` 如何处理已附加行的失效。

Redis 未命中时，应用会从 PostgreSQL 读取并返回该行，再将其存入 Redis 的应用键中。写入时，应用先提交 PostgreSQL 数据，再删除对应的 Redis 键。TTL 限制缓存保留时间，但不能证明数据相对于 PostgreSQL 已提交状态仍然新鲜。请参阅 [Redis cache-aside 指南](https://redis.io/docs/latest/develop/use-cases/cache-aside/)。

## 失效竞争 {#the-invalidation-race}

读者可能先从 PostgreSQL 读取旧行并暂停，等写入者提交新数据并删除缓存键后，才把旧行写入 Redis。下一个读者会看到过期数据，直到缓存过期或该键再次被删除。

缓解方法包括拒绝使用旧数据库版本填充缓存、按键串行化读取，或通过 outbox/CDC 消费者发布已提交的变更。每种方法都会增加协调逻辑。请参阅[事务感知失效指南](cache-invalidation.md)，了解 PostgreSQL 内部类似的迟到填充边界。

## pg_local_cache 的适用场景 {#where-pg_local_cache-fits}

`pg_local_cache` 将完整行按主键存储在 PostgreSQL 有界共享内存中。应用使用经过身份验证的 RESP2 `MGET` 请求行；普通 SQL 和任意查询结果都不会使用此缓存。已附加表上的触发器会为写入设置屏障；缓存资格检查不允许命中时，读取会使用 PostgreSQL。

与 Redis cache-aside 不同，这条路径利用数据库写入路径进行失效，不使用 TTL 或通用 Redis 数据结构。RESP worker 在独立的短事务中使用已配置的 PostgreSQL 角色。连接和安全详情请参阅[RESP 客户端](resp.md)和[技术参考](TECHNICAL.md)。

当需要跨应用共享对象、基于 TTL 管理新鲜度或使用 Redis 数据结构时，请使用 Redis。当需要从一个 PostgreSQL 数据库反复按主键读取整行时，请使用 `pg_local_cache`。组合使用两层缓存时，必须分别管理键、失效和监控。
