---
layout: doc
lang: zh
translation_key: UPGRADING
title: 将 pg_local_cache 从 2.x 升级到 3.0.0
seo_title: "将 pg_local_cache 从 2.x 升级到 3.0.0"
description: 将应用从 SQL mget 迁移到 RESP MGET，安全升级扩展，了解依赖错误并掌握回滚到 2.0.4 的方法。
section: 安装
permalink: /zh/docs/UPGRADING.html
last_modified_at: "2026-10-04"
---

# 将 pg_local_cache 从 2.x 升级到 3.0.0 {#upgrade-pg_local_cache-from-2x-to-300}

3.0.0 版本移除了 SQL 函数 `local_cache.mget(regclass, anyarray)`。
请使用经过身份验证的 RESP `MGET` 读取缓存的完整行：

```text
MGET CRUD:app.public.items:{"id":42} CRUD:app.public.items:{"id":7}
```

RESP worker 使用配置的 PostgreSQL 角色，不会继承应用的 SQL 权限、事务
或快照。投影、连接、行锁以及需要应用会话语义的读取仍使用 SQL。
[RESP 指南](resp.md)介绍客户端设置与键编码。

## 升级顺序 {#upgrade-order}

1. 修改应用，不再调用 SQL `mget`。确认 RESP `MGET` 使用专用 worker
   角色，并符合所需的授权模型。
2. 为当前运行的 PostgreSQL 主版本安装 3.0.0 软件包或库。
3. 如果 2.x listener 使用了 loopback 之外的
   `pg_local_cache.bind_address`，请在重启前配置原生 RESP TLS，并提供服务器
   证书和密钥。设置 `pg_local_cache.tls_ca_file` 可要求客户端证书
   (mTLS)。RESP TLS 与 PostgreSQL 的 `ssl_*` 配置独立。如果确实需要明文
   流量，请仅在可信网络中显式设置 `pg_local_cache.allow_plaintext_network = on`。
   未启用 TLS 且未显式允许明文时，RESP worker 将拒绝启动。
4. 重启 PostgreSQL，使其加载新的共享库。
5. 验证 listener：`SELECT local_cache.health();` 必须显示
   `workers_running = workers_configured`。发送 RESP `PING`，确认返回 `PONG`。
6. 检查已加载的库版本：

   ```sql
   SELECT current_setting('pg_local_cache.binary_version');
   ```

   结果必须为 `3.0.0`。
7. 在每个安装了该扩展的数据库中，以数据库超级用户身份连接并运行：

   ```sql
   ALTER EXTENSION pg_local_cache UPDATE;
   ```

迁移会删除并重新创建 `local_cache.metrics()`，因为移除 SQL 读取 API 的计数器后，
表结果类型发生了变化。如果用户对象依赖该函数，PostgreSQL
会拒绝删除。`local_cache.metrics()` 上的自定义授权会保留。迁移特意不使用 `CASCADE`。请自行删除或修改依赖它的视图
和函数，再重试扩展更新。其他保留的 C 函数会原位替换，因此其 OID、授权
以及关联表上的触发器保持有效。

## 回滚到 2.0.4 {#rollback-to-204}

没有降级脚本。要回到 2.0.4，请重新安装对应软件包，重启 PostgreSQL
以加载旧库，解除所有已映射表的关联，然后在每个数据库中重新创建扩展
并重新关联这些表：

```sql
SELECT local_cache.detach_table('public.items'::regclass);
DROP EXTENSION pg_local_cache;
CREATE EXTENSION pg_local_cache VERSION '2.0.4';
SELECT local_cache.attach_table('public.items'::regclass);
```

每张已映射表都要重复执行 `detach_table` 和 `attach_table`。回滚前请
保存已关联表清单以及授予扩展的自定义权限，以便之后恢复。依赖扩展
函数的用户对象可能触发 PostgreSQL 常规依赖错误并阻止删除；请明确
处理这些依赖。

## 库查找路径 {#library-lookup-path}

控制文件的 `module_pathname` 使用不带路径的库名称 `pg_local_cache`。
PostgreSQL 通过 `dynamic_library_path` 查找它（默认值包含 `$libdir`）。
如果服务器覆盖了该设置，请在重启前将软件包安装
`pg_local_cache` 的目录加入该路径。
