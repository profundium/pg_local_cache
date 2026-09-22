---
layout: post
lang: de
translation_key: blog-ordered-batch-reads
title: "PostgreSQL-Batch-Lesevorgänge ohne Verlust von Reihenfolge oder fehlenden Schlüsseln"
description: Ersetzen Sie N+1-Abfragen per Primärschlüssel und bewahren Sie dabei doppelte IDs, Eingabereihenfolge, NULL-Positionen und fehlende Zeilen. Vergleichen Sie ANY, WITH ORDINALITY und SQL mget.
permalink: /de/blog/ordered-batch-reads/
date: "2026-09-22"
last_modified_at: "2026-09-22"
topic: application
---

# Batch-Lesevorgänge brauchen einen Ergebnisvertrag {#batch-reads-need-a-result-contract}

Eine Schleife von Primärschlüssel-Abfragen durch eine einzelne `ANY`-Abfrage zu
ersetzen, entfernt Roundtrips. Dabei kann sich aber die Form der Antwort ändern.
Ein Aufrufer könnte `[42, 7, 42, NULL, -1]` anfordern und fünf
Ergebnispositionen erwarten. SQL-Mengen-Semantik verspricht diese Ausrichtung
nicht.

## Eine Zeilenmenge ist keine Liste von Antworten {#set-versus-list}

Mit `WHERE id = ANY($1::bigint[])` trifft eine doppelte ID normalerweise nur
einmal auf ihre Tabellenzeile. Eine fehlende ID liefert keine Zeile. Eine Eingabe
`NULL` trifft keinen nicht-nullbaren Primärschlüssel, und die Ausgabe hat keine
garantierte Eingabereihenfolge. `ORDER BY id` sortiert nach Schlüssel; die
angeforderten Positionen werden trotzdem nicht reproduziert.

Wenn Verbraucher eine Menge benötigen, ist das in Ordnung. Wenn sie ein
Ergebnis für jede Eingabe brauchen, machen Sie die Positionen zum Teil der
Abfrage oder stellen Sie sie in der Anwendung wieder her.

## Positionen in SQL explizit halten {#explicit-positions}

Starten Sie die [lokale Demo](../docs/QUICKSTART.md) und führen Sie dies in ihrer
`psql`-Sitzung aus:

```sql
WITH requested AS (
  SELECT key, position
  FROM unnest(ARRAY[42, 7, 42, NULL, -1]::bigint[])
       WITH ORDINALITY AS input(key, position)
)
SELECT requested.position,
       requested.key,
       CASE WHEN items.id IS NULL THEN NULL
            ELSE row_to_json(items)::text END AS row
FROM requested
LEFT JOIN public.items AS items ON items.id = requested.key
ORDER BY requested.position;
```

Die Ordinalitätsspalte unterscheidet beide Vorkommen von 42. Der Left Join
behält alle fünf Positionen einschließlich der Null-Eingabe und jedes fehlenden
Schlüssels. Für einen fehlenden Schlüssel ist `row` SQL `NULL`. Übergeben Sie im
Anwendungscode das Array als Parameter, statt IDs an SQL anzuhängen. Das
[Node.js-Beispiel](../docs/node-postgres.md) zeigt die clientseitige Ausrichtung.

## Die API für vollständige Zeilen vergleichen {#whole-row-api}

Bei einer angehängten Tabelle lautet der entsprechende explizite Cache-Aufruf:

```sql
SELECT local_cache.mget(
  'public.items'::regclass,
  ARRAY[42, 7, 42, NULL, -1]::bigint[]
) AS rows;
```

Er gibt `text[]` zurück, wobei Eingabereihenfolge und Duplikate erhalten bleiben.
Fehlende Schlüssel und Null-Eingaben erzeugen ausgerichtete SQL-`NULL`-Elemente;
jedes vorhandene Element ist eine serialisierte vollständige Zeile. Ein
Cache-Fehltreffer oder Bypass liest PostgreSQL. Die API akzeptiert höchstens
1.024 Schlüssel pro Aufruf. Sie ersetzt keine Projektionen, Joins, Zeilensperren
oder das Caching beliebiger Abfrageergebnisse.

## Batches begrenzen und beobachtbar halten {#bounded-batches}

Bei mehr als 1.024 Schlüsseln teilen Sie Anfragen explizit auf oder behalten Sie
eine gewöhnliche SQL-Abfrage bei. Chunking über mehrere Anweisungen kann
unterschiedliche `READ COMMITTED`-Snapshots sehen; wählen Sie die
Transaktionssemantik bewusst. Größere Batches erhöhen außerdem Antwortgröße und
Dekodierarbeit des Clients, daher beweist „weniger Abfragen“ allein keine
schnellere Anfrage.

Für GraphQL muss eine DataLoader-Batch-Funktion eine Antwort pro Eingabeschlüssel
in derselben Reihenfolge zurückgeben. Anfragebezogene Memoization und der
gemeinsame Cache von PostgreSQL sind getrennte Ebenen; löschen Sie betroffene
Loader-Einträge nach Mutationen. Siehe den [vollständigen Batching-Leitfaden](../docs/batch-primary-key-lookups.md)
und vergleichen Sie Latenz, Payloads und Durchsatz mit dem
[Benchmark-Runner](../docs/BENCHMARKS.md).
