---
layout: doc
lang: es
translation_key: benchmarks-go
title: "Benchmarks de Go: SQL y RESP"
description: Resultados locales de pgx y RESP2 en Apple M3 Max, con CPU, memoria y escalado de conexiones de PostgreSQL.
section: Benchmarks
permalink: /es/docs/benchmarks-go.html
last_modified_at: "2026-09-16"
---

# Benchmarks de Go: SQL y RESP {#go-benchmarks-sql-and-resp}

[Resumen](BENCHMARKS.md) · [Node.js](benchmarks-node.md) · [Go y RESP](benchmarks-go.md)

Go 1.27.1 con pgx 5.11.0 y un cliente RESP2 de la biblioteca estándar; `GOMAXPROCS=8`.
[Máquina y método de medición](BENCHMARKS.md#test-environment).


Medido el 15 de septiembre de 2026 con la compilación de la extensión `f03ed22`.
El cliente Go se ejecuta dentro de la VM Linux, en un contenedor separado que comparte el espacio de nombres de red de PostgreSQL. La CPU y memoria del cliente se excluyen de los contadores de recursos del servidor. RESP usa ocho workers, un presupuesto de caché/buffer de 1 GiB y un límite de 512 clientes.

Mediana de **solicitudes/s**, tres muestras de cinco segundos por caso:

| Claves/solicitud | Conexiones | SQL preparado | SQL mget | RESP MGET |
|---:|---:|---:|---:|---:|
| 1 | 64 | 277,088 | 211,251 | 722,133 |
| 1 | 256 | 253,790 | 186,296 | 839,678 |
| 64 | 64 | 26,459 | 38,122 | 38,877 |
| 64 | 256 | 27,615 | 43,647 | 52,774 |

El SQL de 64 claves varió entre 19 y 28k solicitudes/s entre reinicios del servidor a pesar de que los datos, ajustes y planes de índice eran idénticos. La tabla usa la ejecución repetida más rápida; la causa de la variación sigue sin resolverse.
[Las 129 muestras](../../assets/benchmarks/2026-09-15-m3-max-resp.json) incluyen ambas ejecuciones, las revisiones exactas del código fuente, hashes de binarios y planes de consulta.

Las muestras de caché de solo lectura tuvieron un 100% de aciertos y ningún error ni rechazo por límite de conexiones. El arnés comprueba el margen de conexiones de SQL y RESP. Una prueba separada con 256 conexiones, 64 claves y 12 hilos Go no mejoró RESP; SQL `mget` ganó un 6% frente a ocho hilos.

### Recursos del servidor {#server-resources}

Con **256 conexiones**, medianas de las mismas muestras:

| Claves/solicitud | Recorrido | Núcleos de CPU del cliente | Núcleos de CPU del servidor (% de la VM) | µs del servidor/solicitud | Pico de MiB muestreado |
|---:|---|---:|---:|---:|---:|
| 1 | SQL | 3.97 | 8.94 (63.9%) | 36.3 | 679.8 |
| 1 | SQL mget | 3.36 | 9.93 (70.9%) | 54.4 | 684.2 |
| 1 | RESP MGET | 5.98 | 6.11 (43.7%) | 7.8 | 263.7 |
| 64 | SQL | 3.72 | 9.45 (67.5%) | 348.3 | 689.8 |
| 64 | SQL mget | 5.12 | 4.99 (35.6%) | 116.0 | 707.7 |
| 64 | RESP MGET | 5.76 | 4.97 (35.5%) | 95.8 | 266.7 |

### Cliente Go en macOS {#go-client-on-macos}

A través de los puertos publicados por Docker, con **64 conexiones**; medianas de tres muestras de cinco segundos, en solicitudes/s:

| Claves/solicitud, 64 conexiones | SQL preparado | SQL mget | RESP MGET |
|---:|---:|---:|---:|
| 1 | 49,194 | 48,010 | 52,426 |
| 64 | 14,324 | 15,751 | 16,240 |

Los casos de la VM y del host difieren tanto en el sistema operativo del cliente como en la ruta de red. Por tanto, los resultados mediante el puerto del host no pueden aislar el límite de rendimiento de PostgreSQL. RESP también tiene un contrato de sesión diferente: los workers usan un rol de base de datos configurado y no heredan la transacción ni el snapshot SQL de quien llama. Consulta la [referencia RESP](TECHNICAL.md#optional-resp2-endpoint).

## Reproduce {#reproduce}

<details markdown="1">
<summary>Ejecuta el benchmark de Go</summary>

Desde la raíz del repositorio, con Docker, Node.js 20+ y Go 1.25+:

```bash
./examples/benchmark.sh go > go.json
```

El script compila un servidor PostgreSQL desechable y el cliente Go, ejecuta cada caso tres veces, registra los recursos del servidor y después elimina sus contenedores. El cliente se ejecuta en la VM de Docker, en un cgroup separado de PostgreSQL. Valores predeterminados actuales: 4/64/256 conexiones, 1/16/64 claves y cinco segundos por muestra. Usa `all` para ejecutar Node.js y Go contra el mismo servidor con la [matriz común](BENCHMARKS.md#run-the-same-comparison-on-every-client). Para reproducir la ejecución registrada, usa las revisiones del JSON de mediciones.

Anulaciones opcionales: `CONNECTIONS`, `BATCHES`, `REPEATS`, `DURATION_SECONDS` y `GOMAXPROCS`. El JSON registra el ID de compilación de la extensión en ejecución.

</details>
