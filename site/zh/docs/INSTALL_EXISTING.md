---
layout: doc
lang: zh
translation_key: INSTALL_EXISTING
title: 在 PostgreSQL 14–18 上安装 pg_local_cache
seo_title: 在 PostgreSQL 14–18 上安装 pg_local_cache
description: 安装经过验证的 Debian 或 RPM 软件包，使用 PGXN 或 PGXS，配置 preload，重启 PostgreSQL，并初始化、升级或卸载 pg_local_cache。
section: 安装
permalink: /zh/docs/INSTALL_EXISTING.html
last_modified_at: "2026-10-05"
---

# 在现有 PostgreSQL 服务器上安装 pg_local_cache {#install-pg_local_cache-on-an-existing-postgresql-server}

本指南适用于 Linux 和 PostgreSQL 14–18。安装和配置需要数据库超级用户权限。将扩展加入 <code>shared_preload_libraries</code> 后需要重启一次 PostgreSQL。

## 1. 要求 {#choose-an-installation-path}

从源码构建时，请使用目标服务器对应的 <code>pg_config</code>。应由负责管理 PostgreSQL 集群的服务或 operator 安排重启。

## 2. 安装软件包 {#fast-binary-install}

从同一个 [GitHub 发布版本](https://github.com/profundium/pg_local_cache/releases)下载适用于 PostgreSQL 主版本和系统架构的软件包及 <code>SHA256SUMS</code>。

### Debian 和 Ubuntu {#controlled-binary-install}

下载 <code>postgresql-&lt;major&gt;-pg-local-cache_&lt;version&gt;-1_&lt;arch&gt;.deb</code>。软件包在 Debian 12 上构建，要求 glibc 版本不低于 2.36。校验清单包含该版本的全部文件；只下载部分文件时请使用 <code>--ignore-missing</code>。

先验证校验和与构建来源，再安装：

```bash
sha256sum -c --ignore-missing SHA256SUMS
gh attestation verify <file>.deb --repo profundium/pg_local_cache
sudo apt install ./<file>.deb
```

### RHEL、Rocky Linux 和 AlmaLinux 9

这些 RPM 要求使用相同主版本的 PGDG PostgreSQL。请为你的 EL 版本和
PostgreSQL 主版本启用
[PGDG Yum 仓库](https://www.postgresql.org/download/linux/redhat/)，并先从
PGDG 安装 `postgresql<major>-server`。扩展 RPM 安装在
`/usr/pgsql-<major>`。发行版自带的 PostgreSQL 软件包使用不同的软件包名、
路径和服务名；这类服务器请按[源码构建说明](#build-from-source)操作。

下载与 PostgreSQL 主版本和系统架构匹配的 <code>.rpm</code>。验证后安装：

```bash
sha256sum -c --ignore-missing SHA256SUMS
gh attestation verify <file>.rpm --repo profundium/pg_local_cache
sudo dnf install ./<file>.rpm
```

### PGXN

安装 PGXN 客户端、目标 PostgreSQL 服务器的开发头文件、C 编译器和 GNU
Make。请明确指定目标服务器的 <code>pg_config</code>；否则 PGXN 会使用
<code>PATH</code> 中第一个找到的版本。安装系统文件需要 root 权限。参见
[PGXN install 选项](https://pgxn.github.io/pgxnclient/usage.html#pgxn-install)。

```bash
pgxn install --pg_config /path/to/pg_config --sudo sudo pg_local_cache
```

### 从源码构建 {#build-from-source}

安装目标 PostgreSQL 版本的开发头文件、C 编译器和 GNU Make。使用该版本的 <code>pg_config</code> 编译并安装扩展：

```bash
make PG_CONFIG=/path/to/pg_config && \
  sudo make PG_CONFIG=/path/to/pg_config install
```

## 3. 配置 <code>postgresql.conf</code> {#configure-before-restart}

保留 `shared_preload_libraries` 中已有的条目，并添加 `pg_local_cache`。将
`app` 替换为扩展服务的数据库名称。

### 配置 RESP 监听器 {#enable-optional-resp2}

使用专用 worker 角色、受保护的令牌文件和原生 TLS 配置 RESP listener。以下配置通过信任客户端 CA 启用双向 TLS (mTLS)。
启用原生 RESP TLS 要求 PostgreSQL 构建时包含 OpenSSL 支持。

```conf
shared_preload_libraries = 'pg_local_cache'
pg_local_cache.database = 'app'
pg_local_cache.role = 'local_cache_worker'
pg_local_cache.port = 6380
pg_local_cache.bind_address = '10.0.0.10'
pg_local_cache.auth_token_file = '/secure/path/token'
pg_local_cache.cache_entries = 16384
pg_local_cache.memory_budget_mb = 384
pg_local_cache.tls = on
pg_local_cache.tls_cert_file = '/secure/path/resp.crt'
pg_local_cache.tls_key_file = '/secure/path/resp.key'
pg_local_cache.tls_ca_file = '/secure/path/client-ca.crt'
pg_local_cache.tls_min_protocol_version = 'TLSv1.2'
pg_local_cache.allow_plaintext_network = off
```

将 `10.0.0.10` 替换为客户端可访问的地址。TLS 默认关闭；启用时必须提供服务器证书和密钥。此配置使用 TLS 保护非 loopback RESP
listener，并通过 CA 设置验证客户端证书。若只验证服务器，请将 `pg_local_cache.tls_ca_file`
留空并省略客户端证书。RESP TLS 与 PostgreSQL 的 `ssl_*` 配置独立。未启用 TLS 时，loopback 之外的 listener
必须设置 `pg_local_cache.allow_plaintext_network = on`；此显式选项仅限可信网络。

私钥须遵循 [PostgreSQL
服务器密钥文件规则](https://www.postgresql.org/docs/current/ssl-tcp.html)：若归 PostgreSQL
操作系统用户所有，权限为 `0600`；或者归 root 所有、权限为 `0640`，并可由服务器所属组读取。

请共同规划缓存条目数、关系状态、worker 和客户端数量，以及
`memory_budget_mb`。容量和内存建议见[技术参考](TECHNICAL.md#shared-memory-and-configuration)。

## 4. 重启 PostgreSQL

通过管理该集群的服务或 operator 执行重启。使用 systemd 时：

```bash
# Debian and Ubuntu
sudo systemctl restart postgresql@<major>-main
# RHEL, Rocky Linux, and AlmaLinux
sudo systemctl restart postgresql-<major>
```

使用 Patroni 时，请更新集群配置并通过 Patroni 重启：

```bash
patronictl edit-config <cluster>
patronictl restart <cluster>
```

在 Kubernetes 中，构建包含对应软件包的自定义 PostgreSQL 镜像，再通过 operator 部署。CloudNativePG 扩展镜像正在规划中。

## 5. 初始化 {#initialize-a-source-installation}

以数据库超级用户连接到已配置的数据库。创建扩展和专用 worker 角色，并授予其访问元数据的权限：

```sql
CREATE EXTENSION IF NOT EXISTS pg_local_cache;
CREATE ROLE local_cache_worker LOGIN NOINHERIT NOSUPERUSER
    NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT CONNECT ON DATABASE app TO local_cache_worker;
GRANT USAGE ON SCHEMA local_cache TO local_cache_worker;
GRANT SELECT ON TABLE local_cache.mapping TO local_cache_worker;
```

请将 worker 角色与应用表所有者角色分开。为每个具有受支持主键的永久表启用缓存：

### 关联表 {#attach-a-table}

```sql
SELECT local_cache.attach_table('public.items'::regclass);
```

### 检查状态 {#verify-cold-fill-and-warm-hit}

```sql
SELECT local_cache.health();
```

确认扩展已就绪，映射已同步。

## 6. 升级 {#recover-a-failed-binary-install}

从 2.x 升级到 3.0 时，请先遵循[升级指南](UPGRADING.md)。将应用从 SQL
`mget` 迁移到 RESP `MGET`，安装对应的 3.1.0 软件包，重启 PostgreSQL，
然后在每个安装了该扩展的数据库中更新扩展：

```sql
ALTER EXTENSION pg_local_cache UPDATE;
```

## 7. 卸载 {#troubleshooting}

以数据库超级用户身份解除所有表的关联并删除扩展。从 <code>shared_preload_libraries</code> 中移除 <code>pg_local_cache</code>，重启 PostgreSQL，再用相应的软件包管理器卸载软件包：

```sql
SELECT local_cache.detach_table('public.items'::regclass);
DROP EXTENSION pg_local_cache;
```

```bash
sudo apt remove postgresql-<major>-pg-local-cache
sudo dnf remove pg_local_cache_<major>
```

下一步：参阅[技术参考](TECHNICAL.md)，了解 SQL、内存规划、监控和 RESP 安全性。
