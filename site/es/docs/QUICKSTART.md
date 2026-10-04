---
layout: doc
lang: es
translation_key: QUICKSTART
title: Prueba pg_local_cache localmente
seo_title: "Prueba localmente una caché de filas de PostgreSQL | pg_local_cache"
description: "Ejecuta pg_local_cache 3.0 en PostgreSQL desechable, lee filas de ejemplo por RESP, revisa aciertos de caché, prueba actualizaciones y elimina la demo sin modificar una base existente."
section: Inicio rápido
permalink: /es/docs/QUICKSTART.html
last_modified_at: "2026-09-16"
---

# Prueba pg_local_cache localmente {#try-pg_local_cache-locally}

Esta demo construye pg_local_cache desde tu copia en un servidor PostgreSQL 16 independiente. No lo instala en un servidor PostgreSQL existente. El ejemplo de lectura usa el listener RESP configurado mediante la capa de Compose.

Necesitas Git, Docker y Docker Compose con compatibilidad con `up --wait`. La imagen se compila desde el código fuente.

## Inicia la base de datos {#start-the-database}

```bash
git clone https://github.com/profundium/pg_local_cache.git
cd pg_local_cache
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml up --build --wait
```

La demo enlaza PostgreSQL y RESP a los puertos de loopback `55432` y `56379`, no tiene un volumen persistente y almacena los datos en tmpfs local del contenedor. La capa RESP enlaza dentro de la red del contenedor y habilita explícitamente su listener de demostración en claro. Al detener el contenedor se descartan los datos. `demo-only` y el token RESP público son solo para esta demo de loopback; usa tus propias credenciales en producción.

Si el puerto 55432 está ocupado, define `PGLC_DEMO_PORT` antes de iniciar Compose y mantenlo definido al ejecutar el ejemplo de Node.js:

```bash
export PGLC_DEMO_PORT=55433
```

## Leer por RESP {#read-as-an-application-role}

La configuración crea 4.096 filas en `public.items`. Solo esa tabla está asociada a la caché. El rol `demo` no es superusuario.

```bash
export REDISCLI_AUTH=DemoRespToken_0123456789abcdef0123456789
redis-cli -2 -p 56379 MGET \
  'CRUD:pglc_demo.public.items:{"id":42}' \
  'CRUD:pglc_demo.public.items:{"id":7}' \
  'CRUD:pglc_demo.public.items:{"id":42}' \
  'CRUD:pglc_demo.public.items:{"id":999999}'
```

La respuesta conserva el orden de las claves y los duplicados. La primera y la tercera posición corresponden a la fila 42; la última posición es RESP null porque la clave 999999 no existe. Una solicitud RESP omite las claves de entrada nulas; los helpers del cliente pueden restaurar esas posiciones cuando haga falta.

Inspecciona los contadores como administrador de la base de datos:

```bash
docker compose -f examples/compose.yaml exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 -U postgres -d pglc_demo \
  -c 'SELECT local_cache.health();' \
  -c 'SELECT local_cache.stats();'
```

En esta demo recién creada, `local_cache.health()` debe indicar `ready: true`, y repetir las lecturas debe aumentar `cache_hits`. Si hace falta, revisa `cache_misses`, `database_reads` y `cache_enabled` en `local_cache.stats()` y `local_cache.health()`.

## Comprueba el commit y el rollback {#check-commit-and-rollback}

Con Node.js 20 o posterior:

```bash
npm --prefix examples/node-postgres ci --ignore-scripts
npm --prefix examples/node-postgres run demo
```

La prueba usa RESP para las lecturas y PostgreSQL para las escrituras. Comprueba un acierto caliente, el orden de entrada, claves duplicadas y ausentes, la invalidación de caché y la visibilidad de las actualizaciones confirmadas. Los workers RESP usan un rol PostgreSQL configurado y no comparten la transacción SQL ni la instantánea de la aplicación.

Consulta la [guía de invalidación de caché](cache-invalidation.md) o la [explicación de consultas de Node.js](resp.md#nodejs).

## Conecta tu aplicación {#connect-your-application}

- [Node.js](resp.md#nodejs): usa RESP para lecturas en caché y `pg` para escrituras SQL.
- [Go](resp.md#go): usa RESP para lecturas en caché y `pgx` para escrituras SQL.
- [RESP](resp.md): conecta con un cliente Redis.

A continuación, [compara la misma carga de SQL preparado y RESP](BENCHMARKS.md#run-the-same-comparison-on-every-client). Para informar de resultados o problemas de configuración, abre un [informe de carga](https://github.com/profundium/pg_local_cache/issues/new?template=workload.yml) con el entorno, el JSON del benchmark o el registro de errores.

## Elimina la demo {#remove-the-demo}

```bash
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml down
```

La imagen Docker compilada localmente queda disponible para otra ejecución. No es necesario reiniciar ni restaurar el servicio PostgreSQL del host.

Para una base de datos existente, sigue la [guía de instalación](INSTALL_EXISTING.md). Ese recorrido tiene privilegios, configuración y requisitos de reinicio diferentes.
