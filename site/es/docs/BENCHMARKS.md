---
layout: doc
lang: es
translation_key: BENCHMARKS
title: Benchmarks de caché PostgreSQL, 3.1.0
description: "Resultados de pg_local_cache 3.1.0 en dos máquinas: lecturas fijadas y completas, escrituras, lecturas durante escrituras y metodología."
section: Benchmarks
permalink: /es/docs/BENCHMARKS.html
redirect_from:
  - /es/docs/benchmarks-go.html
  - /es/docs/benchmarks-node.html
  - /es/docs/benchmarks-go/
  - /es/docs/benchmarks-node/
last_modified_at: "2026-10-07"
---
# Benchmarks de caché PostgreSQL: 3.1.0

Estas mediciones comparan pg_local_cache RESP MGET, Valkey y SQL preparado en dos VM conectadas por una red privada. Todos los resultados corresponden a la compilación final 3.1.0 c431bcc. Se muestran medianas de ejecuciones repetidas para las matrices de lectura y las pruebas de escritura; los dos casos fijados de un núcleo con Valkey io-threads=1 tienen dos repeticiones y los demás casos fijados, cinco. Las pruebas mixtas, las sondas de 120 segundos y el soak de una hora son ejecuciones únicas. Estas muestras no prometen capacidad de producción.

## Qué muestran las pruebas de la compilación final

- Por núcleo fijado, la mejor configuración medida de Valkey fue io-threads=1 con un núcleo (180k lecturas/s frente a 210k de pg_local_cache); con dos núcleos fue io-threads=4 (266k frente a 337k). Con cuatro núcleos, el rendimiento llegó a un límite del cliente de unos 361k frente a 357k; pg_local_cache usó 5,24 frente a 7,95 vCPU del servidor. Valkey io-threads=1 usó menos CPU de servidor por solicitud. SQL preparado quedó muy por detrás en las pruebas fijadas de una clave.
- Con Valkey io-threads=8, las diferencias observadas entre medianas de MGET 16/64 estuvieron entre aproximadamente −2,15 % y +1,26 %; los rangos no se solaparon en ninguna de las cuatro comparaciones de 64 claves. Estas muestras no demuestran equivalencia estadística. Las MGET grandes quedaron limitadas por cliente/red. En una fila, p99 fue mayor para pg_local_cache con claves aleatorias, MGET 64 y 256 clientes (43,52 frente a 40,37 ms).
- El coste frente a transacciones de tabla normal fue 3,6–8,2% para UPDATE y 3,7–5,5% para INSERT. UPDATE normal seguido de Valkey DEL quedó 28,9–63,7% por debajo del rendimiento de UPDATE normal.
- Durante las escrituras no se encontraron entradas obsoletas de pg_local_cache. Tras 120 segundos, Valkey cache-aside dejó 2 entradas obsoletas en Uniform y 1 en Zipf. La prueba de una hora completó 252 M MGET y 72 M UPDATE; el crecimiento de RssAnon no superó 44 kB por worker.

## Lecturas por par de vCPU fijado del servidor

El cliente Go/pgx usó 256 clientes y una clave por MGET. Cada caso duró 20 segundos. Los dos casos fijados de un par con Valkey io-threads=1 se repitieron dos veces; los demás casos fijados, cinco. PostgreSQL y Valkey se fijaron a N pares de vCPU (2N vCPU). Valkey usó io-threads=2N e io-threads=1; SQL preparado usó las mismas claves.

