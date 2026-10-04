---
layout: doc
lang: es
translation_key: TECHNICAL
title: Referencia técnica de pg_local_cache
seo_title: API SQL, coherencia, memoria y RESP2 de pg_local_cache
description: "Referencia técnica de pg_local_cache: MGET por RESP, invalidación consciente de las transacciones, memoria compartida acotada de PostgreSQL y monitorización."
section: Técnica
permalink: /es/docs/TECHNICAL.html
---

# Referencia técnica de pg_local_cache {#pg_local_cache-technical-reference}

`pg_local_cache` almacena filas completas por clave primaria completa en la memoria compartida acotada de PostgreSQL y ofrece un endpoint RESP2 con `MGET`, `SET` y `DEL`.

> **El SQL habitual sigue siendo habitual:** la extensión no instala hooks del planificador ni del ejecutor. Un `SELECT` normal siempre usa PostgreSQL y nunca lee esta caché.

## Tablas y claves compatibles {#supported-tables-and-keys}

Las tablas de origen deben ser tablas heap permanentes con una clave primaria válida y sin RLS, particionamiento, herencia ni propiedad de una extensión.

Tipos de clave compatibles:

- `smallint`, `integer` y `bigint`;
- `text`, `varchar` y `char` con intercalaciones deterministas;
- `uuid`;
- claves primarias compuestas formadas únicamente por esos tipos.

Las relaciones no compatibles se rechazan al asociarlas en vez de producir un mapeo parcial inseguro.

## Asocia, reconcilia y separa tablas {#attach-reconcile-and-detach-tables}

`local_cache.attach_table(regclass)` ejecuta una secuencia de configuración protegida:

1. bloquea y valida la relación;
2. registra su espacio de nombres, OID de relación y columnas de clave primaria ordenadas;
3. instala triggers de sentencia, fila y truncado propiedad de la extensión;
4. recarga los mapeos de los workers.

Los triggers de eventos DDL invalidan los metadatos de mapeo almacenados en caché. Ejecuta `local_cache.reconcile_table(...)` o `local_cache.reconcile_all()` después de cambios intencionados del esquema. `local_cache.detach_table(...)` elimina el mapeo y sus triggers.

## Recorrido de lectura y fallback seguro {#read-path-and-safe-fallback}

Cada clave RESP `MGET` consulta primero la caché compartida cuando está habilitada.
En cada fallo, el worker lee la fila de origen en su propia transacción breve.
PostgreSQL sigue devolviendo las filas que superen el límite de carga de la caché,
pero no se almacenan en ella.

## Coherencia de las transacciones {#transaction-consistency}

Los triggers de escritura en tablas mapeadas publican, en cualquier sesión de
PostgreSQL, vallas dirty-writer por clave o relación y aumentan las generaciones
antes de que el commit sea visible. Las lecturas omiten las entradas protegidas
hasta que termina la escritura; las comprobaciones de generación impiden publicar
lecturas obsoletas en curso, por lo que una escritura confirmada no puede ir
seguida de un acierto obsoleto.

Las lecturas RESP usan `pg_local_cache.role`, no el rol PostgreSQL del cliente, y
se ejecutan en transacciones breves independientes. No ven cambios del cliente
sin confirmar, no comparten su snapshot ni forman parte de su transacción.
`pg_local_cache.enabled = off` omite la caché.

## Memoria compartida y configuración {#shared-memory-and-configuration}

Las entradas de caché, los estados de relaciones, los contadores, las generaciones de workers y los slots de clientes RESP se asignan al iniciar el postmaster. La capacidad está acotada. La expulsión muestrea un conjunto rotatorio acotado y prefiere las entradas obsoletas; si falla la admisión, vuelve a la tabla de origen en lugar de asignar memoria sin límite.

