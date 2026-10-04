---
layout: doc
lang: es
translation_key: TECHNICAL
title: Referencia técnica de pg_local_cache
seo_title: API RESP, coherencia, memoria y configuración de pg_local_cache
description: Referencia sobre lecturas RESP, tablas admitidas, barreras transaccionales, TLS, memoria compartida, métricas y ajustes de PostgreSQL.
section: Técnica
permalink: /es/docs/TECHNICAL.html
---

# Referencia técnica de pg_local_cache {#pg_local_cache-technical-reference}

Referencia técnica del endpoint RESP2, la coherencia de caché, los límites de recursos y la seguridad. Consulte la [guía de inicio rápido](QUICKSTART.md) y la [instalación](INSTALL_EXISTING.md) para conocer los pasos de configuración.

## Tablas y claves admitidas {#supported-tables-and-keys}

Adjunte tablas heap permanentes con una clave primaria válida. No se admiten tablas particionadas, heredadas, protegidas por seguridad a nivel de fila (RLS), temporales o foráneas, ni tablas propiedad de una extensión. Los tipos admitidos para las claves primarias son `smallint`, `integer`, `bigint`, `text`, `varchar`, `char` con collations deterministas y `uuid`; las claves compuestas pueden usar estos tipos y tener hasta 16 columnas.

