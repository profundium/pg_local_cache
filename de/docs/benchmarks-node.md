---
layout: doc
lang: de
translation_key: benchmarks-node
title: Node.js-Benchmarks
description: "Lokale node-postgres-Ergebnisse auf einem Apple M3 Max: Batch-Lesevorgänge, gleichzeitige Updates, PostgreSQL-CPU und Speicher."
section: Benchmarks
permalink: /de/docs/benchmarks-node.html
last_modified_at: "2026-09-16"
---

# Node.js-Benchmarks {#nodejs-benchmarks}

[Übersicht](BENCHMARKS.md) · [Node.js](benchmarks-node.md) · [Go und RESP](benchmarks-go.md)

Node.js 24.18.0 mit node-postgres 8.16.3.
[Rechner und Messmethode](BENCHMARKS.md#test-environment).


Gemessen am 14. September 2026 mit dem Erweiterungs-Build `67e5754`, einem
Cache-Budget von 384 MiB und Clients auf macOS über den veröffentlichten
Docker-SQL-Port.

Bei **64 Schlüsseln pro Anfrage**, Median der Anfragen/s aus drei Stichproben von
10 Sekunden:

| Verbindungen | Vorbereitetes SQL | SQL mget |
|---:|---:|---:|
| 4 | 7,023 | 7,528 |
| 64 | 15,577 | 16,616 |
| 256 | 15,459 | 16,574 |

Bei 64 Verbindungen verwendete `mget` **170 µs Server-CPU pro Anfrage** gegenüber
286 µs für SQL. Die Server-CPU lag im Mittel bei 2.80 gegenüber 4.43 Kernen;
der abgetastete Spitzenwert des Speichers bei 215.0 gegenüber 205.5 MiB. Die
Client-CPU betrug 0.84 gegenüber 0.87 Kernen.

Für **einen Schlüssel** bei 64 Verbindungen war SQL schneller: 55,409 gegenüber
53,646 Anfragen/s. Das Batch-Ergebnis gilt nicht für Lesevorgänge mit einem
Schlüssel.

[Rohmessungen](../../assets/benchmarks/2026-09-14-m3-max-clients.json) enthalten
Latenz-Perzentile, Ressourcenstichproben und Quellrevisionen.

### Lesevorgänge gemischt mit Schreibvorgängen {#reads-mixed-with-writes}

Der Node.js-Anwendungsrunner verwendet **64 Verbindungen**, **50.000 Anfragen pro
Stichprobe** und drei Wiederholungen. Lesevorgänge durchlaufen 128 heiße Zeilen;
5 % der Operationen in der gemischten Arbeitslast aktualisieren Zeilen.

| Arbeitslast | Schlüssel/Anfrage | Vorbereitete SQL-Anfragen/s | mget-JSON-Anfragen/s |
|---|---:|---:|---:|
| Warme Lesevorgänge | 1 | 56,104 (52,246–56,364) | 52,842 (52,494–53,399) |
| Warme Lesevorgänge | 16 | 37,391 (36,996–37,938) | 38,425 (38,340–38,617) |
| Warme Lesevorgänge | 64 | 15,178 (15,109–15,488) | 16,294 (16,131–16,368) |
| 5-%-Updates | 1 | 55,768 (54,273–56,527) | 52,621 (51,432–53,216) |
| 5-%-Updates | 16 | 38,669 (38,429–38,718) | 37,604 (37,481–37,916) |
| 5-%-Updates | 64 | 16,049 (16,008–16,063) | 16,814 (16,771–17,229) |

Werte sind der Median von Anfragen/s mit Minimum–Maximum in Klammern. Gemischte
Stichproben zählen Lese- und Schreibvorgänge zusammen. Das `application_run` des
JSON enthält Fälle mit kaltem Fill und Schreib-Overhead. Kaltes Füllen bei Batch
64 hat nur 64 Latenzbeobachtungen, zu wenige für eine aussagekräftige p99-
Schätzung.

## Abfrageeinrichtung {#query-setup}

```sql
SELECT array_to_json(local_cache.mget('public.items'::regclass, $1::bigint[])) AS rows;
```

Verbindungen und benannte vorbereitete Statements werden wiederverwendet.
JSON-Dekodierung und Wiederherstellung der Eingabepositionen sind in der
Anfragezeit enthalten. Siehe das [Node.js-Beispiel](node-postgres.md).

## Reproduzieren {#reproduce}

<details markdown="1">
<summary>Node.js-Benchmarks ausführen</summary>

Vom Repository-Stammverzeichnis mit Docker und Node.js 20+:

```bash
./examples/benchmark.sh node > node.json
```

Aktuelle Standardwerte: 4/64/256 Verbindungen, 1/16/64 Schlüssel, drei
Fünf-Sekunden-Stichproben pro Fall. Node.js führt jetzt alle drei Pfade aus:
vorbereitetes SQL, SQL `mget` und RESP `MGET`, innerhalb der Docker-VM. Das
Skript erstellt einen verworfenen Server und einen separaten Client-Container,
zeichnet Ressourcen auf und entfernt anschließend beide.
Optionale Überschreibungen: `CONNECTIONS`, `BATCHES`, `REPEATS`,
`DURATION_SECONDS`. Verwenden Sie `all`, um Go in die [gleiche Matrix](BENCHMARKS.md#run-the-same-comparison-on-every-client)
aufzunehmen. Für das aufgezeichnete hostbasierte Setup verwenden Sie die
Revisionen aus dem Mess-JSON.

Für Lesevorgänge gemischt mit Schreibvorgängen:

```bash
BATCHES=1,16,64 ./examples/benchmark.sh node-workload > benchmark.json
python3 scripts/benchmark_report.py benchmark.json
```

Standardwerte: 64 Verbindungen, 50.000 Anfragen pro Stichprobe und drei
Wiederholungen. Der Runner setzt seine Demotabellen zwischen Stichproben zurück.
`CLIENTS`, `REQUESTS`, `BATCHES` und `REPEATS` können optional überschrieben werden.

</details>
