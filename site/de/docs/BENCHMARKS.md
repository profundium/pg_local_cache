---
layout: doc
lang: de
translation_key: BENCHMARKS
title: PostgreSQL-Cache-Benchmarks, 3.1.0
description: "pg_local_cache 3.1.0-Ergebnisse auf zwei Maschinen: gepinnte und vollständige Leseläufe, Schreibkosten, Lesen während Schreibvorgängen und Methodik."
section: Benchmarks
permalink: /de/docs/BENCHMARKS.html
redirect_from:
  - /de/docs/benchmarks-go.html
  - /de/docs/benchmarks-node.html
  - /de/docs/benchmarks-go/
  - /de/docs/benchmarks-node/
last_modified_at: "2026-10-07"
---
# PostgreSQL-Cache-Benchmarks: 3.1.0

Diese Messungen vergleichen pg_local_cache RESP MGET, Valkey und vorbereitetes SQL auf zwei VMs in einem privaten Netzwerk. Alle folgenden Ergebnisse stammen vom finalen 3.1.0-Build c431bcc. Mediane wiederholter Läufe gelten für Lese- und Schreibkostentests; beide gepinnten Ein-Kern-Fälle mit Valkey io-threads=1 haben zwei Läufe, die übrigen gepinnten Fälle fünf. Gemischte Tests, 120-Sekunden-Stale-Probes und einstündiger Soak-Test sind Einzelläufe. Diese Lastmessungen sind keine Zusage für Produktionskapazität.

## Ergebnisse des finalen Builds

- Pro gepinntem Serverkern war io-threads=1 bei einem Kern die stärkste gemessene Valkey-Konfiguration (180k Reads/s gegenüber 210k für pg_local_cache); bei zwei Kernen war es io-threads=4 (266k gegenüber 337k). Bei vier Kernen lag der clientbegrenzte Durchsatz bei etwa 361k gegenüber 357k; pg_local_cache nutzte 5,24 statt 7,95 Server-vCPU. Valkey mit io-threads=1 benötigte pro Anfrage am wenigsten Server-CPU. Vorbereitetes SQL lag bei den gepinnten Einzel-Key-Tests deutlich zurück.
- Mit Valkey io-threads=8 lagen die beobachteten Median-Durchsatzunterschiede für MGET 16/64 zwischen etwa −2,15 % und +1,26 %; bei allen vier 64-Schlüsselvergleichen überlappten sich die beobachteten Bereiche nicht. Diese Stichproben belegen keine statistische Gleichwertigkeit. Breite MGETs waren client-/netzwerkbegrenzt. Bei zufälligen Schlüsseln und MGET 64 mit 256 Clients war p99 in einer Zeile höher (43,52 statt 40,37 ms).
- Der Schreibaufwand gegenüber einfachen Tabellentransaktionen betrug 3,6–8,2 % bei UPDATE und 3,7–5,5 % bei INSERT. Einfaches UPDATE plus Valkey DEL lag beim UPDATE-Durchsatz 28,9–63,7 % darunter.
- Während der Schreibvorgänge fanden Prüfungen keine veralteten pg_local_cache-Einträge. Nach 120 Sekunden blieben bei Valkey cache-aside 2 veraltete Einträge bei Uniform und 1 bei Zipf. Der einstündige Soak-Test schloss 252 Mio. MGET und 72 Mio. UPDATE ab; RssAnon-Wachstum der Worker blieb bei höchstens 44 kB.

## Lesen pro gepinntem Server-vCPU-Paar

Der Go/pgx-Client verwendete 256 Clients und einen Schlüssel pro MGET. Jeder Fall lief 20 Sekunden. Die beiden Ein-Kern-Fälle mit Valkey io-threads=1 liefen zweimal; alle übrigen gepinnten Fälle fünfmal. PostgreSQL und Valkey waren auf N vCPU-Paare (2N vCPU) gepinnt. Valkey lief mit io-threads=2N und io-threads=1; vorbereitetes SQL verwendete dieselben Schlüssel.

