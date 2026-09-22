---
layout: doc
lang: es
translation_key: batch-primary-key-lookups
title: Consultas por lotes de claves primarias de PostgreSQL
seo_title: "Consultas por lotes de claves primarias de PostgreSQL con ANY y mget"
description: Sustituye lecturas de claves primarias N+1 por una consulta parametrizada de PostgreSQL, conserva las posiciones de entrada cuando sea necesario y compara el recorrido explícito pg_local_cache mget.
section: Guías
permalink: /es/docs/batch-primary-key-lookups.html
last_modified_at: "2026-09-16"
---

# Consultas por lotes de claves primarias de PostgreSQL {#batch-postgresql-primary-key-lookups}

Si el código de la aplicación envía una consulta por ID, los viajes de ida y vuelta por red y la sobrecarga de la consulta pueden dominar una lectura de fila pequeña. Primero prueba una sentencia parametrizada:

```sql
SELECT id, value, revision
FROM public.items
WHERE id = ANY($1::bigint[]);
```

Pasa los ID como parámetro de tipo array. Mantén la tabla y las columnas fijas en la sentencia; no construyas SQL a partir de cadenas de ID. PostgreSQL evalúa `ANY` comparando la expresión izquierda con los elementos del array, como se describe en la [documentación de comparaciones de filas y arrays](https://www.postgresql.org/docs/18/functions-comparisons.html#FUNCTIONS-COMPARISONS-ANY-SOME).

## Conoce el contrato del resultado {#know-the-result-contract}

La consulta anterior devuelve un conjunto. No promete el orden de entrada, y un ID duplicado normalmente coincide una sola vez con la fila de la tabla. Los ID que faltan no producen ninguna fila. Una entrada `NULL` no coincide con una clave primaria no nula; un array nulo o elementos nulos también siguen las reglas ternarias de `ANY` de PostgreSQL. Un array vacío no devuelve filas.

Si quien llama necesita un resultado por cada posición solicitada, conserva explícitamente las posiciones:

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

`WITH ORDINALITY` conserva los duplicados y las posiciones `NULL`; la unión izquierda devuelve un `row` nulo para una clave ausente. Es una base útil para un cliente que necesita una alineación explícita. Consulta el [ejemplo de node-postgres](node-postgres.md) para restaurar el mismo contrato en el cliente.

## Cuándo `mget` es la alternativa adecuada {#when-mget-is-the-right-alternative}

Para filas completas por clave primaria, `pg_local_cache` ofrece una API de lotes explícita y acotada:

```sql
SELECT local_cache.mget(
  'public.items'::regclass,
  $1::bigint[]
) AS rows;
```

El `text[]` devuelto conserva el orden de entrada y los duplicados. Las entradas `NULL` y las filas ausentes producen elementos `NULL` alineados. Las llamadas aceptan como máximo 1.024 claves, y la función puede omitir o no acertar la caché según las reglas de transacción, snapshot, mapeo y tamaño de fila; vuelve a PostgreSQL sin cambiar el contrato del resultado. Devuelve filas serializadas completas, así que usa `ANY` o la consulta con ordinality cuando necesites una proyección, uniones, filtros más allá de la clave o un lote sin límite.

## GraphQL, DataLoader y lecturas N+1 {#graphql-dataloader-and-n1-reads}

[DataLoader](https://github.com/graphql/dataloader#batching) combina cargas individuales en un lote. Su función de lote debe devolver un valor por cada clave de entrada en el mismo orden; la restauración anterior proporciona esa forma incluso para las filas ausentes.

La [memorización por solicitud de DataLoader](https://github.com/graphql/dataloader#caching-per-request) es independiente de la caché de filas compartida de PostgreSQL. Crea cargadores para cada solicitud y borra las entradas afectadas después de las mutaciones de esa solicitud. La invalidación de PostgreSQL no puede borrar valores ya almacenados en un cargador JavaScript. Conserva las comprobaciones de autorización de la aplicación; `pg_local_cache` no admite tablas con RLS.

Ejecuta el [quickstart](QUICKSTART.md) y después compara ambos recorridos de lectura en los [benchmarks](BENCHMARKS.md). La [referencia técnica](TECHNICAL.md#sql-mget-api) define la API; la [guía de transacciones](cache-invalidation.md) cubre las escrituras.
