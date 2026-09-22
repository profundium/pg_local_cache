---
layout: post
lang: zh
translation_key: blog-measure-postgresql-row-cache
title: PostgreSQL 行缓存何时有效：测量完整读取路径
description: 使用预备 SQL、SQL mget 与 RESP MGET 设计公平的 PostgreSQL 行缓存比较，分别考察热读取、未命中、批次大小、写入和客户端成本。
permalink: /zh/blog/measure-postgresql-row-cache/
date: '2026-09-22'
last_modified_at: '2026-09-22'
topic: performance
---

# PostgreSQL 行缓存何时有效 {#when-a-postgresql-row-cache-helps}

即使所有数据库页都从内存读取，数据库仍可能花时间执行查询、检查可见性和构造结果。行缓存试图避免其中一部分重复工作，但也引入键处理、缓存检查和序列化成本。真正有用的问题是：对你的工作负载而言，完整应用请求的成本是否下降。

`pg_local_cache` 提供显式 `local_cache.mget` API。普通 `SELECT` 保留原有 PostgreSQL 执行路径。因此，已预热的 `shared_buffers` 与已预热的行缓存，是不同的实验条件。

## 先写清结果约定 {#result-contract}

比较相同的键、列和输出形状。如果应用只需要两列，将这样的 SQL 投影与序列化整行比较，测量的就不是同一工作。如果调用者要求保留重复项、输入顺序，并为每个缺失键返回空结果，应在所有客户端中都计入位置对齐工作。

[批量查找指南](../docs/batch-primary-key-lookups.md)给出了 `ANY` 基线与带顺序的 `WITH ORDINALITY` 基线，两者都不需要扩展。添加缓存前，先建立 SQL 基线。

## 每次只改变一个负载维度 {#workload-dimensions}

| 实验 | 保持不变的条件 | 能观察到什么 |
|---|---|---|
| 热数据重复读取 | 键、结果形状、连接数 | 已填充条目的复用收益 |
| 冷键或缺失键 | 请求分布与批次大小 | 源表读取及不存在结果的成本 |
| 更大的批次 | 请求总键数与载荷形状 | 往返减少与逐键工作的权衡 |
| 并发写入 | 读写比例与事务边界 | 失效、重新填充及可见性成本 |
| 更宽的行 | 键分布与客户端位置 | 序列化、传输及行大小导致的绕过 |

不符合条件的读取使用源表是正常行为。应比较每次实验前后的计数器增量；仅凭低命中率无法判断安装损坏。将 SQL 与 RESP 计数器分开观察。[技术参考](../docs/TECHNICAL.md#health-and-monitoring)说明了 `local_cache.stats()` 和 `local_cache.health()`。

## 使用统一脚本，再检查证据 {#shared-runner}

完成[快速开始](../docs/QUICKSTART.md)后，运行仓库中的比较脚本：

```bash
./examples/benchmark.sh all > comparison.json
python3 scripts/benchmark_report.py comparison.json
```

[基准测试指南](../docs/BENCHMARKS.md)列出前置条件、负载控制项和指标。保留原始 JSON。记录扩展与测试程序版本、PostgreSQL 版本、机器、连接数和客户端位置。除了吞吐量，也比较重复运行结果、延迟分布和服务器资源消耗。短时正确性冒烟测试不能作为可发布的速度测量结果。

## 从应用边界作出决定 {#application-boundary}

SQL `mget` 与 RESP `MGET` 使用不同传输方式和结果处理流程。一种路径的收益不能证明另一种也有收益。项目[注明日期的 Go 测量](../docs/benchmarks-go.md)包含 SQL `mget` 慢于预备 SQL 的单键场景。这说明需要实测，而不是给出普遍预测。

需要连接、投影、锁定或不支持的表形状时，或者缓存没有可测收益时，应保留普通 SQL。对于按主键重复读取整行的场景，请在测试显式 API 时包含应用实际执行的同等客户端工作。继续阅读[缓存选择指南](../docs/postgresql-caching.md)和[失效实验](../docs/cache-invalidation.md)。
