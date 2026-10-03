---
layout: doc
lang: zh
translation_key: INSTALL_EXISTING
title: 在 PostgreSQL 14–18 上安装 pg_local_cache
seo_title: 在 PostgreSQL 14–18 上安装 pg_local_cache | pg_local_cache
description: 使用经过验证的 Linux 二进制包或 PGXS 安装 pg_local_cache 扩展，配置预加载、重启、验证，并按流程安全恢复。
section: 安装
permalink: /zh/docs/INSTALL_EXISTING.html
last_modified_at: '2026-09-05'
---

# 在现有 PostgreSQL 服务器上安装 pg_local_cache {#install-pg_local_cache-on-an-existing-postgresql-server}

使用经过验证的 Linux 软件包安装扩展，或使用 PostgreSQL 的 PGXS 工具链构建。两种方式都需要先执行一次受控的 PostgreSQL 重启，然后才能运行 `CREATE EXTENSION`。

> **安排维护窗口：** 首次启用需要修改 `shared_preload_libraries`。保留其中已有条目，并且仅在预检成功后重启正确的集群。

## 选择安装方式 {#choose-an-installation-path}

| 方式 | 适用场景 | 重启负责人 |
|---|---|---|
| 最新的已验证二进制包 | 本地 Linux amd64 集群 | `pg_ctl` 引导脚本 |
| 固定版本的已验证二进制包 | 生产环境与受管运维 | systemd、`pg_ctl` 或外部运维控制器 |
| PGXS 源码构建 | 未支持的平台或自定义 PostgreSQL 安装 | 现有运维流程 |

已发布的二进制包支持 Linux amd64 上、使用 glibc 或 musl 的 PostgreSQL 14–18。下方固定版本示例使用 pg_local_cache 2.0.1。

## 快速二进制安装 {#fast-binary-install}

对于由 `pg_ctl` 管理的本地集群：

```bash
curl -fsSL https://github.com/profundium/pg_local_cache/releases/latest/download/install-latest.sh | bash -s -- app
```

将 `app` 替换为数据库名。这会启用 SQL-only 模式，设置 `pg_local_cache.port = 0`。

引导脚本会确定一个发布标签，使用该版本的 `SHA256SUMS` 验证 `fetch-release.sh`，选择匹配 PostgreSQL 与 libc 的归档文件，完成校验、安装、重启、创建扩展，最后运行 `local_cache.health()`。

如果你的安全策略不允许 `curl | bash`，请先查看脚本：

```bash
curl -fsSLO https://github.com/profundium/pg_local_cache/releases/latest/download/install-latest.sh
less install-latest.sh
bash install-latest.sh app
```

## 受控二进制安装 {#controlled-binary-install}

使用随版本发布的辅助脚本下载固定版本：

```bash
curl -fsSLO https://github.com/profundium/pg_local_cache/releases/download/v2.0.1/fetch-release.sh
bash fetch-release.sh --release-tag v2.0.1 --output-directory ./pg_local_cache-package
```

先运行预检，再明确选择重启方式：

```bash
sudo ./pg_local_cache-package/install.sh preflight --database app
sudo ./pg_local_cache-package/install.sh install \
  --database app \
  --restart-method systemd \
  --systemd-unit postgresql@16-main
```

支持 `systemd`、`pg_ctl` 和 `none` 三种重启方式。使用 Patroni、Kubernetes operator 或其他外部控制器时，选择 `none`。通过该控制器重启后，再执行验证：

```bash
sudo ./pg_local_cache-package/install.sh verify --database app
```

安装程序会输出状态目录。请保留该目录，直到验证成功；其中包含 `recover` 所需的在线备份。

## 从源码构建 {#build-from-source}

使用与目标 PostgreSQL 服务器相同的 `pg_config`。先安装对应的服务器开发头文件、C 编译器和 GNU Make。

```bash
git clone --branch v2.0.1 --depth 1 https://github.com/profundium/pg_local_cache.git
cd pg_local_cache
make PG_CONFIG=/usr/lib/postgresql/16/bin/pg_config
sudo make install PG_CONFIG=/usr/lib/postgresql/16/bin/pg_config
```

从干净的检出构建，使二进制文件能够记录 Git 提交标识。源码安装只复制扩展文件。之后还需完成预加载配置、重启以及下方的 SQL 初始化。

## 重启前配置 {#configure-before-restart}

使用默认容量与内存预算的最小 SQL-only 配置：

```conf
shared_preload_libraries = 'pg_local_cache'
pg_local_cache.database = 'app'
pg_local_cache.role = 'local_cache_worker'
pg_local_cache.cache_entries = 16384
pg_local_cache.memory_budget_mb = 384
pg_local_cache.port = 0
```

