---
layout: doc
lang: zh
translation_key: BENCHMARKS
title: PostgreSQL 缓存基准测试，3.1.0
description: "pg_local_cache 3.1.0 双机测试结果：固定与全机读取、写入开销、写入期间读取及测试方法。"
section: 基准测试
permalink: /zh/docs/BENCHMARKS.html
redirect_from:
  - /zh/docs/benchmarks-go.html
  - /zh/docs/benchmarks-node.html
  - /zh/docs/benchmarks-go/
  - /zh/docs/benchmarks-node/
last_modified_at: "2026-10-07"
---
# PostgreSQL 缓存基准测试：3.1.0

这些测试在两台私有网络虚拟机上比较 pg_local_cache RESP MGET、Valkey 和预备 SQL。以下结果来自 3.1.0 最终构建 c431bcc。固定与全机读取矩阵及写入测试报告重复运行的中位数；两个固定单核心 Valkey io-threads=1 场景各重复两次，其余固定场景各五次。混合测试、120 秒过期探测和一小时 soak 均为单次运行。这些负载样本不代表生产容量承诺。

## 最终构建的测试结果

- 按固定服务器核心比较，单核心时表现最好的 Valkey 配置为 io-threads=1（180k 次读取/秒，pg_local_cache 为 210k）；双核心时为 io-threads=4（266k 对 337k）。四核心时达到受客户端限制的平台，约为 361k 对 357k；pg_local_cache 使用 5.24 个服务器 vCPU，Valkey 使用 7.95。Valkey io-threads=1 的每请求服务器 CPU 最低。固定单键测试中，预备 SQL 明显落后。
- 使用全部 16 个 vCPU、Valkey io-threads=8 时，MGET 16/64 的观测中位吞吐差异约为 −2.15% 至 +1.26%；四组 64 键比较的观测范围均不重叠。这些样本不能证明统计等价。大批量 MGET 受客户端和网络限制。一行数据显示随机键、MGET 64、256 客户端时 pg_local_cache 的 p99 更高（43.52 对 40.37 ms）。
- 相对普通表事务，pg_local_cache 的写入开销为 UPDATE 3.6–8.2%、INSERT 3.7–5.5%。普通 UPDATE 后执行 Valkey DEL，UPDATE 吞吐量低 28.9–63.7%。
- 写入期间的检查未发现 pg_local_cache 过期条目。120 秒后，Valkey cache-aside 仍有 Uniform 2 条、Zipf 1 条过期记录。一小时 soak 完成 252M 次 MGET 和 72M 次 UPDATE；worker RssAnon 增长最多 44 kB。

## 固定服务器 vCPU 对的读取

Go/pgx 客户端使用 256 个客户端，每个 MGET 一个键。每种情况运行 20 秒。两个固定单对 Valkey io-threads=1 场景各重复两次；其余固定场景各重复五次。PostgreSQL 和 Valkey 固定到 N 对 vCPU（2N vCPU）。Valkey 分别使用 io-threads=2N 和 io-threads=1；预备 SQL 使用相同键集。

| vCPU 对 | 键 | pg_local_cache 请求/秒（p99 ms，vCPU） | Valkey io-threads=2N（请求/秒，p99 ms，vCPU） | Valkey io-threads=1（请求/秒，p99 ms，vCPU） | 预备 SQL（请求/秒，p99 ms，vCPU） |
| --- | --- | --- | --- | --- | --- |
| 1 | 热键 | 209,696 (2.46, 1.99) | 91,478 (3.24, 2.00) | 180,141 (2.59, 0.85) | 23,200 (24.90, 2.00) |
| 1 | 100k 随机键 | 195,950 (2.65, 2.00) | 88,672 (3.38, 2.00) | 175,853 (2.65, 0.92) | 22,433 (25.95, 2.00) |
| 2 | 热键 | 336,691 (2.02, 3.78) | 266,340 (1.69, 4.00) | 162,230 (2.85, 0.66) | 46,386 (16.38, 4.00) |
| 2 | 100k 随机键 | 326,125 (1.98, 3.83) | 267,826 (1.72, 4.00) | 160,220 (2.92, 0.69) | 45,211 (17.56, 4.00) |
| 4 | 热键 | 360,968 (2.33, 5.24) | 357,161 (2.20, 7.95) | 160,633 (2.85, 0.66) | 88,604 (6.75, 8.00) |
| 4 | 100k 随机键 | 353,941 (2.33, 5.27) | 351,344 (2.20, 7.95) | 159,414 (2.92, 0.68) | 86,790 (6.88, 8.00) |

