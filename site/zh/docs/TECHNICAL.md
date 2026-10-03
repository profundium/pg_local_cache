---
layout: doc
lang: zh
translation_key: TECHNICAL
title: pg_local_cache 技术参考
seo_title: pg_local_cache 技术参考 | pg_local_cache
description: pg_local_cache 的 SQL mget、事务感知失效、有界 PostgreSQL 共享内存、监控与可选 RESP2 接口技术参考。
section: 技术参考
permalink: /zh/docs/TECHNICAL.html
---

# pg_local_cache 技术参考 {#pg_local_cache-technical-reference}

`pg_local_cache` 在有界 PostgreSQL 共享内存中，按完整主键缓存整行数据。它提供显式 SQL 函数 `local_cache.mget` 和可选的 RESP2 接口。

> **普通 SQL 保持原有行为：** 扩展不安装规划器或执行器钩子。普通 `SELECT` 始终由 PostgreSQL 执行，不会读取此缓存。

## 支持的表与键 {#supported-tables-and-keys}

源表必须是具有有效主键的永久堆表，且不能启用 RLS、分区、继承，也不能归扩展所有。

支持的键类型：

- `smallint`、`integer` 和 `bigint`；
- 使用确定性排序规则的 `text`、`varchar` 和 `char`；
- `uuid`；
- 仅由上述类型组成的复合主键。

不支持的关系会在关联时被拒绝，不会生成不安全的部分映射。

## 关联、协调与解除关联 {#attach-reconcile-and-detach-tables}

`local_cache.attach_table(regclass)` 执行一组受保护的初始化操作：

1. 锁定并验证关系；
2. 记录命名空间、关系 OID 及按顺序排列的主键列；
3. 安装归扩展所有的语句级、行级和截断触发器；
4. 重新加载工作进程映射。

DDL 事件触发器会使已缓存的映射元数据失效。主动修改表结构后，运行 `local_cache.reconcile_table(...)` 或 `local_cache.reconcile_all()`。`local_cache.detach_table(...)` 会移除映射及其触发器。

## SQL mget API {#sql-mget-api}

函数签名：

```sql
local_cache.mget(relation regclass, key_values anyarray) RETURNS text[]
```

单列键使用该列的原生数组类型。复合键使用矩形 `text[][]`：每行表示一个键，每个分量对应一个主键列。

接口约定：

- 每次调用最多 1,024 个键；
- 保留输入顺序与重复项；
- 输入 `NULL` 和不存在的行返回位置对应的 `NULL`；
- 复合键的分量不能为 `NULL`；
- 每个分量由对应 PostgreSQL 类型的输入函数解析；
- 首次查找前验证整个复合键批次；
- 调用者必须拥有源表的 `SELECT` 权限；
- 函数使用 `SECURITY INVOKER`。

预备的源表查询按函数实例、用户、关系及映射代次分别缓存。

## 读取路径与安全回退 {#read-path-and-safe-fallback}

每个请求键都遵循同一路径：

1. 将完整主键规范化；
2. 仅在可写主库上、未写入映射数据的 `READ COMMITTED` 事务中使用共享缓存；
3. 验证载荷校验和、行描述符、源 `xmin` 和快照可见性；
4. 否则通过 SPI 执行使用索引的源表查询；
5. 仅在通过最新快照校验后，发布存在记录或不存在记录的缓存项。

`REPEATABLE READ`、`SERIALIZABLE`、恢复模式、并行执行以及已写入映射数据的事务会绕过缓存。超过缓存载荷限制的行仍由 PostgreSQL 返回，但不会被缓存。

## 事务一致性 {#transaction-consistency}

映射表上的写操作提交前，触发器会为受影响的键或关系设置屏障。缓存填充携带映射、全局、关系、键和加载器的代次信息，因此过期加载器无法在失效或淘汰后发布结果。

存在记录的缓存项保存源元组的 `xmin` 与 FullXID 观测边界。对当前快照不可用的缓存项会回退到 PostgreSQL。表示记录不存在的缓存项，绝不会作为较旧活动快照的最终依据。

