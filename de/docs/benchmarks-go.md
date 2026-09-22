---
layout: doc
lang: de
translation_key: benchmarks-go
title: "Go-Benchmarks: SQL und RESP"
description: Lokale pgx- und RESP2-Ergebnisse auf einem Apple M3 Max mit PostgreSQL-CPU, Speicher und Skalierung der Verbindungszahl.
section: Benchmarks
permalink: /de/docs/benchmarks-go.html
last_modified_at: "2026-09-16"
---

# Go-Benchmarks: SQL und RESP {#go-benchmarks-sql-and-resp}

[Übersicht](BENCHMARKS.md) · [Node.js](benchmarks-node.md) · [Go und RESP](benchmarks-go.md)

Go 1.27.1 mit pgx 5.11.0 und einem RESP2-Client aus der Standardbibliothek;
`GOMAXPROCS=8`. [Rechner und Messmethode](BENCHMARKS.md#test-environment).


Gemessen am 15. September 2026 mit dem Erweiterungs-Build `f03ed22`.
Der Go-Client läuft innerhalb der Linux-VM in einem separaten Container, der
den Netzwerk-Namespace von PostgreSQL teilt. Client-CPU und -Speicher sind aus
den Server-Ressourcenzählern ausgeschlossen. RESP verwendet acht Worker, ein
Cache-/Buffer-Budget von 1 GiB und ein Limit von 512 Clients.

Median **Anfragen/s**, drei Stichproben von fünf Sekunden pro Fall:

| Schlüssel/Anfrage | Verbindungen | Vorbereitetes SQL | SQL mget | RESP MGET |
|---:|---:|---:|---:|---:|
| 1 | 64 | 277,088 | 211,251 | 722,133 |
| 1 | 256 | 253,790 | 186,296 | 839,678 |
| 64 | 64 | 26,459 | 38,122 | 38,877 |
| 64 | 256 | 27,615 | 43,647 | 52,774 |

SQL mit 64 Schlüsseln variierte über Neustarts des Servers trotz identischer
Daten, Einstellungen und Indexpläne zwischen 19–28k Anfragen/s. Die Tabelle
verwendet den schnelleren wiederholten Lauf; die Ursache der Variation bleibt
ungeklärt. [Alle 129 Stichproben](../../assets/benchmarks/2026-09-15-m3-max-resp.json)
enthalten beide Läufe, exakte Quellrevisionen, Binär-Hashes und Abfragepläne.

Schreibgeschützte Cache-Stichproben hatten 100 % Treffer und keine Fehler oder
Ablehnungen wegen Verbindungsgrenzen. Der Harness prüft SQL- und RESP-
Verbindungsreserven. Eine separate Probe mit 256 Verbindungen, 64 Schlüsseln
und 12 Go-Threads verbesserte RESP nicht; SQL `mget` gewann gegenüber acht
Threads 6 %.

### Serverressourcen {#server-resources}

Bei **256 Verbindungen**, Mediane aus denselben Stichproben:

| Schlüssel/Anfrage | Pfad | Client-CPU-Kerne | Server-CPU-Kerne (% der VM) | Server-µs/Anfrage | Abgetasteter Spitzenwert MiB |
|---:|---|---:|---:|---:|---:|
| 1 | SQL | 3.97 | 8.94 (63.9%) | 36.3 | 679.8 |
| 1 | SQL mget | 3.36 | 9.93 (70.9%) | 54.4 | 684.2 |
| 1 | RESP MGET | 5.98 | 6.11 (43.7%) | 7.8 | 263.7 |
| 64 | SQL | 3.72 | 9.45 (67.5%) | 348.3 | 689.8 |
| 64 | SQL mget | 5.12 | 4.99 (35.6%) | 116.0 | 707.7 |
| 64 | RESP MGET | 5.76 | 4.97 (35.5%) | 95.8 | 266.7 |

### Go-Client unter macOS {#go-client-on-macos}

Über Dockers veröffentlichte Ports, bei **64 Verbindungen**; Mediane aus drei
Stichproben von fünf Sekunden, in Anfragen/s:

| Schlüssel/Anfrage, 64 Verbindungen | Vorbereitetes SQL | SQL mget | RESP MGET |
|---:|---:|---:|---:|
| 1 | 49,194 | 48,010 | 52,426 |
| 64 | 14,324 | 15,751 | 16,240 |

Die VM- und Host-Fälle unterscheiden sich sowohl beim Client-Betriebssystem als
auch bei der Netzwerkroute. Die Host-Port-Ergebnisse können daher die
Durchsatzgrenze von PostgreSQL nicht isolieren. RESP hat außerdem einen anderen
Sitzungsvertrag: Worker verwenden eine konfigurierte Datenbankrolle und erben
weder SQL-Transaktion noch Snapshot des Aufrufers. Siehe die
[RESP-Referenz](TECHNICAL.md#optional-resp2-endpoint).

## Reproduzieren {#reproduce}

<details markdown="1">
<summary>Go-Benchmark ausführen</summary>

Vom Repository-Stammverzeichnis mit Docker, Node.js 20+ und Go 1.25+:

```bash
./examples/benchmark.sh go > go.json
```

Das Skript baut einen verworfenen PostgreSQL-Server und den Go-Client, führt
jeden Fall dreimal aus, zeichnet Serverressourcen auf und entfernt anschließend
seine Container. Der Client läuft in der Docker-VM in einer von PostgreSQL
getrennten cgroup. Aktuelle Standardwerte: 4/64/256 Verbindungen, 1/16/64
Schlüssel und fünf Sekunden pro Stichprobe. Verwenden Sie `all`, um Node.js und
Go gegen denselben Server mit der [gemeinsamen Matrix](BENCHMARKS.md#run-the-same-comparison-on-every-client)
auszuführen. Um den aufgezeichneten Lauf zu reproduzieren, verwenden Sie die
Revisionen aus dem Mess-JSON.

Optionale Überschreibungen: `CONNECTIONS`, `BATCHES`, `REPEATS`,
`DURATION_SECONDS` und `GOMAXPROCS`. Das JSON zeichnet die Build-ID der laufenden
Erweiterung auf.

</details>
