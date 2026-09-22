---
layout: doc
lang: es
translation_key: row-cache-vs-shared-buffers
title: Caché de filas de PostgreSQL frente a shared_buffers
seo_title: "Caché de filas de PostgreSQL frente a shared_buffers | pg_local_cache"
description: Compara la caché de páginas de PostgreSQL con la caché de filas completas de pg_local_cache 2.0. Descubre qué evita un acierto de la caché de filas, qué costes conserva y cuándo no añadir otra caché.
section: Recorridos de lectura
permalink: /es/docs/row-cache-vs-shared-buffers.html
last_modified_at: "2026-09-16"
---

# Caché de filas de PostgreSQL frente a shared_buffers {#postgresql-row-cache-vs-shared_buffers}

El [`shared_buffers`](https://www.postgresql.org/docs/16/runtime-config-resource.html#GUC-SHARED-BUFFERS) de PostgreSQL contiene páginas de la base de datos. pg_local_cache almacena por separado cargas serializadas de filas completas bajo sus claves primarias completas. Una página ya en memoria puede evitar una lectura de almacenamiento, pero una consulta aún debe producir un resultado a partir de las tuplas de la base de datos. Un acierto de la caché de filas puede devolver la carga almacenada después de las comprobaciones de elegibilidad y snapshot.

El sistema operativo también puede almacenar en caché el contenido de los archivos. Usa una base de datos caliente como referencia.

{% include diagrams/read-path.html id="buffers-read" %}

## ¿Almacena PostgreSQL los resultados de SELECT? {#does-postgresql-cache-select-results}

`shared_buffers` almacena en caché las páginas que usa una consulta, no su conjunto de resultados final. Una [sentencia preparada](https://www.postgresql.org/docs/18/sql-prepare.html) reutiliza el trabajo de parseo y puede reutilizar un plan, pero PostgreSQL aún la ejecuta. pg_local_cache añade caché de filas completas mediante llamadas explícitas a `mget`; no almacena en caché resultados arbitrarios de SELECT ni reescribe consultas existentes. El [ejemplo de Node.js](node-postgres.md) muestra las dos API de lectura una al lado de la otra.

## Compara el trabajo, no solo el medio de almacenamiento {#compare-the-work-not-just-the-storage-medium}

| Lectura | Trabajo restante |
|---|---|
| SQL preparado por clave primaria sobre páginas calientes | Gestión del protocolo, ejecución del plan, comprobaciones de visibilidad de filas y conversión del resultado |
| Acierto elegible de la caché SQL mget | Gestión del protocolo, ejecución de la función SQL, conversión de claves, sincronización de caché, comprobaciones de snapshot y devolución de la carga almacenada |
| Fallo u omisión de SQL mget | Las comprobaciones de la función más una consulta a la tabla de origen; un llenado elegible correcto puede poblar la caché |

Un acierto de la caché de filas evita la ejecución repetida de la tabla de origen y la serialización de la fila completa. Las comprobaciones y la sincronización de la caché también consumen CPU, y un acierto sigue usando una conexión y un backend de PostgreSQL. Esta API SQL no elimina los límites de conexiones ni las colas del pool de conexiones.

## Costes que debes incluir {#costs-to-include}

Una fila almacenada en caché usa memoria compartida adicional aunque su página de origen ya esté en memoria. La extensión también mantiene el estado de mapeo e invalidación. Las actualizaciones de tablas asociadas ejecutan los triggers de la extensión. Cuando el conjunto de trabajo supera la capacidad, una aparente optimización de lectura puede convertirse sobre todo en sobrecarga de fallos y expulsiones.

La demo predeterminada compara deliberadamente un conjunto activo de 128 filas con 1.024 huecos de caché y después una primera pasada por 4.096 filas. La [guía de benchmarks](BENCHMARKS.md) explica ambos casos y mide por separado las escrituras en tablas asociadas.

## Cuándo dejar la aplicación como está {#when-to-leave-the-application-alone}

Conserva la consulta existente cuando su latencia de extremo a extremo ya sea aceptable, cuando la aplicación solo necesite una pequeña proyección de una fila grande o cuando dominen las uniones, los rangos y las agregaciones. Primero compara una consulta normal por lotes con las llamadas actuales por clave. Una mejora por agrupar consultas no demuestra una mejora por almacenar en caché.

pg_local_cache 2.0 requiere llamadas explícitas a `mget`, instalar la extensión y precargarla al iniciar. Rechaza tablas con RLS, particionadas o heredadas.

## ¿Caché de filas o caché externa? {#row-cache-or-an-external-cache}

Para datos cuya autoridad sigue siendo PostgreSQL, este diseño mantiene la invalidación en el recorrido de escritura de la base de datos y evita mantener un protocolo de caché aparte en la aplicación. No ofrece semántica general de Redis. El endpoint RESP2 opcional tiene un conjunto limitado de comandos y un modelo de seguridad separado.

Una caché de filas de PostgreSQL no puede sustituir el estado de aplicación basado en TTL, pub/sub ni la coordinación distribuida. Consulta el [contrato técnico](TECHNICAL.md) y los [ejemplos de transacciones](cache-invalidation.md).