| Pares de vCPU | Claves | pg_local_cache solicitudes/s (p99 ms, vCPU) | Valkey io-threads=2N (solicitudes/s, p99 ms, vCPU) | Valkey io-threads=1 (solicitudes/s, p99 ms, vCPU) | SQL preparado (solicitudes/s, p99 ms, vCPU) |
| --- | --- | --- | --- | --- | --- |
| 1 | Calientes | 209,696 (2.46, 1.99) | 91,478 (3.24, 2.00) | 180,141 (2.59, 0.85) | 23,200 (24.90, 2.00) |
| 1 | 100k aleatorias | 195,950 (2.65, 2.00) | 88,672 (3.38, 2.00) | 175,853 (2.65, 0.92) | 22,433 (25.95, 2.00) |
| 2 | Calientes | 336,691 (2.02, 3.78) | 266,340 (1.69, 4.00) | 162,230 (2.85, 0.66) | 46,386 (16.38, 4.00) |
| 2 | 100k aleatorias | 326,125 (1.98, 3.83) | 267,826 (1.72, 4.00) | 160,220 (2.92, 0.69) | 45,211 (17.56, 4.00) |
| 4 | Calientes | 360,968 (2.33, 5.24) | 357,161 (2.20, 7.95) | 160,633 (2.85, 0.66) | 88,604 (6.75, 8.00) |
| 4 | 100k aleatorias | 353,941 (2.33, 5.27) | 351,344 (2.20, 7.95) | 159,414 (2.92, 0.68) | 86,790 (6.88, 8.00) |

Con un núcleo, Valkey de un hilo fue su mejor configuración. Con dos núcleos, io-threads=4 fue la mejor; Valkey tuvo p99 menor pese a menor rendimiento. Con cuatro núcleos, pg_local_cache e io-threads=8 alcanzaron un rendimiento casi igual, limitado por el cliente, y pg_local_cache usó menos CPU de servidor.

Raw runs: [Valkey io-threads=2N](../../docs/benchmarks/3.1.0/read-per-core.jsonl), [Valkey io-threads=1](../../docs/benchmarks/3.1.0/read-per-core-valkey-io-threads-1.jsonl).

## Lecturas con las 16 vCPU disponibles

Estas pruebas no fijaron CPU y se repitieron tres veces. En casos limitados por el cliente, el nodo de carga llegó a unos 7–13 núcleos de CPU. Con Valkey io-threads=8, las diferencias observadas entre medianas de MGET 16/64 estuvieron entre aproximadamente −2,15 % y +1,26 %; los rangos no se solaparon en ninguna de las cuatro comparaciones de 64 claves. Estas muestras no demuestran equivalencia estadística. Con io-threads=1, MGET 16 aleatoria a 256 clientes midió 116.130 frente a 97.489 solicitudes/s (19,1 % más para pg_local_cache). Las MGET grandes quedaron limitadas por cliente/red; no hay resultado io-threads=1 para 64 claves.

| Claves | Claves MGET | Clientes | pg_local_cache (solicitudes/s, p99 ms, vCPU) | Valkey io-threads=8 (solicitudes/s, p99 ms, vCPU) | Valkey io-threads=1 (solicitudes/s, p99 ms, vCPU) | SQL preparado (solicitudes/s, p99 ms, vCPU) |
| --- | --- | --- | --- | --- | --- | --- |
| Calientes | 1 | 64 | 217,724 (0.70, 2.89) | 206,634 (0.70, 7.85) | 167,929 (0.71, 0.66) | 132,433 (0.96, 11.32) |
| Calientes | 1 | 256 | 372,683 (2.39, 4.21) | 365,947 (2.33, 7.92) | 161,455 (2.92, 0.68) | 173,571 (4.26, 13.14) |
| Calientes | 16 | 64 | 82,858 (3.38, 3.13) | 83,098 (3.38, 6.41) | 79,179 (3.38, 0.69) | 73,512 (2.05, 8.78) |
| Calientes | 16 | 256 | 122,496 (9.83, 4.07) | 122,817 (9.83, 7.42) | 111,790 (8.06, 0.83) | 100,966 (6.36, 11.49) |
| Calientes | 64 | 64 | 30,241 (8.78, 3.40) | 30,845 (9.04, 3.57) | — | 27,790 (6.88, 9.05) |
| Calientes | 64 | 256 | 39,531 (43.52, 4.26) | 39,995 (47.71, 5.66) | — | 34,519 (30.67, 12.23) |
| 100k aleatorias | 1 | 64 | 215,310 (0.70, 2.89) | 205,081 (0.70, 7.85) | 162,778 (0.73, 0.67) | 131,563 (0.97, 11.34) |
| 100k aleatorias | 1 | 256 | 366,860 (2.39, 4.31) | 360,894 (2.33, 7.92) | 158,444 (2.98, 0.68) | 169,884 (4.26, 13.19) |
| 100k aleatorias | 16 | 64 | 79,505 (3.51, 3.45) | 79,556 (3.44, 6.68) | 74,270 (3.38, 0.79) | 69,262 (2.13, 9.12) |
| 100k aleatorias | 16 | 256 | 116,130 (10.35, 4.42) | 114,683 (9.83, 7.53) | 97,489 (7.67, 0.95) | 91,572 (7.01, 11.96) |
| 100k aleatorias | 64 | 64 | 29,000 (8.78, 3.81) | 29,464 (9.04, 4.61) | — | 26,169 (7.14, 9.39) |
| 100k aleatorias | 64 | 256 | 37,356 (43.52, 4.93) | 38,175 (40.37, 7.58) | — | 32,756 (31.20, 13.03) |

