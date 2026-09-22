---
layout: doc
lang: de
translation_key: batch-primary-key-lookups
title: Batch-Abfragen von PostgreSQL per Primärschlüssel
seo_title: "Batch-Abfragen von PostgreSQL-Primärschlüsseln mit ANY und mget"
description: Ersetzen Sie N+1-Abfragen per Primärschlüssel durch eine parametrisierte PostgreSQL-Abfrage, erhalten Sie bei Bedarf Eingabepositionen und vergleichen Sie den expliziten pg_local_cache-mget-Pfad.
section: Leitfäden
permalink: /de/docs/batch-primary-key-lookups.html
last_modified_at: "2026-09-16"
---

# Batch-Abfragen von PostgreSQL per Primärschlüssel {#batch-postgresql-primary-key-lookups}

Wenn Anwendungscode eine Abfrage pro ID sendet, können Netzwerk-Roundtrips und
Abfrage-Overhead einen kleinen Lesevorgang dominieren. Probieren Sie zuerst eine
parametrisierte Anweisung:

```sql
SELECT id, value, revision
FROM public.items
WHERE id = ANY($1::bigint[]);
```

Übergeben Sie die IDs als Array-Parameter. Halten Sie Tabelle und Spalten in der
Anweisung fest; bauen Sie kein SQL aus ID-Strings. PostgreSQL wertet `ANY` durch
Vergleich des linken Ausdrucks mit Array-Elementen aus, wie in der
[Dokumentation zu Zeilen- und Array-Vergleichen](https://www.postgresql.org/docs/18/functions-comparisons.html#FUNCTIONS-COMPARISONS-ANY-SOME)
beschrieben.

## Ergebnisvertrag kennen {#know-the-result-contract}

Die obige Abfrage gibt eine Menge zurück. Sie verspricht weder die
Eingabereihenfolge, noch trifft eine doppelte ID normalerweise mehr als einmal
auf dieselbe Tabellenzeile. Fehlende IDs erzeugen keine Zeile. Eine Eingabe
`NULL` trifft keinen nicht-nullbaren Primärschlüssel; ein Null-Array oder
Null-Elemente folgen außerdem PostgreSQLs dreiwertiger `ANY`-Logik. Ein leeres
Array gibt keine Zeilen zurück.

Wenn der Aufrufer für jede angeforderte Position ein Ergebnis benötigt, bewahren
Sie die Positionen explizit:

```sql
WITH requested AS (
  SELECT key, position
  FROM unnest($1::bigint[]) WITH ORDINALITY AS input(key, position)
)
SELECT requested.position,
       requested.key,
       CASE WHEN items.id IS NULL THEN NULL
            ELSE row_to_json(items)::text END AS row
FROM requested
LEFT JOIN public.items AS items ON items.id = requested.key
ORDER BY requested.position;
```

`WITH ORDINALITY` erhält Duplikate und `NULL`-Positionen; der Left Join gibt für
einen fehlenden Schlüssel eine Null-`row` zurück. Dies ist eine nützliche
Baseline für einen Client, der eine explizite Ausrichtung benötigt. Siehe das
[node-postgres-Beispiel](node-postgres.md) für die clientseitige Wiederherstellung
des gleichen Vertrags.

## Wann `mget` die richtige Alternative ist {#when-mget-is-the-right-alternative}

Für vollständige Zeilen per Primärschlüssel bietet `pg_local_cache` eine
explizite, begrenzte Batch-API:

```sql
SELECT local_cache.mget(
  'public.items'::regclass,
  $1::bigint[]
) AS rows;
```

Das zurückgegebene `text[]` bewahrt Eingabereihenfolge und Duplikate. Eingabe-
`NULL` und fehlende Zeilen erzeugen ausgerichtete `NULL`-Elemente. Aufrufe
akzeptieren höchstens 1.024 Schlüssel, und die Funktion kann den Cache gemäß
Transaktions-, Snapshot-, Zuordnungs- und Zeilengrößenregeln umgehen oder
verfehlen; sie fällt auf PostgreSQL zurück, statt den Ergebnisvertrag zu ändern.
Sie gibt vollständige serialisierte Zeilen zurück. Verwenden Sie daher `ANY`
oder die Ordinalitätsabfrage, wenn Sie eine Projektion, Joins, Filter jenseits
des Schlüssels oder einen unbegrenzten Batch benötigen.

## GraphQL, DataLoader und N+1-Lesevorgänge {#graphql-dataloader-and-n1-reads}

[DataLoader](https://github.com/graphql/dataloader#batching) kombiniert einzelne
Ladevorgänge zu einem Batch. Seine Batch-Funktion muss einen Wert pro
Eingabeschlüssel in derselben Reihenfolge zurückgeben; die obige Wiederherstellung
liefert diese Form auch für fehlende Zeilen.

DataLoaders [Memoization pro Anfrage](https://github.com/graphql/dataloader#caching-per-request)
ist vom gemeinsamen Zeilen-Cache von PostgreSQL getrennt. Erstellen Sie Loader
für jede Anfrage und löschen Sie betroffene Loader-Einträge nach Mutationen in
dieser Anfrage. Die PostgreSQL-Invalidation kann Werte nicht löschen, die bereits
in einem JavaScript-Loader gespeichert sind. Behalten Sie
Autorisierungsprüfungen der Anwendung bei; `pg_local_cache` unterstützt keine
RLS-Tabellen.

Führen Sie den [Quickstart](QUICKSTART.md) aus und vergleichen Sie anschließend
beide Lesepfade in den [Benchmarks](BENCHMARKS.md). Die [technische Referenz](TECHNICAL.md#sql-mget-api)
definiert die API; der [Transaktionsleitfaden](cache-invalidation.md) behandelt
Schreibvorgänge.