保留 `shared_preload_libraries` 中已有的条目。在此处以及下方 SQL 授权语句中，将 `app` 替换为实际数据库名。此内存预算仅针对扩展，不是整个 PostgreSQL 服务器。

应一并规划 `cache_entries`、关系状态、客户端、工作进程和 `memory_budget_mb`。二进制安装程序的预检会拒绝不一致的方案。源码构建在重启前也需要同样的容量审查；不要在未检查内存预算的情况下增加缓存项数量。

## 初始化源码安装 {#initialize-a-source-installation}

重启后，以数据库超级用户连接到配置的数据库。首次手动安装时运行：

```sql
CREATE EXTENSION IF NOT EXISTS pg_local_cache;
CREATE ROLE local_cache_worker LOGIN NOINHERIT NOSUPERUSER
    NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT CONNECT ON DATABASE app TO local_cache_worker;
GRANT USAGE ON SCHEMA local_cache TO local_cache_worker;
GRANT SELECT ON TABLE local_cache.mapping TO local_cache_worker;
```

二进制安装程序会创建此角色并赋予元数据访问权限；如果已完成该步骤，请跳过此代码块。若自定义 `pg_local_cache.role`，各处均应使用该名称。不要复用拥有应用表的角色。

**即使 `pg_local_cache.port = 0`，也必须创建此角色。** 关联表时，即使在 SQL-only 模式下也会验证该角色。它必须与表所有者不同，并具有上方列出的角色属性与元数据权限。`attach_table` 会管理它对各映射表的访问权限。SQL-only 模式无需密码或网络监听器。

## 关联表 {#attach-a-table}

使用具有受支持主键的现有永久表。在配置的数据库中，以数据库超级用户运行：

```sql
SELECT local_cache.attach_table('public.items'::regclass);
SELECT local_cache.health();
```

只为现有应用角色授予所需权限：

```sql
GRANT SELECT ON public.items TO app_user;
GRANT USAGE ON SCHEMA local_cache TO app_user;
GRANT EXECUTE ON FUNCTION local_cache.mget(regclass, anyarray) TO app_user;
```

扩展不会重写普通 `SELECT`。

## 验证冷填充与热命中 {#verify-cold-fill-and-warm-hit}

```sql
SELECT local_cache.invalidate('public.items');
SELECT local_cache.mget('public.items'::regclass, ARRAY[1]::bigint[]);
SELECT local_cache.mget('public.items'::regclass, ARRAY[1]::bigint[]);
SELECT local_cache.stats();
```

确认 `local_cache.health()` 处于就绪状态、映射已收敛，且 SQL 缓存计数器按预期变化。

## 启用可选 RESP2 {#enable-optional-resp2}

RESP2 会增加监听器、工作进程和一个共享令牌。它使用关联表时要求的同一个专用 PostgreSQL 角色：

```bash
sudo ./pg_local_cache-package/install.sh preflight \
  --database app \
  --mode resp \
  --token-file /secure/path/token

sudo ./pg_local_cache-package/install.sh install \
  --database app \
  --mode resp \
  --token-file /secure/path/token \
  --restart-method systemd \
  --systemd-unit postgresql@16-main
```

将监听器保持在 `127.0.0.1`，或置于经过认证的 TLS 代理之后。RESP 客户端共享配置的工作角色，不会获得各客户端独立的 PostgreSQL ACL 上下文。

## 恢复失败的二进制安装 {#recover-a-failed-binary-install}

使用安装程序输出的状态目录：

```bash
sudo ./pg_local_cache-package/install.sh recover \
  --state-directory /path/printed/by/install
```

如果新的 postmaster 已开始接收流量，应先检查记录的状态与运维影响，再决定是否执行恢复。

## 故障排查 {#troubleshooting}

- **预加载错误：** 确认目标集群配置，并重启正确的 postmaster。
- **工作角色缺失或被拒绝：** 完成上方 SQL 初始化，包括角色属性和元数据授权；SQL-only 模式也不例外。
- **表被拒绝：** 使用永久、非分区、未启用 RLS 且具有受支持主键的表。
- **`mget` 权限错误：** 授予源表 `SELECT`、模式 `USAGE` 以及函数 `EXECUTE` 权限。
- **绕过缓存：** 检查隔离级别、当前事务写入、恢复状态、行大小与指标。
- **DDL 后映射过期：** 运行 `local_cache.reconcile_table('public.items'::regclass)`。

下一步：阅读[技术参考](TECHNICAL.md)，了解 SQL 约定、一致性、内存规划、监控与 RESP 安全。
