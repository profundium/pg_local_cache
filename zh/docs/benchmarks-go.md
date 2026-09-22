---
layout: doc
lang: zh
translation_key: benchmarks-go
title: Go 基准测试：SQL 与 RESP
description: Apple M3 Max 上 pgx 与 RESP2 的本地结果，包含 PostgreSQL CPU、内存以及连接数扩展情况。
section: 基准测试
permalink: /zh/docs/benchmarks-go.html
last_modified_at: '2026-09-16'
---

# Go 基准测试：SQL 与 RESP {#go-benchmarks-sql-and-resp}

[概览](BENCHMARKS.md) · [Node.js](benchmarks-node.md) · [Go 与 RESP](benchmarks-go.md)

Go 1.27.1、pgx 5.11.0 和标准库 RESP2 客户端；`GOMAXPROCS=8`。[机器配置与测量方法](BENCHMARKS.md#test-environment)。

测量日期为 2026 年 9 月 15 日，扩展构建为 `f03ed22`。Go 客户端运行在 Linux VM 内的独立容器中，共享 PostgreSQL 网络命名空间。服务器资源计数器不包含客户端 CPU 和内存。RESP 使用八个工作进程、1 GiB 缓存/缓冲区预算和 512 个客户端上限。

每种情况三个五秒样本，**requests/s** 中位数：

| 键/请求 | 连接数 | 预备 SQL | SQL mget | RESP MGET |
|---:|---:|---:|---:|---:|
| 1 | 64 | 277,088 | 211,251 | 722,133 |
| 1 | 256 | 253,790 | 186,296 | 839,678 |
| 64 | 64 | 26,459 | 38,122 | 38,877 |
| 64 | 256 | 27,615 | 43,647 | 52,774 |

尽管数据、配置和索引计划相同，服务器重启后 64 键 SQL 的结果仍在 19–28k requests/s 间波动。表格采用较快的重复运行；波动原因仍未查明。[全部 129 个样本](../../assets/benchmarks/2026-09-15-m3-max-resp.json)包含两次运行、精确源码版本、二进制哈希与查询计划。

只读缓存样本命中率为 100%，没有错误或连接数限制拒绝。测试程序检查 SQL 与 RESP 的连接余量。另一次使用 256 个连接、64 个键和 12 个 Go 线程的探测未提升 RESP；SQL `mget` 比八线程提升了 6%。

### 服务器资源 {#server-resources}

**256 个连接**时，相同样本的中位数：

| 键/请求 | 路径 | 客户端 CPU 核数 | 服务器 CPU 核数（VM 占比） | 服务器 µs/请求 | 采样峰值 MiB |
|---:|---|---:|---:|---:|---:|
| 1 | SQL | 3.97 | 8.94 (63.9%) | 36.3 | 679.8 |
| 1 | SQL mget | 3.36 | 9.93 (70.9%) | 54.4 | 684.2 |
| 1 | RESP MGET | 5.98 | 6.11 (43.7%) | 7.8 | 263.7 |
| 64 | SQL | 3.72 | 9.45 (67.5%) | 348.3 | 689.8 |
| 64 | SQL mget | 5.12 | 4.99 (35.6%) | 116.0 | 707.7 |
| 64 | RESP MGET | 5.76 | 4.97 (35.5%) | 95.8 | 266.7 |

### macOS 上的 Go 客户端 {#go-client-on-macos}

通过 Docker 发布的端口、使用 **64 个连接**；三个五秒样本的 requests/s 中位数：

| 键/请求，64 个连接 | 预备 SQL | SQL mget | RESP MGET |
|---:|---:|---:|---:|
| 1 | 49,194 | 48,010 | 52,426 |
| 64 | 14,324 | 15,751 | 16,240 |

VM 与宿主机场景的客户端操作系统和网络路径都不同。因此，宿主机端口测试不能独立测出 PostgreSQL 的吞吐量极限。RESP 的会话约定也不同：工作进程使用配置的数据库角色，不继承调用者的 SQL 事务或快照。详见 [RESP 参考](TECHNICAL.md#optional-resp2-endpoint)。

## 复现 {#reproduce}

<details markdown="1">
<summary>运行 Go 基准测试</summary>

在仓库根目录运行，需要 Docker、Node.js 20+ 和 Go 1.25+：

```bash
./examples/benchmark.sh go > go.json
```

脚本构建一次性 PostgreSQL 服务器和 Go 客户端，每种情况运行三次，记录服务器资源，然后移除自己的容器。客户端位于 Docker VM 内，使用与 PostgreSQL 分开的 cgroup。当前默认值：4/64/256 个连接、1/16/64 个键，每样本五秒。使用 `all` 可让 Node.js 和 Go 对同一服务器运行[统一矩阵](BENCHMARKS.md#run-the-same-comparison-on-every-client)。要复现记录的运行，请使用测量 JSON 中的版本。

可覆盖 `CONNECTIONS`、`BATCHES`、`REPEATS`、`DURATION_SECONDS` 和 `GOMAXPROCS`。JSON 会记录正在运行的扩展构建 ID。

</details>
