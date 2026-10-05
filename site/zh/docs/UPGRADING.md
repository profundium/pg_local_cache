---
layout: doc
lang: zh
translation_key: UPGRADING
title: 将 pg_local_cache 升级到 3.1.0
seo_title: "将 pg_local_cache 升级到 3.1.0"
description: 从 3.0.0 或 2.x 升级 pg_local_cache，检查 3.1 容量与 worker 设置，并在重启后验证扩展。
section: 安装
permalink: /zh/docs/UPGRADING.html
last_modified_at: "2026-10-06"
---

# 将 pg_local_cache 升级到 3.1.0 {#upgrade-pg_local_cache-from-2x-to-310}

3.0.0 移除了 SQL 函数 `local_cache.mget(regclass, anyarray)`。请使用经过
身份验证的 RESP `MGET` 读取缓存的完整行。RESP worker 使用已配置的
PostgreSQL 角色，不会继承应用的 SQL 权限、事务或快照。投影、连接、行锁
以及需要应用会话语义的读取仍使用 SQL。[RESP 指南](resp.md)介绍客户端
设置和键编码。

## 从 3.0.0 升级 {#upgrade-from-300}

3.1.0 会更改共享库和共享内存布局。安装适用于当前 PostgreSQL 主版本的
3.1.0 软件包或库，并在使用前重启 PostgreSQL。`3.0.0--3.1.0` SQL 迁移
不做更改：SQL 对象、已附加表的映射和触发器均保持不变。请在每个数据库
中执行扩展更新，以记录版本 `3.1.0`。

### 设置变更 {#setting-changes}

| 设置 | 3.1.0 行为 |
|---|---|
| `pg_local_cache.cache_entries` | 范围为 `128`–`16777216` 个描述符。内置默认值 `262144` 按默认 384 MiB 预算计算，并至少为 arena 页面预留一半预算。实际字节容量取决于 arena 和行大小；超出配置内存预算时，启动会失败。 |
| `pg_local_cache.lock_partitions` | 默认 `64`；取值为 `16` 至 `256` 的 2 次幂。小型缓存会使用较少分区。 |
| `pg_local_cache.dirty_marker_entries` | 默认 `-1` 表示自动计算：`min(16384, max(1024, floor(cache_entries / 4)))`。显式范围：`128`–`1048576`。 |
| `pg_local_cache.dirty_marker_memory_mb` | 默认 `-1` 表示自动计算：`min(16, max(1, floor(memory_budget_mb / 25)))` MiB。显式范围：`1`–`1024` MiB。 |
| `pg_local_cache.max_clients_per_worker` | 默认 `64`；范围扩大到 `1`–`4096`。`max_clients` 不得超过 `workers × max_clients_per_worker`。每个 worker 的软 `RLIMIT_NOFILE` 至少为 `min(max_clients, max_clients_per_worker) + 33`；需要时提高进程或容器的 `nofile` 限制。 |
| `pg_local_cache.max_deferred_misses` | 新设置。默认 `8`；每个 worker 的关系锁阻塞请求范围为 `1`–`64`。 |

3.1.0 没有删除或重命名 3.0.0 的设置。以上设置都需要重启后生效。

`local_cache.stats()` 新增 `fast_path_hits`、`fast_path_fallbacks` 和`fast_path_fallback_key_form`、`fast_path_fallback_mapping_shape`、`fast_path_fallback_multi_key` 和 `fast_path_fallback_cache_state`；`cache_memory_capacity_bytes`、`cache_memory_used_bytes`、
`cache_fragmentation_bytes`、`arena_admission_rejections_total`；
`dirty_marker_capacity`、`dirty_marker_entries`、`dirty_marker_highwater`、
`dirty_marker_fallbacks_total`、`dirty_marker_entries_effective`、
`dirty_marker_memory_mb_effective`、`dirty_marker_memory_capacity_bytes`、
`dirty_key_limit_fallbacks`，以及 `lock_partitions`、`max_clients_per_worker`
和 `client_slots`。RESP `STAT` 新增 worker 本地字段
`deferred_misses_total`、`deferred_misses_current`、`deferred_timeouts_total`
和 `deferred_rejections_total`。

升级步骤：

1. 安装适用于当前 PostgreSQL 主版本的 3.1.0 软件包或库。
2. 检查上述设置。增加客户端槽位时，按公式提高进程或容器的软 `nofile` 限制。
3. 重启 PostgreSQL，以加载新库并分配新的共享内存布局。
4. 在每个安装了扩展的数据库中，以数据库超级用户连接并执行：

   ```sql
   ALTER EXTENSION pg_local_cache UPDATE;
   ```

5. 验证库版本和 workers：

   ```sql
   SELECT current_setting('pg_local_cache.binary_version');
   SELECT local_cache.health();
   ```

   库版本必须为 `3.1.0`；health 必须显示
   `workers_running = workers_configured`。

## 从 2.x 升级 {#upgrade-from-2x}

升级前，将应用从 SQL `mget` 切换到 RESP `MGET`，使用专用 worker 角色和所需
授权模型。如果 2.x listener 绑定到非 loopback 地址，请在重启前配置原生
RESP TLS，并提供证书和密钥。设置 `pg_local_cache.tls_ca_file` 可强制验证
客户端证书（mTLS）。RESP TLS 与 PostgreSQL `ssl_*` 无关。如果必须使用明文，
请显式设置 `pg_local_cache.allow_plaintext_network = on`，且仅用于可信网络。
未配置 TLS 或此许可时，非 loopback RESP worker 不会启动。

从 2.x 到 3.0 的 SQL 迁移删除了 SQL 读取 API 及其计数器，因此
`local_cache.metrics()` 的结果类型发生变化。自定义授权会保留。依赖
`local_cache.metrics()` 的用户对象可能阻止迁移；迁移不会使用 `CASCADE`，
因此请调整或删除依赖对象后重试。这些 SQL 变更属于早期 3.0 迁移，不属于
3.1 的空 SQL 迁移。随后按上述步骤安装、重启、更新扩展并验证。PostgreSQL 会先应用此前从 2.x 到 3.0 的迁移，再应用从 3.0.0 到 3.1.0 的空迁移。

## 回滚到 2.0.4 {#rollback-to-204}

没有降级脚本。若要返回 2.0.4，请重新安装其软件包，重启 PostgreSQL 以加载
旧库，分离所有已映射的表，然后在每个数据库中重新创建扩展并重新附加表：

```sql
SELECT local_cache.detach_table('public.items'::regclass);
DROP EXTENSION pg_local_cache;
CREATE EXTENSION pg_local_cache VERSION '2.0.4';
SELECT local_cache.attach_table('public.items'::regclass);
```

对每张表重复 `detach_table` 和 `attach_table`。回滚前保存已附加表清单和
扩展自定义授权。依赖扩展函数的用户对象可能阻止删除；请单独处理这些依赖。

## 库查找路径 {#library-lookup-path}

control 文件在 `module_pathname` 中使用裸库名 `pg_local_cache`。PostgreSQL
通过 `dynamic_library_path` 解析该名称（默认包含 `$libdir`）。如果服务器
覆盖了该设置，请在重启前加入软件包安装 `pg_local_cache` 的目录。

2.x 文档：https://github.com/profundium/pg_local_cache/tree/v2.0.4/docs
