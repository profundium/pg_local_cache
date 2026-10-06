---
layout: doc
lang: es
translation_key: row-cache-vs-shared-buffers
title: Caché de filas de PostgreSQL frente a shared_buffers
seo_title: "Caché de filas de PostgreSQL frente a shared_buffers | pg_local_cache"
description: Compare la caché de páginas de PostgreSQL con la caché de filas completas de pg_local_cache: trabajo de origen evitado, costes de caché y cargas de trabajo que deberían seguir usando SQL ordinario.
section: Rutas de lectura
permalink: /es/docs/row-cache-vs-shared-buffers.html
last_modified_at: "2026-10-04"
---

# Caché de filas de PostgreSQL frente a shared_buffers {#postgresql-row-cache-vs-shared_buffers}

Esta guía compara la caché de páginas de PostgreSQL con la caché de filas completas de `pg_local_cache` y muestra qué trabajo conserva cada ruta de lectura.

[`shared_buffers`](https://www.postgresql.org/docs/18/runtime-config-resource.html#GUC-SHARED-BUFFERS) mantiene páginas de la base de datos en memoria. Una página caliente puede evitar E/S de almacenamiento, pero PostgreSQL sigue comprobando la visibilidad de las tuplas, ejecutando la consulta y construyendo el resultado. Un acierto apto de RESP `MGET` puede devolver una carga guardada de la fila completa tras validar la clave, la generación del fence y la carga útil.

Consulte la [referencia técnica de la ruta de lectura](TECHNICAL.md#read-path-and-safe-fallback).

## ¿PostgreSQL almacena en caché resultados de SELECT? {#does-postgresql-cache-select-results}

No. Las cachés de páginas de PostgreSQL almacenan páginas, no resultados finales de consultas. Una [sentencia preparada](https://www.postgresql.org/docs/18/sql-prepare.html) puede reutilizar el parseo y la planificación; PostgreSQL sigue ejecutándola. `pg_local_cache` ofrece caché de filas completas mediante RESP `MGET`, no caché de resultados arbitrarios de `SELECT`. Consulte [Clientes RESP](resp.md).

## Compare el trabajo, no solo el medio de almacenamiento {#compare-the-work-not-just-the-storage-medium}

| Ruta de lectura | Trabajo restante |
|---|---|
| SQL preparado por clave primaria sobre páginas calientes | Protocolo, ejecución de la consulta, comprobaciones de visibilidad y conversión del resultado |
| Acierto apto de RESP `MGET` | Protocolo, conversión de clave, sincronización de caché, comprobaciones de elegibilidad y devolución de la carga |
| Fallo de RESP o bypass | Comprobaciones de caché y lectura de la tabla de origen; las filas aptas pueden llenar la caché |

Un acierto evita repetir la ejecución sobre la tabla de origen y la serialización de la fila completa. Aun así, usa un worker de PostgreSQL y sincronización de caché. Las solicitudes RESP se ejecutan de forma independiente de la transacción SQL del cliente.

## Costes que debe incluir {#costs-to-include}

Las filas y el estado de las asignaciones consumen memoria compartida adicional. Las escrituras en tablas adjuntas ejecutan triggers de invalidación. Un conjunto de trabajo mayor que la capacidad de la caché puede aumentar los fallos y las expulsiones.

Mida en ambas rutas el mismo conjunto de claves, forma de fila, número de conexiones y mezcla de solicitudes. Evalúe SQL por lotes por separado de las lecturas de una sola fila; el batching por sí solo puede reducir los viajes de ida y vuelta sin caché.

## Cuándo dejar la aplicación como está {#when-to-leave-the-application-alone}

Mantenga el SQL ordinario si su latencia de extremo a extremo ya es aceptable, si la aplicación solo necesita una proyección o si predominan los joins, rangos y agregaciones. Use SQL para bloqueos de filas y lecturas que deban compartir una transacción.

## ¿Caché de filas o caché externa? {#row-cache-or-an-external-cache}

Use `pg_local_cache` cuando PostgreSQL siga siendo la fuente de verdad y se lean filas completas repetidamente por clave primaria. Use una caché externa para estado de aplicación con TTL, pub/sub, coordinación distribuida u objetos compartidos entre servicios. La [referencia técnica](TECHNICAL.md) describe el límite de seguridad de RESP.
