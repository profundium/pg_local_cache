---
layout: doc
lang: en
translation_key: BENCHMARKS
title: PostgreSQL cache benchmarks, 3.1.0
description: "Two-machine pg_local_cache 3.1.0 results for pinned and full-server reads, write overhead, and reads during writes, with raw JSONL and reproduction steps."
section: Benchmarks
permalink: /docs/BENCHMARKS.html
redirect_from:
  - /docs/benchmarks-go.html
  - /docs/benchmarks-node.html
  - /docs/benchmarks-go/
  - /docs/benchmarks-node/
last_modified_at: "2026-10-07"
---
# PostgreSQL cache benchmarks: 3.1.0

These measurements compare pg_local_cache RESP MGET, Valkey, and prepared SQL on two private-network VMs. Every result below uses final 3.1.0 build c431bcc. Repeated-run medians are shown for the pinned and all-core read matrices and write-overhead tests; both pinned one-core Valkey io-threads=1 cases have two runs (other pinned cases have five). Mixed, 120-second stale-probe, and one-hour soak results are single runs. These workload samples are not production capacity promises.

## What the final-build runs show

- Per pinned server vCPU pair, the best measured Valkey setting was io-threads=1 at one pair (180k reads/s vs 210k for pg_local_cache); at two pairs it was io-threads=4 (266k vs 337k). At four pairs, throughput reached a client-bound plateau near 361k vs 357k, while pg_local_cache used 5.24 vs 7.95 server vCPU. Valkey io-threads=1 used the least server CPU per request. Prepared SQL was far behind in the pinned single-key tests.
- With all 16 vCPUs available, observed median MGET 16/64 throughput differences with Valkey io-threads=8 ranged from about −2.15% to +1.26%. The observed ranges did not overlap in any of the four 64-key comparisons, so these samples do not establish statistical equivalence. Wide-MGET runs were client/network-bound. For random-key MGET 64 at 256 clients, pg_local_cache had higher p99 in one row (43.52 vs 40.37 ms).
- Write overhead versus plain table transactions was 3.6–8.2% for UPDATE and 3.7–5.5% for INSERT. Plain UPDATE plus Valkey DEL was 28.9–63.7% below plain UPDATE throughput.
- During writes, stale-entry checks found zero pg_local_cache entries. The 120-second Valkey cache-aside probes ended with 2 Uniform and 1 Zipf stale entries. The one-hour soak completed 252M MGET and 72M UPDATE with worker RssAnon growth capped at 44 kB.

## Reads per pinned server vCPU pair

The Go/pgx client used 256 clients and one key per MGET. Each case ran for 20 seconds. The two one-core Valkey io-threads=1 cases ran twice; all other pinned cases ran five times. PostgreSQL and Valkey were pinned to N vCPU pairs (2N vCPU). Valkey used io-threads=2N and io-threads=1; prepared SQL used the same key sets.

| vCPU pairs | Keys | pg_local_cache req/s (p99 ms, vCPU) | Valkey io-threads=2N (req/s, p99 ms, vCPU) | Valkey io-threads=1 (req/s, p99 ms, vCPU) | Prepared SQL (req/s, p99 ms, vCPU) |
| --- | --- | --- | --- | --- | --- |
| 1 | Hot | 209,696 (2.46, 1.99) | 91,478 (3.24, 2.00) | 180,141 (2.59, 0.85) | 23,200 (24.90, 2.00) |
| 1 | 100k random | 195,950 (2.65, 2.00) | 88,672 (3.38, 2.00) | 175,853 (2.65, 0.92) | 22,433 (25.95, 2.00) |
| 2 | Hot | 336,691 (2.02, 3.78) | 266,340 (1.69, 4.00) | 162,230 (2.85, 0.66) | 46,386 (16.38, 4.00) |
| 2 | 100k random | 326,125 (1.98, 3.83) | 267,826 (1.72, 4.00) | 160,220 (2.92, 0.69) | 45,211 (17.56, 4.00) |
| 4 | Hot | 360,968 (2.33, 5.24) | 357,161 (2.20, 7.95) | 160,633 (2.85, 0.66) | 88,604 (6.75, 8.00) |
| 4 | 100k random | 353,941 (2.33, 5.27) | 351,344 (2.20, 7.95) | 159,414 (2.92, 0.68) | 86,790 (6.88, 8.00) |

