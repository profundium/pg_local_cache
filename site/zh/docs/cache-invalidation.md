---
layout: doc
lang: zh
translation_key: cache-invalidation
title: PostgreSQL 中的事务感知缓存失效
seo_title: PostgreSQL 中的事务感知缓存失效 | pg_local_cache
description: "了解 RESP 行读取的触发器失效机制、已提交更新、源表读取以及独立的 SQL 事务边界。"
section: 缓存失效
permalink: /zh/docs/cache-invalidation.html
last_modified_at: '2026-09-16'
---

# PostgreSQL 中的事务感知缓存失效 {#transaction-aware-cache-invalidation-in-postgresql}

如果较早开始的读取能在删除后重新填充缓存，仅删除条目是不够的。假设读者开始加载旧行，写者提交新值并使键失效，随后先前的加载器才发布结果。缓存还必须拒绝这种迟到的发布。

2.0 实现在数据库写入路径上，为受影响的键或关系设置屏障。填充携带代次信息，因此失效后可以被拒绝。存在记录的缓存项还带有元组可见性信息。不符合条件的条目会回退读取源表。约定详见[技术参考](TECHNICAL.md#transaction-consistency)。

{% include diagrams/transaction.html id="invalidation-transaction" %}

## 检查 SQL 与 RESP 之间的失效行为 {#test-with-two-sessions}

启动[本地演示](QUICKSTART.md)。通过 RESP 读取行 42 并记下 revision。然后在 PostgreSQL 中更新该行并提交：

```sql
BEGIN;
UPDATE public.items SET revision = revision + 1 WHERE id = 42;
COMMIT;
```

关联表的触发器会在提交时使受影响的缓存行失效。下一次 RESP 读取会返回已提交的 revision。要观察回滚行为，可再次开始更新并回滚；RESP 仍会返回最后一次已提交的 revision。

RESP worker 使用配置的 PostgreSQL 角色，不共享应用的 SQL 事务或快照。要检查读己之写，必须在同一应用事务中使用 SQL；该路径会照常读取源表。RESP 接口用于由独立 worker 角色执行的读取。

可运行的 [Node.js 测试](https://github.com/profundium/pg_local_cache/blob/master/examples/node-postgres/demo.mjs)会检查 PostgreSQL 写入前后的 RESP 读取。

## 主动绕过缓存的情况 {#cases-that-deliberately-bypass-the-cache}

`REPEATABLE READ`、`SERIALIZABLE`、恢复、并行执行，以及已经写入映射数据的事务，都使用源表路径。超大行可能成功返回，但不会被缓存。命中率接近零不一定代表安装失败：请检查工作负载和绕过计数器。

若应用需要 `SELECT ... FOR UPDATE`，请使用普通 PostgreSQL 操作。RESP `MGET` 不提供行锁或 SQL 会话语义。

## 检查未命中的原因 {#inspect-the-cause-of-a-miss}

以管理员身份使用 `local_cache.stats()` 和 `local_cache.health()`。比较受控测试前后的计数器。缓存计数器描述 RESP 读取路径。执行预期的 DDL 更改后，请按文档运行 `reconcile_table` 或 `reconcile_all`，不要假设旧映射仍描述修改后的表。