| vCPU-Paare | Schlüssel | pg_local_cache Anfragen/s (p99 ms, vCPU) | Valkey io-threads=2N (Anfragen/s, p99 ms, vCPU) | Valkey io-threads=1 (Anfragen/s, p99 ms, vCPU) | Vorbereitetes SQL (Anfragen/s, p99 ms, vCPU) |
| --- | --- | --- | --- | --- | --- |
| 1 | Hot | 209,696 (2.46, 1.99) | 91,478 (3.24, 2.00) | 180,141 (2.59, 0.85) | 23,200 (24.90, 2.00) |
| 1 | 100k zufällig | 195,950 (2.65, 2.00) | 88,672 (3.38, 2.00) | 175,853 (2.65, 0.92) | 22,433 (25.95, 2.00) |
| 2 | Hot | 336,691 (2.02, 3.78) | 266,340 (1.69, 4.00) | 162,230 (2.85, 0.66) | 46,386 (16.38, 4.00) |
| 2 | 100k zufällig | 326,125 (1.98, 3.83) | 267,826 (1.72, 4.00) | 160,220 (2.92, 0.69) | 45,211 (17.56, 4.00) |
| 4 | Hot | 360,968 (2.33, 5.24) | 357,161 (2.20, 7.95) | 160,633 (2.85, 0.66) | 88,604 (6.75, 8.00) |
| 4 | 100k zufällig | 353,941 (2.33, 5.27) | 351,344 (2.20, 7.95) | 159,414 (2.92, 0.68) | 86,790 (6.88, 8.00) |

Bei einem Kern war Valkey mit einem Thread die stärkste Valkey-Konfiguration. Bei zwei Kernen war io-threads=4 am stärksten; Valkey hatte trotz geringeren Durchsatzes ein niedrigeres p99. Bei vier Kernen erreichten pg_local_cache und io-threads=8 nahezu denselben clientbegrenzten Durchsatz, bei geringerer Server-CPU für pg_local_cache.

Raw runs: [Valkey io-threads=2N](../../docs/benchmarks/3.1.0/read-per-core.jsonl), [Valkey io-threads=1](../../docs/benchmarks/3.1.0/read-per-core-valkey-io-threads-1.jsonl).

## Lesen mit allen 16 vCPUs

Diese Läufe waren nicht gepinnt und wurden dreimal wiederholt. Der Load-Client nutzte in clientbegrenzten Fällen etwa 7–13 CPU-Kerne. Mit Valkey io-threads=8 lagen die beobachteten Median-Durchsatzunterschiede für MGET 16/64 zwischen etwa −2,15 % und +1,26 %; bei allen vier 64-Schlüsselvergleichen überlappten sich die Bereiche nicht. Diese Stichproben belegen keine statistische Gleichwertigkeit. Bei zufälligen MGET 16 mit 256 Clients lag pg_local_cache mit io-threads=1 bei 116.130 gegenüber 97.489 Reads/s um 19,1 % vorn. Breite MGETs waren client-/netzwerkbegrenzt; für io-threads=1 gibt es kein Ergebnis mit 64 Schlüsseln.