At one vCPU pair, single-threaded Valkey was the strongest Valkey configuration. At two pairs, io-threads=4 was strongest; Valkey p99 was lower despite lower throughput. At four pairs, pg_local_cache and io-threads=8 were near the same client-bound throughput, with lower pg_local_cache server CPU.

Raw runs: [Valkey io-threads=2N](benchmarks/3.1.0/read-per-core.jsonl), [Valkey io-threads=1](benchmarks/3.1.0/read-per-core-valkey-io-threads-1.jsonl).

## Reads with all 16 vCPUs available

These runs were unpinned and repeated three times. The load node reached about 7–13 CPU cores in client-bound cases. With Valkey io-threads=8, observed median MGET 16/64 throughput differences ranged from about −2.15% to +1.26%; the observed throughput ranges did not overlap in any of the four 64-key comparisons. These samples do not establish statistical equivalence. With io-threads=1, random-key MGET 16 at 256 clients measured 116,130 vs 97,489 req/s (19.1% higher for pg_local_cache). Wide MGETs were client/network-bound; the io-threads=1 column has no 64-key result.

| Keys | MGET keys | Clients | pg_local_cache (req/s, p99 ms, vCPU) | Valkey io-threads=8 (req/s, p99 ms, vCPU) | Valkey io-threads=1 (req/s, p99 ms, vCPU) | Prepared SQL (req/s, p99 ms, vCPU) |
| --- | --- | --- | --- | --- | --- | --- |
| Hot | 1 | 64 | 217,724 (0.70, 2.89) | 206,634 (0.70, 7.85) | 167,929 (0.71, 0.66) | 132,433 (0.96, 11.32) |
| Hot | 1 | 256 | 372,683 (2.39, 4.21) | 365,947 (2.33, 7.92) | 161,455 (2.92, 0.68) | 173,571 (4.26, 13.14) |
| Hot | 16 | 64 | 82,858 (3.38, 3.13) | 83,098 (3.38, 6.41) | 79,179 (3.38, 0.69) | 73,512 (2.05, 8.78) |
| Hot | 16 | 256 | 122,496 (9.83, 4.07) | 122,817 (9.83, 7.42) | 111,790 (8.06, 0.83) | 100,966 (6.36, 11.49) |
| Hot | 64 | 64 | 30,241 (8.78, 3.40) | 30,845 (9.04, 3.57) | — | 27,790 (6.88, 9.05) |
| Hot | 64 | 256 | 39,531 (43.52, 4.26) | 39,995 (47.71, 5.66) | — | 34,519 (30.67, 12.23) |
| 100k random | 1 | 64 | 215,310 (0.70, 2.89) | 205,081 (0.70, 7.85) | 162,778 (0.73, 0.67) | 131,563 (0.97, 11.34) |
| 100k random | 1 | 256 | 366,860 (2.39, 4.31) | 360,894 (2.33, 7.92) | 158,444 (2.98, 0.68) | 169,884 (4.26, 13.19) |
| 100k random | 16 | 64 | 79,505 (3.51, 3.45) | 79,556 (3.44, 6.68) | 74,270 (3.38, 0.79) | 69,262 (2.13, 9.12) |
| 100k random | 16 | 256 | 116,130 (10.35, 4.42) | 114,683 (9.83, 7.53) | 97,489 (7.67, 0.95) | 91,572 (7.01, 11.96) |
| 100k random | 64 | 64 | 29,000 (8.78, 3.81) | 29,464 (9.04, 4.61) | — | 26,169 (7.14, 9.39) |
| 100k random | 64 | 256 | 37,356 (43.52, 4.93) | 38,175 (40.37, 7.58) | — | 32,756 (31.20, 13.03) |

For random-key MGET 64 at 256 clients, pg_local_cache p99 was 43.52 ms versus 40.37 ms for Valkey. Prepared SQL throughput was substantially lower in the single-key pinned tests; full results vary by batch size and latency metric.

Raw runs: [Valkey io-threads=8](benchmarks/3.1.0/read-8-cores.jsonl), [Valkey io-threads=1](benchmarks/3.1.0/read-8-cores-valkey-io-threads-1.jsonl).

