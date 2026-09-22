---
layout: post
lang: es
translation_key: blog-cache-aside-late-fill
title: "Invalidación de caché de PostgreSQL: la carrera del llenado tardío"
description: Recorre una carrera de caché aparte en la que una lectura antigua vuelve a llenar una clave eliminada después del commit. Entiende las vallas de publicación, los snapshots, el rollback y las cachés locales de las solicitudes.
permalink: /es/blog/cache-aside-late-fill/
date: "2026-09-22"
last_modified_at: "2026-09-22"
topic: correctness
---

# Invalidación de caché y la carrera del llenado tardío {#cache-invalidation-and-the-late-fill-race}

«Eliminar la clave de caché después de actualizar la base de datos» deja un problema de tiempo: otra solicitud puede estar cargando ya el valor antiguo. La eliminación quita la entrada que existe ahora; no cancela un resultado que sigue en vuelo.

## Sigue las dos solicitudes {#two-requests}

Supón que PostgreSQL contiene la revisión 0 y que una aplicación usa lecturas con caché aparte. Esta secuencia puede ocurrir aunque el escritor invalide después del commit:

| Paso | Lector A | Escritor B |
|---|---|---|
| 1 | No encuentra la caché y lee la revisión 0 | |
| 2 | Se pausa antes de almacenar el resultado | Actualiza la fila a la revisión 1 |
| 3 | | Confirma y elimina la clave de caché |
| 4 | Publica la revisión 0 que había leído | |
| 5 | Una solicitud posterior lee el valor obsoleto almacenado en caché | |

Un TTL puede limitar cuánto tiempo ese valor sigue siendo elegible. No hace correcto el paso 4. Mover la eliminación antes del commit crea otro intervalo en el que un lector puede volver a poblar desde el estado antiguo confirmado de la base de datos. Consulta la [guía de PostgreSQL y Redis](../docs/postgresql-redis-cache.md) para conocer el límite entre la caché aparte de la aplicación y la caché de filas local de la base de datos.

## Valida la publicación además de la consulta {#publication}

Un llenado de caché necesita pruebas de que su resultado sigue siendo elegible cuando se publica. En `pg_local_cache` 2.0, las escrituras ponen vallas a las claves o relaciones afectadas; los llenados llevan información de generación y las entradas positivas almacenadas llevan información de visibilidad de la tupla. Una generación cambiada puede rechazar un llenado antiguo. Una lectura que no pueda usar una entrada de forma segura vuelve a PostgreSQL.

Estas comprobaciones forman parte de un recorrido de lectura específico de PostgreSQL. No convierten el endpoint RESP opcional en un servidor Redis de propósito general ni invalidan valores que la aplicación ya haya copiado en otro lugar.

## Prueba rollback y commit {#test-transactions}

Usa la [prueba con dos sesiones](../docs/cache-invalidation.md#test-with-two-sessions) en una base de datos desechable. Calienta la fila en una sesión. En otra, actualiza la fila mientras mantienes abierta la transacción. Comprueba tres observaciones:

1. El escritor puede leer su propio cambio mediante el recorrido de la tabla de origen.
2. La otra sesión sigue viendo el valor confirmado mientras la escritura está abierta.
3. Después de rollback, permanece el valor original; después de commit, una nueva sentencia bajo `READ COMMITTED` ve la nueva revisión.

La tercera condición se refiere a una nueva sentencia. Una sentencia que comenzó antes no tiene que adoptar un snapshot más nuevo a mitad de la ejecución. El [contrato de transacciones](../docs/TECHNICAL.md#transaction-consistency) también describe los modos que omiten la caché. Usa SQL normal para bloquear filas.

## Comprueba la siguiente caché de la aplicación {#application-cache}

Un DataLoader local de la solicitud aún puede conservar un valor que cargó antes de una mutación. La invalidación de la base de datos no puede eliminar ese objeto JavaScript. Borra o sustituye la entrada afectada del loader después de una mutación según la autorización y el contrato de resultados de la aplicación. Mantén los loaders limitados a las solicitudes.

La [guía de lotes](../docs/batch-primary-key-lookups.md#graphql-dataloader-and-n1-reads) separa la memorización de la solicitud de la caché de filas compartida. Al diagnosticar una respuesta obsoleta, sigue cada punto de almacenamiento desde el snapshot de la base de datos hasta el objeto de respuesta. La corrección en un límite no elimina los demás.
