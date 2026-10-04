---
layout: doc
lang: es
translation_key: node-postgres
title: "Consultas de filas por lotes con node-postgres"
seo_title: "Consultas por lotes de filas de PostgreSQL con node-postgres"
description: "Usa RESP MGET autenticado desde Node.js para leer filas en caché y node-postgres para escrituras SQL y consultas ordinarias."
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

## Leer mediante RESP {#send-one-parameterized-query}

Con un cliente RESP conectado de `@redis/client`:

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

RESP `MGET` devuelve filas codificadas en JSON en el orden de las claves. El helper omite las claves nulas y restaura sus posiciones; las claves ausentes devuelven null.

Mantén fijo el nombre de la tabla en el código de la aplicación. Pasa los ID como parámetros de consulta; no construyas SQL concatenando cadenas. Consulta la documentación de node-postgres sobre [parámetros y sentencias preparadas con nombre](https://node-postgres.com/features/queries).

El comando RESP acepta como máximo 1.024 claves. El helper ejecutable devuelve `[]` sin enviar una solicitud cuando todas las entradas son nulas. Los campos `bigint` de PostgreSQL y los valores numéricos JSON pueden superar el rango exacto de JavaScript; usa un parser JSON sin pérdida o un contrato de serialización explícito.

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

Una consulta con nombre de node-postgres reutiliza una sentencia preparada en cada conexión. No almacena en caché las filas devueltas. RESP `MGET` usa la caché compartida de filas completas de la extensión, pero su rol de worker y estado de sesión son independientes de la conexión SQL de la aplicación. Consulta la [guía de decisiones de caché](postgresql-caching.md) y la [guía de consultas por lotes](batch-primary-key-lookups.md).

Para RESP2, usa el [ejemplo RESP de Node.js](resp.md#nodejs). Los [resultados registrados de Node.js](benchmarks-node.md) incluyen lecturas por lotes y actualizaciones concurrentes. El [benchmark común](BENCHMARKS.md#run-the-same-comparison-on-every-client) ejecuta Node.js y Go en los mismos escenarios de SQL preparado y RESP.