## Write overhead

Three 15-second repetitions compare plain table transactions, transactions through pg_local_cache, and plain transactions followed by Valkey DEL. Percentages compare throughput with the matching plain-table case.

| Operation | synchronous_commit | Clients | Plain table tx/s (DB µs/tx) | pg_local_cache tx/s (DB µs/tx, change) | Plain + Valkey DEL tx/s (DB µs/tx, change) |
| --- | --- | --- | --- | --- | --- |
| Update | on | 32 | 46,747 (109) | 45,065 (119, −3.6%) | 33,234 (204, −28.9%) |
| Update | on | 64 | 63,479 (108) | 61,027 (121, −3.9%) | 37,659 (238, −40.7%) |
| Update | off | 32 | 119,156 (75) | 110,987 (87, −6.9%) | 53,374 (158, −55.2%) |
| Update | off | 64 | 148,358 (73) | 136,194 (83, −8.2%) | 53,853 (189, −63.7%) |
| Insert | on | 32 | 48,831 (97) | 47,020 (105, −3.7%) | 34,863 (184, −28.6%) |
| Insert | on | 64 | 65,688 (97) | 63,011 (108, −4.1%) | 39,642 (218, −39.7%) |
| Insert | off | 32 | 123,679 (63) | 117,868 (72, −4.7%) | 58,168 (135, −53.0%) |
| Insert | off | 64 | 138,454 (68) | 130,805 (77, −5.5%) | 58,361 (165, −57.8%) |

Across UPDATE cases, pg_local_cache was 3.6–8.2% below plain writes; INSERT overhead was 3.7–5.5%. Plain UPDATE plus Valkey DEL was 28.9–63.7% below plain UPDATE throughput; the INSERT comparison was 28.6–57.8% below.

Raw runs: [write overhead](benchmarks/3.1.0/write-overhead.jsonl).

## Reads during writes

Each case ran once for 30 seconds with 64 readers issuing one-key reads over 60k rows; writers used synchronous_commit=off. Valkey used cache-aside (SQL write followed by DEL) with io-threads=8. Stale is the end-of-run sentinel check; n/a means the stack has no cache entries to inspect.

| Stack | Key distribution | Writer | Reads/s | Read p99 ms | Hit ratio | Writes/s | Stale entries |
| --- | --- | --- | --- | --- | --- | --- | --- |
| pg_local_cache | Uniform | None | 214,508 | 0.71 | 1.000 | 0 | 0 |
| pg_local_cache | Uniform | Update 10,000/s | 195,384 | 0.86 | 0.948 | 10,000 | 0 |
| pg_local_cache | Uniform | Update 30,000/s | 161,919 | 1.04 | 0.837 | 29,999 | 0 |
| pg_local_cache | Uniform | Update unlimited | 82,640 | 1.69 | 0.432 | 111,013 | 0 |
| pg_local_cache | Zipf | None | 216,297 | 0.70 | 1.000 | 0 | 0 |
| pg_local_cache | Zipf | Update 10,000/s | 198,454 | 0.86 | 0.964 | 10,000 | 0 |
| pg_local_cache | Zipf | Update 30,000/s | 168,875 | 1.01 | 0.923 | 30,000 | 0 |
| pg_local_cache | Zipf | Update unlimited | 96,297 | 1.46 | 0.834 | 113,505 | 0 |
| Valkey cache-aside | Uniform | None | 223,537 | 0.61 | 1.000 | 0 | 0 |
| Valkey cache-aside | Uniform | Update 10,000/s | 156,983 | 1.92 | 0.936 | 10,000 | 0 |
| Valkey cache-aside | Uniform | Update 30,000/s | 42,294 | 7.14 | 0.577 | 29,998 | 0 |
| Valkey cache-aside | Uniform | Update unlimited | 29,692 | 8.78 | 0.444 | 38,481 | 1 |
| Valkey cache-aside | Zipf | None | 222,575 | 0.63 | 0.999 | 0 | 0 |
| Valkey cache-aside | Zipf | Update 10,000/s | 168,911 | 1.65 | 0.960 | 10,000 | 0 |
| Valkey cache-aside | Zipf | Update 30,000/s | 75,642 | 4.78 | 0.887 | 29,999 | 0 |
| Valkey cache-aside | Zipf | Update unlimited | 48,993 | 6.88 | 0.852 | 40,735 | 0 |
| Prepared SQL | Uniform | None | 129,714 | 0.97 | — | 0 | n/a |
| Prepared SQL | Uniform | Update 10,000/s | 121,567 | 1.13 | — | 10,000 | n/a |
| Prepared SQL | Uniform | Update 30,000/s | 106,949 | 1.33 | — | 30,000 | n/a |
| Prepared SQL | Uniform | Update unlimited | 77,574 | 2.02 | — | 93,955 | n/a |
| Prepared SQL | Zipf | None | 129,937 | 0.97 | — | 0 | n/a |
| Prepared SQL | Zipf | Update 10,000/s | 122,182 | 1.13 | — | 10,000 | n/a |
| Prepared SQL | Zipf | Update 30,000/s | 107,852 | 1.33 | — | 29,999 | n/a |
| Prepared SQL | Zipf | Update unlimited | 78,601 | 2.08 | — | 95,199 | n/a |
| pg_local_cache | Uniform | Insert unlimited | 117,531 | 1.20 | 1.000 | 114,066 | 0 |
| Valkey cache-aside | Uniform | Insert unlimited | 70,302 | 4.92 | 1.000 | 46,622 | 0 |
| Prepared SQL | Uniform | Insert unlimited | 84,294 | 1.98 | — | 95,094 | n/a |

