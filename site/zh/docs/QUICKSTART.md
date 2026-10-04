---
layout: doc
lang: zh
translation_key: QUICKSTART
title: 在本地试用 pg_local_cache
seo_title: 在本地试用 pg_local_cache | pg_local_cache
description: "在一次性 PostgreSQL 实例中运行 pg_local_cache 3.0，通过 RESP 读取示例行、检查缓存命中、测试更新并清理演示，不修改现有数据库。"
section: 快速开始
permalink: /zh/docs/QUICKSTART.html
last_modified_at: '2026-09-16'
---

# 在本地试用 pg_local_cache {#try-pg_local_cache-locally}

此演示从当前检出构建 pg_local_cache，并启动独立的 PostgreSQL 16 服务器，不会安装到现有 PostgreSQL 服务器中。读取示例使用 Compose 覆盖层配置的 RESP 监听器。

需要 Git、Docker，以及支持 `up --wait` 的 Docker Compose。镜像从源码构建。

## 启动数据库 {#start-the-database}

```bash
git clone https://github.com/profundium/pg_local_cache.git
cd pg_local_cache
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml up --build --wait
```

演示将 PostgreSQL 和 RESP 绑定到 loopback 端口 `55432` 和 `56379`，不使用持久卷，数据存放在容器本地 tmpfs 中。RESP 覆盖层在容器网络内监听，并显式启用演示用明文监听器。停止容器会丢弃数据。`demo-only` 和公开 RESP 令牌仅供此 loopback 演示使用；生产环境请使用自己的凭据。

如果端口 55432 已被占用，请在启动 Compose 前设置 `PGLC_DEMO_PORT`，并在运行 Node.js 示例时保持该变量有效：

```bash
export PGLC_DEMO_PORT=55433
```

## 通过 RESP 读取 {#read-as-an-application-role}

初始化会在 `public.items` 中创建 4,096 行。只有此表关联到缓存。`demo` 角色不是超级用户。

```bash
export REDISCLI_AUTH=DemoRespToken_0123456789abcdef0123456789
redis-cli -2 -p 56379 MGET \
  'CRUD:pglc_demo.public.items:{"id":42}' \
  'CRUD:pglc_demo.public.items:{"id":7}' \
  'CRUD:pglc_demo.public.items:{"id":42}' \
  'CRUD:pglc_demo.public.items:{"id":999999}'
```

响应保留键顺序和重复项。第一和第三个位置对应行 42；最后一个位置是 RESP null，因为键 999999 不存在。RESP 请求会省略输入中的 null 键；需要时客户端辅助函数可恢复这些位置。

以数据库管理员身份查看计数器：

```bash
docker compose -f examples/compose.yaml exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 -U postgres -d pglc_demo \
  -c 'SELECT local_cache.health();' \
  -c 'SELECT local_cache.stats();'
```

在这个新建演示中，`local_cache.health()` 应报告 `ready: true`，重复读取应使 `cache_hits` 增加。如有需要，可在 `local_cache.stats()` 和 `local_cache.health()` 中查看 `cache_misses`、`database_reads` 和 `cache_enabled`。

## 检查提交与回滚 {#check-commit-and-rollback}

使用 Node.js 20 或更高版本：

```bash
npm --prefix examples/node-postgres ci --ignore-scripts
npm --prefix examples/node-postgres run demo
```

测试使用 RESP 读取、使用 PostgreSQL 写入。它检查热缓存命中、输入顺序、重复键与缺失键、缓存失效以及已提交更新的可见性。RESP worker 使用配置的 PostgreSQL 角色，不会共享应用的 SQL 事务或快照。

参阅[缓存失效指南](cache-invalidation.md)或 [Node.js 查询说明](node-postgres.md)。

## 连接应用 {#connect-your-application}

- [Node.js](node-postgres.md)：使用 RESP 读取缓存，使用 `pg` 执行 SQL 写入。
- [Go](go.md)：使用 RESP 读取缓存，使用 `pgx` 执行 SQL 写入。
- [RESP](resp.md)：使用 Redis 客户端连接。

接下来，[比较相同的预备 SQL 与 RESP 工作负载](BENCHMARKS.md#run-the-same-comparison-on-every-client)。若要报告结果或配置问题，请提交[工作负载报告](https://github.com/profundium/pg_local_cache/issues/new?template=workload.yml)，附上环境信息、基准测试 JSON 或错误日志。

## 清理演示 {#remove-the-demo}

```bash
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml down
```

本地构建的 Docker 镜像会保留，供下次运行使用。无需重启或恢复宿主机上的 PostgreSQL 服务。

对于现有数据库，请遵循[安装指南](INSTALL_EXISTING.md)。该流程对权限、配置和重启的要求不同。
