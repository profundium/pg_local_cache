---
layout: doc
lang: de
translation_key: postgresql-caching
title: Leitfaden zur PostgreSQL-Caching-Entscheidung
seo_title: "Leitfaden zum PostgreSQL-Caching: Seiten, Zeilen, Sichten oder Redis"
description: Wählen Sie PostgreSQL-Seiten-Caching, vorbereitetes SQL, Caching vollständiger Zeilen, materialisierte Sichten oder einen externen Cache nach der Arbeit aus, die vermieden werden soll.
section: Leitfäden
permalink: /de/docs/postgresql-caching.html
last_modified_at: "2026-09-16"
---

# Leitfaden zur PostgreSQL-Caching-Entscheidung {#postgresql-caching-decision-guide}

„Einen Cache hinzufügen“ beschreibt mehrere unterschiedliche Änderungen. Das
Cachen von PostgreSQL-Seiten, vorbereitetes SQL, ein Zeilen-Cache, eine
materialisierte Sicht und Redis vermeiden jeweils andere Teile eines
Lesevorgangs. Wählen Sie anhand der wiederholten Arbeit in Ihrer Anfrage und
messen Sie dann den vollständigen Pfad mit dem [Benchmark-Leitfaden](BENCHMARKS.md).

## Beginnen Sie mit der wiederholten Arbeit {#start-with-the-work-you-repeat}

| Bedarf | Erste Option | Was sich ändert |
|---|---|---|
| Tabellen- und Indexseiten im Speicher halten | PostgreSQL `shared_buffers` und der OS-Cache | Weniger Speicherzugriffe; SQL läuft weiterhin |
| Dieselbe Anweisung oft senden | Ein vorbereitetes Statement | Weniger wiederholte Parse- und Planarbeit; Ausführung läuft weiterhin |
| Vollständige Zeilen per Primärschlüssel zurückgeben | PostgreSQL-`mget` von `pg_local_cache` | Wiederverwendung geeigneter Zeilen-Payloads über eine explizite API |
| Einen Join oder ein Aggregat vorab berechnen | Eine materialisierte Sicht | Gespeicherte Ergebnisse lesen; Aktualisierung legt die Frische fest |
| Anwendungsobjekte über Services hinweg teilen | Ein externer Cache wie Redis | Von der Anwendung verwaltete Schlüssel, TTLs und Invalidation |

### Seiten und vorbereitetes SQL {#pages-and-prepared-sql}

[`shared_buffers`](https://www.postgresql.org/docs/18/runtime-config-resource.html#GUC-SHARED-BUFFERS)
hält Datenbankseiten, keine endgültigen `SELECT`-Ergebnisse. Eine aufgewärmte
Seite kann Speicher-I/O vermeiden, aber PostgreSQL plant oder führt die Abfrage
weiterhin aus, prüft Sichtbarkeit und erstellt das Ergebnis. Ein [vorbereitetes Statement](https://www.postgresql.org/docs/18/sql-prepare.html) kann in einer
Sitzung wiederholte Parse- und Analysearbeit vermeiden. Es wird weiterhin gegen
den aktuellen Datenbankzustand ausgeführt, und sein Plan kann generisch oder
benutzerdefiniert sein.

Der [Vergleich der Zeilen-Caches](row-cache-vs-shared-buffers.md) zeigt, welche
Arbeit auf jedem Pfad verbleibt.

### Vollständige Zeilen per Primärschlüssel {#whole-rows-by-primary-key}

`pg_local_cache` speichert serialisierte vollständige Zeilen unter vollständigen
Primärschlüsseln im begrenzten Shared Memory von PostgreSQL. Der Zugriff erfolgt
über `local_cache.mget('public.items'::regclass, $1::bigint[])`; eine gewöhnliche
`SELECT`-Abfrage verwendet den Cache nie. Geeignete saubere `READ COMMITTED`-
Lesevorgänge können Treffer liefern, während strengere Isolationsstufen,
Schreibvorgänge in der Transaktion, Recovery, parallele Ausführung oder zu große
Zeilen PostgreSQL verwenden. Nicht unterstützte Tabellenzuordnungen werden beim
Anhängen abgelehnt. Dies ist ein bestimmter Lesepfad, kein Cache für beliebige
Abfrageergebnisse. Siehe den [Leitfaden zu Batch-Abfragen](batch-primary-key-lookups.md),
den [technischen Vertrag](TECHNICAL.md) und die [Transaktionsprüfungen](cache-invalidation.md).

### Sichten und externe Caches {#views-and-external-caches}

PostgreSQL-[materialisierte Sichten](https://www.postgresql.org/docs/18/rules-materializedviews.html)
speichern ein Abfrageergebnis in einer Relation und werden bei Bedarf
aktualisiert. Sie eignen sich für wiederholbare Berichte, Aggregate und Joins,
wenn ein Aktualisierungsplan als Frischegrenze akzeptabel ist. Sie sind kein
Ersatz für einen Zeilen-Cache pro Schlüssel.

Ein externer Cache wie Redis eignet sich für Anwendungsobjekte, die von mehreren
Prozessen oder Services gemeinsam genutzt werden. Die Anwendung besitzt
Schlüssel, Serialisierung, TTL und Invalidation. Siehe den [Redis-Cache-aside- Leitfaden](postgresql-redis-cache.md). Der [Quickstart](QUICKSTART.md) führt
`pg_local_cache` auf `public.items` aus.
