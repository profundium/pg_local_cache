---
layout: post
lang: es
translation_key: blog-row-cache-inside-postgresql
title: "Cache-aside sin carreras: caché de filas dentro de PostgreSQL, medido"
description: pg_local_cache 3.1.0 guarda filas frecuentes en memoria compartida de PostgreSQL y las sirve por RESP. Comparación con Valkey y SQL preparado, sin filas obsoletas.
permalink: /es/blog/row-cache-inside-postgresql/
date: "2026-10-12"
last_modified_at: "2026-10-12"
topic: performance
---

# Cache-aside sin carreras {#cache-aside-without-the-race}

Cuando aumentan las lecturas por clave primaria, lo habitual es poner Redis o
Valkey junto a PostgreSQL y usar cache-aside: primero se consulta la caché; si
no hay resultado, se lee PostgreSQL y se rellena la caché; después de cada
escritura se elimina la clave. Funciona hasta que una lectura coincide con una
escritura. Un lector que no encontró la clave puede obtener la fila justo antes
de que el escritor confirme; primero llega el `DEL` del escritor y después el
relleno del lector, de modo que la fila antigua permanece en caché hasta que
algo la elimine. Ya explicamos esta [carrera de llenado tardío](cache-aside-late-fill.md).

pg_local_cache lleva la caché al propio PostgreSQL, por lo que la caché y las
escrituras que debe seguir viven dentro del mismo sistema de transacciones.

## Cómo mantiene la corrección {#how-it-stays-correct}

- Las filas de una tabla conectada se guardan en memoria compartida acotada,
  indexadas por clave primaria y servidas por procesos en segundo plano mediante
  RESP2. Cualquier cliente Redis puede leerlas con `MGET`; la clave tiene el
  formato `CRUD:<db>.<schema>.<table>:<json primary key>`.
- Los triggers de fila publican una barrera de invalidación para cada clave
  escrita antes de que la transacción sea visible. Los cambios que afectan a
  toda la relación, como `TRUNCATE`, publican una barrera para toda la tabla.
- Ante una ausencia en caché, el lector obtiene primero un token de lectura. El
  resultado solo se guarda si, entre ese momento y el llenado, no se publicó una
  barrera para la clave, la tabla o toda la caché. Si el llenado coincide con
  una escritura, se descarta.
- Las ausencias simultáneas de una misma clave se agrupan: un lector carga la
  fila y los demás usan su resultado.
- PostgreSQL sigue siendo la fuente de verdad. La caché es temporal y se puede
  descartar en cualquier momento.

## Costes y beneficios {#results}

Estas cifras corresponden a la versión 3.1.0 en dos máquinas virtuales de
16 vCPU (Xeon Gold 6354, 10 GbE), con PostgreSQL 16 y un cliente Go/pgx. La
[página de benchmarks](../docs/BENCHMARKS.md) incluye todas las tablas, los
JSONL originales y los pasos para reproducirlos.

**Lecturas mientras se escribe.** 64 lectores, una clave por lectura,
distribución uniforme entre 60k filas y 30,000 `UPDATE`/s:

| Sistema | Lecturas/s | Proporción de aciertos | Entradas obsoletas |
| --- | --- | --- | --- |
| pg_local_cache | 161,919 | 0.837 | 0 |
| Valkey cache-aside | 42,294 | 0.577 | 0 |
| SQL preparado | 106,949 | — | — |

Con escritores sin límite, Valkey cache-aside terminó con entradas obsoletas
(1 en la ejecución mixta; 2 y 1 en las pruebas de 120 segundos).
pg_local_cache terminó todas las ejecuciones sin ninguna, incluida una prueba
de una hora con 252M `MGET` y 72M `UPDATE`.

**Sobrecoste de escritura.** Frente a escrituras simples en la tabla, el
rendimiento de `UPDATE` mediante pg_local_cache fue un 3.6–8.2% menor; el de
`UPDATE` seguido de `DEL` en Valkey, un 28.9–63.7% menor.

**Rendimiento de lectura.** Con el servidor limitado a un par de vCPU,
pg_local_cache atendió 210k lecturas/s frente a 180k de Valkey con un solo hilo;
con dos pares, 337k frente a 266k de Valkey con hilos de E/S. Con cuatro pares,
ambos llegaron a un límite impuesto por el cliente, cerca de 360k; pg_local_cache
usó 5.2 vCPU del servidor y Valkey 8.0. Valkey con un solo hilo fue el que menos
CPU consumió por solicitud. SQL preparado quedó muy por detrás en las pruebas
de una sola clave.

## Cuándo no es la herramienta adecuada {#when-not-to-use-it}

- No sustituye a Redis: no ofrece TTL, pub/sub ni estructuras de datos; solo
  lecturas de filas completas por clave primaria.
- El conjunto de datos frecuente debe caber en la memoria configurada y cada
  fila está limitada a 64 KiB de JSON por RESP.
- Atiende una base de datos en un único servidor primario que acepta escrituras.
- Requiere `shared_preload_libraries` y reiniciar el servidor.

## Pruébalo {#try-it}

pg_local_cache admite PostgreSQL 14–18 y usa la licencia MIT. Instálalo desde
[GitHub Releases](https://github.com/profundium/pg_local_cache/releases) (deb y
rpm), [PGXN](https://pgxn.org/dist/pg_local_cache/) o los repositorios de Pigsty;
después sigue la [guía de inicio rápido](../docs/QUICKSTART.md).
