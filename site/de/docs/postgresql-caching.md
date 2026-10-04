---
layout: doc
lang: de
translation_key: postgresql-caching
title: Leitfaden zur PostgreSQL-Caching-Entscheidung
seo_title: "Leitfaden zum PostgreSQL-Caching: Seiten, Zeilen, Sichten oder Redis"
description: Vergleichen Sie PostgreSQL-Seiten-Caching, vorbereitetes SQL, vollständiges Zeilen-Caching, materialisierte Sichten und Redis danach, welche Arbeit der jeweilige Leseweg vermeidet.
section: Leitfäden
permalink: /de/docs/postgresql-caching.html
last_modified_at: "2026-10-04"
---

# Leitfaden zur PostgreSQL-Caching-Entscheidung {#postgresql-caching-decision-guide}

Dieser Leitfaden vergleicht PostgreSQL-Seiten-Caching, vorbereitetes SQL, Caching vollständiger Zeilen, materialisierte Sichten und Redis danach, welche Arbeit die jeweilige Option vermeidet.

## Beginnen Sie mit der wiederholten Arbeit {#start-with-the-work-you-repeat}

| Bedarf | Option | Vermiedene Arbeit |
|---|---|---|
| Tabellen- und Indexseiten im Speicher halten | PostgreSQL `shared_buffers` und OS-Cache | Lesezugriffe auf den Speicher; SQL wird weiterhin ausgeführt |
| Ein Statement in einer Sitzung wiederholen | Vorbereitetes Statement | Wiederholtes Parsen und Analysieren |
| Vollständige Zeilen anhand des Primärschlüssels lesen | Authentifiziertes RESP-`MGET` mit `pg_local_cache` | Geeignete Lesevorgänge vollständiger Zeilen aus der Quelle |
| Joins oder Aggregate wiederverwenden | Materialisierte Sicht | Erneute Berechnung des gespeicherten Abfrageergebnisses bis zur Aktualisierung |
| Objekte über Services hinweg teilen | Redis-Cache-aside | Von der Anwendung verwaltete Lesevorgänge aus der Quelle |

### Seiten und vorbereitetes SQL {#pages-and-prepared-sql}

[`shared_buffers`](https://www.postgresql.org/docs/18/runtime-config-resource.html#GUC-SHARED-BUFFERS) cached Datenbankseiten, keine fertigen `SELECT`-Ergebnisse. PostgreSQL prüft weiterhin die Sichtbarkeit, führt die Abfrage aus und erstellt jedes Ergebnis. Ein [vorbereitetes Statement](https://www.postgresql.org/docs/18/sql-prepare.html) reduziert wiederholtes Parsen; es wird weiterhin mit dem aktuellen Datenbankzustand ausgeführt.

Der Vergleich [Zeilen-Cache oder shared_buffers](row-cache-vs-shared-buffers.md) zeigt, welche Arbeit auf den beiden Lesewegen verbleibt.

### Vollständige Zeilen anhand des Primärschlüssels {#whole-rows-by-primary-key}

`pg_local_cache` speichert vollständige Zeilen im begrenzten Shared Memory von PostgreSQL. Ein authentifiziertes RESP-`MGET` kann eine geeignete gecachte Zeile zurückgeben; gewöhnliches SQL liest diesen Cache nie. Fehlt ein Eintrag oder ist der Lesevorgang nicht geeignet, wird die Quelltabelle verwendet. Der Endpunkt verwendet eine konfigurierte Datenbankrolle und teilt weder SQL-Transaktion noch Snapshot des Aufrufers. Siehe den [Batch-Leitfaden](batch-primary-key-lookups.md), die [technische Referenz](TECHNICAL.md) und den [Leitfaden zur Invalidierung](cache-invalidation.md).

### Sichten und externe Caches {#views-and-external-caches}

Eine PostgreSQL-[materialisierte Sicht](https://www.postgresql.org/docs/18/rules-materializedviews.html) speichert ein Abfrageergebnis und wird bei Bedarf aktualisiert. Sie eignet sich für Berichte und Aggregate, bei denen der Aktualisierungszeitpunkt die Frische bestimmt.

Redis eignet sich für Anwendungsobjekte, die über mehrere Prozesse geteilt werden. Die Anwendung verwaltet Schlüssel, Serialisierung, TTLs und Invalidierung. Siehe [PostgreSQL und Redis mit Cache-aside](postgresql-redis-cache.md). Mit dem [Schnellstart](QUICKSTART.md) können Sie den Zeilen-Cache ausprobieren.
