---
layout: doc
lang: es
translation_key: postgresql-caching
title: Guía para decidir el almacenamiento en caché de PostgreSQL
seo_title: "Guía de caché para PostgreSQL: páginas, filas, vistas o Redis"
description: Compare la caché de páginas de PostgreSQL, SQL preparado, caché de filas completas, vistas materializadas y Redis según el trabajo que evita cada ruta de lectura.
section: Guías
permalink: /es/docs/postgresql-caching.html
last_modified_at: "2026-10-04"
---

# Guía para decidir el almacenamiento en caché de PostgreSQL {#postgresql-caching-decision-guide}

Esta guía compara la caché de páginas de PostgreSQL, SQL preparado, caché de filas completas, vistas materializadas y Redis según el trabajo que evita cada opción.

## Empiece por el trabajo que repite {#start-with-the-work-you-repeat}

| Necesidad | Opción | Trabajo que evita |
|---|---|---|
| Mantener en memoria las páginas de tablas e índices | `shared_buffers` de PostgreSQL y caché del sistema operativo | Lecturas de almacenamiento; SQL sigue ejecutándose |
| Repetir una sentencia en una sesión | Sentencia preparada | Parseo y análisis repetidos |
| Leer filas completas por clave primaria | `MGET` autenticado de RESP con `pg_local_cache` | Lecturas aptas de filas completas desde el origen |
| Reutilizar joins o agregados | Vista materializada | Recalcular el resultado almacenado hasta que se actualice |
| Compartir objetos entre servicios | Redis cache-aside | Lecturas de origen gestionadas por la aplicación |

### Páginas y SQL preparado {#pages-and-prepared-sql}

[`shared_buffers`](https://www.postgresql.org/docs/18/runtime-config-resource.html#GUC-SHARED-BUFFERS) almacena en caché páginas de la base de datos, no resultados finales de `SELECT`. PostgreSQL sigue comprobando la visibilidad, ejecutando la consulta y construyendo cada resultado. Una [sentencia preparada](https://www.postgresql.org/docs/18/sql-prepare.html) reduce el parseo repetido, pero sigue ejecutándose sobre el estado actual de la base de datos.

Consulte [caché de filas frente a shared_buffers](row-cache-vs-shared-buffers.md) para ver el trabajo que queda en cada ruta.

### Filas completas por clave primaria {#whole-rows-by-primary-key}

`pg_local_cache` almacena filas completas en la memoria compartida acotada de PostgreSQL. Un `MGET` RESP autenticado puede devolver una fila apta en caché; el SQL ordinario nunca consulta esta caché. Los fallos de caché y las lecturas no aptas usan la tabla de origen. El endpoint usa un único rol de base de datos configurado y no comparte la transacción SQL ni la instantánea del cliente. Consulte la [guía de lotes](batch-primary-key-lookups.md), la [referencia técnica](TECHNICAL.md) y la [guía de invalidación](cache-invalidation.md).

### Vistas y cachés externos {#views-and-external-caches}

Una [vista materializada](https://www.postgresql.org/docs/18/rules-materializedviews.html) de PostgreSQL almacena el resultado de una consulta y se actualiza cuando se solicita. Es adecuada para informes y agregados cuya frescura queda definida por el momento de actualización.

Redis es adecuado para objetos de aplicación compartidos entre procesos. La aplicación gestiona las claves, la serialización, los TTL y la invalidación. Consulte [PostgreSQL y Redis cache-aside](postgresql-redis-cache.md). Use la [guía de inicio rápido](QUICKSTART.md) para probar la caché de filas.
