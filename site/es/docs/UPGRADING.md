---
layout: doc
lang: es
translation_key: UPGRADING
title: Actualizar pg_local_cache a 3.1.0
seo_title: "Actualizar pg_local_cache a 3.1.0"
description: Actualice pg_local_cache desde 3.0.0 o 2.x, revise los ajustes de capacidad y workers de 3.1 y compruebe la extensión tras reiniciar.
section: Instalación
permalink: /es/docs/UPGRADING.html
last_modified_at: "2026-10-06"
---

# Actualizar pg_local_cache a 3.1.0 {#upgrade-pg_local_cache-from-2x-to-310}

La versión 3.0.0 eliminó la función SQL `local_cache.mget(regclass, anyarray)`.
Use RESP `MGET` autenticado para leer filas completas almacenadas en caché. Los
workers RESP usan el rol configurado de PostgreSQL y no heredan los permisos,
la transacción ni la instantánea SQL de la aplicación. Mantenga SQL para
proyecciones, joins, bloqueos de filas y lecturas que requieran la semántica de
la sesión de la aplicación. La [guía RESP](resp.md) explica la configuración
cliente y la codificación de claves.

## Actualizar desde 3.0.0 {#upgrade-from-300}

La versión 3.1.0 cambia la biblioteca compartida y la disposición de memoria
compartida. Instale el paquete o la biblioteca 3.1.0 correspondiente a la versión
principal de PostgreSQL y reinicie PostgreSQL antes de usarla. La migración SQL
`3.0.0--3.1.0` no hace cambios: los objetos SQL, las asignaciones de tablas y
los triggers permanecen iguales. Ejecute la actualización de la extensión en
cada base de datos para registrar la versión `3.1.0`.

### Cambios en los ajustes {#setting-changes}

| Ajuste | Comportamiento en 3.1.0 |
|---|---|
| `pg_local_cache.cache_entries` | Rango de `128` a `16777216` descriptores. El valor predeterminado integrado es `262144`, calculado con el presupuesto predeterminado de 384 MiB y una reserva mínima de la mitad para páginas de arena. La capacidad real en bytes depende de la arena y del tamaño de las filas; el inicio falla si se supera el presupuesto configurado. |
| `pg_local_cache.lock_partitions` | Predeterminado `64`; potencia de dos entre `16` y `256`. Las cachés pequeñas usan menos particiones. |
| `pg_local_cache.dirty_marker_entries` | El valor predeterminado `-1` activa el cálculo automático: `min(16384, max(1024, floor(cache_entries / 4)))`. Rango explícito: `128`–`1048576`. |
| `pg_local_cache.dirty_marker_memory_mb` | El valor predeterminado `-1` activa el cálculo automático: `min(16, max(1, floor(memory_budget_mb / 25)))` MiB. Rango explícito: `1`–`1024` MiB. |
| `pg_local_cache.max_clients_per_worker` | Predeterminado `64`; rango ampliado a `1`–`4096`. `max_clients` no puede superar `workers × max_clients_per_worker`. El límite suave `RLIMIT_NOFILE` de cada worker debe ser al menos `min(max_clients, max_clients_per_worker) + 33`; aumente el límite `nofile` del proceso/contenedor según sea necesario. |
| `pg_local_cache.max_deferred_misses` | Ajuste nuevo. Predeterminado `8`; rango `1`–`64` solicitudes aplazadas por worker cuando la relación está bloqueada. |

La versión 3.1.0 no elimina ni renombra ajustes de 3.0.0. Todos estos ajustes
se aplican después de reiniciar.

`local_cache.stats()` añade `fast_path_hits`, `fast_path_fallbacks` y cuatro
`fast_path_fallback_key_form`, `fast_path_fallback_mapping_shape`, `fast_path_fallback_multi_key` y `fast_path_fallback_cache_state`; `cache_memory_capacity_bytes`,
`cache_memory_used_bytes`, `cache_fragmentation_bytes`,
`arena_admission_rejections_total`; `dirty_marker_capacity`,
`dirty_marker_entries`, `dirty_marker_highwater`,
`dirty_marker_fallbacks_total`, `dirty_marker_entries_effective`,
`dirty_marker_memory_mb_effective`, `dirty_marker_memory_capacity_bytes`,
`dirty_key_limit_fallbacks`, además de `lock_partitions`,
`max_clients_per_worker` y `client_slots`. RESP `STAT` añade campos locales del
worker: `deferred_misses_total`, `deferred_misses_current`,
`deferred_timeouts_total` y `deferred_rejections_total`.

