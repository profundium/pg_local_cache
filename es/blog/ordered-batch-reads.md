---
layout: post
lang: es
translation_key: blog-ordered-batch-reads
title: "Lecturas por lotes de PostgreSQL sin perder el orden ni las claves ausentes"
description: Sustituye consultas de claves primarias N+1 conservando los ID duplicados, el orden de entrada, las posiciones NULL y las filas ausentes. Compara ANY, WITH ORDINALITY y SQL mget.
permalink: /es/blog/ordered-batch-reads/
date: "2026-09-22"
last_modified_at: "2026-09-22"
topic: application
---

# Las lecturas por lotes necesitan un contrato de resultados {#batch-reads-need-a-result-contract}

Sustituir un bucle de consultas por clave primaria por una consulta `ANY` elimina viajes de ida y vuelta. También puede cambiar la forma de la respuesta. Quien llama podría solicitar `[42, 7, 42, NULL, -1]` y esperar cinco posiciones de resultado. La semántica de conjuntos SQL no promete esa alineación.

## Un conjunto de filas no es una lista de respuestas {#set-versus-list}

Con `WHERE id = ANY($1::bigint[])`, un ID duplicado normalmente coincide una sola vez con su fila de tabla. Un ID ausente no aporta ninguna fila. Una entrada `NULL` no coincide con una clave primaria no nula y el resultado no garantiza el orden de entrada. Añadir `ORDER BY id` ordena por clave; aún no reproduce las posiciones solicitadas.

Si los consumidores necesitan un conjunto, está bien. Si necesitan un resultado por cada entrada, convierte las posiciones en parte de la consulta o restáuralas en la aplicación.

## Mantén las posiciones explícitas en SQL {#explicit-positions}

Inicia la [demo local](../docs/QUICKSTART.md) y ejecuta esto en su sesión `psql`:

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

La columna de ordinality distingue las dos apariciones de 42. La unión izquierda conserva las cinco posiciones, incluida la entrada nula y cualquier clave ausente. Para una clave ausente, `row` es `NULL` SQL. En el código de la aplicación, pasa el array como parámetro en vez de concatenar los ID en SQL. El [ejemplo de Node.js](../docs/node-postgres.md) demuestra la alineación desde el cliente.

## Compara la API de filas completas {#whole-row-api}

En una tabla asociada, la llamada explícita de caché correspondiente es:

```sql
SELECT local_cache.mget(
  'public.items'::regclass,
  ARRAY[42, 7, 42, NULL, -1]::bigint[]
) AS rows;
```

Devuelve `text[]`, conservando el orden de entrada y los duplicados. Las claves ausentes y las entradas nulas producen elementos `NULL` SQL alineados; cada elemento presente es una fila completa serializada. Un fallo o una omisión de caché lee PostgreSQL. La API acepta como máximo 1.024 claves por llamada. No sustituye proyecciones, uniones, bloqueos de filas ni el almacenamiento en caché arbitrario de resultados de consultas.

## Mantén los lotes acotados y observables {#bounded-batches}

Para más de 1.024 claves, divide las solicitudes explícitamente o conserva una consulta SQL normal. Dividir entre sentencias puede observar snapshots `READ COMMITTED` diferentes; elige deliberadamente la semántica de transacción. Los lotes más grandes también aumentan el tamaño de la respuesta y el trabajo de descodificación del cliente, así que «menos consultas» por sí solo no demuestra una solicitud más rápida.

En GraphQL, una función de lote de DataLoader debe devolver una respuesta por cada clave de entrada en el mismo orden. La memorización local de la solicitud y la caché compartida de PostgreSQL son capas separadas; borra las entradas afectadas después de las mutaciones. Consulta la [guía completa de lotes](../docs/batch-primary-key-lookups.md) y compara latencia, cargas útiles y rendimiento con el [ejecutor de benchmarks](../docs/BENCHMARKS.md).
