---
layout: doc
lang: de
translation_key: postgresql-redis-cache
title: Cache-aside mit PostgreSQL und Redis
seo_title: "Cache-aside mit PostgreSQL und Redis: Invalidation und Race Conditions"
description: Verwenden Sie PostgreSQL als Quelle der Wahrheit mit einem Redis-Cache-aside-Pfad, verstehen Sie Race Conditions bei veralteten Lesevorgängen und sehen Sie, wo pg_local_cache passt.
section: Leitfäden
permalink: /de/docs/postgresql-redis-cache.html
last_modified_at: "2026-09-16"
---

# Cache-aside mit PostgreSQL und Redis {#postgresql-and-redis-cache-aside}

Redis-Cache-aside setzt die Anwendung zwischen einen Lesevorgang und seinen
maßgeblichen Speicher. Bei einem Fehltreffer liest sie PostgreSQL, gibt diesen
Wert zurück und schreibt ihn nach Redis; bei einem Schreibvorgang aktualisiert
sie PostgreSQL und invalidiert den zugehörigen Redis-Schlüssel. Der
[Redis-Pattern-Leitfaden](https://redis.io/docs/latest/develop/use-cases/cache-aside/)
beschreibt diesen Ablauf, macht einen Anwendungscache aber nicht transaktional
konsistent mit PostgreSQL.

Für eine Zeile mit dem Schlüssel `public.items.id` sieht der Ablauf so aus:

```text
GET item:42
miss -> SELECT * FROM public.items WHERE id = $1
     -> SET item:42 <serialized row> EX <ttl>
write -> UPDATE public.items ...
      -> COMMIT
      -> DEL item:42
```

Verwenden Sie parametrisiertes SQL und einen Schlüssel-Namensraum. Eine TTL
begrenzt, wie lange ein gespeicherter Wert in Redis bleibt; sie beweist nicht
seine Aktualität relativ zu einem PostgreSQL-Commit. Explizites Löschen behandelt
gewöhnliche Schreibvorgänge, entfernt aber nicht jede Race Condition.

## Die Invalidation-Race-Condition {#the-invalidation-race}

Betrachten Sie zwei Anfragen. Leser R1 findet den Schlüssel nicht in Redis und
liest die alte Zeile aus PostgreSQL. Schreiber W führt einen Commit der neuen
Zeile aus und löscht `item:42`. Danach setzt R1 die alte Zeile in Redis. Der
nächste Leser sieht veraltete Daten, bis der Schlüssel abläuft oder ein weiterer
Schreibvorgang ihn löscht.

Mögliche Gegenmaßnahmen sind erneutes Löschen nach Abschluss eines Ladevorgangs,
das Speichern einer Datenbankversion und Zurückweisen älterer Werte, das
Serialisieren von Ladevorgängen pro Schlüssel oder das Veröffentlichen
bestätigter Änderungen über einen Outbox- oder CDC-Consumer. Jede Maßnahme fügt
Koordination und Fehlerfälle hinzu. Der [Leitfaden zur Cache- Invalidation](cache-invalidation.md) zeigt das entsprechende Problem der späten
Befüllung innerhalb von PostgreSQL.

## Wo pg_local_cache passt {#where-pg_local_cache-fits}

`pg_local_cache` ist eine engere PostgreSQL-lokale Option für vollständige Zeilen
per Primärschlüssel. `local_cache.mget` ist explizit; eine gewöhnliche `SELECT`
und eine beliebige Abfrageform lesen den Cache nie. Trigger angehängter Tabellen
setzen auf dem Datenbank-Schreibpfad Sperren für betroffene Schlüssel oder
Relationen, und geeignete Lesevorgänge können auf PostgreSQL zurückfallen, wenn
Transaktions- oder Snapshot-Regeln einen Cache-Treffer verbieten. Beginnen Sie
mit dem [Leitfaden zu Batch-Abfragen](batch-primary-key-lookups.md) und dem
[technischen Vertrag](TECHNICAL.md).

Diese Erweiterung bietet keine allgemeine Redis-Kompatibilität, keine Redis-
TTLs und kein verteiltes Anwendungs-Cache-Protokoll. Ihr optionaler RESP2-
Endpunkt stellt einen begrenzten authentifizierten Befehlssatz über dieselben
Zuordnungen bereit und hat ein eigenes Sicherheitsmodell; TLS gibt es nicht.
Verwenden Sie ihn, wenn transaktionsbewusste vollständige Zeilen-Lesevorgänge
innerhalb von PostgreSQL das Problem sind. Verwenden Sie Redis, wenn mehrere
Anwendungsinstanzen gemeinsame Objekte, TTL-basierte Aktualität oder Redis-
Datenstrukturen benötigen. Die Kombination beider Systeme erfordert getrennte
Schlüssel, Invalidation und Metriken für jede Schicht.

Starten Sie den [Quickstart](QUICKSTART.md), vergleichen Sie ihn mit der
gewöhnlichen Client-Abfrage im [node-postgres-Beispiel](node-postgres.md) und
prüfen Sie die getrennten SQL- und RESP-Zähler. Der [Leitfaden zur Caching-Entscheidung](postgresql-caching.md) listet die anderen PostgreSQL-
Optionen auf.
