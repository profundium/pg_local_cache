---
layout: doc
lang: zh
translation_key: cache-invalidation
title: PostgreSQL 中的事务感知缓存失效
seo_title: "PostgreSQL 缓存失效：提交与回滚 | pg_local_cache"
description: 了解 PostgreSQL 触发器如何在提交、回滚和并发缓存填充期间保护 RESP 行读取。
section: 缓存失效
permalink: /zh/docs/cache-invalidation.html
last_modified_at: "2026-10-04"
---

# PostgreSQL 中的事务感知缓存失效 {#transaction-aware-cache-invalidation-in-postgresql}

本指南说明，已附加表上的触发器如何防止 PostgreSQL 写入提交后仍命中过期的 RESP 缓存项。

![写入失效：已提交的更新发布屏障；仅在屏障发布前回滚才会保留原缓存项。](../../docs/diagrams/write-invalidation.svg)

触发器会在写事务中记录脏键或脏关系。提交时，扩展发布失效屏障并递增代数。屏障建立前开始的填充无法发布过期数据。仅在屏障发布前回滚，事务的脏状态才会被丢弃，原缓存项仍然有效。若屏障发布后事务中止，失效不会撤销，受影响的缓存项仍为无效。

完整的读取路径约定请参阅[技术一致性参考](TECHNICAL.md#transaction-consistency)。

## 检查 SQL 与 RESP 间的失效行为 {#test-with-two-sessions}

启动[本地演示](QUICKSTART.md)，然后分别从 RESP 和 PostgreSQL 读取同一个键：

```text
MGET CRUD:pglc_demo.public.items:{"id":42}
```

在另一个 SQL 会话中更新并提交：

```sql
BEGIN;
UPDATE public.items SET revision = revision + 1 WHERE id = 42;
COMMIT;
```

下一条 RESP 命令会返回已提交的版本。如果写事务回滚，RESP 仍会返回最后一次已提交的版本。

RESP 使用配置的 PostgreSQL 角色，并运行在独立的短事务中。它不会共享应用程序的角色、事务或快照。需要在事务中读取自己的写入或使用 `SELECT ... FOR UPDATE` 时，请在应用程序事务中使用 SQL。

## 会绕过缓存的情况 {#cases-that-deliberately-bypass-the-cache}

当 `pg_local_cache.enabled` 关闭时，RESP `MGET` 会跳过缓存查询和填充，直接读取源表。键、关系或全局范围内存在活动写入屏障时，也会阻止从缓存读取和写入缓存，读取改走源表。RESP worker 仅在恢复结束后启动，因此恢复不是单独的绕过条件。超出单个缓存项容量的行仍可由 PostgreSQL 返回，只要其 JSON 未超过 RESP 值大小限制；但不会存入缓存。

## 检查未命中的原因 {#inspect-the-cause-of-a-miss}

在受控工作负载运行前后，对比 `local_cache.stats()` 和 `local_cache.health()`。检查 bypass、未命中、失效和映射重载计数器。指标列表请参阅[技术参考](TECHNICAL.md#health-and-monitoring)。
