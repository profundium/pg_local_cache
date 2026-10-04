---
layout: doc
lang: zh
translation_key: BENCHMARKS
title: PostgreSQL 缓存基准测试
description: 在 Apple M3 Max 上使用 Node.js、Go 与 RESP 测量 pg_local_cache，提供机器配置、PostgreSQL CPU 和内存数据及测试方法。
section: 基准测试
permalink: /zh/docs/BENCHMARKS.html
last_modified_at: '2026-09-16'
---

# PostgreSQL 缓存基准测试 {#postgresql-cache-benchmarks}

结果记录于运行 PostgreSQL 16 的 Apple M3 Max。每组比较为缓存读取与普通 SQL 读取使用相同客户端、数据集和解码后的行结果。

已发布的 SQL mget 结果是 2.x 历史数据；SQL mget 已在 3.0.0 中移除。当前支持的缓存读取接口是 RESP MGET。

## 缓存在哪些场景有效，哪些场景无效 {#where-the-cache-helpedand-where-it-did-not}

Node.js 和 Go 会解码 RESP JSON 行；这些客户端开销计入计时。历史 SQL mget 比较均标注为 2.x 数据。

[Node.js 测量](benchmarks-node.md)使用 macOS 客户端与 Docker 服务器；[Go 与 RESP 测量](benchmarks-go.md)将两者均置于 Docker VM 内。每个页面都链接到原始重复测量、精确版本和服务器资源成本。这些不同配置不能用来给语言排名。连接示例见 [Node.js](resp.md#nodejs)、[Go](resp.md#go) 或 [RESP](resp.md)。

## 在每种客户端运行相同比较 {#run-the-same-comparison-on-every-client}

在仓库根目录运行，需要 Docker、Node.js 20+ 和 Go 1.25+：

```bash
./examples/benchmark.sh all > comparison.json
python3 scripts/benchmark_report.py comparison.json
```

测试程序构建一次性 PostgreSQL 服务器，并运行以下统一矩阵：

Node.js 和 Go 会解码 RESP JSON 行；这些客户端开销计入计时。历史 SQL mget 比较均标注为 2.x 数据。

默认共 162 个样本，计时部分约 14 分钟，另加初始化时间。脚本在成功或失败后均移除自己的容器。短时间正确性检查：

```bash
CONNECTIONS=4 BATCHES=1,16,64 REPEATS=1 DURATION_SECONDS=1 \
  ./examples/benchmark.sh all > smoke.json
```

Node.js 和 Go 会解码 RESP JSON 行；这些客户端开销计入计时。历史 SQL mget 比较均标注为 2.x 数据。

相同工作负载并不意味着协议可以互换：RESP 工作进程使用配置的数据库角色，不加入调用者的 SQL 事务或快照。详见 [RESP 约定](TECHNICAL.md#optional-resp2-endpoint)。统一比较测量的是热读取。冷读取、读写混合与写入开销诊断仍由独立的 `node-workload` 提供。

下方发布于 9 月 14–15 日的结果早于这个统一运行脚本。数据仍附有原始环境与源码版本；这些并非统一矩阵产生的新测量结果。

## 测试环境 {#test-environment}

| 组件 | 配置 |
|---|---|
| 宿主机 | MacBook Pro `Mac15,10`，Apple M3 Max：10 个性能核 + 4 个能效核，36 GiB RAM |
| 操作系统 | macOS 26.5.2，构建 `25F84`，arm64 |
| Docker VM | Engine 29.7.2，Linux `7.0.12-linuxkit`，14 CPUs，7.65 GiB RAM；容器无 CPU 或 RAM 配额 |
| PostgreSQL | 16.15，Debian bookworm；300 个连接、128 MiB shared buffers、256 MiB `/dev/shm` |
| 数据 | 4,096 行，值大小 128 字节；1,024 个缓存项；数据与 WAL 位于 tmpfs |

客户端和服务器与另外十个开发容器共享 Mac 的 CPU。所有客户端都编码请求并解码完整 JSON 行，保留输入顺序、重复项和缺失位置。SQL 使用预备语句；连接、认证和预热不计入计时。未使用 TLS 或流水线。

## 测量方法 {#measurement-method}

每个连接在收到响应后才发送下一请求：这是**闭环（closed-loop）**工作负载，没有修正 coordinated omission（协调遗漏）。每次重复会轮换查询顺序；在途请求完成后才停止计时。只读比较使用已在缓存中的固定键。记录的 SQL 计划使用 `items_pkey`，共享块磁盘读取数为零。

服务器 CPU 数据来自 PostgreSQL 容器的 cgroup 计数器。一核表示每经过一秒消耗一个 CPU 秒；容量百分比按 14 核计算。CPU µs/request 为服务器 CPU 时间除以完成请求数。采样窗口包含监控开销和客户端生成报告的短暂间隔。

内存取 cgroup 的 `memory.current`，每 500 ms 采样，并记录起止点。表格报告各次重复中采样峰值的中位数，包含共享内存、tmpfs 和页缓存；这不是进程 RSS。JSON 文件还包含块 I/O、节流、内存事件，以及 SQL 状态/等待快照。网络计数器不含回环流量，因此也不含 VM 内客户端流量。

这些在共享笔记本上进行的短时热缓存测试，不能用来估计生产容量。数据和 WAL 使用 tmpfs，启用 `fsync`、`full_page_writes` 与 `synchronous_commit`；未测试磁盘性能。

失败的运行会以非零状态退出，并保留已完成样本；Markdown 渲染器会拒绝部分结果。需要复现精确代码时，使用各 JSON 中的 `harness_ref` 和 `extension_ref`。各客户端页面提供复现命令；结果写入 `benchmark.json` 等 JSON 文件。
