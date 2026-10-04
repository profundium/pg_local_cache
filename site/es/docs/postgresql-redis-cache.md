---
layout: doc
lang: es
translation_key: postgresql-redis-cache
title: PostgreSQL y Redis cache-aside
seo_title: "PostgreSQL y Redis cache-aside: invalidación y condiciones de carrera"
description: Compare Redis cache-aside gestionado por la aplicación con las lecturas RESP de pg_local_cache, incluidas las carreras por cargas obsoletas y la invalidación de escrituras.
section: Guías
permalink: /es/docs/postgresql-redis-cache.html
last_modified_at: "2026-10-04"
---

# PostgreSQL y Redis cache-aside {#postgresql-and-redis-cache-aside}

Esta guía explica Redis cache-aside con PostgreSQL como fuente de verdad, la carrera por cargas obsoletas y cómo `pg_local_cache` gestiona la invalidación de filas adjuntas.

Cuando se produce un fallo de caché en Redis, la aplicación lee la fila de PostgreSQL, la devuelve y la almacena con una clave de aplicación. Al escribir, confirma los datos de PostgreSQL y elimina la clave de Redis. Un TTL limita cuánto tiempo se conserva un valor, pero no demuestra que siga actualizado. Consulte la [guía de Redis sobre cache-aside](https://redis.io/docs/latest/develop/use-cases/cache-aside/).

## La carrera de invalidación {#the-invalidation-race}

Un lector puede cargar una fila antigua de PostgreSQL, detenerse y guardarla después de que un escritor confirme los cambios y elimine la clave. El siguiente lector ve datos obsoletos hasta que caduque la entrada o se vuelva a eliminar.

Entre las mitigaciones están rechazar cargas con una versión antigua de la base de datos, serializar las cargas por clave o publicar los cambios confirmados mediante un consumidor de outbox o CDC. Cada opción añade coordinación. La [guía de invalidación consciente de las transacciones](cache-invalidation.md) describe el límite equivalente para cargas tardías dentro de PostgreSQL.

## Dónde encaja pg_local_cache {#where-pg_local_cache-fits}

`pg_local_cache` almacena filas completas por clave primaria en la memoria compartida acotada de PostgreSQL. Las aplicaciones solicitan filas mediante `MGET` autenticado con RESP2; ni el SQL ordinario ni los resultados de consultas arbitrarias usan esta caché. Los triggers de tablas adjuntas ponen barreras a las escrituras, y las lecturas usan PostgreSQL cuando las comprobaciones de elegibilidad impiden un acierto de caché.

A diferencia de Redis cache-aside, esta ruta usa la ruta de escritura de la base de datos para invalidar y no usa TTL ni estructuras de datos generales de Redis. Los workers RESP usan el rol de PostgreSQL configurado en transacciones breves e independientes. Consulte [Clientes RESP](resp.md) y la [referencia técnica](TECHNICAL.md) para información de conexión y seguridad.

Use Redis para objetos de aplicación compartidos, frescura gestionada por TTL o estructuras de datos de Redis. Use `pg_local_cache` para lecturas repetidas de filas completas de una base de datos PostgreSQL. Si combina ambas capas, mantenga claves, invalidación y monitorización separadas.
