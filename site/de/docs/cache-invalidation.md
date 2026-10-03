---
layout: doc
lang: de
translation_key: cache-invalidation
title: Transaktionsbewusste Cache-Invalidation in PostgreSQL
seo_title: "PostgreSQL-Cache-Invalidation: Commit und Rollback | pg_local_cache"
description: Testen Sie die Invalidation von pg_local_cache 2.0 mit gleichzeitigen PostgreSQL-Sitzungen. Prüfen Sie nicht bestätigte Updates, Read-your-writes, Rollback, bestätigte Lesevorgänge und Fallback-Regeln.
section: Cache-Invalidation
permalink: /de/docs/cache-invalidation.html
last_modified_at: "2026-09-16"
---

# Transaktionsbewusste Cache-Invalidation in PostgreSQL {#transaction-aware-cache-invalidation-in-postgresql}

Das Löschen eines Cache-Eintrags reicht nicht aus, wenn ein früherer Lesevorgang
ihn nach dem Löschen erneut füllen kann. Stellen Sie sich vor, ein Leser beginnt,
eine alte Zeile zu laden, ein Schreiber bestätigt einen neuen Wert und
invalidiert den Schlüssel, und anschließend veröffentlicht der frühere Loader
sein Ergebnis. Ein Cache muss auch diese späte Veröffentlichung ablehnen.

Die Implementierung 2.0 setzt auf dem Datenbank-Schreibpfad Sperren für
betroffene Schlüssel oder Relationen. Ein Fill trägt Generationsinformationen,
damit es nach einer Invalidation abgelehnt werden kann. Positive Cache-Einträge
tragen außerdem Tupel-Sichtbarkeitsinformationen. Ein ungeeigneter Eintrag fällt
auf einen Quelltabellen-Lesevorgang zurück. Siehe die [technische Referenz](TECHNICAL.md#transaction-consistency)
für den Vertrag.

{% include diagrams/transaction.html id="invalidation-transaction" %}

## Mit zwei Sitzungen testen {#test-with-two-sessions}

Starten Sie die [lokale Demo](QUICKSTART.md). Öffnen Sie diesen Befehl in zwei Terminals:

```bash
docker compose -f examples/compose.yaml exec postgres \
  psql -X -v ON_ERROR_STOP=1 -U demo -d pglc_demo
```

Lesen Sie in Sitzung A Zeile 42 und notieren Sie ihre Revision, dann lesen Sie sie erneut, um sie aufzuwärmen:

```sql
SELECT (local_cache.mget('public.items'::regclass, ARRAY[42]::bigint[]))[1]::jsonb ->> 'revision';
SELECT (local_cache.mget('public.items'::regclass, ARRAY[42]::bigint[]))[1]::jsonb ->> 'revision';
```

Aktualisieren Sie in Sitzung B die Zeile, lassen Sie die Transaktion aber offen:

```sql
BEGIN;
UPDATE public.items SET revision = revision + 1 WHERE id = 42;
SELECT (local_cache.mget('public.items'::regclass, ARRAY[42]::bigint[]))[1]::jsonb ->> 'revision';
```

B sieht seine eigene Erhöhung. Dieser Lesevorgang umgeht den Cache. Wiederholen
Sie As Abfrage, während B offen bleibt: A muss weiterhin die bestätigte Revision
sehen, nicht Bs unbestätigten Wert. Führen Sie in B `ROLLBACK` aus; eine weitere
Abfrage in A muss weiterhin die ursprüngliche Revision zurückgeben.

Führen Sie nun in B Folgendes aus:

```sql
BEGIN;
UPDATE public.items SET revision = revision + 1 WHERE id = 42;
COMMIT;
```

Eine in A nach diesem Commit gestartete Abfrage muss die erhöhte Revision
zurückgeben. Dies ist die relevante Grenze: Eine ältere laufende Anweisung muss
nicht auf einen nach ihrem Start aufgenommenen Snapshot wechseln. PostgreSQL
dokumentiert dieses Verhalten unter [Read Committed](https://www.postgresql.org/docs/16/transaction-iso.html#XACT-READ-COMMITTED).

Der ausführbare [Node.js-Test](https://github.com/profundium/pg_local_cache/blob/master/examples/node-postgres/demo.mjs)
behauptet diese Beobachtungen mit getrennten Verbindungen.

## Fälle, die den Cache absichtlich umgehen {#cases-that-deliberately-bypass-the-cache}

`REPEATABLE READ`, `SERIALIZABLE`, Recovery, parallele Ausführung und
Transaktionen, die zugeordnete Daten geschrieben haben, verwenden den
Quelltabellenpfad. Eine übergroße Zeile kann erfolgreich zurückgegeben werden,
ohne gecached zu werden. Eine Trefferrate nahe null bedeutet nicht unbedingt,
dass die Installation fehlgeschlagen ist: Prüfen Sie Arbeitslast und
Bypass-Zähler.

Wenn die Anwendung `SELECT ... FOR UPDATE` benötigt, verwenden Sie den normalen
PostgreSQL-Vorgang; `mget` ersetzt keine Zeilensperre.

## Ursache eines Fehltreffers prüfen {#inspect-the-cause-of-a-miss}

Verwenden Sie `local_cache.stats()` und `local_cache.health()` als Administrator.
Vergleichen Sie Zähler-Snapshots vor und nach einem kontrollierten Test. Halten
Sie SQL-`mget`-Zähler von RESP-Zählern getrennt. Folgen Sie nach absichtlichem
DDL dem dokumentierten Verfahren für `reconcile_table` oder `reconcile_all`,
statt anzunehmen, dass eine zuvor angehängte Zuordnung die geänderte Tabelle
noch beschreibt.