单核心时，单线程是 Valkey 的最佳配置。双核心时，io-threads=4 最佳；尽管吞吐量较低，Valkey 的 p99 更低。四核心时，pg_local_cache 和 io-threads=8 达到近似的客户端限制吞吐量，pg_local_cache 使用的服务器 CPU 更少。

Raw runs: [Valkey io-threads=2N](../../docs/benchmarks/3.1.0/read-per-core.jsonl), [Valkey io-threads=1](../../docs/benchmarks/3.1.0/read-per-core-valkey-io-threads-1.jsonl).

## 使用全部 16 个 vCPU 的读取

这些测试未固定 CPU，每种情况重复三次。客户端受限时，负载节点约占用 7–13 个 CPU 核心。使用 Valkey io-threads=8 时，MGET 16/64 的观测中位吞吐差异约为 −2.15% 至 +1.26%；四组 64 键比较的观测范围均不重叠。这些样本不能证明统计等价。Valkey io-threads=1 时，256 客户端随机键 MGET 16 测得 pg_local_cache 为 116,130/s、Valkey 为 97,489/s（高 19.1%）。大批量 MGET 受客户端和网络限制；io-threads=1 没有 64 键结果。

| 键 | MGET 键数 | 客户端 | pg_local_cache（请求/秒，p99 ms，vCPU） | Valkey io-threads=8（请求/秒，p99 ms，vCPU） | Valkey io-threads=1（请求/秒，p99 ms，vCPU） | 预备 SQL（请求/秒，p99 ms，vCPU） |
| --- | --- | --- | --- | --- | --- | --- |
| 热键 | 1 | 64 | 217,724 (0.70, 2.89) | 206,634 (0.70, 7.85) | 167,929 (0.71, 0.66) | 132,433 (0.96, 11.32) |
| 热键 | 1 | 256 | 372,683 (2.39, 4.21) | 365,947 (2.33, 7.92) | 161,455 (2.92, 0.68) | 173,571 (4.26, 13.14) |
| 热键 | 16 | 64 | 82,858 (3.38, 3.13) | 83,098 (3.38, 6.41) | 79,179 (3.38, 0.69) | 73,512 (2.05, 8.78) |
| 热键 | 16 | 256 | 122,496 (9.83, 4.07) | 122,817 (9.83, 7.42) | 111,790 (8.06, 0.83) | 100,966 (6.36, 11.49) |
| 热键 | 64 | 64 | 30,241 (8.78, 3.40) | 30,845 (9.04, 3.57) | — | 27,790 (6.88, 9.05) |
| 热键 | 64 | 256 | 39,531 (43.52, 4.26) | 39,995 (47.71, 5.66) | — | 34,519 (30.67, 12.23) |
| 100k 随机键 | 1 | 64 | 215,310 (0.70, 2.89) | 205,081 (0.70, 7.85) | 162,778 (0.73, 0.67) | 131,563 (0.97, 11.34) |
| 100k 随机键 | 1 | 256 | 366,860 (2.39, 4.31) | 360,894 (2.33, 7.92) | 158,444 (2.98, 0.68) | 169,884 (4.26, 13.19) |
| 100k 随机键 | 16 | 64 | 79,505 (3.51, 3.45) | 79,556 (3.44, 6.68) | 74,270 (3.38, 0.79) | 69,262 (2.13, 9.12) |
| 100k 随机键 | 16 | 256 | 116,130 (10.35, 4.42) | 114,683 (9.83, 7.53) | 97,489 (7.67, 0.95) | 91,572 (7.01, 11.96) |
| 100k 随机键 | 64 | 64 | 29,000 (8.78, 3.81) | 29,464 (9.04, 4.61) | — | 26,169 (7.14, 9.39) |
| 100k 随机键 | 64 | 256 | 37,356 (43.52, 4.93) | 38,175 (40.37, 7.58) | — | 32,756 (31.20, 13.03) |

随机键、MGET 64、256 个客户端时，pg_local_cache 的 p99 为 43.52 ms，Valkey 为 40.37 ms。固定单键测试中，预备 SQL 吞吐量明显更低；批量大小和延迟指标会影响完整结果。

Raw runs: [Valkey io-threads=8](../../docs/benchmarks/3.1.0/read-8-cores.jsonl), [Valkey io-threads=1](../../docs/benchmarks/3.1.0/read-8-cores-valkey-io-threads-1.jsonl).

