---
layout: doc
lang: zh
translation_key: TECHNICAL
title: pg_local_cache 技术参考
seo_title: pg_local_cache RESP API、一致性、内存与配置
description: 参考文档涵盖 RESP 读取、支持的表、事务屏障、TLS、共享内存、指标和 PostgreSQL 设置。
section: 技术
permalink: /zh/docs/TECHNICAL.html
---

# pg_local_cache 技术参考 {#pg_local_cache-technical-reference}

本文介绍 RESP2 端点、缓存一致性、资源限制和安全性。配置步骤请参阅[快速入门](QUICKSTART.md)和[安装指南](INSTALL_EXISTING.md)。

## 支持的表和键 {#supported-tables-and-keys}

可附加具有有效主键的永久堆表。不支持分区表、继承表、启用行级安全性（RLS）的表、临时表、外部表以及归扩展所有的表。支持的主键类型包括 `smallint`、`integer`、`bigint`、`text`、`varchar`、使用确定性排序规则的 `char`，以及 `uuid`；复合键最多可包含 16 列，且列类型必须来自上述类型。

DDL 变更后必须重新协调映射。请参阅[安装指南](INSTALL_EXISTING.md#attach-a-table)。

## 读取路径与安全回退 {#read-path-and-safe-fallback}

![RESP MGET 读取路径：缓存命中、受屏障保护的源数据填充，以及通过紧急开关绕过缓存。](../../docs/diagrams/read-path.svg)

每个 RESP `MGET` 键都会在查找前经过校验和规范化。符合条件的缓存命中会返回完整行的 JSON。未命中时，worker 会在一个简短事务中读取源表；只有读取屏障仍有效时才会发布缓存填充。不存在的行返回 `nil`。如果行的负载无法放入共享缓存，只要 JSON 未超过 RESP 值大小限制，仍可由 PostgreSQL 返回。

无分配快速路径仅适用于单键请求，且表的主键必须只有一列，类型为受支持的整数、`text` 或无长度上限的 `varchar`，typmod 不受限，并且键 JSON 符合扫描器支持的格式。文本键要求数据库编码为 UTF-8；整数键在其他编码下也可使用快速路径。其他键形式、多键请求和不受支持的缓存状态使用通用路径。

### 延迟处理未命中与锁期限 {#deferred-misses-and-lock-deadlines}

进入 SPI 前，worker 会尝试不等待地获取源关系的 `AccessShareLock`。若锁被占用，worker
会释放加载 claim、回滚事务，并将请求放入每个 worker 的有界队列：最多
`pg_local_cache.max_deferred_misses` 个（默认 `8`），且每个 worker 保留的请求字节总量不
超过 512 KiB。每个客户端最多有一个延迟请求。队列已满时按顺序返回
`-ERR busy: relation locked, retry`。同一客户端的后续命令等待，其他客户端继续运行。
重试会验证 mapping generation，并使用剩余的 `statement_timeout`；超时则按顺序返回
`-ERR MGET deadline exceeded`。此机制只覆盖最初的关系锁。RESP `STAT` 会报告 worker 本地
的 `deferred_misses_total`、`deferred_misses_current`、`deferred_timeouts_total` 和
`deferred_rejections_total`。

## 事务一致性 {#transaction-consistency}

![写入失效：提交前的屏障保护已提交写入；仅在屏障发布前回滚才会保留原缓存项。](../../docs/diagrams/write-invalidation.svg)

映射表上的行级和语句级触发器会在事务本地状态中记录脏键或受影响的关系。提交前回调会发布失效屏障并递增代数。屏障建立前开始的填充无法发布过期数据。仅在屏障发布前回滚，事务的脏状态才会被丢弃，原缓存项仍然有效。若屏障发布后事务中止，失效不会撤销，受影响的缓存项仍为无效。

RESP 源读取使用 `pg_local_cache.role`，并在独立于客户端 SQL 事务的短事务中执行。

缓存、索引、marker 和 arena 使用独立分区锁。写入会收集去重后的键；按键 fence 保护已有
缓存项，独立 marker 保护没有缓存项的键，并在 writer 持有时阻止新填充。marker 或事务键
容量耗尽时，fence 扩大到整个关系；关系状态不可用时扩大到全局范围。这是 generation
fence，不是单一的全局缓存锁。

## 内存与设置 {#shared-memory-and-configuration}

扩展会在 PostgreSQL 启动时预分配有界的共享缓存、映射以及 worker/客户端状态。`memory_budget_mb` 限制扩展可确定性分配的内存。准入失败和逐出都不会突破配置容量；读取会回退到 PostgreSQL。

`cache_entries` 统计描述符，而非固定行槽位。键和行 JSON 位于各分区 arena；按需分配
64 KiB 页面，块类别为 256 字节至 16 KiB。无可用块时，行仍由 PostgreSQL 返回，但不会
进入缓存。`lock_partitions` 默认 `64`，范围为 `16` 至 `256` 的 2 次幂；小缓存减少分区数，以每分区 32 个描述符为目标，但分区数至少为 16。Marker 自动上限为 `min(16384, max(1024, floor(cache_entries / 4)))` 个条目和
`min(16, max(1, floor(memory_budget_mb / 25)))` MiB 键内存；`-1` 表示自动计算。内置
`cache_entries` 默认值 `262144` 按 384 MiB 默认预算计算，并至少为 arena 预留一半；范围
为 `128`–`16777216`。内存充足且行较小时可容纳数百万个键。所有组件都纳入预算检查。

每个 RESP worker 的软 `RLIMIT_NOFILE` 至少为 `min(max_clients, max_clients_per_worker) + 33`；提高客户端槽位时，按需提高进程或容器的 `nofile` 限制。

| 设置 | 默认值 | 范围 | 重载方式 |
|---|---:|---|---|
| `pg_local_cache.enabled` | `on` | `on` / `off` | SIGHUP |
| `pg_local_cache.allow_plaintext_network` | `off` | `on` / `off` | 重启 |
| `pg_local_cache.tls` | `off` | `on` / `off` | 重启 |
| `pg_local_cache.tls_cert_file` | 空 | PEM 文件路径 | 重启 |
| `pg_local_cache.tls_key_file` | 空 | PEM 文件路径 | 重启 |
| `pg_local_cache.tls_ca_file` | 空 | CA PEM 文件路径 | 重启 |
| `pg_local_cache.tls_min_protocol_version` | `TLSv1.2` | `TLSv1.2` / `TLSv1.3` | 重启 |
| `pg_local_cache.port` | `6380` | `0`–`65535`；`0` 表示禁用 RESP | 重启 |
| `pg_local_cache.workers` | `4` | `1`–`32` | 重启 |
| `pg_local_cache.cache_entries` | `262144` | `128`–`16777216` | 重启 |
| `pg_local_cache.dirty_marker_entries` | `-1` | `-1` 或 `128`–`1048576` | 重启 |
| `pg_local_cache.dirty_marker_memory_mb` | `-1` | `-1` 或 `1`–`1024` MiB | 重启 |
| `pg_local_cache.lock_partitions` | `64` | `16`–`256` 的 2 次幂；小缓存使用较少分区 | 重启 |
| `pg_local_cache.relation_states` | `1024` | `128`–`8192` | 重启 |
| `pg_local_cache.max_clients` | `256` | `1`–`4096`；不得超过 worker 槽位数 | 重启 |
| `pg_local_cache.max_clients_per_worker` | `64` | `1`–`4096` | 重启 |
| `pg_local_cache.memory_budget_mb` | `384` | `64`–`8192` MB | 重启 |
| `pg_local_cache.idle_timeout_ms` | `300000` | `1000`–`86400000` | 重启 |
| `pg_local_cache.statement_timeout_ms` | `2000` | `100`–`60000` | 重启 |
| `pg_local_cache.lock_timeout_ms` | `250` | `10`–`60000` | 重启 |
| `pg_local_cache.singleflight_wait_ms` | `25` | `0`–`1000` | 重启 |
| `pg_local_cache.max_deferred_misses` | `8` | `1`–`64` per worker | 重启 |
| `pg_local_cache.max_pipeline_commands` | `256` | `1`–`4096` | 重启 |
| `pg_local_cache.max_dirty_keys` | `4096` | `128`–`16384` | 重启 |
| `pg_local_cache.bind_address` | `127.0.0.1` | IPv4 地址 | 重启 |
| `pg_local_cache.database` | `postgres` | 数据库名称 | 重启 |
| `pg_local_cache.role` | `local_cache_worker` | PostgreSQL LOGIN 角色 | 重启 |
| `pg_local_cache.auth_token_file` | 空 | 由 PostgreSQL 操作系统用户拥有、权限为 `0400` 或 `0600` 的文件 | 重启 |
| `pg_local_cache.auth_token` | 空 | 内联令牌；仅用于开发 | 重启 |
| `pg_local_cache.allow_superuser` | `off` | `on` / `off`；仅用于开发 | 重启 |

除 `enabled` 外，所有设置都是 postmaster 参数，修改后需重启。客户端槽位要求 `max_clients <= workers × max_clients_per_worker`。

## RESP2 端点 {#optional-resp2-endpoint}

端点接受 RESP2。键格式为 `CRUD:<db>.<schema>.<table>:<json pk>`。`MGET` 按请求顺序返回结果并保留重复项；不存在的行对应一个 `nil` 元素。每个请求最多包含 1,024 个键，每行 JSON 最大为 65,536 字节，编码后的响应最大为 66,560 字节。

支持的数据命令包括 `MGET`、`SET` 和 `DEL`；必须先执行 `AUTH`。端点还支持 `PING`、`ECHO`、`INFO`、`STAT`/`STATS`、有作用域限制的 `INVALIDATE`、`HELLO 2`、`QUIT`、`CLIENT SETINFO`/`SETNAME`/`GETNAME`/`ID`、`COMMAND` 和 `SELECT 0`。不支持的命令会返回错误。RESP 客户端使用数据库 0；数据库和表的作用域由每个缓存键指定。

## TLS 与安全模型 {#security-model}

默认情况下，监听器绑定到 IPv4 loopback。RESP TLS 使用扩展专属设置，与 PostgreSQL 的 `ssl_*` 设置无关。它要求 PostgreSQL 使用 OpenSSL 构建、配置 PEM 格式的服务器证书和密钥，并重启 PostgreSQL。设置 `tls_ca_file` 后，必须验证客户端证书（mTLS）；默认最低 TLS 版本为 1.2。

关闭 TLS 时，非 loopback 的明文监听器必须启用 `allow_plaintext_network=on`，且只能用于可信网络。非 loopback 监听器要求令牌至少为 32 字节。建议使用权限受限的令牌文件。所有 RESP 客户端共用一个已配置的 PostgreSQL LOGIN 角色；PostgreSQL 不会分别检查每个网络客户端的授权。默认禁用超级用户 worker，且仅建议在开发环境中使用。

## 缓存紧急开关 {#cache-kill-switch}

`pg_local_cache.enabled` 是通过 SIGHUP 重载的缓存紧急开关。关闭后，RESP 读取会绕过共享缓存并读取源表；`SET` 和 `DEL` 仍会通过 PostgreSQL 写入。worker 会在命令边界异步应用重载。`local_cache.health()` 报告发起调用的 SQL 会话设置，不代表每个 worker 都已确认。重新启用时，会先递增缓存 epoch，然后 worker 才恢复缓存读取。

## 指标与健康状态 {#health-and-monitoring}

`local_cache.health()` 报告就绪状态、缓存状态和映射收敛情况。`local_cache.stats()` 返回 JSON 计数器；`local_cache.metrics()` 返回供 exporter 使用的类型化指标行。

指标包括缓存命中、未命中和负缓存命中；源数据读写；失效和逐出；singleflight 的 leader、等待者、复用次数和超时；当前及峰值客户端数；连接数限制导致的拒绝；认证和协议错误；输出背压和慢客户端断开；worker 启动；脏键回退；映射重载失败和重试；TLS 握手及失败。Gauge 指标包括缓存项和关系容量、客户端和 worker 数量、映射收敛情况、共享/worker/估算内存以及配置的内存预算。


`stats()` 新增快速路径计数器（`fast_path_hits`、`fast_path_fallbacks` 及原因）、
`cache_memory_capacity_bytes`、`cache_memory_used_bytes`、`cache_fragmentation_bytes`、
`arena_admission_rejections_total`、marker 容量/用量/高水位/回退计数器以及有效 marker
限制。

接下来请参阅[快速入门](QUICKSTART.md)、[安装指南](INSTALL_EXISTING.md)和[升级指南](UPGRADING.md)。