| Schlüssel | MGET-Schlüssel | Clients | pg_local_cache (Anfragen/s, p99 ms, vCPU) | Valkey io-threads=8 (Anfragen/s, p99 ms, vCPU) | Valkey io-threads=1 (Anfragen/s, p99 ms, vCPU) | Vorbereitetes SQL (Anfragen/s, p99 ms, vCPU) |
| --- | --- | --- | --- | --- | --- | --- |
| Hot | 1 | 64 | 217,724 (0.70, 2.89) | 206,634 (0.70, 7.85) | 167,929 (0.71, 0.66) | 132,433 (0.96, 11.32) |
| Hot | 1 | 256 | 372,683 (2.39, 4.21) | 365,947 (2.33, 7.92) | 161,455 (2.92, 0.68) | 173,571 (4.26, 13.14) |
| Hot | 16 | 64 | 82,858 (3.38, 3.13) | 83,098 (3.38, 6.41) | 79,179 (3.38, 0.69) | 73,512 (2.05, 8.78) |
| Hot | 16 | 256 | 122,496 (9.83, 4.07) | 122,817 (9.83, 7.42) | 111,790 (8.06, 0.83) | 100,966 (6.36, 11.49) |
| Hot | 64 | 64 | 30,241 (8.78, 3.40) | 30,845 (9.04, 3.57) | — | 27,790 (6.88, 9.05) |
| Hot | 64 | 256 | 39,531 (43.52, 4.26) | 39,995 (47.71, 5.66) | — | 34,519 (30.67, 12.23) |
| 100k zufällig | 1 | 64 | 215,310 (0.70, 2.89) | 205,081 (0.70, 7.85) | 162,778 (0.73, 0.67) | 131,563 (0.97, 11.34) |
| 100k zufällig | 1 | 256 | 366,860 (2.39, 4.31) | 360,894 (2.33, 7.92) | 158,444 (2.98, 0.68) | 169,884 (4.26, 13.19) |
| 100k zufällig | 16 | 64 | 79,505 (3.51, 3.45) | 79,556 (3.44, 6.68) | 74,270 (3.38, 0.79) | 69,262 (2.13, 9.12) |
| 100k zufällig | 16 | 256 | 116,130 (10.35, 4.42) | 114,683 (9.83, 7.53) | 97,489 (7.67, 0.95) | 91,572 (7.01, 11.96) |
| 100k zufällig | 64 | 64 | 29,000 (8.78, 3.81) | 29,464 (9.04, 4.61) | — | 26,169 (7.14, 9.39) |
| 100k zufällig | 64 | 256 | 37,356 (43.52, 4.93) | 38,175 (40.37, 7.58) | — | 32,756 (31.20, 13.03) |

Bei zufälligen Schlüsseln, MGET 64 und 256 Clients betrug p99 für pg_local_cache 43,52 ms gegenüber 40,37 ms für Valkey. Vorbereitetes SQL erreichte bei gepinnten Einzel-Key-Tests deutlich weniger Durchsatz; bei größeren Batches unterscheiden sich Durchsatz und Latenz.

Raw runs: [Valkey io-threads=8](../../docs/benchmarks/3.1.0/read-8-cores.jsonl), [Valkey io-threads=1](../../docs/benchmarks/3.1.0/read-8-cores-valkey-io-threads-1.jsonl).

## Schreibkosten

Drei Läufe zu je 15 Sekunden vergleichen einfache Tabellentransaktionen, Transaktionen über pg_local_cache und einfache Transaktionen mit anschließendem Valkey DEL. Prozentwerte beziehen sich auf den passenden Fall mit einfachen Tabellentransaktionen.

| Vorgang | synchronous_commit | Clients | Einfache Tabelle tx/s (DB µs/tx) | pg_local_cache tx/s (DB µs/tx, Änderung) | Einfache Tabelle + Valkey DEL tx/s (DB µs/tx, Änderung) |
| --- | --- | --- | --- | --- | --- |
| Update | on | 32 | 46,747 (109) | 45,065 (119, −3.6%) | 33,234 (204, −28.9%) |
| Update | on | 64 | 63,479 (108) | 61,027 (121, −3.9%) | 37,659 (238, −40.7%) |
| Update | off | 32 | 119,156 (75) | 110,987 (87, −6.9%) | 53,374 (158, −55.2%) |
| Update | off | 64 | 148,358 (73) | 136,194 (83, −8.2%) | 53,853 (189, −63.7%) |
| Insert | on | 32 | 48,831 (97) | 47,020 (105, −3.7%) | 34,863 (184, −28.6%) |
| Insert | on | 64 | 65,688 (97) | 63,011 (108, −4.1%) | 39,642 (218, −39.7%) |
| Insert | off | 32 | 123,679 (63) | 117,868 (72, −4.7%) | 58,168 (135, −53.0%) |
| Insert | off | 64 | 138,454 (68) | 130,805 (77, −5.5%) | 58,361 (165, −57.8%) |

Bei UPDATE lag pg_local_cache 3,6–8,2 % unter einfachen Schreibvorgängen; bei INSERT betrug der Aufwand 3,7–5,5 %. Einfaches UPDATE plus Valkey DEL lag beim UPDATE-Durchsatz 28,9–63,7 % darunter; beim INSERT-Vergleich waren es 28,6–57,8 %.

Raw runs: [write overhead](../../docs/benchmarks/3.1.0/write-overhead.jsonl).

## Lesen während Schreibvorgängen

