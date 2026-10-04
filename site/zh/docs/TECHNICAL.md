---
layout: doc
lang: zh
translation_key: TECHNICAL
title: pg_local_cache 技术参考
seo_title: pg_local_cache 技术参考 | pg_local_cache
description: "pg_local_cache 技术参考：RESP MGET、事务感知失效、有界 PostgreSQL 共享内存与监控。"
section: 技术参考
permalink: /zh/docs/TECHNICAL.html
---

# pg_local_cache 技术参考 {#pg_local_cache-technical-reference}

`pg_local_cache` 在有界 PostgreSQL 共享内存中按完整主键缓存整行数据。RESP2 接口提供 `MGET`、`SET` 和 `DEL` 命令。

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

## 读取路径与安全回退 {#read-path-and-safe-fallback}

启用缓存时，每个 RESP `MGET` 键都会先查询共享缓存。每次未命中时，worker 都会
在自己的短事务中读取源表行。超过缓存载荷上限的行仍由 PostgreSQL 返回，但不会
写入缓存。

## 事务一致性 {#transaction-consistency}

任意 PostgreSQL 会话中的映射表写入触发器，都会在提交变得可见前，通过
dirty-writer 计数为受影响的键或关系发布屏障并推进代次。写入完成前，读取会
绕过这些缓存项；代次检查会阻止过期的进行中读取发布结果，因此已提交写入之后
不会命中旧缓存。

RESP 读取使用 `pg_local_cache.role`，而不是客户端的 PostgreSQL 角色，并在各自
独立的短事务中执行。它们看不到客户端未提交的更改，不共享客户端的快照，也不
属于客户端的事务。设置 `pg_local_cache.enabled = off` 会绕过缓存。

## 共享内存与配置 {#shared-memory-and-configuration}

缓存项、关系状态、计数器、工作进程代次和 RESP 客户端槽位都在 postmaster 启动时分配，容量有明确上限。淘汰策略会检查一个大小有界、轮转取样的集合，并优先移除过期项；无法接纳新项时，回退读取源表，不会无限分配内存。

| 配置项 | 默认值 | 含义 |
|---|---:|---|
| `pg_local_cache.database` | `postgres` | 扩展服务的数据库 |
| `pg_local_cache.cache_entries` | `16384` | 共享行缓存容量 |
| `pg_local_cache.relation_states` | `1024` | 共享映射状态容量 |
| `pg_local_cache.memory_budget_mb` | `384` | 扩展启动内存预算 |
| `pg_local_cache.port` | `6380` | RESP 端口；`0` 仅用于回归测试和诊断，不提供读取服务 |
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
| `pg_local_cache.enabled` | `on` | SIGHUP 缓存紧急开关；关闭时 RESP 直接读取源表 |
| `pg_local_cache.tls` | `off` | 启用 RESP listener TLS；PostgreSQL 构建须支持 OpenSSL |
| `pg_local_cache.tls_cert_file` | 空 | PEM 格式的服务器证书/链；启用 TLS 时必需 |
| `pg_local_cache.tls_key_file` | 空 | PEM 格式的服务器私钥；启用 TLS 时必需 |
| `pg_local_cache.tls_ca_file` | 空 | 受信任的客户端 CA；设置后启用双向 TLS (mTLS) |
| `pg_local_cache.tls_min_protocol_version` | `TLSv1.2` | 最低 TLS 版本（`TLSv1.2` 或 `TLSv1.3`） |
| `pg_local_cache.allow_plaintext_network` | `off` | postmaster 设置，用于允许 IPv4 loopback 之外的明文监听 |
| `pg_local_cache.allow_superuser` | `off` | 仅供开发使用的角色限制覆盖 |

这些都是 postmaster 配置项。应在重启前设定容量。参阅[安装指南](INSTALL_EXISTING.md)了解软件包和重启步骤。

## RESP2 接口 {#optional-resp2-endpoint}

RESP2 使用相同的映射和共享缓存。协议键格式如下：

```text
CRUD:database.schema.table:{"pk_column":<json-scalar>,...}
```

RESP TLS 使用独立的 `pg_local_cache.tls_*` 配置，与 PostgreSQL 的 `ssl_*` 配置互不影响。SQL 端口上的
PostgreSQL TLS 不会保护 RESP；RESP TLS 也不会更改 SQL listener。启用 `pg_local_cache.tls`
并提供服务器证书和密钥；设置 `pg_local_cache.tls_ca_file` 后会验证客户端证书并启用双向 TLS (mTLS)。最低协议版本默认为
`TLSv1.2`，可提高到 `TLSv1.3`。使用 OpenSSL 系统默认密码套件。私钥须遵循 [PostgreSQL
服务器密钥文件规则](https://www.postgresql.org/docs/current/ssl-tcp.html)。loopback 之外建议使用
TLS。TLS 关闭时，loopback 之外的明文 listener 必须显式设置
`pg_local_cache.allow_plaintext_network = on`，且仅限可信网络。非 loopback listener 仍要求至少
32 字节的令牌；应优先使用权限受限的令牌文件，而非内联令牌。

运维参数 `pg_local_cache.enabled` 是 SIGHUP 参数，可用作缓存服务的紧急开关。要禁用缓存服务：

```sql
ALTER SYSTEM SET pg_local_cache.enabled = off;
SELECT pg_reload_conf();
```

每个 RESP worker 都会在下一个命令边界异步应用重载，且会等当前执行的命令结束。`local_cache.health()` 中的 `cache_enabled` 字段报告调用它的 SQL 会话所见设置；它不表示所有 worker 都已应用该设置。要重新启用，请同时执行：

```sql
ALTER SYSTEM SET pg_local_cache.enabled = on;
SELECT pg_reload_conf();
```

## 健康状态与监控 {#health-and-monitoring}

`local_cache.health()` 报告就绪状态与映射收敛情况。`local_cache.stats()` 返回 JSON 计数器。`local_cache.metrics()` 提供导出器使用的有类型指标行。

`stats()` 和 `metrics()` 中的 RESP 计数器包括：

- `sql_gets`
- `sql_meta`
- `sql_sets`
- `sql_dels`
- `sql_result_reuses`
- `tls_handshakes_total`
- `tls_handshake_failures_total`

数据库读取、失效、接纳拒绝、脏键回退、singleflight、工作进程和 RESP 的计数器分别统计。

下一步：参阅[安装指南](INSTALL_EXISTING.md)，了解 Debian 和 RPM 软件包验证、PGXS 源码构建、配置、重启、升级与卸载。
