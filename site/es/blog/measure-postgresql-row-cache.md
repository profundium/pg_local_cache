---
layout: post
lang: es
translation_key: blog-measure-postgresql-row-cache
title: "Cuándo ayuda una caché de filas de PostgreSQL: mide toda la lectura"
description: Diseña una comparación justa de cachés de filas de PostgreSQL con SQL preparado, SQL mget y RESP MGET. Separa lecturas calientes, fallos, tamaños de lote, escrituras y costes del cliente.
permalink: /es/blog/measure-postgresql-row-cache/
date: "2026-09-22"
last_modified_at: "2026-09-22"
topic: performance
---

# Cuándo ayuda una caché de filas de PostgreSQL {#when-a-postgresql-row-cache-helps}

Una base de datos puede servir cada página desde la memoria y aun así dedicar tiempo a ejecutar consultas, comprobar la visibilidad y construir resultados. Una caché de filas intenta evitar parte de ese trabajo repetido. También añade gestión de claves, comprobaciones de caché y costes de serialización. La pregunta útil es si la solicitud completa de la aplicación resulta más barata para tu carga de trabajo.

`pg_local_cache` expone una API explícita `local_cache.mget`. Las consultas `SELECT` normales conservan su recorrido de ejecución habitual de PostgreSQL. Por tanto, una caché caliente de `shared_buffers` y una caché caliente de filas son condiciones experimentales distintas.

## Escribe primero el contrato del resultado {#result-contract}

Compara las mismas claves, columnas y forma de salida. Si la aplicación solo necesita dos columnas, comparar esa proyección SQL con filas completas serializadas mide trabajos distintos. Si quien llama espera duplicados, orden de entrada y un resultado nulo por cada clave ausente, incluye ese trabajo de alineación en cada cliente.

La [guía de consultas por lotes](../docs/batch-primary-key-lookups.md) ofrece una línea base `ANY` y otra ordenada con `WITH ORDINALITY`. Ninguna necesita la extensión. Establece la línea base SQL antes de añadir una caché.

## Cambia una dimensión de la carga cada vez {#workload-dimensions}

| Experimento | Qué mantener fijo | Qué revela |
|---|---|---|
| Lecturas repetidas calientes | Claves, forma del resultado, conexiones | Reutilización de entradas ya pobladas |
| Claves frías o ausentes | Distribución de solicitudes y tamaño de lote | Costes de la tabla de origen y del resultado negativo |
| Lotes más grandes | Total de claves solicitadas y forma de la carga | Ahorro de viajes de ida y vuelta frente al trabajo por clave |
| Escrituras concurrentes | Mezcla de lectura/escritura y límites de las transacciones | Costes de invalidación, relleno y visibilidad |
| Filas más anchas | Distribución de claves y ubicación del cliente | Serialización, transporte y omisión por tamaño de fila |

Una lectura no elegible puede usar legítimamente la tabla de origen. Inspecciona los deltas de los contadores alrededor de cada experimento; una tasa de aciertos baja por sí sola no diagnostica una instalación rota. Mantén separados los contadores de SQL y RESP. La [referencia técnica](../docs/TECHNICAL.md#health-and-monitoring) describe `local_cache.stats()` y `local_cache.health()`.

## Usa el ejecutor compartido y después inspecciona las pruebas {#shared-runner}

Después del [quickstart](../docs/QUICKSTART.md), ejecuta la comparación del repositorio:

```bash
./examples/benchmark.sh all > comparison.json
python3 scripts/benchmark_report.py comparison.json
```

La [guía de benchmarks](../docs/BENCHMARKS.md) enumera los requisitos previos, los controles de carga y las métricas. Conserva el JSON bruto. Registra las revisiones de la extensión y del arnés, la versión de PostgreSQL, la máquina, la cantidad de conexiones y la ubicación del cliente. Compara ejecuciones repetidas, distribuciones de latencia y uso de recursos del servidor junto al rendimiento. Una prueba corta de corrección no es un resultado de velocidad publicable.

## Decide desde el límite de la aplicación {#application-boundary}

SQL `mget` y RESP `MGET` usan transportes y gestión de resultados diferentes. Una mejora en uno no demuestra una mejora en el otro. Las [mediciones fechadas de Go](../docs/benchmarks-go.md) incluyen un caso de una sola clave en el que SQL `mget` fue más lento que SQL preparado. Es una razón para probar, no una predicción universal.

Conserva el SQL habitual cuando se necesiten uniones, proyecciones, bloqueos o formas de tabla no compatibles, o cuando la caché no aporte un beneficio medido. Para lecturas repetidas de filas completas por clave primaria, prueba la API explícita con el mismo trabajo de cliente que realiza realmente tu aplicación. Continúa con la [guía para decidir la caché](../docs/postgresql-caching.md) y el [experimento de invalidación](../docs/cache-invalidation.md).
