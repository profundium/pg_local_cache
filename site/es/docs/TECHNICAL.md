---
layout: doc
lang: es
translation_key: TECHNICAL
title: Referencia técnica de pg_local_cache
seo_title: API SQL, coherencia, memoria y RESP2 de pg_local_cache
description: Referencia técnica de SQL mget de pg_local_cache, invalidación consciente de las transacciones, memoria compartida acotada de PostgreSQL, monitorización y RESP2 opcional.
section: Técnica
permalink: /es/docs/TECHNICAL.html
---

# Referencia técnica de pg_local_cache {#pg_local_cache-technical-reference}

`pg_local_cache` almacena filas completas por clave primaria completa en memoria compartida acotada de PostgreSQL. Expone una función SQL explícita `local_cache.mget` y un endpoint RESP2 opcional.

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

## API SQL mget {#sql-mget-api}

Firma:

```sql
local_cache.mget(relation regclass, key_values anyarray) RETURNS text[]
```

Las claves de una sola columna usan su tipo de array nativo. Las claves compuestas usan `text[][]` rectangular, con una clave por fila y un componente por columna de la clave primaria.

Contrato:

- máximo de 1.024 claves por llamada;
- se conservan el orden de entrada y los duplicados;
- las entradas `NULL` y las filas ausentes producen resultados `NULL` alineados;
- los componentes de una clave compuesta no pueden ser `NULL`;
- cada componente se analiza mediante la función de entrada de su tipo PostgreSQL;
- el lote compuesto completo se valida antes de la primera consulta;
- quien llama necesita `SELECT` sobre la tabla de origen;
- la función es `SECURITY INVOKER`.

Una consulta de origen preparada se almacena en caché por instancia de función, usuario, relación y generación de mapeo.

## Recorrido de lectura y fallback seguro {#read-path-and-safe-fallback}

Cada clave solicitada sigue el mismo recorrido:

1. canoniza la clave primaria completa;
2. usa la caché compartida solo en una transacción limpia `READ COMMITTED` sobre la primaria escribible;
3. valida la suma de comprobación de la carga, el descriptor de fila, el `xmin` de origen y la visibilidad del snapshot;
4. si no, ejecuta mediante SPI la consulta indexada sobre la tabla de origen;
5. publica una entrada positiva o negativa solo después de demostrar que el snapshot sigue siendo el más reciente.

`REPEATABLE READ`, `SERIALIZABLE`, la recuperación, la ejecución paralela y una transacción que haya escrito datos mapeados omiten la caché. Las filas mayores que el límite de carga de la caché siguen devolviéndose desde PostgreSQL, pero no se almacenan en caché.

## Coherencia de las transacciones {#transaction-consistency}

Antes de que una escritura mapeada pueda confirmarse, los triggers ponen una valla a la clave o relación afectada. Un llenado lleva las generaciones de mapeo, global, de relación, de clave y del loader, por lo que un loader obsoleto no puede publicar después de una invalidación o expulsión.

Las entradas positivas registran el `xmin` de la tupla de origen y un horizonte de observación FullXID. Las entradas que no son elegibles por snapshot vuelven a PostgreSQL. Las entradas negativas nunca son autoritativas para un snapshot activo más antiguo.

El rollback elimina el estado sucio local de la transacción sin publicar datos nuevos. Por tanto, la lectura de los propios cambios procede de PostgreSQL, no de contenido especulativo de la caché.

## Memoria compartida y configuración {#shared-memory-and-configuration}

Las entradas de caché, los estados de relaciones, los contadores, las generaciones de workers y los slots de clientes RESP se asignan al iniciar el postmaster. La capacidad está acotada. La expulsión muestrea un conjunto rotatorio acotado y prefiere las entradas obsoletas; si falla la admisión, vuelve a la tabla de origen en lugar de asignar memoria sin límite.

| Configuración | Predeterminado | Significado |
|---|---:|---|
| `pg_local_cache.database` | `postgres` | base de datos servida por la extensión |
| `pg_local_cache.cache_entries` | `16384` | capacidad de filas compartidas |
| `pg_local_cache.relation_states` | `1024` | capacidad del estado de mapeo compartido |
| `pg_local_cache.memory_budget_mb` | `384` | presupuesto de inicio de la extensión |
| `pg_local_cache.port` | `6380` | puerto RESP; `0` deshabilita RESP |
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
| `pg_local_cache.allow_superuser` | `off` | anulación de rol solo para desarrollo |

Son ajustes del postmaster. Dimensiona antes de reiniciar; el preflight del instalador binario comprueba el plan combinado.

## Endpoint RESP2 opcional {#optional-resp2-endpoint}

RESP2 usa los mismos mapeos y la misma caché compartida. Las claves del cable tienen esta forma:

```text
CRUD:database.schema.table:{"pk_column":<json-scalar>,...}
```

Los comandos compatibles son `MGET`, `SET`, `DEL` y la invalidación acotada, todos autenticados y acotados. Los workers RESP usan un único rol PostgreSQL configurado; no heredan las ACL de base de datos de cada cliente de red.

El endpoint no tiene TLS. Enlázalo a loopback o colócalo detrás de un proxy TLS autenticado. Prefiere un archivo de tokens restringido por permisos frente a un token insertado.

## Salud y monitorización {#health-and-monitoring}

`local_cache.health()` informa de la preparación y la convergencia del mapeo. `local_cache.stats()` devuelve contadores JSON. `local_cache.metrics()` expone la fila de métricas tipadas que usa el exportador.

Los contadores de la caché SQL describen solo llamadas explícitas a `mget`:

- `sql_cache_hits`
- `sql_cache_misses`
- `sql_cache_fills`
- `sql_cache_bypasses`

Las lecturas de la base de datos, las invalidaciones, el rechazo de admisión, el fallback por claves sucias, el singleflight y los contadores de workers y RESP permanecen separados.

A continuación: usa la [guía de instalación](INSTALL_EXISTING.md) para binarios verificados, compilaciones PGXS desde código fuente, reinicios controlados, verificación y recuperación.