Para claves aleatorias, MGET 64 y 256 clientes, p99 de pg_local_cache fue 43,52 ms frente a 40,37 ms de Valkey. SQL preparado tuvo mucho menos rendimiento en las pruebas fijadas de una clave; los resultados varían según el tamaño del lote y la latencia.

Raw runs: [Valkey io-threads=8](../../docs/benchmarks/3.1.0/read-8-cores.jsonl), [Valkey io-threads=1](../../docs/benchmarks/3.1.0/read-8-cores-valkey-io-threads-1.jsonl).

## Coste de escritura

Tres repeticiones de 15 segundos comparan transacciones de tabla normal, transacciones mediante pg_local_cache y transacciones normales seguidas de Valkey DEL. Los porcentajes comparan con el caso equivalente de tabla normal.

| Operación | synchronous_commit | Clientes | Tabla normal tx/s (DB µs/tx) | pg_local_cache tx/s (DB µs/tx, cambio) | Tabla normal + Valkey DEL tx/s (DB µs/tx, cambio) |
| --- | --- | --- | --- | --- | --- |
| Update | on | 32 | 46,747 (109) | 45,065 (119, −3.6%) | 33,234 (204, −28.9%) |
| Update | on | 64 | 63,479 (108) | 61,027 (121, −3.9%) | 37,659 (238, −40.7%) |
| Update | off | 32 | 119,156 (75) | 110,987 (87, −6.9%) | 53,374 (158, −55.2%) |
| Update | off | 64 | 148,358 (73) | 136,194 (83, −8.2%) | 53,853 (189, −63.7%) |
| Insert | on | 32 | 48,831 (97) | 47,020 (105, −3.7%) | 34,863 (184, −28.6%) |
| Insert | on | 64 | 65,688 (97) | 63,011 (108, −4.1%) | 39,642 (218, −39.7%) |
| Insert | off | 32 | 123,679 (63) | 117,868 (72, −4.7%) | 58,168 (135, −53.0%) |
| Insert | off | 64 | 138,454 (68) | 130,805 (77, −5.5%) | 58,361 (165, −57.8%) |

En UPDATE, pg_local_cache quedó 3,6–8,2% por debajo de las escrituras normales; en INSERT, el coste fue 3,7–5,5%. UPDATE normal más Valkey DEL quedó 28,9–63,7% por debajo; para INSERT, la diferencia fue 28,6–57,8%.

Raw runs: [write overhead](../../docs/benchmarks/3.1.0/write-overhead.jsonl).

## Lecturas durante las escrituras

