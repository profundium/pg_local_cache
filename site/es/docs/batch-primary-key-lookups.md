---
layout: doc
lang: es
translation_key: batch-primary-key-lookups
title: Consultas por lotes de claves primarias de PostgreSQL
seo_title: "Lectura por lotes de filas PostgreSQL con RESP MGET"
description: "Aprende a evitar lecturas N+1 por clave primaria y a leer filas completas por lotes con RESP MGET autenticado."
section: Guías
permalink: /es/docs/batch-primary-key-lookups.html
last_modified_at: "2026-10-04"
---

# Consultas de PostgreSQL por lotes y clave primaria {#batch-postgresql-primary-key-lookups}

Esta guía explica cómo los lotes RESP `MGET` evitan una petición a la base de datos por clave y conservan las posiciones de los resultados solicitados.

## Evita las lecturas N+1 {#graphql-dataloader-and-n1-reads}

Si una aplicación obtiene una fila por ID, realiza N lecturas de origen después de la consulta inicial. Agrupa las claves primarias conocidas en un solo `MGET` para enviar una petición por lotes acotada. Es útil para repetir lecturas de filas completas; no almacena en caché SQL arbitrario ni sustituye las uniones o proyecciones.

## Contrato del resultado {#know-the-result-contract}

`MGET key [key ...]` devuelve un elemento del array por cada clave de entrada y mantiene el orden. Conserva los duplicados. Las filas ausentes producen elementos `nil`. Cada clave usa el formato `CRUD:<db>.<schema>.<table>:<json pk>`; la codificación y los ejemplos ejecutables están en [Clientes RESP](resp.md#key-and-response-contract).

## Cuándo encaja MGET {#when-mget-is-the-right-alternative}

Un comando admite hasta 1.024 claves. La respuesta codificada está limitada a 66.560 bytes, por lo que las filas grandes pueden exigir lotes menores aunque haya pocas claves. Divide los lotes según el número de claves y el tamaño previsto de la carga. Una respuesta demasiado grande produce un error, no un array parcial.

El SQL normal es mejor para filtros, uniones, bloqueos de filas, proyecciones o lecturas que deban compartir una transacción. La [guía de invalidación de caché](cache-invalidation.md) y la [referencia técnica](TECHNICAL.md#transaction-consistency) comparan el comportamiento de la consulta de origen y RESP.

La versión 3.0.0 eliminó la función SQL `local_cache.mget(regclass, anyarray)` de 2.x; consulta la [guía de actualización](UPGRADING.md).