## 写入开销

三次 15 秒测试比较普通表事务、经 pg_local_cache 的事务，以及普通事务后执行 Valkey DEL。百分比相对于相同条件下的普通表事务。

| 操作 | synchronous_commit | 客户端 | 普通表 tx/s（DB µs/tx） | pg_local_cache tx/s（DB µs/tx，变化） | 普通表 + Valkey DEL tx/s（DB µs/tx，变化） |
| --- | --- | --- | --- | --- | --- |
| Update | on | 32 | 46,747 (109) | 45,065 (119, −3.6%) | 33,234 (204, −28.9%) |
| Update | on | 64 | 63,479 (108) | 61,027 (121, −3.9%) | 37,659 (238, −40.7%) |
| Update | off | 32 | 119,156 (75) | 110,987 (87, −6.9%) | 53,374 (158, −55.2%) |
| Update | off | 64 | 148,358 (73) | 136,194 (83, −8.2%) | 53,853 (189, −63.7%) |
| Insert | on | 32 | 48,831 (97) | 47,020 (105, −3.7%) | 34,863 (184, −28.6%) |
| Insert | on | 64 | 65,688 (97) | 63,011 (108, −4.1%) | 39,642 (218, −39.7%) |
| Insert | off | 32 | 123,679 (63) | 117,868 (72, −4.7%) | 58,168 (135, −53.0%) |
| Insert | off | 64 | 138,454 (68) | 130,805 (77, −5.5%) | 58,361 (165, −57.8%) |

UPDATE 场景中，pg_local_cache 比普通写入低 3.6–8.2%；INSERT 开销为 3.7–5.5%。普通 UPDATE 加 Valkey DEL 的吞吐量低 28.9–63.7%；INSERT 对比低 28.6–57.8%。

Raw runs: [write overhead](../../docs/benchmarks/3.1.0/write-overhead.jsonl).

## 写入期间的读取

每种情况单次运行 30 秒，64 个读取客户端从 60k 行中每次读取一个键；写入客户端使用 synchronous_commit=off。Valkey 使用 cache-aside（SQL 写入后执行 DEL）和 io-threads=8。“过期条目”是结束时的哨兵检查；n/a 表示该方案没有可检查的缓存条目。

| 方案 | 键分布 | 写入负载 | 读取/秒 | 读取 p99 ms | 命中率 | 写入/秒 | 过期条目 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| pg_local_cache | Uniform | 无 | 214,508 | 0.71 | 1.000 | 0 | 0 |
| pg_local_cache | Uniform | Update 10,000/s | 195,384 | 0.86 | 0.948 | 10,000 | 0 |
| pg_local_cache | Uniform | Update 30,000/s | 161,919 | 1.04 | 0.837 | 29,999 | 0 |
| pg_local_cache | Uniform | Update unlimited | 82,640 | 1.69 | 0.432 | 111,013 | 0 |
| pg_local_cache | Zipf | 无 | 216,297 | 0.70 | 1.000 | 0 | 0 |
| pg_local_cache | Zipf | Update 10,000/s | 198,454 | 0.86 | 0.964 | 10,000 | 0 |
| pg_local_cache | Zipf | Update 30,000/s | 168,875 | 1.01 | 0.923 | 30,000 | 0 |
| pg_local_cache | Zipf | Update unlimited | 96,297 | 1.46 | 0.834 | 113,505 | 0 |
| Valkey cache-aside | Uniform | 无 | 223,537 | 0.61 | 1.000 | 0 | 0 |
| Valkey cache-aside | Uniform | Update 10,000/s | 156,983 | 1.92 | 0.936 | 10,000 | 0 |
| Valkey cache-aside | Uniform | Update 30,000/s | 42,294 | 7.14 | 0.577 | 29,998 | 0 |
| Valkey cache-aside | Uniform | Update unlimited | 29,692 | 8.78 | 0.444 | 38,481 | 1 |
| Valkey cache-aside | Zipf | 无 | 222,575 | 0.63 | 0.999 | 0 | 0 |
| Valkey cache-aside | Zipf | Update 10,000/s | 168,911 | 1.65 | 0.960 | 10,000 | 0 |
| Valkey cache-aside | Zipf | Update 30,000/s | 75,642 | 4.78 | 0.887 | 29,999 | 0 |
| Valkey cache-aside | Zipf | Update unlimited | 48,993 | 6.88 | 0.852 | 40,735 | 0 |
| Prepared SQL | Uniform | 无 | 129,714 | 0.97 | — | 0 | n/a |
| Prepared SQL | Uniform | Update 10,000/s | 121,567 | 1.13 | — | 10,000 | n/a |
| Prepared SQL | Uniform | Update 30,000/s | 106,949 | 1.33 | — | 30,000 | n/a |
| Prepared SQL | Uniform | Update unlimited | 77,574 | 2.02 | — | 93,955 | n/a |
| Prepared SQL | Zipf | 无 | 129,937 | 0.97 | — | 0 | n/a |
| Prepared SQL | Zipf | Update 10,000/s | 122,182 | 1.13 | — | 10,000 | n/a |
| Prepared SQL | Zipf | Update 30,000/s | 107,852 | 1.33 | — | 29,999 | n/a |
| Prepared SQL | Zipf | Update unlimited | 78,601 | 2.08 | — | 95,199 | n/a |
| pg_local_cache | Uniform | Insert unlimited | 117,531 | 1.20 | 1.000 | 114,066 | 0 |
| Valkey cache-aside | Uniform | Insert unlimited | 70,302 | 4.92 | 1.000 | 46,622 | 0 |
| Prepared SQL | Uniform | Insert unlimited | 84,294 | 1.98 | — | 95,094 | n/a |

