---
layout: doc
lang: zh
translation_key: benchmarks-node
title: Node.js 基准测试
description: Apple M3 Max 上的 node-postgres 本地结果：批量读取、并发更新，以及 PostgreSQL CPU 与内存开销。
section: 基准测试
permalink: /zh/docs/benchmarks-node.html
last_modified_at: '2026-09-16'
---

# Node.js 基准测试 {#nodejs-benchmarks}

[概览](BENCHMARKS.md) · [Node.js](benchmarks-node.md) · [Go 与 RESP](benchmarks-go.md)

Node.js 24.18.0，node-postgres 8.16.3。[机器配置与测量方法](BENCHMARKS.md#test-environment)。

测量日期为 2026 年 9 月 14 日，扩展构建为 `67e5754`，缓存预算 384 MiB；客户端在 macOS 上通过 Docker 发布的 SQL 端口连接。

**每请求 64 个键**时，三个 10 秒样本的 requests/s 中位数：

SQL `mget` 路径及以下所有结果均为 2.x 历史测量。SQL `mget` 已在 3.0.0 中移除；当前支持的缓存读取接口是 RESP `MGET`。

| 连接数 | 预备 SQL | SQL mget （2.x；3.0.0 已移除）|
|---:|---:|---:|
| 4 | 7,023 | 7,528 |
| 64 | 15,577 | 16,616 |
| 256 | 15,459 | 16,574 |

64 个连接时，`mget` **每请求消耗 170 µs 服务器 CPU**，SQL 则为 286 µs。服务器平均 CPU 为 2.80 核与 4.43 核；采样内存峰值为 215.0 MiB 与 205.5 MiB。客户端 CPU 为 0.84 核与 0.87 核。

64 个连接、**每请求一个键**时，SQL 更快：55,409 对 53,646 requests/s。批量结果不适用于单键读取。

[原始测量](../../docs/benchmarks/2026-09-14-m3-max-clients.json)包含延迟分位数、资源样本与源码版本。

### 读写混合 {#reads-mixed-with-writes}

Node.js 应用测试程序使用 **64 个连接**、**每样本 50,000 个请求**，重复三次。读取循环访问 128 行热数据；混合负载中 5% 的操作更新行。

| 工作负载 | 键/请求 | 预备 SQL requests/s | mget JSON requests/s （2.x；3.0.0 已移除）|
|---|---:|---:|---:|
| 热读取 | 1 | 56,104 (52,246–56,364) | 52,842 (52,494–53,399) |
| 热读取 | 16 | 37,391 (36,996–37,938) | 38,425 (38,340–38,617) |
| 热读取 | 64 | 15,178 (15,109–15,488) | 16,294 (16,131–16,368) |
| 5% 更新 | 1 | 55,768 (54,273–56,527) | 52,621 (51,432–53,216) |
| 5% 更新 | 16 | 38,669 (38,429–38,718) | 37,604 (37,481–37,916) |
| 5% 更新 | 64 | 16,049 (16,008–16,063) | 16,814 (16,771–17,229) |

数值为 requests/s 中位数，括号内为最小值–最大值。混合样本合并统计读写操作。JSON 中的 `application_run` 包含冷填充与写入开销场景。批次为 64 的冷填充只有 64 个延迟观测值，不足以得到有意义的 p99 估计。

## 历史 SQL 基准查询 {#query-setup}

为便于复现，历史 SQL `mget` 查询仍保留在链接的原始测量数据中。3.0.0 已不提供该查询。当前基准比较预备 SQL 与 RESP `MGET`；参阅 [Node.js 示例](node-postgres.md)。

## 复现 {#reproduce}

<details markdown="1">
<summary>运行 Node.js 基准测试</summary>

在仓库根目录运行，需要 Docker 和 Node.js 20+：

```bash
./examples/benchmark.sh node > node.json
```

当前默认值：4/64/256 个连接、1/16/64 个键，每个案例进行三次 5 秒采样。Node.js 在 Docker 虚拟机中运行预备 SQL 和 RESP `MGET`。脚本创建一次性服务器和客户端容器、记录资源数据，然后移除两者。

运行读写混合测试：

```bash
BATCHES=1,16,64 ./examples/benchmark.sh node-workload > benchmark.json
python3 scripts/benchmark_report.py benchmark.json
```

默认值：64 个连接、每样本 50,000 个请求，重复三次。测试程序在样本之间重置演示表。可覆盖 `CLIENTS`、`REQUESTS`、`BATCHES` 和 `REPEATS`。

</details>
