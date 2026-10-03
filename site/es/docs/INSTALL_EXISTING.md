---
layout: doc
lang: es
translation_key: INSTALL_EXISTING
title: Instala pg_local_cache en PostgreSQL 14-18
seo_title: Instala pg_local_cache en PostgreSQL 14-18
description: Instala la extensión pg_local_cache con binarios de Linux verificados o PGXS y configura la precarga, el reinicio, la verificación y la recuperación de forma segura.
section: Instalación
permalink: /es/docs/INSTALL_EXISTING.html
last_modified_at: "2026-09-05"
---

# Instala pg_local_cache en un servidor PostgreSQL existente {#install-pg_local_cache-on-an-existing-postgresql-server}

Instala la extensión con un paquete Linux verificado o compílala con la cadena de herramientas PGXS de PostgreSQL. Ambos recorridos requieren un reinicio controlado de PostgreSQL antes de `CREATE EXTENSION`.

> **Planifica una ventana de mantenimiento:** la primera activación cambia `shared_preload_libraries`. Conserva sus entradas existentes y reinicia el clúster correcto solo después de que el preflight termine correctamente.

## Elige un recorrido de instalación {#choose-an-installation-path}

| Recorrido | Adecuado para | Responsable del reinicio |
|---|---|---|
| Último binario verificado | Clúster Linux amd64 local | Bootstrap de `pg_ctl` |
| Binario verificado fijo | Operaciones de producción y gestionadas | systemd, `pg_ctl` u operador externo |
| Compilación de código fuente con PGXS | Plataforma no compatible o instalación personalizada de PostgreSQL | Tu flujo operativo habitual |

Los binarios publicados son compatibles con PostgreSQL 14-18 en Linux amd64 con glibc o musl. Los ejemplos de versión fija siguientes usan pg_local_cache 2.0.1.

## Instalación rápida de un binario {#fast-binary-install}

Para un clúster local controlado por `pg_ctl`:

```bash
curl -fsSL https://github.com/profundium/pg_local_cache/releases/latest/download/install-latest.sh | bash -s -- app
```

Sustituye `app` por el nombre de la base de datos. Esto habilita el modo solo SQL con `pg_local_cache.port = 0`.

El bootstrap resuelve una etiqueta de versión, verifica `fetch-release.sh` contra el `SHA256SUMS` de esa versión, selecciona el archivo compatible de PostgreSQL y libc, lo verifica, lo instala, reinicia, crea la extensión y ejecuta `local_cache.health()`.

Si `curl | bash` queda fuera de tu política, inspecciona primero el script:

```bash
curl -fsSLO https://github.com/profundium/pg_local_cache/releases/latest/download/install-latest.sh
less install-latest.sh
bash install-latest.sh app
```

## Instalación controlada de un binario {#controlled-binary-install}

Descarga una versión fija con su helper publicado:

```bash
curl -fsSLO https://github.com/profundium/pg_local_cache/releases/download/v2.0.1/fetch-release.sh
bash fetch-release.sh --release-tag v2.0.1 --output-directory ./pg_local_cache-package
```

Ejecuta el preflight y después elige explícitamente quién será responsable del reinicio:

```bash
sudo ./pg_local_cache-package/install.sh preflight --database app
sudo ./pg_local_cache-package/install.sh install \
  --database app \
  --restart-method systemd \
  --systemd-unit postgresql@16-main
```

Los métodos de reinicio admitidos son `systemd`, `pg_ctl` y `none`. Usa `none` con Patroni, un operador de Kubernetes u otro controlador externo. Reinicia mediante ese controlador y después verifica:

```bash
sudo ./pg_local_cache-package/install.sh verify --database app
```

El instalador imprime un directorio de estado. Consérvalo hasta que termine la verificación; contiene la copia de seguridad en línea que necesita `recover`.

## Compila desde el código fuente {#build-from-source}

Usa el mismo `pg_config` que el servidor PostgreSQL de destino. Instala primero sus cabeceras de desarrollo del servidor, un compilador C y GNU Make.

```bash
git clone --branch v2.0.1 --depth 1 https://github.com/profundium/pg_local_cache.git
cd pg_local_cache
make PG_CONFIG=/usr/lib/postgresql/16/bin/pg_config
sudo make install PG_CONFIG=/usr/lib/postgresql/16/bin/pg_config
```

Compila desde una copia limpia para que el binario registre su commit de Git. La instalación desde el código fuente copia solo los archivos de la extensión. Continúa con la configuración de precarga, el reinicio y la inicialización SQL siguientes.

## Configura antes de reiniciar {#configure-before-restart}

Configuración mínima solo SQL con la capacidad y el presupuesto de memoria predeterminados:

```conf
shared_preload_libraries = 'pg_local_cache'
pg_local_cache.database = 'app'
pg_local_cache.role = 'local_cache_worker'
pg_local_cache.cache_entries = 16384
pg_local_cache.memory_budget_mb = 384
pg_local_cache.port = 0
```