pg_local_cache 在所有列出的混合测试结束时均无过期条目。Valkey 仅在一个 UPDATE 场景留下一个过期哨兵条目：Uniform 键且更新速率不限。Uniform 键每秒 UPDATE 30k 时，pg_local_cache 读取为 161,919/s，Valkey cache-aside 为 42,294/s，预备 SQL 为 106,949/s。

Raw runs: [mixed reads and writes](../../docs/benchmarks/3.1.0/mixed.jsonl).

## 120 秒过期探测

每项探测单次运行，使用 64 个读取客户端和 64 个不限速 UPDATE 写入客户端，然后执行完整过期条目检查。Valkey 使用 io-threads=8。

| 方案 | 键分布 | 读取/秒 | 读取 p99 ms | 写入/秒 | 过期条目 | 备注 |
| --- | --- | --- | --- | --- | --- | --- |
| pg_local_cache | Uniform | 82,565 | 1.687552 | 108,580 | 0 | — |
| pg_local_cache | Zipf | 0 | n/a | 101,797 | 0 | 见下文备注。 |
| Valkey cache-aside | Uniform | 30,210 | 8.781824 | 36,508 | 2 | — |
| Valkey cache-aside | Zipf | 28,568 | 8.781824 | 35,789 | 1 | — |

并发写入期间，pg_local_cache Zipf 读取器未通过启动时的等价性检查（harness 问题），因此读取数为零。过期检查仍然执行，结果为零。

Raw runs: [120-second stale probes](../../docs/benchmarks/3.1.0/stale-probes-120s.jsonl).

## 一小时 soak

单次运行：64 个客户端对 100k 随机键执行 MGET 16，同时每秒 UPDATE 20,000 次。每分钟采样 RESP worker 的 RssAnon。

- 读取：3,600 秒内完成 251,987,814 次 MGET（69,997/s），p99 为 3.57 ms。
- 写入：71,999,993 次 UPDATE（20,000/s），p99 为 1.13 ms，错误数为零。
- Worker RssAnon 保持平稳：60 次采样中，每个 worker 最大增长 44 kB。

Raw runs: [one-hour soak](../../docs/benchmarks/3.1.0/soak-1h.jsonl).

## 环境与方法

根据 lscpu，每台虚拟机报告 16 个 vCPU，拓扑为 2 个 socket × 每个 socket 4 个 core × 每个 core 2 个 thread（虚拟机可见拓扑），内存 62 GB，Debian 13，内核 6.12.111+deb13-amd64。数据库虚拟机运行 PostgreSQL 16.15（max_connections=1000，shared_buffers=1048576）和 pg_local_cache 3.1.0 c431bcc（已启用，memory_budget_mb=1024，workers=8）。表含 100,000 行 JSON，平均每行 166 字节。写入、混合和过期探测期间，Valkey 8.1.1 使用 io-threads=8。从负载虚拟机到数据库虚拟机，iperf3 测得 10.4 Gbit/s，ping 报告 RTT 最小/平均/最大值为 0.090/0.143/0.871 ms。Go/pgx 负载客户端运行在第二台虚拟机。读取运行 20 秒；写入开销测试 15 秒，混合测试 30 秒，过期探测 120 秒，soak 3,600 秒。

