---
layout: doc
lang: es
translation_key: cache-invalidation
title: Invalidación de caché consciente de las transacciones en PostgreSQL
seo_title: "Invalidación de caché de PostgreSQL: commit y rollback | pg_local_cache"
description: "Comprende la invalidación mediante triggers para lecturas RESP de filas, actualizaciones confirmadas, lecturas de origen y el límite de la transacción SQL."
section: Invalidación de caché
permalink: /es/docs/cache-invalidation.html
last_modified_at: "2026-09-16"
---

# Invalidación de caché consciente de las transacciones en PostgreSQL {#transaction-aware-cache-invalidation-in-postgresql}

Eliminar una entrada de caché no basta si una lectura anterior puede volver a llenarla después de la eliminación. Supón que un lector empieza a cargar una fila antigua, un escritor confirma un valor nuevo e invalida la clave y después ese loader anterior publica su resultado. La caché también debe rechazar esa publicación tardía.

La implementation pone una valla a las claves o relaciones afectadas en el recorrido de escritura de la base de datos. Un llenado lleva información de generación para poder rechazarlo después de una invalidación. Las entradas positivas almacenadas también llevan información de visibilidad de la tupla. Una entrada no elegible vuelve a leer la tabla de origen. Consulta la [referencia técnica](TECHNICAL.md#transaction-consistency) para conocer el contrato.

{% include diagrams/transaction.html id="invalidation-transaction" %}

## Comprueba la invalidación entre SQL y RESP {#test-with-two-sessions}

Inicia la [demo local](QUICKSTART.md). Lee la fila 42 por RESP y anota su revisión. Después, actualiza la fila en PostgreSQL y confirma la transacción:

```sql
BEGIN;
UPDATE public.items SET revision = revision + 1 WHERE id = 42;
COMMIT;
```

El trigger de la tabla asociada invalida la fila afectada al confirmar la transacción. La siguiente lectura RESP devuelve la revisión confirmada. Para observar un rollback, inicia otra actualización y reviértela; RESP seguirá devolviendo la última revisión confirmada.

Los workers RESP usan el rol PostgreSQL configurado y no comparten la transacción SQL ni la instantánea de la aplicación. Una comprobación de lectura de los propios cambios debe usar SQL en la misma transacción de la aplicación, que sigue el recorrido normal por la tabla de origen. El endpoint RESP está pensado para lecturas separadas con el rol de worker.

La [prueba ejecutable de Node.js](https://github.com/profundium/pg_local_cache/blob/master/examples/node-postgres/demo.mjs) comprueba lecturas RESP alrededor de escrituras PostgreSQL.

## Casos que omiten deliberadamente la caché {#cases-that-deliberately-bypass-the-cache}

`REPEATABLE READ`, `SERIALIZABLE`, la recuperación, la ejecución paralela y las transacciones que han escrito datos mapeados usan el recorrido de la tabla de origen. Una fila sobredimensionada puede devolverse correctamente sin almacenarse en caché. Una tasa de aciertos cercana a cero no implica necesariamente una instalación fallida: comprueba la carga de trabajo y los contadores de omisiones.

Si la aplicación necesita `SELECT ... FOR UPDATE`, usa la operación PostgreSQL normal. RESP `MGET` no ofrece bloqueos de filas ni semántica de sesión SQL.

## Inspecciona la causa de un fallo {#inspect-the-cause-of-a-miss}

Como administrador, usa `local_cache.stats()` y `local_cache.health()`. Compara los contadores antes y después de una prueba controlada. Los contadores de caché describen la ruta de lectura RESP. Tras cambios DDL intencionados, sigue el procedimiento documentado `reconcile_table` o `reconcile_all`; no supongas que un mapeo anterior sigue describiendo la tabla modificada.