pg_local_cache ended every listed mixed run with zero stale entries. Valkey ended one UPDATE case with one stale sentinel entry: Uniform keys at an unlimited update rate. At 30k UPDATE/s on Uniform keys, read rates were 161,919/s for pg_local_cache, 42,294/s for Valkey cache-aside, and 106,949/s for prepared SQL.

Raw runs: [mixed reads and writes](benchmarks/3.1.0/mixed.jsonl).

## 120-second stale probes

Each probe ran once with 64 readers and 64 unlimited UPDATE writers, then performed a full stale-entry check. Valkey used io-threads=8.

| Stack | Key distribution | Reads/s | Read p99 ms | Writes/s | Stale entries | Note |
| --- | --- | --- | --- | --- | --- | --- |
| pg_local_cache | Uniform | 82,565 | 1.687552 | 108,580 | 0 | — |
| pg_local_cache | Zipf | 0 | n/a | 101,797 | 0 | See note below. |
| Valkey cache-aside | Uniform | 30,210 | 8.781824 | 36,508 | 2 | — |
| Valkey cache-aside | Zipf | 28,568 | 8.781824 | 35,789 | 1 | — |

The pg_local_cache Zipf reader failed its startup equivalence check while concurrent writes were active (harness issue), so it reported zero reads. Its stale check still ran and found zero stale entries.

Raw runs: [120-second stale probes](benchmarks/3.1.0/stale-probes-120s.jsonl).

## One-hour soak

Single run: MGET 16 over 100k random keys, 64 clients, plus 20,000 UPDATE/s. RESP worker RssAnon was sampled every minute.

- Reads: 251,987,814 MGET over 3,600 seconds (69,997/s), p99 3.57 ms.
- Writes: 71,999,993 UPDATE (20,000/s), p99 1.13 ms, zero errors.
- Worker RssAnon stayed flat: maximum per-worker growth was 44 kB across 60 samples.

Raw runs: [one-hour soak](benchmarks/3.1.0/soak-1h.jsonl).

## Stand and method

Each VM reported 16 vCPU arranged as 2 sockets × 4 cores per socket × 2 threads per core, 62 GB RAM, and Debian 13 with kernel 6.12.111+deb13-amd64; this is the VM-visible topology. The DB VM ran PostgreSQL 16.15 (max_connections=1000, shared_buffers=1048576) and pg_local_cache 3.1.0 c431bcc (enabled, memory_budget_mb=1024, workers=8). Its table held 100,000 JSON rows averaging 166 bytes. Valkey 8.1.1 used io-threads=8 during write-overhead, mixed, and stale-probe runs. From the load VM to the DB VM, iperf3 measured 10.4 Gbit/s and ping reported min/avg/max RTT of 0.090/0.143/0.871 ms. The Go/pgx load client ran on the second VM. Reads used 20-second runs; write overhead used 15 seconds, mixed reads/writes 30 seconds, stale probes 120 seconds, and the soak 3,600 seconds.

