---
layout: post
lang: de
translation_key: blog-measure-postgresql-row-cache
title: "Wann ein PostgreSQL-Zeilen-Cache hilft: den gesamten Lesepfad messen"
description: Einen fairen Vergleich des PostgreSQL-Zeilen-Caches mit vorbereitetem SQL und RESP MGET entwerfen. Warme Lesevorgänge, Fehltreffer, Batch-Größen, Schreibvorgänge und Client-Kosten getrennt messen.
permalink: /de/blog/measure-postgresql-row-cache/
date: "2026-09-22"
last_modified_at: "2026-10-04"
topic: performance
---

# Wann ein PostgreSQL-Zeilen-Cache hilft {#when-a-postgresql-row-cache-helps}

Eine Datenbank kann jede Seite aus dem Speicher liefern und trotzdem Zeit mit
Abfragen, Sichtbarkeitsprüfungen und der Erstellung von Ergebnissen verbringen.
Ein Zeilen-Cache versucht, einen Teil dieser wiederholten Arbeit zu vermeiden.
Er fügt aber auch Schlüsselverarbeitung, Cache-Prüfungen und
Serialisierungskosten hinzu. Die nützliche Frage ist, ob die vollständige
Anwendungsanfrage für Ihre Arbeitslast günstiger wird.

> **Versionshinweis vom 04.10.2026:** Die SQL-API `local_cache.mget(regclass, anyarray)` aus 2.x wurde in 3.0.0 entfernt; verwenden Sie RESP `MGET` für Zeilen-Lesevorgänge.

`pg_local_cache` stellt für Lesezugriffe auf vollständige Zeilen den expliziten RESP-Endpunkt `MGET` bereit. Gewöhnliche `SELECT`-Abfragen behalten ihren normalen PostgreSQL-Ausführungspfad. Ein warmer `shared_buffers`-Cache und ein warmer Zeilen-Cache sind daher unterschiedliche Versuchsbedingungen.

## Ergebnisvertrag zuerst festhalten {#result-contract}

Vergleichen Sie dieselben Schlüssel, Spalten und dieselbe Ausgabeform. Wenn die
Anwendung nur zwei Spalten benötigt, misst der Vergleich dieser SQL-Projektion
mit vollständigen serialisierten Zeilen unterschiedliche Arbeit. Wenn Aufrufer
Duplikate, Eingabereihenfolge und ein Null-Ergebnis für jeden fehlenden Schlüssel
erwarten, beziehen Sie diese Ausrichtung in jeden Client ein.

Der erhaltene [Node.js-Quellcode](https://github.com/profundium/pg_local_cache/blob/master/examples/node-postgres/queries.mjs)
enthält die vorbereitete `ANY`-Baseline und eine Hilfsfunktion, die die
Eingabepositionen wiederherstellt. Das [geordnete `WITH ORDINALITY`-Beispiel](ordered-batch-reads.md#explicit-positions)
zeigt die Positionsbehandlung in SQL. Beides benötigt die Erweiterung nicht.

## Pro Experiment nur eine Arbeitslastdimension ändern {#workload-dimensions}

| Experiment | Konstant halten | Was es zeigt |
|---|---|---|
| Wiederholte warme Lesevorgänge | Schlüssel, Ergebnisform, Verbindungen | Wiederverwendung bereits gefüllter Einträge |
| Kalte oder fehlende Schlüssel | Anfrageverteilung und Batch-Größe | Kosten von Quelltabellen und negativen Ergebnissen |
| Größere Batches | Insgesamt angeforderte Schlüssel und Payload-Form | Einsparung bei Roundtrips gegenüber Arbeit pro Schlüssel |
| Gleichzeitige Schreibvorgänge | Lese-/Schreibmix und Transaktionsgrenzen | Kosten von Invalidation, Refill und Sichtbarkeit |
| Breitere Zeilen | Schlüsselverteilung und Client-Platzierung | Kosten von Serialisierung, Transport und Bypass nach Zeilengröße |

Ein ungeeigneter Lesevorgang darf die Quelltabelle verwenden. Prüfen Sie
Zählerdeltas rund um jedes Experiment; eine niedrige Trefferrate allein
diagnostiziert keine fehlerhafte Installation. Halten Sie SQL- und RESP-Zähler
getrennt. Die [technische Referenz](../docs/TECHNICAL.md#health-and-monitoring)
beschreibt `local_cache.stats()` und `local_cache.health()`.

## Gemeinsamen Runner verwenden und anschließend die Belege prüfen {#shared-runner}

Führen Sie nach dem [Quickstart](../docs/QUICKSTART.md) den Vergleich des
Repositorys aus:

```bash
./examples/benchmark.sh all > comparison.json
python3 scripts/benchmark_report.py comparison.json
```

Der [Benchmark-Leitfaden](../docs/BENCHMARKS.md) listet Voraussetzungen,
Arbeitslaststeuerung und Metriken auf. Bewahren Sie das Roh-JSON auf. Notieren
Sie Erweiterungs- und Harness-Revisionen, PostgreSQL-Version, Rechner,
Verbindungszahl und Client-Platzierung. Vergleichen Sie wiederholte Läufe,
Latenzverteilungen und Serverressourcen zusammen mit dem Durchsatz. Ein kurzer
Korrektheits-Smoke-Lauf ist kein veröffentlichbares Geschwindigkeitsergebnis.

## An der Anwendungsgrenze entscheiden {#application-boundary}

Vorbereitetes SQL und RESP `MGET` verwenden unterschiedliche Lesewege und Ergebnisverarbeitung. Ein Gewinn für das eine belegt keinen Gewinn für das andere. Messen Sie beide mit dem tatsächlichen Anfragemuster Ihrer Anwendung; ein einzelnes Ergebnis sagt die Leistung für andere Arbeitslasten nicht voraus.

Behalten Sie gewöhnliches SQL bei, wenn Joins, Projektionen, Sperren oder nicht unterstützte Tabellenformen erforderlich sind oder der Cache keinen messbaren Nutzen bringt. Testen Sie für wiederholte Lesezugriffe auf vollständige Zeilen per Primärschlüssel RESP `MGET` mit derselben Clientarbeit, die Ihre Anwendung tatsächlich ausführt. Lesen Sie weiter im [Leitfaden zur Caching-Entscheidung](../docs/postgresql-caching.md) und beim [Invalidation-Experiment](../docs/cache-invalidation.md).
