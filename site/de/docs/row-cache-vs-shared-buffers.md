---
layout: doc
lang: de
translation_key: row-cache-vs-shared-buffers
title: PostgreSQL-Zeilen-Cache im Vergleich zu shared_buffers
seo_title: "PostgreSQL-Zeilen-Cache im Vergleich zu shared_buffers | pg_local_cache"
description: Vergleichen Sie das PostgreSQL-Seiten-Caching mit dem Caching vollständiger Zeilen durch pg_local_cache 3.0. Sehen Sie, welche Arbeit ein Treffer im Zeilen-Cache vermeidet, welche Kosten bleiben und wann kein weiterer Cache nötig ist.
section: Lesepfade
permalink: /de/docs/row-cache-vs-shared-buffers.html
last_modified_at: "2026-10-04"
---

# PostgreSQL-Zeilen-Cache im Vergleich zu shared_buffers {#postgresql-row-cache-vs-shared_buffers}

PostgreSQLs [`shared_buffers`](https://www.postgresql.org/docs/16/runtime-config-resource.html#GUC-SHARED-BUFFERS)
enthält Datenbankseiten. pg_local_cache speichert separat serialisierte
vollständige Zeilen-Payloads unter ihren vollständigen Primärschlüsseln. Eine
bereits im Speicher befindliche Seite kann einen Speicherzugriff vermeiden,
aber eine Abfrage muss weiterhin aus den Datenbank-Tupeln ein Ergebnis erzeugen.
Ein Treffer im Zeilen-Cache kann die gespeicherte Payload nach Eignungs- und
Snapshot-Prüfungen zurückgeben.

Auch das Betriebssystem kann Dateiinhalte cachen. Verwenden Sie eine aufgewärmte
Datenbank als Baseline.

{% include diagrams/read-path.html id="buffers-read" %}

## Cached PostgreSQL SELECT-Ergebnisse? {#does-postgresql-cache-select-results}

`shared_buffers` cached die von einer Abfrage verwendeten Seiten statt ihres endgültigen Ergebnissatzes. Ein vorbereitetes Statement verwendet Parse-Arbeit wieder und kann einen Plan wiederverwenden, aber PostgreSQL führt es weiterhin aus. pg_local_cache ergänzt das Caching vollständiger Zeilen über authentifiziertes RESP `MGET`; beliebige SELECT-Ergebnisse werden nicht gecached und bestehende Abfragen nicht umgeschrieben. Das Node.js-Beispiel zeigt getrennte RESP- und SQL-Pfade.

## Arbeit vergleichen, nicht nur das Speichermedium {#compare-the-work-not-just-the-storage-medium}

| Lesen | Verbleibende Arbeit |
|---|---|
| Vorbereitetes Primärschlüssel-SQL über aufgewärmte Seiten | Protokollverarbeitung, Planausführung, Prüfung der Zeilensichtbarkeit und Ergebnisumwandlung |
| Geeigneter RESP-MGET-Cache-Treffer | Protokollverarbeitung, Schlüsselumwandlung, Cache-Synchronisierung, Snapshot-Prüfungen und Rückgabe der gespeicherten Payload |
| RESP-MGET-Fehltreffer oder Bypass | Cache-Prüfungen plus Quelltabellenabfrage; ein erfolgreicher geeigneter Fill kann den Cache füllen |

Ein Treffer im Zeilen-Cache vermeidet die wiederholte Ausführung der Quelltabelle und die Serialisierung der vollständigen Zeile. Cache-Prüfungen und Synchronisierung verbrauchen ebenfalls CPU. Der RESP-Worker verwendet die konfigurierte Datenbankrolle; RESP teilt weder SQL-Transaktion noch Snapshot des aufrufenden Clients.

## Einzubeziehende Kosten {#costs-to-include}

Eine gecachte Zeile benötigt zusätzlichen Shared Memory, selbst wenn ihre
Quellseite bereits im Speicher liegt. Die Erweiterung verwaltet außerdem
Zuordnungs- und Invalidation-Zustand. Updates an angehängten Tabellen führen die
Trigger der Erweiterung aus. Wenn die Arbeitsmenge die Kapazität überschreitet,
kann eine scheinbare Leseoptimierung überwiegend aus Fehltreffern und
Verdrängungsaufwand bestehen.

Die Standarddemo vergleicht bewusst eine heiße Menge von 128 Zeilen mit 1.024
Cache-Slots und anschließend einen ersten Durchlauf über 4.096 Zeilen. Der
[Benchmark-Leitfaden](BENCHMARKS.md) erklärt beide Fälle und misst Schreibvorgänge
an angehängten Tabellen separat.

## Wann die Anwendung unverändert bleiben sollte {#when-to-leave-the-application-alone}

Behalten Sie die bestehende Abfrage bei, wenn ihre End-to-End-Latenz bereits
akzeptabel ist, wenn die Anwendung nur eine kleine Projektion einer großen Zeile
benötigt oder wenn Joins, Bereiche und Aggregation dominieren. Vergleichen Sie
zuerst eine gewöhnliche Batch-Abfrage mit den aktuellen Aufrufen pro Schlüssel in
der Anwendung. Ein Gewinn durch Batching ist kein Beleg für einen Gewinn durch
Caching.

pg_local_cache 3.0 erfordert explizite RESP `MGET`-Aufrufe, die Installation der Erweiterung und ein Preload beim Start. RLS-, partitionierte und vererbte Tabellen werden abgelehnt.

## Zeilen-Cache oder externer Cache? {#row-cache-or-an-external-cache}

Für Daten, die in PostgreSQL maßgeblich bleiben, hält dieses Design die
Invalidation im Datenbank-Schreibpfad und vermeidet ein eigenes
Cache-aside-Protokoll der Anwendung. Es bietet keine allgemeine Redis-Semantik.
Der RESP2-Endpunkt hat einen begrenzten Befehlssatz und ein eigenes
Sicherheitsmodell.

Ein PostgreSQL-Zeilen-Cache kann anwendungsseitigen Zustand mit TTL, Pub/Sub oder
verteilter Koordination nicht ersetzen. Siehe den [technischen Vertrag](TECHNICAL.md)
und die [Transaktionsbeispiele](cache-invalidation.md).