Jeder Fall lief einmal 30 Sekunden mit 64 Lesern über 60k Zeilen; Writer verwendeten synchronous_commit=off. Valkey nutzte cache-aside (SQL-Schreibvorgang, danach DEL) mit io-threads=8. „Veraltet“ ist die Sentinel-Prüfung am Laufende; n/a bedeutet, dass der Stack keine Cache-Einträge zum Prüfen hat.

| Stack | Schlüsselverteilung | Writer | Lesen/s | Lese-p99 ms | Trefferquote | Schreiben/s | Veraltete Einträge |
| --- | --- | --- | --- | --- | --- | --- | --- |
| pg_local_cache | Uniform | Keine | 214,508 | 0.71 | 1.000 | 0 | 0 |
| pg_local_cache | Uniform | Update 10,000/s | 195,384 | 0.86 | 0.948 | 10,000 | 0 |
| pg_local_cache | Uniform | Update 30,000/s | 161,919 | 1.04 | 0.837 | 29,999 | 0 |
| pg_local_cache | Uniform | Update unlimited | 82,640 | 1.69 | 0.432 | 111,013 | 0 |
| pg_local_cache | Zipf | Keine | 216,297 | 0.70 | 1.000 | 0 | 0 |
| pg_local_cache | Zipf | Update 10,000/s | 198,454 | 0.86 | 0.964 | 10,000 | 0 |
| pg_local_cache | Zipf | Update 30,000/s | 168,875 | 1.01 | 0.923 | 30,000 | 0 |
| pg_local_cache | Zipf | Update unlimited | 96,297 | 1.46 | 0.834 | 113,505 | 0 |
| Valkey cache-aside | Uniform | Keine | 223,537 | 0.61 | 1.000 | 0 | 0 |
| Valkey cache-aside | Uniform | Update 10,000/s | 156,983 | 1.92 | 0.936 | 10,000 | 0 |
| Valkey cache-aside | Uniform | Update 30,000/s | 42,294 | 7.14 | 0.577 | 29,998 | 0 |
| Valkey cache-aside | Uniform | Update unlimited | 29,692 | 8.78 | 0.444 | 38,481 | 1 |
| Valkey cache-aside | Zipf | Keine | 222,575 | 0.63 | 0.999 | 0 | 0 |
| Valkey cache-aside | Zipf | Update 10,000/s | 168,911 | 1.65 | 0.960 | 10,000 | 0 |
| Valkey cache-aside | Zipf | Update 30,000/s | 75,642 | 4.78 | 0.887 | 29,999 | 0 |
| Valkey cache-aside | Zipf | Update unlimited | 48,993 | 6.88 | 0.852 | 40,735 | 0 |
| Prepared SQL | Uniform | Keine | 129,714 | 0.97 | — | 0 | n/a |
| Prepared SQL | Uniform | Update 10,000/s | 121,567 | 1.13 | — | 10,000 | n/a |
| Prepared SQL | Uniform | Update 30,000/s | 106,949 | 1.33 | — | 30,000 | n/a |
| Prepared SQL | Uniform | Update unlimited | 77,574 | 2.02 | — | 93,955 | n/a |
| Prepared SQL | Zipf | Keine | 129,937 | 0.97 | — | 0 | n/a |
| Prepared SQL | Zipf | Update 10,000/s | 122,182 | 1.13 | — | 10,000 | n/a |
| Prepared SQL | Zipf | Update 30,000/s | 107,852 | 1.33 | — | 29,999 | n/a |
| Prepared SQL | Zipf | Update unlimited | 78,601 | 2.08 | — | 95,199 | n/a |
| pg_local_cache | Uniform | Insert unlimited | 117,531 | 1.20 | 1.000 | 114,066 | 0 |
| Valkey cache-aside | Uniform | Insert unlimited | 70,302 | 4.92 | 1.000 | 46,622 | 0 |
| Prepared SQL | Uniform | Insert unlimited | 84,294 | 1.98 | — | 95,094 | n/a |

pg_local_cache beendete alle aufgeführten Mischläufe ohne veraltete Einträge. Valkey ließ nur in einem UPDATE-Fall einen veralteten Sentinel-Eintrag zurück: Uniform-Schlüssel bei unbegrenzter Update-Rate. Bei 30k UPDATE/s und Uniform-Schlüsseln lagen die Leseraten bei 161.919/s für pg_local_cache, 42.294/s für Valkey cache-aside und 106.949/s für vorbereitetes SQL.

