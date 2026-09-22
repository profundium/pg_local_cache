---
layout: doc
lang: zh
translation_key: QUICKSTART
title: 在本地试用 pg_local_cache
seo_title: 在本地试用 pg_local_cache | pg_local_cache
description: 在一次性 PostgreSQL 实例中运行 pg_local_cache 2.0，读取示例行、检查缓存命中、测试更新并清理演示，不修改现有数据库。
section: 快速开始
permalink: /zh/docs/QUICKSTART.html
last_modified_at: '2026-09-16'
---

# 在本地试用 pg_local_cache {#try-pg_local_cache-locally}

此演示从你的本地检出构建 pg_local_cache，并启动独立的 PostgreSQL 16 服务器，不会安装到现有 PostgreSQL 服务器中。

需要 Git、Docker，以及支持 `up --wait` 的 Docker Compose。镜像从源码构建。

## 启动数据库 {#start-the-database}

```bash
git clone https://github.com/profundium/pg_local_cache.git
cd pg_local_cache
docker compose -f examples/compose.yaml up --build --wait
```

演示将 PostgreSQL 绑定到 `127.0.0.1:55432`，不启用 RESP 监听器，也不使用持久卷；数据存放在容器本地 tmpfs 中。停止容器会丢弃数据。`demo-only` 仅适用于此回环地址演示；生产环境请使用自己的凭据。

如果端口 55432 已被占用，请在启动 Compose 前设置 `PGLC_DEMO_PORT`，并在运行 Node.js 示例时保持该变量有效：

```bash
export PGLC_DEMO_PORT=55433
```

## 使用应用角色读取 {#read-as-an-application-role}

初始化会在 `public.items` 中创建 4,096 行。只有此表关联到缓存。`demo` 角色不是超级用户。

```bash
docker compose -f examples/compose.yaml exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 -U demo -d pglc_demo <<'SQL'
SELECT unnest(local_cache.mget(
  'public.items'::regclass,
  ARRAY[42, 7, 42, NULL, 999999]::bigint[]
));
SELECT unnest(local_cache.mget(
  'public.items'::regclass,
  ARRAY[42, 7, 42, NULL, 999999]::bigint[]
));
SQL
```

两次调用都返回相同且有序的行。第一和第三个位置对应行 42。最后两个位置是 SQL `NULL`：一个输入为空，另一个键 999999 不存在。psql 默认将 SQL 空值显示为空白。

函数返回 **`text[]`**。上面的 `unnest` 将数组中的每个元素显示为单独一行。

以数据库管理员身份查看计数器：

```bash
docker compose -f examples/compose.yaml exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 -U postgres -d pglc_demo \
  -c 'SELECT local_cache.health();' \
  -c 'SELECT local_cache.stats();'
```

在这个新建演示中，`local_cache.health()` 应报告 `ready: true`，重复读取应使 `sql_cache_hits` 增加。如果命中始终为零，按照[失效指南](cache-invalidation.md#inspect-the-cause-of-a-miss)检查 `sql_cache_misses`、`sql_cache_fills` 和 `sql_cache_bypasses`。

## 检查提交与回滚 {#check-commit-and-rollback}

使用 Node.js 20 或更高版本：

```bash
npm --prefix examples/node-postgres ci --ignore-scripts
npm --prefix examples/node-postgres run demo
```

测试会分别建立读连接和写连接，检查热缓存命中、输入顺序、重复键与缺失键、未提交更新、读取自身写入、回滚以及已提交更新。断言失败时，以非零状态退出。

参阅[双会话 SQL 演练](cache-invalidation.md)或 [Node.js 查询说明](node-postgres.md)。

## 连接应用 {#connect-your-application}

- [Node.js](node-postgres.md)：使用现有 `pg` 连接或连接池。
- [Go](go.md)：使用 `pgx` 连接，并解码返回的行。
- [RESP](resp.md)：启用可选接口，并通过 Redis 客户端连接。

接下来，[比较相同的 SQL 与 RESP 工作负载](BENCHMARKS.md#run-the-same-comparison-on-every-client)。若要报告结果或配置问题，请提交[工作负载报告](https://github.com/profundium/pg_local_cache/issues/new?template=workload.yml)，附上环境信息以及基准测试 JSON 或错误日志。

## 清理演示 {#remove-the-demo}

```bash
docker compose -f examples/compose.yaml down
```

本地构建的 Docker 镜像会保留，供下次运行使用。无需重启或恢复宿主机上的 PostgreSQL 服务。

对于现有数据库，请遵循[安装指南](INSTALL_EXISTING.md)。该流程对权限、配置和重启的要求不同。
