---
layout: doc
lang: es
translation_key: QUICKSTART
title: Prueba pg_local_cache localmente
seo_title: "Prueba localmente una caché de filas de PostgreSQL | pg_local_cache"
description: Ejecuta pg_local_cache 2.0 en PostgreSQL desechable, lee filas de ejemplo, inspecciona los aciertos de caché, prueba actualizaciones y elimina la demo sin cambiar una base de datos existente.
section: Inicio rápido
permalink: /es/docs/QUICKSTART.html
last_modified_at: "2026-09-16"
---

# Prueba pg_local_cache localmente {#try-pg_local_cache-locally}

Esta demo compila pg_local_cache desde tu copia de trabajo en un servidor PostgreSQL 16 separado. No lo instala en un servidor PostgreSQL existente.

Necesitas Git, Docker y Docker Compose con compatibilidad con `up --wait`. La imagen se compila desde el código fuente.

## Inicia la base de datos {#start-the-database}

```bash
git clone https://github.com/profundium/pg_local_cache.git
cd pg_local_cache
docker compose -f examples/compose.yaml up --build --wait
```

La demo enlaza PostgreSQL en `127.0.0.1:55432`, no tiene listener RESP ni volumen persistente y almacena los datos en tmpfs local del contenedor. Detener el contenedor descarta sus datos. `demo-only` es para esta demo de loopback; usa tus propias credenciales en producción.

Si el puerto 55432 está ocupado, define `PGLC_DEMO_PORT` antes de iniciar Compose y mantenlo definido al ejecutar el ejemplo de Node.js:

```bash
export PGLC_DEMO_PORT=55433
```

## Lee como un rol de aplicación {#read-as-an-application-role}

La configuración crea 4.096 filas en `public.items`. Solo esa tabla está asociada a la caché. El rol `demo` no es superusuario.

```bash
docker compose -f examples/compose.yaml exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 -U demo -d pglc_demo <<'SQL'
SELECT unnest(local_cache.mget(
  'public.items'::regclass,
  ARRAY[42, 7, 42, NULL, 999999]::bigint[]
));
SELECT unnest(local_cache.mget(
  'public.items'::regclass,
  ARRAY[42, 7, 42, NULL, 999999]::bigint[]
));
SQL
```

Ambas llamadas devuelven las mismas filas ordenadas. La primera y tercera posiciones se refieren a la fila 42. Las dos últimas posiciones son `NULL` SQL: una entrada es nula y la clave 999999 no existe. En psql, los valores nulos SQL aparecen en blanco de forma predeterminada.

La función devuelve **`text[]`**. El `unnest` anterior muestra una entrada del array por línea.

Inspecciona los contadores como administrador de la base de datos:

```bash
docker compose -f examples/compose.yaml exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 -U postgres -d pglc_demo \
  -c 'SELECT local_cache.health();' \
  -c 'SELECT local_cache.stats();'
```

En esta demo recién creada, `local_cache.health()` debe indicar `ready: true`, y repetir las lecturas debe aumentar `sql_cache_hits`. Si los aciertos permanecen en cero, inspecciona `sql_cache_misses`, `sql_cache_fills` y `sql_cache_bypasses` usando la [guía de invalidación](cache-invalidation.md#inspect-the-cause-of-a-miss).

## Comprueba el commit y el rollback {#check-commit-and-rollback}

Con Node.js 20 o posterior:

```bash
npm --prefix examples/node-postgres ci --ignore-scripts
npm --prefix examples/node-postgres run demo
```

La prueba abre conexiones de lectura y escritura separadas. Comprueba un acierto caliente, el orden de entrada, claves duplicadas y ausentes, una actualización no confirmada, la lectura de los propios cambios, el rollback y una actualización confirmada. Termina con código distinto de cero si falla una aserción.

Consulta el [recorrido SQL con dos sesiones](cache-invalidation.md) o la [explicación de la consulta de Node.js](node-postgres.md).

## Conecta tu aplicación {#connect-your-application}

- [Node.js](node-postgres.md): usa tu conexión o pool de `pg` existente.
- [Go](go.md): conéctate con `pgx` y descodifica las filas devueltas.
- [RESP](resp.md): habilita el endpoint opcional y conéctate con un cliente Redis.

A continuación, [compara la misma carga de trabajo SQL y RESP](BENCHMARKS.md#run-the-same-comparison-on-every-client). Para problemas de resultados o configuración, abre un [informe de carga de trabajo](https://github.com/profundium/pg_local_cache/issues/new?template=workload.yml) con tu entorno y el JSON del benchmark o el registro de errores.

## Elimina la demo {#remove-the-demo}

```bash
docker compose -f examples/compose.yaml down
```

La imagen Docker compilada localmente queda disponible para otra ejecución. No es necesario reiniciar ni restaurar el servicio PostgreSQL del host.

Para una base de datos existente, sigue la [guía de instalación](INSTALL_EXISTING.md). Ese recorrido tiene privilegios, configuración y requisitos de reinicio diferentes.
