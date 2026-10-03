---
layout: doc
lang: es
translation_key: node-postgres
title: Consultas por lotes de filas con node-postgres
seo_title: "Consultas por lotes de filas de PostgreSQL con node-postgres"
description: Usa pg_local_cache 2.0 desde Node.js con un array bigint parametrizado y transporte JSON. Conserva el orden y los valores nulos, y compáralo con una consulta ANY preparada.
section: Node.js
permalink: /es/docs/node-postgres.html
last_modified_at: "2026-09-16"
---

# Consultas por lotes de filas con node-postgres {#batch-row-lookups-with-node-postgres}

Lee filas por clave primaria usando tu conexión o pool existente de node-postgres.

Inicia la [demo](QUICKSTART.md), instala las dependencias y ejecuta sus aserciones de integración:

```bash
npm --prefix examples/node-postgres ci --ignore-scripts
npm --prefix examples/node-postgres run demo
```

## Envía una consulta parametrizada {#send-one-parameterized-query}

Dado un cliente o pool de node-postgres conectado:

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

`mget` devuelve `text[]`. `array_to_json` envía el array exterior como JSON, por lo que node-postgres aplica su descodificador JSON. Cada elemento no nulo es una fila serializada y necesita `JSON.parse`; las posiciones coinciden con las posiciones de entrada, y las claves ausentes o las entradas nulas producen `null`.

Mantén fijo el nombre de la tabla en el código de la aplicación. Pasa los ID como parámetros de consulta, no como SQL ensamblado a partir de cadenas. Consulta la documentación de node-postgres sobre [parámetros y sentencias preparadas con nombre](https://node-postgres.com/features/queries).

El helper ejecutable rechaza lotes de más de 1.024 claves y devuelve `[]` sin hacer una consulta para un lote vacío. Usa ID de demostración que son enteros seguros. Los campos `bigint` y `numeric` de PostgreSQL en JSON pueden superar el rango numérico exacto de JavaScript; usa un parser JSON sin pérdida o un contrato de serialización explícito para esos valores.

## Compara con la consulta por lotes existente {#compare-with-the-existing-batch-query}

La línea base usa:

```sql
SELECT id::text AS key, row_to_json(i)::text AS row
FROM public.items AS i
WHERE id = ANY($1::bigint[]);
```

`ANY` no conserva el orden de entrada ni las posiciones solicitadas duplicadas. El ejemplo las restaura en el cliente y proporciona `null` para las filas ausentes antes de comparar los resultados.

La implementación ejecutable está en [examples/node-postgres](https://github.com/profundium/pg_local_cache/tree/master/examples/node-postgres). El helper recibe un cliente existente en vez de crear un pool en cada llamada.

## Transacciones y límites de la aplicación {#transactions-and-application-boundaries}

Usa un único cliente adquirido durante toda una transacción. Las lecturas posteriores a las escrituras en la misma transacción usan el recorrido de la tabla de origen de PostgreSQL. La demo lo comprueba con conexiones de lectura y escritura separadas; consulta [invalidación de caché](cache-invalidation.md).

## Sentencias preparadas y caché de resultados {#prepared-statements-and-result-caching}

Una consulta con nombre de node-postgres reutiliza una sentencia preparada en cada conexión. No almacena en caché las filas devueltas. `local_cache.mget` añade una caché compartida separada de filas completas dentro de PostgreSQL; el cliente sigue enviando una consulta y descodificando su resultado. Consulta la [guía para decidir la caché](postgresql-caching.md) para comparar las capas y la [guía de consultas por lotes](batch-primary-key-lookups.md) para una alternativa solo SQL que conserva las posiciones solicitadas.

Para RESP2, usa el [ejemplo RESP de Node.js](resp.md#nodejs). Los [resultados registrados de Node.js](benchmarks-node.md) incluyen lecturas por lotes y actualizaciones concurrentes. El [benchmark común](BENCHMARKS.md#run-the-same-comparison-on-every-client) ejecuta Node.js y Go mediante los mismos escenarios SQL y RESP.
