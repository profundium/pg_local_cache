---
layout: doc
lang: es
translation_key: cache-invalidation
title: Invalidación de caché consciente de las transacciones en PostgreSQL
seo_title: "Invalidación de caché de PostgreSQL: commit y rollback | pg_local_cache"
description: Prueba la invalidación de pg_local_cache 2.0 con sesiones concurrentes de PostgreSQL. Comprueba actualizaciones no confirmadas, lectura de los propios cambios, rollback, lecturas confirmadas y reglas de omisión.
section: Invalidación de caché
permalink: /es/docs/cache-invalidation.html
last_modified_at: "2026-09-16"
---

# Invalidación de caché consciente de las transacciones en PostgreSQL {#transaction-aware-cache-invalidation-in-postgresql}

Eliminar una entrada de caché no basta si una lectura anterior puede volver a llenarla después de la eliminación. Supón que un lector empieza a cargar una fila antigua, un escritor confirma un valor nuevo e invalida la clave y después ese loader anterior publica su resultado. La caché también debe rechazar esa publicación tardía.

La implementación 2.0 pone una valla a las claves o relaciones afectadas en el recorrido de escritura de la base de datos. Un llenado lleva información de generación para poder rechazarlo después de una invalidación. Las entradas positivas almacenadas también llevan información de visibilidad de la tupla. Una entrada no elegible vuelve a leer la tabla de origen. Consulta la [referencia técnica](TECHNICAL.md#transaction-consistency) para conocer el contrato.

{% include diagrams/transaction.html id="invalidation-transaction" %}

## Prueba con dos sesiones {#test-with-two-sessions}

Inicia la [demo local](QUICKSTART.md). Abre este comando en dos terminales:

```bash
docker compose -f examples/compose.yaml exec postgres \
  psql -X -v ON_ERROR_STOP=1 -U demo -d pglc_demo
```

En la sesión A, lee la fila 42 y anota su revisión; después vuelve a leerla para calentarla:

```sql
SELECT (local_cache.mget('public.items'::regclass, ARRAY[42]::bigint[]))[1]::jsonb ->> 'revision';
SELECT (local_cache.mget('public.items'::regclass, ARRAY[42]::bigint[]))[1]::jsonb ->> 'revision';
```

En la sesión B, actualiza la fila pero deja abierta la transacción:

```sql
BEGIN;
UPDATE public.items SET revision = revision + 1 WHERE id = 42;
SELECT (local_cache.mget('public.items'::regclass, ARRAY[42]::bigint[]))[1]::jsonb ->> 'revision';
```

B ve su propio incremento. Esta lectura omite la caché. Repite la consulta de A mientras B siga abierta: A debe seguir viendo la revisión confirmada, no el valor no confirmado de B. En B, ejecuta `ROLLBACK`; otra consulta en A debe seguir devolviendo la revisión original.

Ahora ejecuta en B:

```sql
BEGIN;
UPDATE public.items SET revision = revision + 1 WHERE id = 42;
COMMIT;
```

Una consulta iniciada en A después de ese commit debe devolver la revisión incrementada. Este es el límite relevante: una sentencia antigua que ya está en ejecución no tiene que cambiar al snapshot tomado después de iniciarse. PostgreSQL documenta ese comportamiento bajo [Read Committed](https://www.postgresql.org/docs/16/transaction-iso.html#XACT-READ-COMMITTED).

La [prueba ejecutable de Node.js](https://github.com/profundium/pg_local_cache/blob/master/examples/node-postgres/demo.mjs) afirma estas observaciones con conexiones separadas.

## Casos que omiten deliberadamente la caché {#cases-that-deliberately-bypass-the-cache}

`REPEATABLE READ`, `SERIALIZABLE`, la recuperación, la ejecución paralela y las transacciones que han escrito datos mapeados usan el recorrido de la tabla de origen. Una fila sobredimensionada puede devolverse correctamente sin almacenarse en caché. Una tasa de aciertos cercana a cero no implica necesariamente una instalación fallida: comprueba la carga de trabajo y los contadores de omisiones.

Cuando la aplicación necesite `SELECT ... FOR UPDATE`, usa la operación normal de PostgreSQL; `mget` no sustituye el bloqueo de filas.

## Inspecciona la causa de un fallo {#inspect-the-cause-of-a-miss}

Usa `local_cache.stats()` y `local_cache.health()` como administrador. Compara instantáneas de contadores antes y después de una prueba controlada. Mantén separados los contadores de SQL `mget` y los de RESP. Después de un DDL intencionado, sigue el procedimiento documentado `reconcile_table` o `reconcile_all` en vez de suponer que un mapeo asociado anteriormente aún describe la tabla modificada.
