---
layout: doc
lang: de
translation_key: BENCHMARKS
title: Benchmarks des PostgreSQL-Caches
description: Gemessene Ergebnisse von pg_local_cache mit Node.js, Go und RESP auf einem Apple M3 Max. Enthält Rechner, PostgreSQL-CPU, Speicher und Methodik.
section: Benchmarks
permalink: /de/docs/BENCHMARKS.html
last_modified_at: "2026-09-16"
---

# Benchmarks des PostgreSQL-Caches {#postgresql-cache-benchmarks}

Auf einem Apple M3 Max mit PostgreSQL 16 aufgezeichnet. Jeder Vergleich verwendet
denselben Client, Datensatz und dekodierte Zeilenergebnisse für gecachte und
gewöhnliche SQL-Lesevorgänge.

Die veröffentlichten SQL-mget-Ergebnisse sind historische Daten aus 2.x; SQL mget wurde in 3.0.0 entfernt. RESP MGET ist die unterstützte Schnittstelle für gecachte Lesevorgänge.

## Wo der Cache half – und wo nicht {#where-the-cache-helpedand-where-it-did-not}

Node.js und Go dekodieren RESP-JSON-Zeilen; diese Client-Kosten sind in der Messzeit enthalten. Die historischen SQL-mget-Vergleiche bleiben als 2.x-Daten gekennzeichnet.

Die [Node.js-Messungen](benchmarks-node.md) verwenden einen macOS-Client und
einen Docker-Server; die [Go- und RESP-Messungen](benchmarks-go.md) führen beide
innerhalb der Docker-VM aus. Jede Seite verlinkt Rohwiederholungen, exakte
Versionen und Server-Ressourcenkosten. Diese getrennten Setups ordnen keine
Sprachen. Beispiele für Verbindungen finden Sie unter [Node.js](node-postgres.md),
[Go](go.md) oder [RESP](resp.md).

## Mit jedem Client denselben Vergleich ausführen {#run-the-same-comparison-on-every-client}

Im Repository-Stammverzeichnis mit Docker, Node.js 20+ und Go 1.25+:

```bash
./examples/benchmark.sh all > comparison.json
python3 scripts/benchmark_report.py comparison.json
```

Der Runner baut einen verworfenen PostgreSQL-Server und führt diese gemeinsame
Matrix aus:

Node.js und Go dekodieren RESP-JSON-Zeilen; diese Client-Kosten sind in der Messzeit enthalten. Die historischen SQL-mget-Vergleiche bleiben als 2.x-Daten gekennzeichnet.

Standardmäßig gibt es 162 Stichproben, etwa 14 Minuten Zeitmessung plus Setup.
Das Skript entfernt seine Container nach Erfolg oder Fehler. Für einen kurzen
Korrektheitslauf:

```bash
CONNECTIONS=4 BATCHES=1,16,64 REPEATS=1 DURATION_SECONDS=1 \
  ./examples/benchmark.sh all > smoke.json
```

Node.js und Go dekodieren RESP-JSON-Zeilen; diese Client-Kosten sind in der Messzeit enthalten. Die historischen SQL-mget-Vergleiche bleiben als 2.x-Daten gekennzeichnet.

