---
layout: doc
lang: es
translation_key: resp
title: Conéctate mediante RESP
description: Lee filas de PostgreSQL mediante RESP2 con redis-cli o Node.js. Incluye autenticación, configuración del cliente, ejemplos ejecutables y limpieza.
section: RESP
permalink: /es/docs/resp.html
last_modified_at: "2026-09-16"
---

# Conéctate mediante RESP {#connect-over-resp}

Usa `MGET` para leer filas de PostgreSQL almacenadas en caché con un cliente RESP2. Inicia la [demo desechable](QUICKSTART.md) con su configuración RESP:

```bash
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml up --build --wait
```

Esto habilita RESP en `127.0.0.1:56379`. Recrear la demo descarta sus datos. El token siguiente es público y solo sirve para esta demo local.

## redis-cli {#redis-cli}

```bash
export REDISCLI_AUTH=DemoRespToken_0123456789abcdef0123456789
redis-cli -2 -p 56379 MGET 'CRUD:pglc_demo.public.items:{"id":42}'
```

La respuesta contiene la fila 42 como JSON. Las filas ausentes devuelven `nil`. [redis-cli](https://redis.io/docs/latest/develop/tools/cli/) lee el token desde `REDISCLI_AUTH`.

## Node.js {#nodejs}

```bash
npm --prefix examples/node-postgres ci --ignore-scripts
npm --prefix examples/node-postgres run resp
```

El ejemplo usa el paquete oficial `@redis/client` y comprueba el orden, los duplicados, las posiciones de entrada nulas y las filas ausentes. Su helper omite las claves nulas en el cable y restaura sus posiciones después de descodificar. Para conectarte desde tu aplicación:

```js
import { createClient } from '@redis/client';

const client = createClient({
  url: 'redis://127.0.0.1:56379',
  password: process.env.PGLC_RESP_TOKEN,
  RESP: 2,
  disableClientInfo: true,
});
client.on('error', console.error);
await client.connect();

try {
  const values = await client.mGet(['CRUD:pglc_demo.public.items:{"id":42}']);
  const rows = values.map(value => value === null ? null : JSON.parse(value));
  console.log(rows);
} finally {
  await client.close();
}
```

Define `PGLC_RESP_TOKEN` con el token de tu servidor. Mantén abierta esta conexión entre solicitudes. Esta [configuración del cliente](https://github.com/redis/node-redis/blob/master/docs/client-configuration.md) selecciona RESP2 y omite los comandos de metadatos específicos de Redis. Usa autenticación solo con token, sin nombre de usuario ni número de base de datos de Redis.

Los workers RESP usan un rol de PostgreSQL configurado para todos los clientes. Para lecturas dentro de una transacción SQL, usa [SQL de Node.js](node-postgres.md) o [SQL de Go](go.md). Consulta la [referencia RESP](TECHNICAL.md#optional-resp2-endpoint) para ver los comandos y límites.

## Compara con SQL {#compare-with-sql}

El [benchmark común](BENCHMARKS.md#run-the-same-comparison-on-every-client) ejecuta RESP `MGET`, SQL `mget` y SQL preparado con las mismas claves y resultados descodificados tanto en Node.js como en Go. Para una caché de aplicaciones más amplia, lee la [guía de caché aparte de PostgreSQL y Redis](postgresql-redis-cache.md).

## Detén la demo {#stop-the-demo}

```bash
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml down
```