Los cambios DDL requieren reconciliar las asignaciones. Consulte [Instalación](INSTALL_EXISTING.md#attach-a-table).

## Ruta de lectura y fallback seguro {#read-path-and-safe-fallback}

![Ruta de lectura RESP MGET: acierto de caché, carga de origen protegida y bypass mediante el interruptor de emergencia.](../../docs/diagrams/read-path.svg)

Cada clave de RESP `MGET` se valida y canoniza antes de buscarla. Un acierto apto devuelve la fila completa en JSON. Si no hay acierto, el worker lee la tabla de origen en una transacción breve y solo publica la carga si su barrera de lectura sigue vigente. Las filas inexistentes devuelven `nil`. Las filas cuya carga no cabe en la caché compartida aún pueden devolverse desde PostgreSQL si su JSON cabe en el límite de valores RESP.

## Coherencia transaccional {#transaction-consistency}

![Invalidación de escrituras: las barreras previas al commit protegen las escrituras confirmadas; un rollback anterior a publicar la barrera conserva válidas las entradas anteriores.](../../docs/diagrams/write-invalidation.svg)

Los triggers de fila y de sentencia de las tablas asignadas recopilan las claves modificadas o una relación afectada en el estado local de la transacción. La función de callback pre-commit publica barreras de invalidación e incrementa las generaciones. Una carga que empezó antes de la barrera no puede publicar datos obsoletos. Un rollback antes de publicar la barrera descarta el estado modificado y conserva válidas las entradas anteriores. Si la transacción se aborta después de la publicación, la invalidación no se revierte y las entradas afectadas siguen siendo inválidas.

Las lecturas RESP usan `pg_local_cache.role` en transacciones breves e independientes. No comparten el rol SQL, la transacción, las escrituras sin confirmar ni la instantánea del cliente.

## Memoria y configuración {#shared-memory-and-configuration}

La extensión preasigna una caché compartida acotada y el estado de asignaciones, workers y clientes al iniciar PostgreSQL. `memory_budget_mb` limita la asignación determinista de memoria de la extensión. Los fallos de admisión y la expulsión no superan la capacidad configurada; las lecturas recurren a PostgreSQL.

| Ajuste | Valor predeterminado | Rango | Recarga |
|---|---:|---|---|
| `pg_local_cache.enabled` | `on` | `on` / `off` | SIGHUP |
| `pg_local_cache.allow_plaintext_network` | `off` | `on` / `off` | Reinicio |
| `pg_local_cache.tls` | `off` | `on` / `off` | Reinicio |
| `pg_local_cache.tls_cert_file` | vacío | Ruta a archivo PEM | Reinicio |
| `pg_local_cache.tls_key_file` | vacío | Ruta a archivo PEM | Reinicio |
| `pg_local_cache.tls_ca_file` | vacío | Ruta a archivo CA PEM | Reinicio |
| `pg_local_cache.tls_min_protocol_version` | `TLSv1.2` | `TLSv1.2` / `TLSv1.3` | Reinicio |
| `pg_local_cache.port` | `6380` | `0`–`65535`; `0` desactiva RESP | Reinicio |
| `pg_local_cache.workers` | `4` | `1`–`32` | Reinicio |
| `pg_local_cache.cache_entries` | `16384` | `128`–`65536` | Reinicio |
| `pg_local_cache.relation_states` | `1024` | `128`–`8192` | Reinicio |
| `pg_local_cache.max_clients` | `256` | `1`–`4096`; como máximo, el número de slots de worker | Reinicio |
| `pg_local_cache.max_clients_per_worker` | `64` | `1`–`128` | Reinicio |
| `pg_local_cache.memory_budget_mb` | `384` | `64`–`8192` MB | Reinicio |
| `pg_local_cache.idle_timeout_ms` | `300000` | `1000`–`86400000` | Reinicio |
| `pg_local_cache.statement_timeout_ms` | `2000` | `100`–`60000` | Reinicio |
| `pg_local_cache.lock_timeout_ms` | `250` | `10`–`60000` | Reinicio |
| `pg_local_cache.singleflight_wait_ms` | `25` | `0`–`1000` | Reinicio |
| `pg_local_cache.max_pipeline_commands` | `256` | `1`–`4096` | Reinicio |
| `pg_local_cache.max_dirty_keys` | `4096` | `128`–`16384` | Reinicio |
| `pg_local_cache.bind_address` | `127.0.0.1` | Dirección IPv4 | Reinicio |
| `pg_local_cache.database` | `postgres` | Nombre de base de datos | Reinicio |
| `pg_local_cache.role` | `local_cache_worker` | Rol LOGIN de PostgreSQL | Reinicio |
| `pg_local_cache.auth_token_file` | vacío | Archivo propiedad del usuario del sistema operativo de PostgreSQL, modo `0400` o `0600` | Reinicio |
| `pg_local_cache.auth_token` | vacío | Token en línea; solo para desarrollo | Reinicio |
| `pg_local_cache.allow_superuser` | `off` | `on` / `off`; solo para desarrollo | Reinicio |

Todos los ajustes salvo `enabled` son parámetros del postmaster y requieren reiniciar. Los slots de cliente requieren `max_clients <= workers × max_clients_per_worker`.

## Endpoint RESP2 {#optional-resp2-endpoint}

El endpoint acepta RESP2. Las claves siguen el formato `CRUD:<db>.<schema>.<table>:<json pk>`. `MGET` conserva el orden y los duplicados de la solicitud; una fila inexistente se devuelve como elemento `nil`. Cada solicitud admite como máximo 1.024 claves, cada fila JSON puede ocupar hasta 65.536 bytes y la respuesta codificada hasta 66.560 bytes.

Los comandos de datos admitidos son `MGET`, `SET` y `DEL`; `AUTH` es obligatorio. El endpoint también admite `PING`, `ECHO`, `INFO`, `STAT`/`STATS`, `INVALIDATE` con ámbito limitado, `HELLO 2`, `QUIT`, `CLIENT SETINFO`/`SETNAME`/`GETNAME`/`ID`, `COMMAND` y `SELECT 0`. Los comandos no admitidos devuelven un error. Los clientes RESP usan la base de datos 0; el ámbito de base de datos y tabla procede de cada clave de caché.

## TLS y modelo de seguridad {#security-model}

De forma predeterminada, el listener se vincula a la interfaz de loopback IPv4. RESP TLS usa ajustes específicos de la extensión, no los `ssl_*` de PostgreSQL. Requiere una compilación de PostgreSQL con OpenSSL, un certificado y una clave de servidor PEM, y un reinicio. Al configurar `tls_ca_file`, también se exige y verifica el certificado del cliente (mTLS); la versión TLS mínima predeterminada es 1.2.

Con TLS desactivado, un listener de texto claro fuera de loopback requiere `allow_plaintext_network=on` y una red de confianza. Los listeners fuera de loopback requieren un token de al menos 32 bytes. Es preferible usar un archivo de token con permisos restringidos. Todos los clientes RESP comparten un único rol LOGIN de PostgreSQL configurado; PostgreSQL no evalúa por separado los permisos concedidos a cada cliente de red. Los workers superusuario están desactivados de forma predeterminada y solo se prevén para desarrollo.

## Interruptor de emergencia de la caché {#cache-kill-switch}

`pg_local_cache.enabled` es un interruptor de emergencia de caché que se recarga con SIGHUP. Cuando está desactivado, las lecturas RESP omiten la caché compartida y leen la tabla de origen; `SET` y `DEL` siguen escribiendo a través de PostgreSQL. Los workers aplican las recargas de forma asíncrona en los límites entre comandos. `local_cache.health()` informa del ajuste de la sesión SQL que realiza la llamada, no de la confirmación de cada worker. Al volver a activarlo, se incrementa la época de caché antes de que los workers reanuden las lecturas de caché.

## Métricas y estado {#health-and-monitoring}

`local_cache.health()` informa de la disponibilidad, el estado de la caché y la convergencia de asignaciones. `local_cache.stats()` devuelve contadores JSON; `local_cache.metrics()` devuelve la fila tipada para el exporter.

Las métricas incluyen aciertos, fallos y aciertos negativos de caché; lecturas y escrituras de origen; invalidaciones y expulsiones; líderes, esperas, reutilizaciones y tiempos de espera de singleflight; clientes activos y máximos; rechazos por límite de conexiones; errores de autenticación y protocolo; contrapresión de salida y desconexiones de clientes lentos; inicios de workers; fallbacks por claves modificadas; fallos y reintentos al recargar asignaciones; handshakes y fallos TLS. Los indicadores incluyen capacidades de entradas y relaciones, recuentos de clientes y workers, convergencia de asignaciones, memoria compartida, de workers y estimada, y el presupuesto configurado.

A continuación: [inicio rápido](QUICKSTART.md), [instalación](INSTALL_EXISTING.md) y [actualización](UPGRADING.md).
