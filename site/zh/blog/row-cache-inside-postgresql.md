---
layout: post
lang: zh
translation_key: blog-row-cache-inside-postgresql
title: "无竞态的 cache-aside：PostgreSQL 内部行缓存实测"
description: pg_local_cache 3.1.0 将热点行保存在 PostgreSQL 共享内存中并通过 RESP 提供读取。对比 Valkey 与预编译 SQL，不返回过期行。
permalink: /zh/blog/row-cache-inside-postgresql/
date: "2026-10-12"
last_modified_at: "2026-10-12"
topic: performance
---

# 无竞态的 cache-aside {#cache-aside-without-the-race}

主键读取变热后，常见做法是在 PostgreSQL 旁边部署 Redis 或 Valkey，并
使用 cache-aside：先读缓存，未命中时再读 PostgreSQL 并填充缓存，每次
写入后删除对应的键。读取和写入一旦重叠，这种做法就会出问题。读者缓存
未命中后，可能恰好在写者提交前读到该行；写者的 `DEL` 先到，读者的填充
随后到达，旧行便留在缓存中，直到其他操作将它移除。我们之前介绍过这
[种延迟填充竞态](cache-aside-late-fill.md)。

pg_local_cache 将缓存放进 PostgreSQL 本身，因此缓存及其需要跟随的写入
都处于同一个事务系统中。

## 如何保持正确性 {#how-it-stays-correct}

- 已挂接表中的行保存在有界共享内存中，以主键索引，并由后台进程通过
  RESP2 提供读取。任何 Redis 客户端都可以用 `MGET` 读取；键的格式为
  `CRUD:<db>.<schema>.<table>:<json primary key>`。
- 表行触发器会在事务变为可见之前，为每个写入的键发布失效屏障。影响
  整个关系的变更（例如 `TRUNCATE`）会为整张表发布屏障。
- 缓存未命中时，读者先取得读取令牌。只有在此之后到填充之前，没有针对
  该键、该表或整个缓存发布屏障，结果才会写入缓存。与写入发生竞态的填充
  会被丢弃。
- 多个并发请求未命中同一个键时会合并：一个读者加载该行，其余读者复用
  结果。
- PostgreSQL 仍是数据的唯一可信来源。缓存是临时的，可以随时丢弃。

## 成本与收益 {#results}

以下数据来自 3.1.0：两台各配备 16 vCPU 的虚拟机（Xeon Gold 6354，10 GbE），
运行 PostgreSQL 16，并使用 Go/pgx 客户端。[基准测试页面](../docs/BENCHMARKS.md)
列出所有表格、原始 JSONL 文件和复现步骤。

**写入期间的读取。** 64 个读者，每次读取一个键，在 60k 行中均匀选键，
30,000 `UPDATE`/秒：

| 方案 | 读取/秒 | 命中率 | 过期条目 |
| --- | --- | --- | --- |
| pg_local_cache | 161,919 | 0.837 | 0 |
| Valkey cache-aside | 42,294 | 0.577 | 0 |
| 预编译 SQL | 106,949 | — | — |

写者数量不受限制时，Valkey cache-aside 在运行结束时留下了过期条目（混合
测试中为 1 个；两个 120 秒探测中分别为 2 个和 1 个）。pg_local_cache 在
每次测试结束时都没有过期条目，包括持续一小时、执行 252M `MGET` 和 72M
`UPDATE` 的测试。

**写入开销。** 相比直接写表，通过 pg_local_cache 执行 `UPDATE` 的吞吐量低
3.6–8.2%；执行 `UPDATE` 后再向 Valkey 发送 `DEL`，吞吐量低 28.9–63.7%。

**读取吞吐量。** 将服务器限制为一对 vCPU 时，pg_local_cache 每秒处理
210k 次读取，单线程 Valkey 为 180k；使用两对时，pg_local_cache 为 337k，
带 I/O 线程的 Valkey 为 266k。使用四对时，两者都达到受客户端限制的约
360k 平台；pg_local_cache 使用 5.2 个服务器 vCPU，Valkey 使用 8.0 个。
单线程 Valkey 每个请求消耗的 CPU 最少。在单键测试中，预编译 SQL 明显落后。

## 哪些场景不适用 {#when-not-to-use-it}

- 它不能替代 Redis：不支持 TTL、pub/sub 或数据结构，只能按主键读取整行。
- 热数据集必须能放入配置的内存；通过 RESP 返回的 JSON 行最大为 64 KiB。
- 它服务于单个可写主库上的一个数据库。
- 需要配置 `shared_preload_libraries` 并重启 PostgreSQL。

## 开始使用 {#try-it}

pg_local_cache 支持 PostgreSQL 14–18，采用 MIT 许可证。可从
[GitHub Releases](https://github.com/profundium/pg_local_cache/releases)（deb
和 rpm）、[PGXN](https://pgxn.org/dist/pg_local_cache/) 或 Pigsty 仓库安装，
然后阅读[快速入门](../docs/QUICKSTART.md)。
