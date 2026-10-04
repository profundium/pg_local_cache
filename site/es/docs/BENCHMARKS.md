---
layout: doc
lang: es
translation_key: BENCHMARKS
title: Benchmarks de la caché de PostgreSQL
description: Resultados medidos de pg_local_cache con Node.js, Go y RESP en Apple M3 Max. Incluye la máquina, la CPU de PostgreSQL, la memoria y la metodología.
section: Benchmarks
permalink: /es/docs/BENCHMARKS.html
last_modified_at: "2026-09-16"
---

# Benchmarks de la caché de PostgreSQL {#postgresql-cache-benchmarks}

Registrados en un Apple M3 Max con PostgreSQL 16. Cada comparación usa el mismo cliente, conjunto de datos y resultados de filas descodificados para las lecturas en caché y SQL normales.

Los resultados publicados de SQL mget son datos históricos de 2.x; SQL mget se eliminó en 3.0.0. RESP MGET es la interfaz compatible para lecturas en caché.

## Dónde ayudó la caché y dónde no {#where-the-cache-helpedand-where-it-did-not}

Node.js y Go descodifican filas JSON de RESP; esos costes del cliente se incluyen en el tiempo medido. Las comparaciones históricas de SQL mget se identifican como datos de 2.x.

Las [mediciones de Node.js](benchmarks-node.md) usan un cliente macOS y un servidor Docker; las [mediciones de Go y RESP](benchmarks-go.md) ejecutan ambos dentro de la VM de Docker. Cada página enlaza repeticiones brutas, versiones exactas y costes de recursos del servidor. Estas configuraciones separadas no clasifican los lenguajes. Para ver ejemplos de conexiones, consulta [Node.js](node-postgres.md), [Go](go.md) o [RESP](resp.md).

## Ejecuta la misma comparación en cada cliente {#run-the-same-comparison-on-every-client}

Desde la raíz del repositorio, con Docker, Node.js 20+ y Go 1.25+:

```bash
./examples/benchmark.sh all > comparison.json
python3 scripts/benchmark_report.py comparison.json
```

El ejecutor compila un servidor PostgreSQL desechable y ejecuta esta matriz común:

Node.js y Go descodifican filas JSON de RESP; esos costes del cliente se incluyen en el tiempo medido. Las comparaciones históricas de SQL mget se identifican como datos de 2.x.

Hay 162 muestras de forma predeterminada, unos 14 minutos de trabajo medido más la configuración. El script elimina sus contenedores tras el éxito o el fallo. Para una ejecución corta de corrección:

```bash
CONNECTIONS=4 BATCHES=1,16,64 REPEATS=1 DURATION_SECONDS=1 \
  ./examples/benchmark.sh all > smoke.json
```

Node.js y Go descodifican filas JSON de RESP; esos costes del cliente se incluyen en el tiempo medido. Las comparaciones históricas de SQL mget se identifican como datos de 2.x.

Las cargas idénticas no hacen intercambiables los protocolos: los workers RESP usan su rol de base de datos configurado y no se unen a la transacción ni al snapshot SQL de quien llama. Consulta el [contrato RESP](TECHNICAL.md#optional-resp2-endpoint). La comparación común mide lecturas calientes. Los diagnósticos de llenado en frío, lecturas/escrituras mixtas y sobrecarga de escritura siguen separados en `node-workload`.

Los resultados publicados del 14–15 de septiembre que siguen son anteriores a este ejecutor común. Sus entornos originales y revisiones del código fuente siguen adjuntos a los datos; no son mediciones nuevas de la matriz unificada.

## Entorno de prueba {#test-environment}


| Componente | Configuración |
|---|---|
| Host | MacBook Pro `Mac15,10`, Apple M3 Max: 10 núcleos de rendimiento + 4 de eficiencia, 36 GiB de RAM |
| SO | macOS 26.5.2, compilación `25F84`, arm64 |
| VM de Docker | Engine 29.7.2, Linux `7.0.12-linuxkit`, 14 CPU, 7.65 GiB de RAM; sin cuota de CPU o RAM del contenedor |
| PostgreSQL | 16.15, Debian bookworm; 300 conexiones, 128 MiB de buffers compartidos, 256 MiB de `/dev/shm` |
| Datos | 4.096 filas, valores de 128 bytes; 1.024 entradas de caché; datos y WAL en tmpfs |

El cliente y el servidor comparten las CPU del Mac con otros diez contenedores de desarrollo. Todos los clientes codifican las solicitudes y descodifican filas JSON completas, conservando el orden de entrada, los duplicados y las posiciones ausentes. SQL usa sentencias preparadas; las conexiones, la autenticación y el calentamiento quedan fuera de la medición. No hay TLS ni pipelining.

## Método de medición {#measurement-method}


Cada conexión espera su respuesta antes de enviar otra solicitud: una carga de trabajo de **bucle cerrado**, sin corrección por omisión coordinada. El orden de las consultas rota entre repeticiones; las solicitudes en vuelo terminan antes de detener la medición. Las comparaciones de solo lectura usan claves fijas ya presentes en la caché. Los planes SQL registrados usan `items_pkey`, con cero lecturas de bloques compartidos.

La CPU del servidor procede de los contadores cgroup del contenedor de PostgreSQL. Un núcleo significa un segundo de CPU por segundo transcurrido; los porcentajes de capacidad se dividen por 14. Los µs de CPU por solicitud dividen el tiempo de CPU del servidor entre las solicitudes completadas. La ventana de muestreo incluye el trabajo de monitorización y la breve pausa de informe del cliente.

La memoria es `memory.current` del cgroup, muestreada cada 500 ms y en los endpoints. Las tablas informan de la mediana del pico muestreado de cada repetición, incluida la memoria compartida, tmpfs y la caché de páginas; no es RSS del proceso. Los archivos JSON también contienen E/S de bloques, limitación, eventos de memoria y snapshots de estado/espera SQL. Los contadores de red excluyen loopback y, por tanto, omiten el tráfico del cliente de la VM.

Estas ejecuciones cortas con caché caliente en un portátil compartido no son estimaciones de capacidad de producción. Los datos y WAL usan tmpfs con `fsync`, `full_page_writes` y `synchronous_commit` habilitados; el rendimiento del disco no se ha probado.

Una ejecución fallida termina con código distinto de cero y conserva las muestras completadas; el renderizador Markdown rechaza resultados parciales. Para conocer el código registrado exacto, usa `harness_ref` y `extension_ref` de cada JSON. Los comandos de reproducción están en las páginas de clientes; los resultados se escriben en archivos JSON como `benchmark.json`.
