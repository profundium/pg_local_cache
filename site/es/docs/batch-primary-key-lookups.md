---
layout: doc
lang: es
translation_key: batch-primary-key-lookups
title: Consultas por lotes de claves primarias de PostgreSQL
seo_title: "Consultas por lotes de claves primarias de PostgreSQL con ANY y RESP MGET"
description: "Sustituye lecturas N+1 por clave primaria con una consulta PostgreSQL parametrizada, conserva posiciones de entrada cuando haga falta y compara con RESP MGET autenticado."
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

## Cuándo encaja RESP `MGET` {#when-mget-is-the-right-alternative}

Para leer filas completas por clave primaria, `pg_local_cache` ofrece el comando RESP2 autenticado `MGET`. Las claves usan la base de datos, el esquema, la tabla y los valores de clave primaria de la tabla asociada:

```text
MGET CRUD:app.public.items:{"id":42} CRUD:app.public.items:{"id":7}
```

La respuesta conserva el orden de las claves y los duplicados; las filas ausentes devuelven null. Cada solicitud acepta como máximo 1.024 claves y devuelve filas JSON completas. Los workers RESP usan el rol de base de datos configurado y no comparten la transacción SQL ni la instantánea del llamador. Usa SQL `ANY` o la consulta con ordinality para proyecciones, joins, filtros adicionales o semántica de transacción SQL.

## GraphQL, DataLoader y lecturas N+1 {#graphql-dataloader-and-n1-reads}

[DataLoader](https://github.com/graphql/dataloader#batching) combina cargas individuales en un lote. Su función de lote debe devolver un valor por cada clave de entrada en el mismo orden; la restauración anterior proporciona esa forma incluso para las filas ausentes.

La [memorización por solicitud de DataLoader](https://github.com/graphql/dataloader#caching-per-request) es independiente de la caché de filas compartida de PostgreSQL. Crea cargadores para cada solicitud y borra las entradas afectadas después de las mutaciones de esa solicitud. La invalidación de PostgreSQL no puede borrar valores ya almacenados en un cargador JavaScript. Conserva las comprobaciones de autorización de la aplicación; `pg_local_cache` no admite tablas con RLS.

Ejecuta el [quickstart](QUICKSTART.md) y después compara ambos recorridos de lectura en los [benchmarks](BENCHMARKS.md). La [referencia técnica](TECHNICAL.md#optional-resp2-endpoint) define la API; la [guía de transacciones](cache-invalidation.md) cubre las escrituras.