Raw runs: [mixed reads and writes](../../docs/benchmarks/3.1.0/mixed.jsonl).

## 120-Sekunden-Prüfungen auf veraltete Einträge

Jeder Probe-Lauf erfolgte einmal mit 64 Lesern und 64 unbegrenzten UPDATE-Writern, danach folgte die vollständige Prüfung veralteter Einträge. Valkey verwendete io-threads=8.

| Stack | Schlüsselverteilung | Lesen/s | Lese-p99 ms | Schreiben/s | Veraltete Einträge | Hinweis |
| --- | --- | --- | --- | --- | --- | --- |
| pg_local_cache | Uniform | 82,565 | 1.687552 | 108,580 | 0 | — |
| pg_local_cache | Zipf | 0 | n/a | 101,797 | 0 | Siehe Hinweis unten. |
| Valkey cache-aside | Uniform | 30,210 | 8.781824 | 36,508 | 2 | — |
| Valkey cache-aside | Zipf | 28,568 | 8.781824 | 35,789 | 1 | — |

Der pg_local_cache-Zipf-Leser bestand während paralleler Schreibvorgänge die Äquivalenzprüfung beim Start nicht (Harness-Problem) und meldete daher null Lesevorgänge. Die Prüfung lief trotzdem und fand null veraltete Einträge.

Raw runs: [120-second stale probes](../../docs/benchmarks/3.1.0/stale-probes-120s.jsonl).

## Einstündiger Soak-Test

Einzellauf: MGET 16 über 100k zufällige Schlüssel, 64 Clients, plus 20.000 UPDATE/s. RssAnon der RESP-Worker wurde minütlich erfasst.

- Lesen: 251.987.814 MGET in 3.600 Sekunden (69.997/s), p99 3,57 ms.
- Schreiben: 71.999.993 UPDATE (20.000/s), p99 1,13 ms, null Fehler.
- RssAnon der Worker blieb stabil: maximales Wachstum pro Worker 44 kB über 60 Messungen.

Raw runs: [one-hour soak](../../docs/benchmarks/3.1.0/soak-1h.jsonl).

## Teststand und Methodik

Laut lscpu meldete jede VM 16 vCPU in 2 Sockets × 4 Kernen pro Socket × 2 Threads pro Kern (VM-Topologie), 62 GB RAM und Debian 13 mit Kernel 6.12.111+deb13-amd64. Die DB-VM nutzte PostgreSQL 16.15 (max_connections=1000, shared_buffers=1048576) und pg_local_cache 3.1.0 c431bcc (aktiviert, memory_budget_mb=1024, workers=8). Die Tabelle hatte 100.000 JSON-Zeilen mit durchschnittlich 166 Byte. Valkey 8.1.1 nutzte io-threads=8 bei Schreibkosten-, Misch- und Stale-Probe-Läufen. Vom Load- zur DB-VM maß iperf3 10,4 Gbit/s; ping meldete RTT min/avg/max 0,090/0,143/0,871 ms. Der Go/pgx-Client lief auf der zweiten VM. Leseläufe dauerten 20 Sekunden; Schreibtests 15 Sekunden, gemischte Läufe 30 Sekunden, Stale-Probes 120 Sekunden und der Soak-Test 3.600 Sekunden.

Die gepinnte Matrix verwendete 256 Clients; jeder Fall hatte fünf Läufe außer den beiden Ein-Kern-Fällen mit Valkey io-threads=1, die zwei Läufe hatten. All-Core-Lesematrix und Schreibkosten hatten je drei Läufe; gemischte Tests, Stale-Probes und Soak-Test je einen. NIC-Interrupts waren nicht gepinnt. Alle Tabellen verwenden den finalen Build c431bcc. Durchsatz ist Anfragen/s, sofern nicht Transaktionen/s angegeben sind; Latenz ist p99, CPU ist Server-vCPU.

