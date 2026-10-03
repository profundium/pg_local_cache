---
layout: doc
lang: zh
translation_key: cache-invalidation
title: PostgreSQL 中的事务感知缓存失效
seo_title: PostgreSQL 中的事务感知缓存失效 | pg_local_cache
description: 用并发 PostgreSQL 会话测试 pg_local_cache 2.0 失效，检查未提交更新、读取自身写入、回滚、已提交读取以及回退规则。
section: 缓存失效
permalink: /zh/docs/cache-invalidation.html
last_modified_at: '2026-09-16'
---

# PostgreSQL 中的事务感知缓存失效 {#transaction-aware-cache-invalidation-in-postgresql}

如果较早开始的读取能在删除后重新填充缓存，仅删除条目是不够的。假设读者开始加载旧行，写者提交新值并使键失效，随后先前的加载器才发布结果。缓存还必须拒绝这种迟到的发布。

2.0 实现在数据库写入路径上，为受影响的键或关系设置屏障。填充携带代次信息，因此失效后可以被拒绝。存在记录的缓存项还带有元组可见性信息。不符合条件的条目会回退读取源表。约定详见[技术参考](TECHNICAL.md#transaction-consistency)。

{% include diagrams/transaction.html id="invalidation-transaction" %}

## 使用两个会话测试 {#test-with-two-sessions}

启动[本地演示](QUICKSTART.md)，在两个终端中分别执行：

```bash
docker compose -f examples/compose.yaml exec postgres \
  psql -X -v ON_ERROR_STOP=1 -U demo -d pglc_demo
```

在会话 A 中读取行 42 并记下 revision，再读取一次以预热缓存：

```sql
SELECT (local_cache.mget('public.items'::regclass, ARRAY[42]::bigint[]))[1]::jsonb ->> 'revision';
SELECT (local_cache.mget('public.items'::regclass, ARRAY[42]::bigint[]))[1]::jsonb ->> 'revision';
```

在会话 B 中更新该行，但保持事务打开：

```sql
BEGIN;
UPDATE public.items SET revision = revision + 1 WHERE id = 42;
SELECT (local_cache.mget('public.items'::regclass, ARRAY[42]::bigint[]))[1]::jsonb ->> 'revision';
```

B 能看到自己增加后的值。此读取会绕过缓存。保持 B 事务打开，在 A 中重复查询：A 必须仍看到已提交的 revision，而不是 B 的未提交值。在 B 中执行 `ROLLBACK`；A 的后续查询仍应返回原始 revision。

现在在 B 中运行：

```sql
BEGIN;
UPDATE public.items SET revision = revision + 1 WHERE id = 42;
COMMIT;
```

A 中在这次提交之后启动的查询，必须返回增加后的 revision。这是相关边界：较早启动且仍在运行的语句，无需切换到启动之后创建的新快照。PostgreSQL 的 [Read Committed](https://www.postgresql.org/docs/16/transaction-iso.html#XACT-READ-COMMITTED) 文档说明了该行为。

可运行的 [Node.js 测试](https://github.com/profundium/pg_local_cache/blob/master/examples/node-postgres/demo.mjs)使用独立连接验证这些观察结果。

## 主动绕过缓存的情况 {#cases-that-deliberately-bypass-the-cache}

`REPEATABLE READ`、`SERIALIZABLE`、恢复、并行执行，以及已经写入映射数据的事务，都使用源表路径。超大行可能成功返回，但不会被缓存。命中率接近零不一定代表安装失败：请检查工作负载和绕过计数器。

如果应用需要 `SELECT ... FOR UPDATE`，应使用普通 PostgreSQL 操作；`mget` 不能替代行锁。

## 检查未命中的原因 {#inspect-the-cause-of-a-miss}

以管理员身份使用 `local_cache.stats()` 和 `local_cache.health()`。比较受控测试前后的计数器快照，并将 SQL `mget` 与 RESP 计数器分开观察。主动执行 DDL 后，遵循文档中的 `reconcile_table` 或 `reconcile_all` 流程，不要假定旧映射仍然描述修改后的表。
