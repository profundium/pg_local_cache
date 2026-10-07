---
layout: doc
lang: de
translation_key: row-cache-vs-shared-buffers
title: PostgreSQL-Zeilen-Cache im Vergleich zu shared_buffers
seo_title: "PostgreSQL-Zeilen-Cache im Vergleich zu shared_buffers | pg_local_cache"
description: "Vergleichen Sie PostgreSQL-Seiten-Caching mit dem Caching vollständiger Zeilen durch pg_local_cache: vermiedene Quellarbeit, Cache-Kosten und Arbeitslasten, die gewöhnliches SQL verwenden sollten."
section: Lesewege
permalink: /de/docs/row-cache-vs-shared-buffers.html
last_modified_at: "2026-10-04"
---

# PostgreSQL-Zeilen-Cache im Vergleich zu shared_buffers {#postgresql-row-cache-vs-shared_buffers}

Dieser Leitfaden vergleicht PostgreSQL-Seiten-Caching mit dem Caching vollständiger Zeilen durch `pg_local_cache` und zeigt, welche Arbeit auf den einzelnen Lesewegen verbleibt.

[`shared_buffers`](https://www.postgresql.org/docs/18/runtime-config-resource.html#GUC-SHARED-BUFFERS) hält Datenbankseiten im Speicher. Eine aufgewärmte Seite kann Speicherzugriffe vermeiden, doch PostgreSQL prüft weiterhin die Tupel-Sichtbarkeit, führt die Abfrage aus und erstellt das Ergebnis. Ein geeigneter RESP-`MGET`-Treffer kann nach Prüfung von Schlüssel, Fence-Generation und Payload eine gespeicherte vollständige Zeilen-Payload zurückgeben.

Siehe die [technische Referenz zum Leseweg](TECHNICAL.md#read-path-and-safe-fallback).

## Cached PostgreSQL SELECT-Ergebnisse? {#does-postgresql-cache-select-results}

Nein. PostgreSQL-Seiten-Caches speichern Seiten, keine endgültigen Abfrageergebnisse. Ein [vorbereitetes Statement](https://www.postgresql.org/docs/18/sql-prepare.html) kann Parsing und Planung wiederverwenden; PostgreSQL führt es trotzdem aus. `pg_local_cache` stellt das Caching vollständiger Zeilen über RESP-`MGET` bereit, nicht das Caching beliebiger `SELECT`-Ergebnisse. Siehe [RESP-Clients](resp.md).

## Vergleichen Sie die Arbeit, nicht nur das Speichermedium {#compare-the-work-not-just-the-storage-medium}

| Leseweg | Verbleibende Arbeit |
|---|---|
| Vorbereitetes Primärschlüssel-SQL über aufgewärmten Seiten | Protokollverarbeitung, Abfrageausführung, Sichtbarkeitsprüfungen und Ergebnisumwandlung |
| Geeigneter RESP-`MGET`-Treffer | Protokollverarbeitung, Schlüsselumwandlung, Cache-Synchronisierung, Eignungsprüfungen und Rückgabe der Payload |
| RESP-Fehltreffer oder Bypass | Cache-Prüfungen und Lesevorgang in der Quelltabelle; geeignete Zeilen können den Cache befüllen |

Ein Treffer vermeidet die wiederholte Ausführung der Quelltabellenabfrage und Serialisierung der vollständigen Zeile. Er verwendet weiterhin einen PostgreSQL-Worker und Cache-Synchronisierung. RESP-Anfragen laufen unabhängig von der SQL-Transaktion des aufrufenden Clients.

## Zu berücksichtigende Kosten {#costs-to-include}

Zeilen und Zuordnungszustand benötigen zusätzlichen gemeinsamen Speicher. Schreibvorgänge an angehängten Tabellen führen Invalidierungs-Trigger aus. Eine Arbeitsmenge, die größer ist als die Cache-Kapazität, kann die Zahl der Fehltreffer und Verdrängungen erhöhen.

Messen Sie auf beiden Lesewegen dieselbe Schlüsselmenge, Zeilenform, Verbindungszahl und Anfragemischung. Bewerten Sie Batch-SQL getrennt von Einzelzeilen-Lesevorgängen; Batching allein kann Roundtrips ohne Cache einsparen.

## Wann die Anwendung unverändert bleiben sollte {#when-to-leave-the-application-alone}

Behalten Sie gewöhnliches SQL bei, wenn die Ende-zu-Ende-Latenz akzeptabel ist, die Anwendung nur eine Projektion benötigt oder Joins, Bereiche und Aggregationen überwiegen. Verwenden Sie SQL für Zeilensperren und Lesevorgänge, die dieselbe Transaktion verwenden müssen.

## Zeilen-Cache oder externer Cache? {#row-cache-or-an-external-cache}

Verwenden Sie `pg_local_cache`, wenn PostgreSQL maßgeblich bleibt und vollständige Zeilen wiederholt anhand des Primärschlüssels gelesen werden. Verwenden Sie einen externen Cache für TTL-gesteuerten Anwendungszustand, Pub/Sub, verteilte Koordination oder gemeinsam genutzte Objekte über mehrere Services hinweg. Die [technische Referenz](TECHNICAL.md) beschreibt die Sicherheitsgrenze von RESP.