Umgebung und JSONL-Rohdaten: [Umgebung](../../docs/benchmarks/3.1.0/env.txt), [Standnachweis](../../docs/benchmarks/3.1.0/stand.txt), [gepinntes Lesen](../../docs/benchmarks/3.1.0/read-per-core.jsonl), [gepinntes Lesen mit Valkey io-threads=1](../../docs/benchmarks/3.1.0/read-per-core-valkey-io-threads-1.jsonl), [Lesen auf allen Kernen](../../docs/benchmarks/3.1.0/read-8-cores.jsonl), [alle Kerne, Valkey io-threads=1](../../docs/benchmarks/3.1.0/read-8-cores-valkey-io-threads-1.jsonl), [Schreibkosten](../../docs/benchmarks/3.1.0/write-overhead.jsonl), [Lesen während Schreibvorgängen](../../docs/benchmarks/3.1.0/mixed.jsonl), [120-Sekunden-Stale-Probes](../../docs/benchmarks/3.1.0/stale-probes-120s.jsonl) und [einstündiger Soak-Test](../../docs/benchmarks/3.1.0/soak-1h.jsonl).

## Mit bench/ reproduzieren

Benötigt werden Python 3 und SSH-Aliase für Datenbank- und Load-VM. Die Datenbank-VM braucht PostgreSQL 16, Valkey, mpstat, sar, ip und systemd; die Load-VM den kompilierten Go-Client unter /root/bench-client. Siehe [bench/README.md](https://github.com/profundium/pg_local_cache/blob/c431bcc4f69d4ecd28f1695548e121e55dfdd2fa/bench/README.md) und den [Go/pgx-Client](https://github.com/profundium/pg_local_cache/tree/c431bcc4f69d4ecd28f1695548e121e55dfdd2fa/examples/go-pgx/).

Standardmatrix für Reads: 20 Sekunden, drei Wiederholungen, 1/16/64 Schlüssel, 16/64/256 Clients, Hot-Set und 100k Schlüsselraum:

    python3 bench/run_bench.py reads.jsonl

Beispiel für ein gepinntes vCPU-Paar mit 256 Clients und fünf Wiederholungen; 0-1 durch die logischen CPU-IDs dieses vCPU-Paars ersetzen:

    PGLC_SERVER_CPUS=0-1 REPEATS=5 BATCHES=1 CLIENTS=256 KEY_SPACES=0,100000 python3 bench/run_bench.py pinned.jsonl

Der Runner setzt AllowedCPUs für PostgreSQL und Valkey und hebt die Begrenzung danach auf. Er erfasst Server-CPU, Latenz, Netzwerk-RX/TX und Client-CPU. Valkeys io-threads vor dem Lauf passend zur gewünschten Konfiguration setzen.

Schreibkosten und 30-Sekunden-Mischlast starten:

    REPEATS=3 WRITE_SECONDS=15 WRITE_CLIENTS=32,64 python3 bench/write_bench.py writes.jsonl
    REPEATS=1 MIXED_SECONDS=30 MIXED_RATES=none,10000,30000,unlimited MIXED_KEY_DISTS=uniform,zipf python3 bench/write_bench.py mixed.jsonl

Die Skripte hängen JSONL-Zeilen an und behalten abgeschlossene Samples bei Fehlern. Der Schreibtest misst DB-CPU pro Transaktion; der Mischtest enthält außerdem Leselatenz, Treffer/Fehlschläge, Schreibrate und die abschließende Prüfung veralteter Einträge.

## Historische 2.x-Ergebnisse auf dem MacBook

Diese älteren JSON-Dateien sind historische Apple-M3-Max-Messungen mit PostgreSQL 16; Umgebung und Client-Topologie unterscheiden sich vom Zwei-VM-Test oben. Sie enthalten die in 3.0.0 entfernte SQL-mget-API aus 2.x. Die [Dokumentation zu 2.0.4](https://github.com/profundium/pg_local_cache/tree/v2.0.4/docs) beschreibt diese API; aktuelle Cache-Reads verwenden RESP MGET.

- [Go-Messungen vom 14. September 2026](../../docs/benchmarks/2026-09-14-m3-max.json)
- [Client-Messungen vom 14. September 2026](../../docs/benchmarks/2026-09-14-m3-max-clients.json)
- [Go/RESP-Messungen vom 15. September 2026](../../docs/benchmarks/2026-09-15-m3-max-resp.json)