Pasos de actualización:

1. Instale el paquete o la biblioteca 3.1.0 correspondiente a la versión
   principal de PostgreSQL.
2. Revise los ajustes anteriores. Si aumenta los slots de cliente, eleve el
   límite suave `nofile` del proceso/contenedor según la fórmula.
3. Reinicie PostgreSQL para cargar la biblioteca y reservar la nueva disposición
   de memoria compartida.
4. En cada base de datos que tenga instalada la extensión, conéctese como
   superusuario de esa base y ejecute:

   ```sql
   ALTER EXTENSION pg_local_cache UPDATE;
   ```

5. Compruebe la biblioteca y los workers:

   ```sql
   SELECT current_setting('pg_local_cache.binary_version');
   SELECT local_cache.health();
   ```

   La versión debe ser `3.1.0`; health debe mostrar
   `workers_running = workers_configured`.

## Actualizar desde 2.x {#upgrade-from-2x}

Antes de actualizar, cambie las aplicaciones de SQL `mget` a RESP `MGET`, con
un rol worker dedicado y el modelo de autorización requerido. Si el listener
2.x usaba una dirección distinta de loopback, configure TLS nativo de RESP con
su certificado y clave antes de reiniciar. `pg_local_cache.tls_ca_file` exige
certificados de cliente (mTLS). RESP TLS es independiente de PostgreSQL `ssl_*`.
Si necesita texto sin cifrar, active explícitamente
`pg_local_cache.allow_plaintext_network = on` y úselo solo en una red de
confianza. Sin TLS o esa autorización, los workers RESP externos a loopback no
se inician.

La migración SQL de 2.x a 3.0 eliminó la API SQL de lectura y sus contadores, por
lo que cambió el tipo de resultado de `local_cache.metrics()`. Los permisos
personalizados se conservan. Un objeto dependiente puede bloquear la migración;
esta no usa `CASCADE`. Modifique o elimine esas dependencias y vuelva a intentarlo.
Esos cambios SQL pertenecen a la migración anterior a 3.0, no a la migración 3.1
sin cambios. Después, siga los pasos anteriores de instalación, reinicio, actualización de la extensión y verificación. PostgreSQL aplicará primero la migración anterior de 2.x a 3.0 y luego la migración sin cambios de 3.0.0 a 3.1.0.

## Volver a 2.0.4 {#rollback-to-204}

No hay un script de downgrade. Para volver a 2.0.4, reinstale su paquete,
reinicie PostgreSQL para cargar la biblioteca anterior, desconecte todas las
tablas asignadas, vuelva a crear la extensión en cada base de datos y adjunte
las tablas otra vez:

```sql
SELECT local_cache.detach_table('public.items'::regclass);
DROP EXTENSION pg_local_cache;
CREATE EXTENSION pg_local_cache VERSION '2.0.4';
SELECT local_cache.attach_table('public.items'::regclass);
```

Repita `detach_table` y `attach_table` para cada tabla. Guarde antes del rollback
la lista de tablas conectadas y los permisos personalizados de la extensión.
Los objetos que dependan de funciones de la extensión pueden impedir su
eliminación; resuelva esas dependencias explícitamente.

## Ruta de búsqueda de la biblioteca {#library-lookup-path}

El archivo de control usa el nombre simple `pg_local_cache` en `module_pathname`.
PostgreSQL lo resuelve mediante `dynamic_library_path` (que incluye `$libdir`
por defecto). Si el servidor sobrescribe ese ajuste, añada antes de reiniciar
el directorio donde se instaló `pg_local_cache`.

Documentación para 2.x: https://github.com/profundium/pg_local_cache/tree/v2.0.4/docs