Cada caso se ejecutó una vez durante 30 segundos con 64 lectores sobre 60k filas; los escritores usaron synchronous_commit=off. Valkey usó cache-aside (escritura SQL seguida de DEL) con io-threads=8. “Obsoletas” es la comprobación de centinelas al final; n/a significa que no hay caché que revisar.

| Stack | Distribución de claves | Escritor | Lecturas/s | p99 lectura ms | Tasa de aciertos | Escrituras/s | Entradas obsoletas |
| --- | --- | --- | --- | --- | --- | --- | --- |
| pg_local_cache | Uniform | Ninguno | 214,508 | 0.71 | 1.000 | 0 | 0 |
| pg_local_cache | Uniform | Update 10,000/s | 195,384 | 0.86 | 0.948 | 10,000 | 0 |
| pg_local_cache | Uniform | Update 30,000/s | 161,919 | 1.04 | 0.837 | 29,999 | 0 |
| pg_local_cache | Uniform | Update unlimited | 82,640 | 1.69 | 0.432 | 111,013 | 0 |
| pg_local_cache | Zipf | Ninguno | 216,297 | 0.70 | 1.000 | 0 | 0 |
| pg_local_cache | Zipf | Update 10,000/s | 198,454 | 0.86 | 0.964 | 10,000 | 0 |
| pg_local_cache | Zipf | Update 30,000/s | 168,875 | 1.01 | 0.923 | 30,000 | 0 |
| pg_local_cache | Zipf | Update unlimited | 96,297 | 1.46 | 0.834 | 113,505 | 0 |
| Valkey cache-aside | Uniform | Ninguno | 223,537 | 0.61 | 1.000 | 0 | 0 |
| Valkey cache-aside | Uniform | Update 10,000/s | 156,983 | 1.92 | 0.936 | 10,000 | 0 |
| Valkey cache-aside | Uniform | Update 30,000/s | 42,294 | 7.14 | 0.577 | 29,998 | 0 |
| Valkey cache-aside | Uniform | Update unlimited | 29,692 | 8.78 | 0.444 | 38,481 | 1 |
| Valkey cache-aside | Zipf | Ninguno | 222,575 | 0.63 | 0.999 | 0 | 0 |
| Valkey cache-aside | Zipf | Update 10,000/s | 168,911 | 1.65 | 0.960 | 10,000 | 0 |
| Valkey cache-aside | Zipf | Update 30,000/s | 75,642 | 4.78 | 0.887 | 29,999 | 0 |
| Valkey cache-aside | Zipf | Update unlimited | 48,993 | 6.88 | 0.852 | 40,735 | 0 |
| Prepared SQL | Uniform | Ninguno | 129,714 | 0.97 | — | 0 | n/a |
| Prepared SQL | Uniform | Update 10,000/s | 121,567 | 1.13 | — | 10,000 | n/a |
| Prepared SQL | Uniform | Update 30,000/s | 106,949 | 1.33 | — | 30,000 | n/a |
| Prepared SQL | Uniform | Update unlimited | 77,574 | 2.02 | — | 93,955 | n/a |
| Prepared SQL | Zipf | Ninguno | 129,937 | 0.97 | — | 0 | n/a |
| Prepared SQL | Zipf | Update 10,000/s | 122,182 | 1.13 | — | 10,000 | n/a |
| Prepared SQL | Zipf | Update 30,000/s | 107,852 | 1.33 | — | 29,999 | n/a |
| Prepared SQL | Zipf | Update unlimited | 78,601 | 2.08 | — | 95,199 | n/a |
| pg_local_cache | Uniform | Insert unlimited | 117,531 | 1.20 | 1.000 | 114,066 | 0 |
| Valkey cache-aside | Uniform | Insert unlimited | 70,302 | 4.92 | 1.000 | 46,622 | 0 |
| Prepared SQL | Uniform | Insert unlimited | 84,294 | 1.98 | — | 95,094 | n/a |