The pinned matrix used 256 clients; every case had five runs except the two one-core Valkey io-threads=1 cases, which had two. The all-core matrix and write-overhead cases had three runs each; mixed, stale-probe, and soak tests each ran once. NIC interrupts were not pinned. The 64-key results are client/network-bound. Every table uses final build c431bcc. Throughput is requests/s unless the table says transactions/s; latency is p99, CPU is server vCPU.

Environment and raw JSONL: [environment](benchmarks/3.1.0/env.txt), [stand evidence](benchmarks/3.1.0/stand.txt), [pinned reads](benchmarks/3.1.0/read-per-core.jsonl), [Valkey io-threads=1 pinned reads](benchmarks/3.1.0/read-per-core-valkey-io-threads-1.jsonl), [all-core reads](benchmarks/3.1.0/read-8-cores.jsonl), [all-core Valkey io-threads=1 reads](benchmarks/3.1.0/read-8-cores-valkey-io-threads-1.jsonl), [write overhead](benchmarks/3.1.0/write-overhead.jsonl), [reads during writes](benchmarks/3.1.0/mixed.jsonl), [120-second stale probes](benchmarks/3.1.0/stale-probes-120s.jsonl), and [one-hour soak](benchmarks/3.1.0/soak-1h.jsonl).

## Reproduce with bench/

The scripts need Python 3 and SSH aliases for the database and load VMs. The
DB VM needs PostgreSQL 16, Valkey, mpstat, sar, ip, and systemd; the load VM
needs the compiled Go client in /root/bench-client. See [bench/README.md](https://github.com/profundium/pg_local_cache/blob/c431bcc4f69d4ecd28f1695548e121e55dfdd2fa/bench/README.md)
and the [Go/pgx client](https://github.com/profundium/pg_local_cache/tree/c431bcc4f69d4ecd28f1695548e121e55dfdd2fa/examples/go-pgx/).

Run the default 20-second read matrix (three repeats, 1/16/64 keys, 16/64/256
clients, hot and 100k-key spaces):

    python3 bench/run_bench.py reads.jsonl

For a pinned one-vCPU-pair, one-key comparison with 256 clients and five repeats,
replace 0-1 with the DB VM logical CPU IDs for one vCPU pair:

    PGLC_SERVER_CPUS=0-1 REPEATS=5 BATCHES=1 CLIENTS=256 KEY_SPACES=0,100000 python3 bench/run_bench.py pinned.jsonl

The runner applies AllowedCPUs to both PostgreSQL and Valkey, then clears it
afterward. It records server CPU, latency, network RX/TX, and client CPU.
Configure Valkey's io-threads on the DB VM before the run to match the
configuration being reproduced.

Run write-overhead and 30-second mixed workloads:

    REPEATS=3 WRITE_SECONDS=15 WRITE_CLIENTS=32,64 python3 bench/write_bench.py writes.jsonl
    REPEATS=1 MIXED_SECONDS=30 MIXED_RATES=none,10000,30000,unlimited MIXED_KEY_DISTS=uniform,zipf python3 bench/write_bench.py mixed.jsonl

The write runner records database CPU per transaction; mixed rows also record
read latency, hit/miss counts, writer rate, and the end-of-run stale-entry
check. The scripts append JSONL rows and preserve completed samples on failure.

## Historical 2.x laptop results

These older JSON files are historical Apple M3 Max laptop measurements using
PostgreSQL 16; their environment and client topology differ from the 3.1.0
two-VM stand above. They include the 2.x SQL mget API, removed in 3.0.0.
Use the [2.0.4 documentation](https://github.com/profundium/pg_local_cache/tree/v2.0.4/docs)
for that API; current cached reads use RESP MGET.

- [14 September 2026 Go measurements](benchmarks/2026-09-14-m3-max.json)
- [14 September 2026 client measurements](benchmarks/2026-09-14-m3-max-clients.json)
- [15 September 2026 Go/RESP measurements](benchmarks/2026-09-15-m3-max-resp.json)
