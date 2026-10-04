---
layout: doc
lang: de
translation_key: node-postgres
title: "Batch-Abfragen von Zeilen mit node-postgres"
seo_title: "Batch-Abfragen von PostgreSQL-Zeilen mit node-postgres"
description: "Verwenden Sie authentifiziertes RESP MGET in Node.js für gecachte Zeilen-Lesevorgänge und node-postgres für SQL-Schreibvorgänge sowie gewöhnliche Abfragen."
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

## Über RESP lesen {#send-one-parameterized-query}

Mit einem verbundenen RESP-Client aus `@redis/client`:

```js
const ids = [42, 7, 42, null, 999999];
const wireKeys = ids.filter(id => id !== null).map(id =>
  `CRUD:app.public.items:${JSON.stringify({ id })}`
);
const values = await client.mGet(wireKeys);
let position = 0;
const rows = ids.map(id => {
  if (id === null) return null;
  const value = values[position++];
  return value === null ? null : JSON.parse(value);
});
```

RESP `MGET` liefert JSON-kodierte Zeilen in Schlüsselreihenfolge. Der Helfer lässt NULL-Eingabeschlüssel aus und stellt ihre Positionen wieder her; fehlende Schlüssel liefern null.

Halten Sie den Tabellennamen im Anwendungscode fest. Übergeben Sie IDs als Abfrageparameter und setzen Sie kein SQL aus Zeichenfolgen zusammen. Siehe die node-postgres-Dokumentation zu [Parametern und benannten vorbereiteten Statements](https://node-postgres.com/features/queries).

Der RESP-Befehl akzeptiert höchstens 1.024 Schlüssel. Der ausführbare Helfer gibt `[]` ohne Anfrage zurück, wenn alle Eingaben null sind. PostgreSQL-`bigint`- und numerische JSON-Felder können den exakten Zahlenbereich von JavaScript überschreiten; verwenden Sie einen verlustfreien JSON-Parser oder einen expliziten Serialisierungsvertrag.

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

Ein benannter node-postgres-Aufruf verwendet pro Verbindung ein vorbereitetes Statement erneut. Er cached keine zurückgegebenen Zeilen. RESP `MGET` nutzt den gemeinsamen Ganzzeilen-Cache der Erweiterung, aber Worker-Rolle und Sitzungszustand sind von der SQL-Verbindung der Anwendung getrennt. Siehe den [Caching-Entscheidungsleitfaden](postgresql-caching.md) und den [Batch-Leitfaden](batch-primary-key-lookups.md).

Für RESP2 siehe das [Node.js-RESP-Beispiel](resp.md#nodejs). Die [aufgezeichneten Node.js-Ergebnisse](benchmarks-node.md) umfassen Batch-Lesevorgänge und gleichzeitige Updates. Der [gemeinsame Benchmark](BENCHMARKS.md#run-the-same-comparison-on-every-client) führt Node.js und Go durch dieselben vorbereiteten SQL- und RESP-Szenarien.
