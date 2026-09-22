---
layout: doc
lang: de
translation_key: node-postgres
title: Batch-Zeilenabfragen mit node-postgres
seo_title: "Batch-Abfragen von PostgreSQL-Zeilen mit node-postgres"
description: Verwenden Sie pg_local_cache 2.0 aus Node.js mit einem parametrisierten bigint-Array und JSON-Transport. Erhalten Sie Reihenfolge und NULL-Werte und vergleichen Sie mit einer vorbereiteten ANY-Abfrage.
section: Node.js
permalink: /de/docs/node-postgres.html
last_modified_at: "2026-09-16"
---

# Batch-Zeilenabfragen mit node-postgres {#batch-row-lookups-with-node-postgres}

Lesen Sie Zeilen per Primärschlüssel über Ihre bestehende node-postgres-Verbindung oder Ihren Pool.

Starten Sie die [Demo](QUICKSTART.md), installieren Sie die Abhängigkeiten und führen Sie deren Integrationsprüfungen aus:

```bash
npm --prefix examples/node-postgres ci --ignore-scripts
npm --prefix examples/node-postgres run demo
```

## Eine parametrisierte Abfrage senden {#send-one-parameterized-query}

Mit einem verbundenen node-postgres-Client oder Pool:

```js
const result = await client.query({
  name: 'items-mget',
  text: "SELECT array_to_json(local_cache.mget('public.items'::regclass, $1::bigint[])) AS rows",
  values: [[42, 7, 42, null, 999999]],
});
const rows = result.rows[0].rows.map(row =>
  row === null ? null : JSON.parse(row)
);
```

`mget` gibt `text[]` zurück. `array_to_json` sendet das äußere Array als JSON,
damit node-postgres seinen JSON-Decoder anwendet. Jedes Element ungleich null
ist eine serialisierte Zeile und benötigt `JSON.parse`; die Positionen stimmen
mit den Eingabepositionen überein, fehlende Schlüssel oder Null-Eingaben ergeben
`null`.

Halten Sie den Tabellennamen im Anwendungscode fest. Übergeben Sie IDs als
Query-Parameter, nicht als aus Strings zusammengesetztes SQL. Siehe die
node-postgres-Dokumentation zu
[Parametern und benannten vorbereiteten Statements](https://node-postgres.com/features/queries).

Der ausführbare Helfer weist Batches mit mehr als 1.024 Schlüsseln zurück und
gibt für einen leeren Batch ohne Abfrage `[]` zurück. Er verwendet sichere
Demo-IDs. PostgreSQL-`bigint`- und numerische Felder in JSON können den exakten
Zahlenbereich von JavaScript überschreiten; verwenden Sie für solche Werte einen
verlustfreien JSON-Parser oder einen expliziten Serialisierungsvertrag.

## Mit der bestehenden Batch-Abfrage vergleichen {#compare-with-the-existing-batch-query}

Die Baseline verwendet:

```sql
SELECT id::text AS key, row_to_json(i)::text AS row
FROM public.items AS i
WHERE id = ANY($1::bigint[]);
```

`ANY` erhält weder die Eingabereihenfolge noch doppelte angeforderte Positionen.
Das Beispiel stellt sie im Client wieder her und liefert fehlende Zeilen als
null, bevor es Ergebnisse vergleicht.

Die ausführbare Implementierung liegt in
[examples/node-postgres](https://github.com/profundium/pg_local_cache/tree/master/examples/node-postgres).
Der Helfer nimmt einen vorhandenen Client statt für jeden Aufruf einen Pool zu erzeugen.

## Transaktionen und Anwendungsgrenzen {#transactions-and-application-boundaries}

Verwenden Sie innerhalb einer Transaktion durchgehend denselben entnommenen
Client. Lesevorgänge nach Schreibvorgängen in derselben Transaktion verwenden
den Quelltabellenpfad von PostgreSQL. Die Demo prüft dies mit getrennten Lese-
und Schreibverbindungen; siehe [Cache-Invalidation](cache-invalidation.md).

## Vorbereitete Statements und Ergebnis-Caching {#prepared-statements-and-result-caching}

Eine benannte node-postgres-Abfrage verwendet auf jeder Verbindung ein
wiederverwendetes vorbereitetes Statement. Sie cached keine zurückgegebenen
Zeilen. `local_cache.mget` fügt einen separaten gemeinsamen Cache vollständiger
Zeilen in PostgreSQL hinzu; der Client sendet weiterhin eine Abfrage und dekodiert
deren Ergebnis. Siehe den [Leitfaden zur Caching-Entscheidung](postgresql-caching.md)
für den Vergleich der Ebenen und den [Leitfaden zu Batch-Abfragen](batch-primary-key-lookups.md)
für eine reine SQL-Alternative, die angeforderte Positionen erhält.

Für RESP2 verwenden Sie das [Node.js-RESP-Beispiel](resp.md#nodejs).
[Aufgezeichnete Node.js-Ergebnisse](benchmarks-node.md) enthalten Batch-Lesevorgänge
und gleichzeitige Updates. Der [gemeinsame Benchmark](BENCHMARKS.md#run-the-same-comparison-on-every-client)
führt Node.js und Go durch dieselben SQL- und RESP-Szenarien.