Conserva las entradas existentes de `shared_preload_libraries`. Sustituye `app` por el nombre real de la base de datos aquí y en las concesiones SQL siguientes. El presupuesto de memoria es para la extensión, no para todo el servidor PostgreSQL.

Dimensiona juntos `cache_entries`, los estados de relaciones, los clientes, los workers y `memory_budget_mb`. El preflight del instalador binario rechaza planes incoherentes. Las compilaciones desde el código fuente requieren la misma revisión de capacidad antes del reinicio; no aumentes el número de entradas sin revisar el presupuesto de memoria.

## Inicializa una instalación desde el código fuente {#initialize-a-source-installation}

Después de reiniciar, conéctate a la base de datos configurada como superusuario de la base de datos. Para una primera instalación manual, ejecuta:

```sql
CREATE EXTENSION IF NOT EXISTS pg_local_cache;
CREATE ROLE local_cache_worker LOGIN NOINHERIT NOSUPERUSER
    NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT CONNECT ON DATABASE app TO local_cache_worker;
GRANT USAGE ON SCHEMA local_cache TO local_cache_worker;
GRANT SELECT ON TABLE local_cache.mapping TO local_cache_worker;
```

El instalador binario crea este rol y sus concesiones de metadatos; omite este bloque cuando esa configuración ya se haya completado. Para un `pg_local_cache.role` personalizado, usa ese nombre de forma coherente. No reutilices un rol propietario de tablas de la aplicación.

**El rol es necesario incluso con `pg_local_cache.port = 0`.** La asociación de tablas también lo valida en modo solo SQL. Debe ser independiente del propietario de la tabla y tener los atributos y las concesiones de metadatos mostrados arriba. `attach_table` gestiona su acceso a cada tabla mapeada. No se necesita contraseña ni listener de red para operar solo con SQL.

## Asocia una tabla {#attach-a-table}

Usa una tabla permanente existente con una clave primaria compatible. Como superusuario de la base de datos configurada:

```sql
SELECT local_cache.attach_table('public.items'::regclass);
SELECT local_cache.health();
```

Concede a un rol de aplicación existente solo lo que necesita:

```sql
GRANT SELECT ON public.items TO app_user;
GRANT USAGE ON SCHEMA local_cache TO app_user;
GRANT EXECUTE ON FUNCTION local_cache.mget(regclass, anyarray) TO app_user;
```

La extensión no reescribe los `SELECT` habituales.

## Verifica el llenado en frío y el acierto en caliente {#verify-cold-fill-and-warm-hit}

```sql
SELECT local_cache.invalidate('public.items');
SELECT local_cache.mget('public.items'::regclass, ARRAY[1]::bigint[]);
SELECT local_cache.mget('public.items'::regclass, ARRAY[1]::bigint[]);
SELECT local_cache.stats();
```

Confirma que `local_cache.health()` está listo, que el mapeo ha convergido y que los contadores de la caché SQL avanzan como se espera.

## Habilita RESP2 opcional {#enable-optional-resp2}

RESP2 añade un listener, procesos worker y un token compartido. Usa el mismo rol de PostgreSQL dedicado que requiere la asociación de tablas:

```bash
sudo ./pg_local_cache-package/install.sh preflight \
  --database app \
  --mode resp \
  --token-file /secure/path/token

sudo ./pg_local_cache-package/install.sh install \
  --database app \
  --mode resp \
  --token-file /secure/path/token \
  --restart-method systemd \
  --systemd-unit postgresql@16-main
```

Mantén el listener en `127.0.0.1` o detrás de TLS autenticado. Los clientes RESP comparten el rol de worker configurado y no reciben el contexto ACL de PostgreSQL de cada cliente.

## Recupera una instalación binaria fallida {#recover-a-failed-binary-install}

Usa el directorio de estado que imprime el instalador:

```bash
sudo ./pg_local_cache-package/install.sh recover \
  --state-directory /path/printed/by/install
```

No recuperes después de que un postmaster nuevo haya aceptado tráfico hasta revisar el estado registrado y el impacto operativo.

## Solución de problemas {#troubleshooting}

- **Error de precarga:** confirma la configuración del clúster de destino y reinicia el postmaster correcto.
- **Rol worker ausente o rechazado:** completa la inicialización SQL anterior, incluidos sus atributos y concesiones de metadatos, incluso en modo solo SQL.
- **Tabla rechazada:** usa una tabla permanente, no particionada y sin RLS con una clave primaria compatible.
- **Error de permisos de `mget`:** concede `SELECT` sobre la tabla de origen, `USAGE` sobre el esquema y `EXECUTE` sobre la función.
- **Omisiones de caché:** inspecciona el nivel de aislamiento, las escrituras de la transacción actual, el estado de recuperación, el tamaño de las filas y las métricas.
- **Mapeo obsoleto tras DDL:** ejecuta `local_cache.reconcile_table('public.items'::regclass)`.

A continuación: lee la [referencia técnica](TECHNICAL.md) para conocer los contratos SQL, el dimensionamiento de memoria, la monitorización y la seguridad de RESP.
