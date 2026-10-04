---
layout: doc
lang: de
translation_key: cache-invalidation
title: Transaktionsbewusste Cache-Invalidation in PostgreSQL
seo_title: "PostgreSQL-Cache-Invalidation: Commit und Rollback | pg_local_cache"
description: "Trigger-Invalidierung für RESP-Zeilen-Lesevorgänge, bestätigte Updates, Quell-Lesevorgänge und die getrennte SQL-Transaktionsgrenze verstehen."
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

## Invalidierung über SQL und RESP prüfen {#test-with-two-sessions}

Starten Sie die [lokale Demo](QUICKSTART.md). Lesen Sie Zeile 42 über RESP und notieren Sie ihre Revision. Aktualisieren Sie die Zeile anschließend in PostgreSQL und bestätigen Sie die Transaktion:

```sql
BEGIN;
UPDATE public.items SET revision = revision + 1 WHERE id = 42;
COMMIT;
```

Der Trigger der angehängten Tabelle invalidiert den betroffenen Cache-Eintrag beim Commit. Der nächste RESP-Lesevorgang liefert die bestätigte Revision. Für einen Rollback beginnen Sie eine weitere Aktualisierung und rollen sie zurück; RESP liefert weiterhin die letzte bestätigte Revision.

RESP-Worker verwenden die konfigurierte PostgreSQL-Rolle und teilen weder SQL-Transaktion noch Snapshot der Anwendung. Eine Read-your-writes-Prüfung muss SQL in derselben Anwendungstransaktion verwenden; dieser Weg liest wie üblich aus der Quelltabelle. Der RESP-Endpunkt ist für separate Lesevorgänge mit der Worker-Rolle gedacht.

Der ausführbare [Node.js-Test](https://github.com/profundium/pg_local_cache/blob/master/examples/node-postgres/demo.mjs) prüft RESP-Lesevorgänge rund um PostgreSQL-Schreibvorgänge.

## Fälle, die den Cache absichtlich umgehen {#cases-that-deliberately-bypass-the-cache}

`REPEATABLE READ`, `SERIALIZABLE`, Recovery, parallele Ausführung und
Transaktionen, die zugeordnete Daten geschrieben haben, verwenden den
Quelltabellenpfad. Eine übergroße Zeile kann erfolgreich zurückgegeben werden,
ohne gecached zu werden. Eine Trefferrate nahe null bedeutet nicht unbedingt,
dass die Installation fehlgeschlagen ist: Prüfen Sie Arbeitslast und
Bypass-Zähler.

Wenn die Anwendung `SELECT ... FOR UPDATE` benötigt, verwenden Sie den normalen PostgreSQL-Aufruf. RESP `MGET` bietet weder Zeilensperren noch SQL-Sitzungssemantik.

## Ursache eines Fehltreffers prüfen {#inspect-the-cause-of-a-miss}

Verwenden Sie `local_cache.stats()` und `local_cache.health()` als Administrator.
Vergleichen Sie Zähler-Snapshots vor und nach einem kontrollierten Test. Halten
Sie SQL-`mget`-Zähler von RESP-Zählern getrennt. Folgen Sie nach absichtlichem
DDL dem dokumentierten Verfahren für `reconcile_table` oder `reconcile_all`,
statt anzunehmen, dass eine zuvor angehängte Zuordnung die geänderte Tabelle
noch beschreibt.