| Configuración | Predeterminado | Significado |
|---|---:|---|
| `pg_local_cache.database` | `postgres` | base de datos servida por la extensión |
| `pg_local_cache.cache_entries` | `16384` | capacidad de filas compartidas |
| `pg_local_cache.relation_states` | `1024` | capacidad del estado de mapeo compartido |
| `pg_local_cache.memory_budget_mb` | `384` | presupuesto de inicio de la extensión |
| `pg_local_cache.port` | `6380` | puerto RESP; `0` solo para pruebas de regresión y diagnóstico, no sirve lecturas |
| `pg_local_cache.bind_address` | `127.0.0.1` | dirección de enlace RESP |
| `pg_local_cache.workers` | `4` | workers RESP |
| `pg_local_cache.role` | `local_cache_worker` | rol PostgreSQL de RESP |
| `pg_local_cache.max_clients` | `256` | límite global de clientes RESP |
| `pg_local_cache.max_clients_per_worker` | `64` | slots por worker |
| `pg_local_cache.idle_timeout_ms` | `300000` | plazo para clientes inactivos o lentos |
| `pg_local_cache.statement_timeout_ms` | `2000` | plazo de sentencia del worker |
| `pg_local_cache.lock_timeout_ms` | `250` | plazo de bloqueo del worker |
| `pg_local_cache.singleflight_wait_ms` | `25` | espera del seguidor para la misma clave |
| `pg_local_cache.max_pipeline_commands` | `256` | comandos por turno del bucle de eventos |
| `pg_local_cache.max_dirty_keys` | `4096` | límite de vallas de claves de la transacción |
| `pg_local_cache.auth_token_file` | vacío | credencial RESP preferida |
| `pg_local_cache.auth_token` | vacío | token insertado solo para desarrollo |
| `pg_local_cache.enabled` | `on` | interruptor de emergencia SIGHUP de la caché; con `off`, RESP lee directamente de la tabla de origen |
| `pg_local_cache.allow_plaintext_network` | `off` | opción de postmaster para listeners en claro fuera del loopback IPv4 |
| `pg_local_cache.allow_superuser` | `off` | anulación de rol solo para desarrollo |

Son ajustes del postmaster. Dimensiona antes de reiniciar. La [guía de instalación](INSTALL_EXISTING.md) describe los paquetes y los reinicios.

## Endpoint RESP2 {#optional-resp2-endpoint}

RESP2 usa los mismos mapeos y la misma caché compartida. Las claves del cable tienen esta forma:

```text
CRUD:database.schema.table:{"pk_column":<json-scalar>,...}
```

El endpoint no tiene TLS. `pg_local_cache.allow_plaintext_network` está desactivado de forma predeterminada. Para un listener en claro fuera de loopback, habilítalo explícitamente; el contenedor de demostración también requiere habilitarlo explícitamente para su listener en claro. Enlázalo a loopback o colócalo detrás de un proxy TLS autenticado. Prefiere un archivo de token con permisos restringidos en lugar de un token integrado.

El parámetro operativo `pg_local_cache.enabled` es de tipo SIGHUP y funciona como interruptor de emergencia. Para desactivar el servicio de caché:

```sql
ALTER SYSTEM SET pg_local_cache.enabled = off;
SELECT pg_reload_conf();
```

Cada worker RESP aplica la recarga de forma asíncrona en su siguiente límite entre comandos, después de que termine el comando que esté ejecutando. El campo `cache_enabled` de `local_cache.health()` muestra el valor que ve la sesión SQL que llama a la función; no confirma que todos los workers hayan aplicado el cambio. Para volver a habilitarlo, ejecuta también:

```sql
ALTER SYSTEM SET pg_local_cache.enabled = on;
SELECT pg_reload_conf();
```

## Salud y monitorización {#health-and-monitoring}

`local_cache.health()` informa de la preparación y la convergencia del mapeo. `local_cache.stats()` devuelve contadores JSON. `local_cache.metrics()` expone la fila de métricas tipadas que usa el exportador.

Los contadores RESP de `stats()` y `metrics()` incluyen:

- `sql_gets`
- `sql_meta`
- `sql_sets`
- `sql_dels`
- `sql_result_reuses`

Las lecturas de la base de datos, las invalidaciones, el rechazo de admisión, el fallback por claves sucias, el singleflight y los contadores de workers y RESP permanecen separados.

A continuación: usa la [guía de instalación](INSTALL_EXISTING.md) para verificar paquetes Debian y RPM, compilar con PGXS, configurar, reiniciar, actualizar y desinstalar.