固定矩阵使用 256 个客户端；除两个固定单对 Valkey io-threads=1 场景各运行两次外，其余固定场景各运行五次。全机矩阵和写入测试各运行三次；混合、过期探测和 soak 各运行一次。NIC 中断未固定。所有表格均使用最终构建 c431bcc。吞吐量单位为请求/秒，表格标为 tx/s 时除外；延迟为 p99，CPU 为服务器 vCPU。

环境和原始 JSONL：[环境](../../docs/benchmarks/3.1.0/env.txt), [环境证据](../../docs/benchmarks/3.1.0/stand.txt)、[固定核心读取](../../docs/benchmarks/3.1.0/read-per-core.jsonl)、[固定核心 Valkey io-threads=1](../../docs/benchmarks/3.1.0/read-per-core-valkey-io-threads-1.jsonl)、[全核心读取](../../docs/benchmarks/3.1.0/read-8-cores.jsonl)、[全核心 Valkey io-threads=1](../../docs/benchmarks/3.1.0/read-8-cores-valkey-io-threads-1.jsonl)、[写入开销](../../docs/benchmarks/3.1.0/write-overhead.jsonl)、[写入期间读取](../../docs/benchmarks/3.1.0/mixed.jsonl)、[120 秒过期探测](../../docs/benchmarks/3.1.0/stale-probes-120s.jsonl)和[一小时 soak](../../docs/benchmarks/3.1.0/soak-1h.jsonl)。

## 使用 bench/ 重现

需要 Python 3，以及指向数据库和负载虚拟机的 SSH 别名。数据库虚拟机需要 PostgreSQL 16、Valkey、mpstat、sar、ip 和 systemd；负载虚拟机需要位于 /root/bench-client 的已编译 Go 客户端。参见 [bench/README.md](https://github.com/profundium/pg_local_cache/blob/c431bcc4f69d4ecd28f1695548e121e55dfdd2fa/bench/README.md) 和 [Go/pgx 客户端](https://github.com/profundium/pg_local_cache/tree/c431bcc4f69d4ecd28f1695548e121e55dfdd2fa/examples/go-pgx/)。

默认读取矩阵：20 秒，重复三次；1/16/64 个键；16/64/256 个客户端；热键集和 100k 键空间：

    python3 bench/run_bench.py reads.jsonl

固定一个 vCPU 对、256 个客户端并重复五次的示例；将 0-1 替换为该 vCPU 对对应的逻辑 CPU ID：

    PGLC_SERVER_CPUS=0-1 REPEATS=5 BATCHES=1 CLIENTS=256 KEY_SPACES=0,100000 python3 bench/run_bench.py pinned.jsonl

脚本对 PostgreSQL 和 Valkey 应用 AllowedCPUs，结束后清除设置。结果包含服务器 CPU、延迟、网络 RX/TX 和客户端 CPU。运行前需为 Valkey 配置相应的 io-threads。

运行写入测试和 30 秒混合负载：

    REPEATS=3 WRITE_SECONDS=15 WRITE_CLIENTS=32,64 python3 bench/write_bench.py writes.jsonl
    REPEATS=1 MIXED_SECONDS=30 MIXED_RATES=none,10000,30000,unlimited MIXED_KEY_DISTS=uniform,zipf python3 bench/write_bench.py mixed.jsonl

脚本以 JSONL 追加结果，失败时保留已完成样本。写入测试记录每笔事务的数据库 CPU；混合测试还记录读取延迟、命中/未命中、写入速率和结束时的过期条目检查。

## 历史 2.x MacBook 结果

这些旧 JSON 是 Apple M3 Max、PostgreSQL 16 上的历史测试；环境和客户端拓扑不同于上面的双机测试。它们包含 3.0.0 中移除的 2.x SQL mget API。旧 API 见 [2.0.4 文档](https://github.com/profundium/pg_local_cache/tree/v2.0.4/docs)；当前缓存读取使用 RESP MGET。

- [2026 年 9 月 14 日 Go 测试](../../docs/benchmarks/2026-09-14-m3-max.json)
- [2026 年 9 月 14 日客户端测试](../../docs/benchmarks/2026-09-14-m3-max-clients.json)
- [2026 年 9 月 15 日 Go/RESP 测试](../../docs/benchmarks/2026-09-15-m3-max-resp.json)