pg_local_cache terminó todas las pruebas mixtas listadas sin entradas obsoletas. Valkey dejó una sola entrada centinela obsoleta en un caso UPDATE: claves Uniform con tasa de actualización ilimitada. Con 30k UPDATE/s y claves Uniform, las tasas fueron 161.919/s para pg_local_cache, 42.294/s para Valkey cache-aside y 106.949/s para SQL preparado.

Raw runs: [mixed reads and writes](../../docs/benchmarks/3.1.0/mixed.jsonl).

## Sondas de datos obsoletos de 120 segundos

Cada sonda se ejecutó una vez con 64 lectores y 64 escritores UPDATE sin límite; después se comprobó por completo si quedaban entradas obsoletas. Valkey usó io-threads=8.

| Stack | Distribución de claves | Lecturas/s | p99 lectura ms | Escrituras/s | Entradas obsoletas | Nota |
| --- | --- | --- | --- | --- | --- | --- |
| pg_local_cache | Uniform | 82,565 | 1.687552 | 108,580 | 0 | — |
| pg_local_cache | Zipf | 0 | n/a | 101,797 | 0 | Ver nota abajo. |
| Valkey cache-aside | Uniform | 30,210 | 8.781824 | 36,508 | 2 | — |
| Valkey cache-aside | Zipf | 28,568 | 8.781824 | 35,789 | 1 | — |

El lector Zipf de pg_local_cache no superó la comprobación de equivalencia inicial durante escrituras concurrentes (problema del harness), por lo que informó cero lecturas. La comprobación de entradas obsoletas sí se ejecutó y encontró cero.

Raw runs: [120-second stale probes](../../docs/benchmarks/3.1.0/stale-probes-120s.jsonl).

## Prueba de una hora

Ejecución única: MGET 16 sobre 100k claves aleatorias, 64 clientes, más 20.000 UPDATE/s. RssAnon de los workers RESP se midió cada minuto.

- Lecturas: 251.987.814 MGET en 3.600 segundos (69.997/s), p99 3,57 ms.
- Escrituras: 71.999.993 UPDATE (20.000/s), p99 1,13 ms, cero errores.
- RssAnon se mantuvo estable: crecimiento máximo de 44 kB por worker en 60 muestras.

Raw runs: [one-hour soak](../../docs/benchmarks/3.1.0/soak-1h.jsonl).

## Entorno y método

Según lscpu, cada VM tenía 16 vCPU en 2 sockets × 4 núcleos por socket × 2 hilos por núcleo (topología visible de la VM), 62 GB y Debian 13 con kernel 6.12.111+deb13-amd64. La VM de base de datos ejecutaba PostgreSQL 16.15 (max_connections=1000, shared_buffers=1048576) y pg_local_cache 3.1.0 c431bcc (activado, memory_budget_mb=1024, workers=8). La tabla contenía 100.000 filas JSON con una media de 166 bytes. Valkey 8.1.1 usó io-threads=8 en las pruebas de escritura, mixtas y de datos obsoletos. De la VM de carga a la de base de datos, iperf3 midió 10,4 Gbit/s y ping informó RTT min/media/max de 0,090/0,143/0,871 ms. El cliente Go/pgx se ejecutó en la segunda VM. Las lecturas duraron 20 segundos; escritura 15 segundos, pruebas mixtas 30 segundos, sondas 120 segundos y soak 3.600 segundos.

La matriz fijada usó 256 clientes; cada caso tuvo cinco ejecuciones salvo los dos casos fijados de un par con Valkey io-threads=1, que tuvieron dos. La matriz completa y las pruebas de escritura tuvieron tres ejecuciones; las pruebas mixtas, sondas y soak, una cada una. Las interrupciones NIC no se fijaron. Todas las tablas usan la compilación final c431bcc. El rendimiento se expresa en solicitudes/s salvo que la tabla indique transacciones/s; la latencia es p99 y la CPU son vCPU del servidor.

