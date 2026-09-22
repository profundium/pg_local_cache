---
layout: doc
lang: es
translation_key: postgresql-redis-cache
title: Caché aparte de PostgreSQL y Redis
seo_title: "Caché aparte de PostgreSQL y Redis: invalidación y condiciones de carrera"
description: Usa PostgreSQL como fuente de verdad con un recorrido de caché aparte de Redis, entiende las carreras de lecturas obsoletas y descubre dónde encaja pg_local_cache.
section: Guías
permalink: /es/docs/postgresql-redis-cache.html
last_modified_at: "2026-09-16"
---

# Caché aparte de PostgreSQL y Redis {#postgresql-and-redis-cache-aside}

La caché aparte de Redis sitúa la aplicación entre una lectura y su almacenamiento autoritativo. En un fallo, lee PostgreSQL, devuelve ese valor y lo escribe en Redis; en una escritura, actualiza PostgreSQL e invalida la clave de Redis correspondiente. La [guía del patrón de Redis](https://redis.io/docs/latest/develop/use-cases/cache-aside/) describe este flujo, pero no hace que una caché de aplicación sea coherente transaccionalmente con PostgreSQL.

Para una fila identificada por `public.items.id`, el esquema es:

```text
GET item:42
miss -> SELECT * FROM public.items WHERE id = $1
     -> SET item:42 <serialized row> EX <ttl>
write -> UPDATE public.items ...
      -> COMMIT
      -> DEL item:42
```

Usa SQL parametrizado y un espacio de nombres para las claves. Un TTL limita cuánto tiempo permanece un valor almacenado en Redis; no demuestra que esté actualizado respecto a un commit de PostgreSQL. La eliminación explícita gestiona las escrituras habituales, pero no elimina todas las carreras.

## La carrera de invalidación {#the-invalidation-race}

Considera dos solicitudes. El lector R1 no encuentra la clave en Redis y lee la fila antigua de PostgreSQL. El escritor W confirma una fila nueva y elimina `item:42`. Después R1 continúa y almacena su valor antiguo en Redis. El siguiente lector ve datos obsoletos hasta que esa clave caduca o una escritura posterior la elimina.

Entre las posibles mitigaciones están volver a eliminar después de que termine un loader, almacenar una versión de la base de datos y rechazar valores antiguos, serializar las cargas por clave o publicar los cambios confirmados mediante un outbox o un consumidor CDC. Cada una añade coordinación y casos de fallo. La [guía de invalidación de caché](cache-invalidation.md) demuestra el problema análogo del llenado tardío dentro de PostgreSQL.

## Dónde encaja pg_local_cache {#where-pg_local_cache-fits}

`pg_local_cache` es una opción más acotada y local a PostgreSQL para filas completas identificadas por clave primaria. `local_cache.mget` es explícito; un `SELECT` normal y una forma de consulta arbitraria nunca leen la caché. Los triggers de tablas asociadas ponen vallas a las claves o relaciones afectadas en el recorrido de escritura de la base de datos, y las lecturas elegibles pueden volver a PostgreSQL cuando las reglas de transacción o snapshot impiden un acierto. Empieza por la [guía de consultas por lotes](batch-primary-key-lookups.md) y el [contrato técnico](TECHNICAL.md).

Esta extensión no ofrece compatibilidad general con Redis, TTL de Redis ni un protocolo distribuido de caché de aplicaciones. Su endpoint RESP2 opcional expone un conjunto limitado de comandos autenticados sobre los mismos mapeos y tiene su propio modelo de seguridad; no tiene TLS. Úsala cuando el problema sean las lecturas de filas completas locales a PostgreSQL y conscientes de las transacciones. Usa Redis cuando varias instancias de la aplicación necesiten objetos compartidos, frescura basada en TTL o estructuras de datos de Redis. Combinar ambos requiere claves, invalidación y métricas separadas para cada capa.

Ejecuta el [quickstart](QUICKSTART.md), compara con la consulta normal del cliente en el [ejemplo de node-postgres](node-postgres.md) e inspecciona los contadores separados de SQL y RESP. La [guía de decisión sobre caché](postgresql-caching.md) enumera las demás opciones de PostgreSQL.
