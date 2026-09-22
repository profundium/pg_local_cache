---
layout: doc
lang: es
translation_key: postgresql-caching
title: Guía para decidir la caché de PostgreSQL
seo_title: "Guía para decidir la caché de PostgreSQL: páginas, filas, vistas o Redis"
description: Elige entre caché de páginas de PostgreSQL, SQL preparado, caché de filas completas, vistas materializadas o una caché externa según el trabajo que necesites evitar.
section: Guías
permalink: /es/docs/postgresql-caching.html
last_modified_at: "2026-09-16"
---

# Guía para decidir la caché de PostgreSQL {#postgresql-caching-decision-guide}

«Añadir una caché» describe varios cambios distintos. La caché de páginas de PostgreSQL, el SQL preparado, una caché de filas completas, una vista materializada y Redis evitan partes diferentes de una lectura. Elige según el trabajo que se repite en tu solicitud y después mide el recorrido completo con la [guía de benchmarks](BENCHMARKS.md).

## Empieza por el trabajo que repites {#start-with-the-work-you-repeat}

| Necesidad | Primera opción | Qué cambia |
|---|---|---|
| Mantener calientes las páginas de tablas e índices | `shared_buffers` de PostgreSQL y la caché del sistema operativo | Menos lecturas de almacenamiento; el SQL sigue ejecutándose |
| Enviar muchas veces la misma sentencia | Una sentencia preparada | Menos trabajo repetido de análisis y planificación; la ejecución sigue ocurriendo |
| Devolver filas completas por clave primaria | SQL `mget` de `pg_local_cache` | Reutiliza cargas útiles de filas completas elegibles mediante una API explícita |
| Precalcular una unión o un agregado | Una vista materializada | Lee resultados persistidos; la actualización define la frescura |
| Compartir objetos de aplicación entre servicios | Una caché externa como Redis | Claves, TTL e invalidación gestionados por la aplicación |

### Páginas y SQL preparado {#pages-and-prepared-sql}

[`shared_buffers`](https://www.postgresql.org/docs/18/runtime-config-resource.html#GUC-SHARED-BUFFERS) contiene páginas de la base de datos, no resultados finales de `SELECT`. Una página caliente puede evitar E/S de almacenamiento, pero PostgreSQL aún planifica o ejecuta la consulta, comprueba la visibilidad y construye el resultado. Una [sentencia preparada](https://www.postgresql.org/docs/18/sql-prepare.html) puede evitar trabajo repetido de análisis y parseo en una sesión. Aun así se ejecuta contra el estado actual de la base de datos, y su plan puede ser genérico o personalizado.

La [comparación de cachés de filas](row-cache-vs-shared-buffers.md) muestra qué trabajo queda en cada recorrido.

### Filas completas por clave primaria {#whole-rows-by-primary-key}

`pg_local_cache` almacena cargas serializadas de filas completas bajo claves primarias completas en memoria compartida acotada de PostgreSQL. Se accede mediante `local_cache.mget('public.items'::regclass, $1::bigint[])`; un `SELECT` normal nunca la consulta. Las lecturas elegibles y limpias con `READ COMMITTED` pueden acertar, mientras que los modos de aislamiento más estrictos, las escrituras en la transacción, la recuperación, la ejecución paralela o las filas demasiado grandes usan PostgreSQL. Los mapeos de tablas no compatibles se rechazan al asociarlos. Es un recorrido de lectura específico, no una caché de resultados de consultas arbitrarias. Consulta la [guía de consultas por lotes](batch-primary-key-lookups.md), el [contrato técnico](TECHNICAL.md) y las [comprobaciones de transacciones](cache-invalidation.md).

### Vistas y cachés externas {#views-and-external-caches}

Las [vistas materializadas de PostgreSQL](https://www.postgresql.org/docs/18/rules-materializedviews.html) conservan el resultado de una consulta en una relación y se actualizan bajo demanda. Son adecuadas para informes, agregados y uniones repetibles cuando una frecuencia de actualización es un límite de frescura aceptable. No sustituyen una caché de filas por clave.

Una caché externa como Redis sirve para objetos de aplicación compartidos por varios procesos o servicios. La aplicación es propietaria de las claves, la serialización, el TTL y la invalidación. Consulta la [guía de caché aparte de Redis](postgresql-redis-cache.md). El [quickstart](QUICKSTART.md) ejecuta `pg_local_cache` sobre `public.items`.
