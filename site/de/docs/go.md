---
layout: doc
lang: de
translation_key: go
title: Batch-Zeilenabfragen mit Go und pgx
seo_title: "Batch-Abfragen von PostgreSQL-Zeilen mit Go und pgx"
description: Verwenden Sie pg_local_cache aus Go mit pgx, parametrisierten Schlüsseln und dekodierten JSON-Zeilen.
section: Go
permalink: /de/docs/go.html
last_modified_at: "2026-09-16"
---

# Batch-Zeilenabfragen mit Go und pgx {#batch-row-lookups-with-go-and-pgx}

Starten Sie zuerst die [verworfene Datenbank](QUICKSTART.md), dann führen Sie Folgendes aus:

```bash
go -C examples/go-pgx run ./demo
```

Sie verwendet `127.0.0.1:55432`, die Datenbank `pglc_demo` und die Zugangsdaten
`demo` / `demo-only`. Setzen Sie `PGLC_DEMO_PORT`, wenn der Quickstart einen
anderen Port verwendet.

Die Demo sendet Schlüssel als Query-Parameter:

```sql
SELECT local_cache.mget('public.items'::regclass, $1::bigint[]);
```

`mget` gibt `text[]` zurück; jedes Element ungleich null ist eine JSON-Zeile.
Das Beispiel fordert `42, 7, 42, NULL, 999999` an und gibt die Zeilen in der
Eingabereihenfolge aus. Das doppelte `42` bleibt an beiden Positionen; die
Null-Eingabe und das fehlende `999999` erzeugen Null-Elemente.

## Zeilen vergleichen, nicht nur Roundtrips {#compare-rows-not-just-round-trips}

Eine gewöhnliche Abfrage mit `WHERE id = ANY($1::bigint[])` erhält die
angeforderten Positionen nicht. Stellen Sie Reihenfolge, Duplikate und fehlende
Zeilen wieder her, bevor Sie mit `mget` vergleichen; der [Leitfaden zu Batch-Abfragen](batch-primary-key-lookups.md) zeigt sowohl clientseitige als
auch SQL-Ansätze.

Der Benchmark verwendet persistente Verbindungen und vorbereitete Statements.
Die Vorbereitung von SQL cached keine Ergebniszeilen: siehe den
[PostgreSQL-Caching-Leitfaden](postgresql-caching.md). Der [gemeinsame Benchmark](BENCHMARKS.md#run-the-same-comparison-on-every-client) testet Go und
Node.js mit denselben Schlüsseln, Batch-Größen, Verbindungszahlen und derselben
Dauer über SQL und RESP. RESP teilt die SQL-Transaktion des Aufrufers nicht.

Siehe den [Quickstart](QUICKSTART.md) für die Einrichtung und die
[Transaktionsprüfungen](cache-invalidation.md), bevor Sie den Lesepfad anpassen.
