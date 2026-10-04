---
layout: doc
lang: es
translation_key: benchmarks-node
title: Benchmarks de Node.js
description: "Resultados locales de node-postgres en Apple M3 Max: lecturas por lotes, actualizaciones concurrentes y CPU y memoria de PostgreSQL."
section: Benchmarks
permalink: /es/docs/benchmarks-node.html
last_modified_at: "2026-09-16"
---

# Benchmarks de Node.js {#nodejs-benchmarks}

[Resumen](BENCHMARKS.md) · [Node.js](benchmarks-node.md) · [Go y RESP](benchmarks-go.md)

Node.js 24.18.0 con node-postgres 8.16.3.
[Máquina y método de medición](BENCHMARKS.md#test-environment).


Medido el 14 de septiembre de 2026 con la compilación de la extensión `67e5754`, un presupuesto de caché de 384 MiB y clientes en macOS mediante el puerto SQL publicado por Docker.

Con **64 claves por solicitud**, mediana de solicitudes/s de tres muestras de 10 segundos:

La ruta SQL `mget` y todos los resultados siguientes son mediciones históricas de 2.x. SQL `mget` se eliminó en 3.0.0; la interfaz compatible para lecturas en caché es RESP `MGET`.

| Conexiones | SQL preparado | SQL mget (2.x; eliminado en 3.0.0)|
|---:|---:|---:|
| 4 | 7,023 | 7,528 |
| 64 | 15,577 | 16,616 |
| 256 | 15,459 | 16,574 |

Con 64 conexiones, `mget` usó **170 µs de CPU del servidor por solicitud**, frente a 286 µs para SQL. La CPU del servidor promedió 2.80 frente a 4.43 núcleos; la memoria máxima muestreada fue 215.0 frente a 205.5 MiB. La CPU del cliente fue 0.84 frente a 0.87 núcleos.

Para **una clave** con 64 conexiones, SQL fue más rápido: 55,409 frente a 53,646 solicitudes/s. El resultado por lotes no se aplica a las lecturas de una sola clave.

[Mediciones brutas](../../docs/benchmarks/2026-09-14-m3-max-clients.json) incluyen percentiles de latencia, muestras de recursos y revisiones del código fuente.

### Lecturas mezcladas con escrituras {#reads-mixed-with-writes}

El ejecutor de aplicaciones de Node.js usa **64 conexiones**, **50.000 solicitudes por muestra** y tres repeticiones. Las lecturas recorren 128 filas activas; el 5% de las operaciones de la carga mixta actualiza filas.

| Carga de trabajo | Claves/solicitud | Solicitudes/s de SQL preparado | Solicitudes/s de mget JSON (2.x; eliminado en 3.0.0)|
|---|---:|---:|---:|
| Lecturas calientes | 1 | 56,104 (52,246–56,364) | 52,842 (52,494–53,399) |
| Lecturas calientes | 16 | 37,391 (36,996–37,938) | 38,425 (38,340–38,617) |
| Lecturas calientes | 64 | 15,178 (15,109–15,488) | 16,294 (16,131–16,368) |
| 5% de actualizaciones | 1 | 55,768 (54,273–56,527) | 52,621 (51,432–53,216) |
| 5% de actualizaciones | 16 | 38,669 (38,429–38,718) | 37,604 (37,481–37,916) |
| 5% de actualizaciones | 64 | 16,049 (16,008–16,063) | 16,814 (16,771–17,229) |

Los valores son solicitudes/s medianas con mínimo–máximo entre paréntesis. Las muestras mixtas cuentan juntas las lecturas y escrituras. `application_run` del JSON incluye casos de llenado en frío y sobrecarga de escritura. El llenado en frío con lote 64 solo tiene 64 observaciones de latencia, demasiado pocas para una estimación p99 útil.

## Configuración histórica del benchmark SQL {#query-setup}

La consulta SQL `mget` histórica se conserva en los datos brutos enlazados para reproducibilidad. No está disponible en 3.0.0. El benchmark actual compara SQL preparado con RESP `MGET`; consulta el [ejemplo de Node.js](resp.md#nodejs).

## Reproduce {#reproduce}

<details markdown="1">
<summary>Ejecuta los benchmarks de Node.js</summary>

Desde la raíz del repositorio, con Docker y Node.js 20+:

```bash
./examples/benchmark.sh node > node.json
```

Valores predeterminados actuales: 4/64/256 conexiones, 1/16/64 claves y tres muestras de cinco segundos por caso. Node.js ejecuta SQL preparado y RESP `MGET` dentro de la máquina virtual de Docker. El script crea un servidor y un contenedor cliente desechables, registra recursos y elimina ambos.

Para lecturas mezcladas con escrituras:

```bash
BATCHES=1,16,64 ./examples/benchmark.sh node-workload > benchmark.json
python3 scripts/benchmark_report.py benchmark.json
```

Valores predeterminados: 64 conexiones, 50.000 solicitudes por muestra y tres repeticiones. El ejecutor restablece sus tablas de demo entre muestras. `CLIENTS`, `REQUESTS`, `BATCHES` y `REPEATS` son anulaciones opcionales.

</details>