Identische Arbeitslasten machen die Protokolle nicht austauschbar: RESP-Worker
verwenden ihre konfigurierte Datenbankrolle und treten weder der SQL-Transaktion
noch dem Snapshot eines Aufrufers bei. Siehe den [RESP-Vertrag](TECHNICAL.md#optional-resp2-endpoint).
Der gemeinsame Vergleich misst aufgewärmte Lesevorgänge. Diagnosen für kalte,
gemischte Lese-/Schreibvorgänge und Schreib-Overhead bleiben in `node-workload`
separat.

Die unten veröffentlichten Ergebnisse vom 14.–15. September entstanden vor
diesem gemeinsamen Launcher. Ihre ursprünglichen Umgebungen und Quellrevisionen
bleiben an den Daten hängen; es sind keine neuen Messungen aus der einheitlichen
Matrix.

## Testumgebung {#test-environment}


| Komponente | Konfiguration |
|---|---|
| Host | MacBook Pro `Mac15,10`, Apple M3 Max: 10 Performance- + 4 Effizienzkerne, 36 GiB RAM |
| Betriebssystem | macOS 26.5.2, Build `25F84`, arm64 |
| Docker-VM | Engine 29.7.2, Linux `7.0.12-linuxkit`, 14 CPUs, 7.65 GiB RAM; kein Container-CPU- oder RAM-Limit |
| PostgreSQL | 16.15, Debian bookworm; 300 Verbindungen, 128 MiB Shared Buffers, 256 MiB `/dev/shm` |
| Daten | 4.096 Zeilen, 128-Byte-Werte; 1.024 Cache-Einträge; Daten und WAL auf tmpfs |

Client und Server teilen die CPUs des Macs mit zehn weiteren
Entwicklungscontainern. Alle Clients codieren Anfragen und dekodieren
vollständige JSON-Zeilen, wobei Eingabereihenfolge, Duplikate und fehlende
Positionen erhalten bleiben. SQL verwendet vorbereitete Statements; Verbindungen,
Authentifizierung und Aufwärmen liegen außerhalb der Zeitmessung. Es gibt weder
TLS noch Pipelining.

Die Server-CPU stammt aus den cgroup-Zählern des PostgreSQL-Containers. Ein Kern
bedeutet eine CPU-Sekunde pro verstrichener Sekunde; Kapazitätsprozente teilen
durch 14. CPU-Mikrosekunden pro Anfrage teilen die Server-CPU-Zeit durch die
abgeschlossenen Anfragen. Das Stichprobenfenster enthält Überwachungsarbeit und
die kurze Lücke für den Clientbericht.

Der Speicher ist `memory.current` aus der cgroup, alle 500 ms plus an den
Endpunkten abgetastet. Tabellen melden den Median des abgetasteten Spitzenwerts
jeder Wiederholung, einschließlich Shared Memory, tmpfs und Page Cache; dies ist
nicht der Prozess-RSS. JSON-Dateien enthalten außerdem Block-I/O,
Drosselung, Speicherereignisse und Schnappschüsse des SQL-Zustands/Wartens.
Netzwerkzähler schließen Loopback aus und lassen daher den Datenverkehr des
VM-Clients weg.

Diese kurzen Aufwärmläufe auf einem gemeinsam genutzten Laptop sind keine
Schätzungen der Produktionskapazität. Daten und WAL verwenden tmpfs mit
aktiviertem `fsync`, `full_page_writes` und `synchronous_commit`; die
Festplattenleistung ist ungetestet.

Ein fehlgeschlagener Lauf endet mit einem Fehlercode und bewahrt abgeschlossene
Stichproben; der Markdown-Renderer lehnt unvollständige Ergebnisse ab. Für exakt
aufgezeichneten Code verwenden Sie `harness_ref` und `extension_ref` jeder JSON-
Datei. Reproduktionsbefehle stehen auf den Client-Seiten; Ergebnisse werden in
JSON-Dateien wie `benchmark.json` geschrieben.

## Messmethode {#measurement-method}


Jede Verbindung wartet auf ihre Antwort, bevor sie eine weitere Anfrage sendet:
eine **Closed-Loop**-Arbeitslast ohne Korrektur für Coordinated Omission.
Die Abfragereihenfolge wechselt zwischen Wiederholungen; laufende Anfragen
werden vor dem Ende der Zeitmessung abgeschlossen. Schreibgeschützte Vergleiche
verwenden feste Schlüssel, die sich bereits im Cache befinden. Aufgezeichnete
SQL-Pläne verwenden `items_pkey` und haben null gelesene Shared Blocks.

Die Server-CPU stammt aus den cgroup-Zählern des PostgreSQL-Containers. Ein Kern
bedeutet eine CPU-Sekunde pro verstrichener Sekunde; Kapazitätsprozente teilen
durch 14. CPU-µs pro Anfrage teilen die Server-CPU-Zeit durch die abgeschlossenen
Anfragen. Das Stichprobenfenster enthält Überwachungsarbeit und die kurze Lücke
für den Clientbericht.

Der Speicher ist cgroup-`memory.current`, alle 500 ms plus an den Endpunkten
abgetastet. Tabellen melden den Median des abgetasteten Spitzenwerts jeder
Wiederholung, einschließlich Shared Memory, tmpfs und Page Cache; dies ist nicht
der Prozess-RSS. JSON-Dateien enthalten außerdem Block-I/O, Drosselung,
Speicherereignisse und Schnappschüsse des SQL-Zustands/Wartens. Netzwerkzähler
schließen Loopback aus und lassen daher den Datenverkehr des VM-Clients weg.

Diese kurzen Aufwärmläufe auf einem gemeinsam genutzten Laptop sind keine
Schätzungen der Produktionskapazität. Daten und WAL verwenden tmpfs mit
aktiviertem `fsync`, `full_page_writes` und `synchronous_commit`; die
Festplattenleistung ist ungetestet.

Ein fehlgeschlagener Lauf endet mit einem Fehlercode und bewahrt abgeschlossene
Stichproben; der Markdown-Renderer lehnt unvollständige Ergebnisse ab. Für exakt
aufgezeichneten Code verwenden Sie `harness_ref` und `extension_ref` jeder JSON-
Datei. Reproduktionsbefehle stehen auf den Client-Seiten; Ergebnisse werden in
JSON-Dateien wie `benchmark.json` geschrieben.