Entorno y JSONL originales: [entorno](../../docs/benchmarks/3.1.0/env.txt), [evidencia del entorno](../../docs/benchmarks/3.1.0/stand.txt), [lecturas fijadas](../../docs/benchmarks/3.1.0/read-per-core.jsonl), [Valkey io-threads=1 fijado](../../docs/benchmarks/3.1.0/read-per-core-valkey-io-threads-1.jsonl), [lecturas con todos los núcleos](../../docs/benchmarks/3.1.0/read-8-cores.jsonl), [todos los núcleos Valkey io-threads=1](../../docs/benchmarks/3.1.0/read-8-cores-valkey-io-threads-1.jsonl), [coste de escritura](../../docs/benchmarks/3.1.0/write-overhead.jsonl), [lecturas durante escrituras](../../docs/benchmarks/3.1.0/mixed.jsonl), [sondas 120 s](../../docs/benchmarks/3.1.0/stale-probes-120s.jsonl) y [prueba de una hora](../../docs/benchmarks/3.1.0/soak-1h.jsonl).

## Reproducir con bench/

Se necesitan Python 3 y alias SSH para las VM de base de datos y carga. La VM de base de datos necesita PostgreSQL 16, Valkey, mpstat, sar, ip y systemd; la VM de carga necesita el cliente Go compilado en /root/bench-client. Véanse [bench/README.md](https://github.com/profundium/pg_local_cache/blob/c431bcc4f69d4ecd28f1695548e121e55dfdd2fa/bench/README.md) y el [cliente Go/pgx](https://github.com/profundium/pg_local_cache/tree/c431bcc4f69d4ecd28f1695548e121e55dfdd2fa/examples/go-pgx/).

Matriz de lectura predeterminada: 20 s, tres repeticiones, 1/16/64 claves, 16/64/256 clientes, conjunto caliente y espacio de 100k claves:

    python3 bench/run_bench.py reads.jsonl

Ejemplo para fijar un par de vCPU, 256 clientes y cinco repeticiones; sustituya 0-1 por los ID lógicos de CPU de ese par de vCPU:

    PGLC_SERVER_CPUS=0-1 REPEATS=5 BATCHES=1 CLIENTS=256 KEY_SPACES=0,100000 python3 bench/run_bench.py pinned.jsonl

El script aplica AllowedCPUs a PostgreSQL y Valkey y lo restablece al terminar. Registra CPU del servidor, latencia, RX/TX de red y CPU del cliente. Configure io-threads de Valkey antes de cada ejecución.

Pruebas de escritura y carga mixta de 30 s:

    REPEATS=3 WRITE_SECONDS=15 WRITE_CLIENTS=32,64 python3 bench/write_bench.py writes.jsonl
    REPEATS=1 MIXED_SECONDS=30 MIXED_RATES=none,10000,30000,unlimited MIXED_KEY_DISTS=uniform,zipf python3 bench/write_bench.py mixed.jsonl

Los scripts añaden filas JSONL y conservan las muestras completadas si falla una ejecución. El script de escritura registra CPU de base de datos por transacción; la carga mixta también registra latencia, aciertos/fallos, tasa de escritura y la comprobación final de entradas obsoletas.

## Resultados históricos 2.x en MacBook

Estos JSON son mediciones históricas en Apple M3 Max con PostgreSQL 16; el entorno y la topología de cliente difieren de las dos VM anteriores. Incluyen la API SQL mget de 2.x, eliminada en 3.0.0. Consulte la [documentación 2.0.4](https://github.com/profundium/pg_local_cache/tree/v2.0.4/docs) para esa API; las lecturas en caché actuales usan RESP MGET.

- [Mediciones Go, 14 de septiembre de 2026](../../docs/benchmarks/2026-09-14-m3-max.json)
- [Mediciones de cliente, 14 de septiembre de 2026](../../docs/benchmarks/2026-09-14-m3-max-clients.json)
- [Mediciones Go/RESP, 15 de septiembre de 2026](../../docs/benchmarks/2026-09-15-m3-max-resp.json)