回滚会清除事务本地的脏状态，不会发布新数据。因此，事务读取自身写入时得到的是 PostgreSQL 的结果，而不是推测性的缓存内容。

## 共享内存与配置 {#shared-memory-and-configuration}

缓存项、关系状态、计数器、工作进程代次和 RESP 客户端槽位都在 postmaster 启动时分配，容量有明确上限。淘汰策略会检查一个大小有界、轮转取样的集合，并优先移除过期项；无法接纳新项时，回退读取源表，不会无限分配内存。

| 配置项 | 默认值 | 含义 |
|---|---:|---|
| `pg_local_cache.database` | `postgres` | 扩展服务的数据库 |
| `pg_local_cache.cache_entries` | `16384` | 共享行缓存容量 |
| `pg_local_cache.relation_states` | `1024` | 共享映射状态容量 |
| `pg_local_cache.memory_budget_mb` | `384` | 扩展启动内存预算 |
| `pg_local_cache.port` | `6380` | RESP 端口；`0` 禁用 RESP |
| `pg_local_cache.bind_address` | `127.0.0.1` | RESP 绑定地址 |
| `pg_local_cache.workers` | `4` | RESP 工作进程数 |
| `pg_local_cache.role` | `local_cache_worker` | RESP 使用的 PostgreSQL 角色 |
| `pg_local_cache.max_clients` | `256` | RESP 全局客户端上限 |
| `pg_local_cache.max_clients_per_worker` | `64` | 每个工作进程的槽位数 |
| `pg_local_cache.idle_timeout_ms` | `300000` | 空闲与慢客户端截止时间 |
| `pg_local_cache.statement_timeout_ms` | `2000` | 工作进程语句截止时间 |
| `pg_local_cache.lock_timeout_ms` | `250` | 工作进程锁等待截止时间 |
| `pg_local_cache.singleflight_wait_ms` | `25` | 同键后续请求的等待时间 |
| `pg_local_cache.max_pipeline_commands` | `256` | 每轮事件循环的命令数 |
| `pg_local_cache.max_dirty_keys` | `4096` | 事务键屏障数量上限 |
| `pg_local_cache.auth_token_file` | 空 | 首选 RESP 凭据文件 |
| `pg_local_cache.auth_token` | 空 | 仅供开发使用的内联令牌 |
| `pg_local_cache.allow_superuser` | `off` | 仅供开发使用的角色限制覆盖 |

这些都是 postmaster 配置项。应在重启前设定容量；二进制安装程序会在预检中验证整体配置方案。

## 可选 RESP2 接口 {#optional-resp2-endpoint}

RESP2 使用相同的映射和共享缓存。协议键格式如下：

```text
CRUD:database.schema.table:{"pk_column":<json-scalar>,...}
```

支持经过认证、大小有界的 `MGET`、`SET`、`DEL` 以及限定范围的失效操作。RESP 工作进程使用统一配置的 PostgreSQL 角色，不会继承各网络客户端的数据库 ACL。

该接口不提供 TLS。请绑定到回环地址，或置于要求认证的 TLS 代理之后。优先使用权限受限的令牌文件，而不是内联令牌。

## 健康状态与监控 {#health-and-monitoring}

`local_cache.health()` 报告就绪状态与映射收敛情况。`local_cache.stats()` 返回 JSON 计数器。`local_cache.metrics()` 提供导出器使用的有类型指标行。

SQL 缓存计数器仅描述显式 `mget` 调用：

- `sql_cache_hits`
- `sql_cache_misses`
- `sql_cache_fills`
- `sql_cache_bypasses`

数据库读取、失效、接纳拒绝、脏键回退、singleflight、工作进程和 RESP 的计数器分别统计。

下一步：参阅[安装指南](INSTALL_EXISTING.md)，了解经过校验的二进制包、PGXS 源码构建、受控重启、验证与恢复。
