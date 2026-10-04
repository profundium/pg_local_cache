---
layout: doc
lang: es
translation_key: resp
title: Conéctate mediante RESP
description: Lee filas de PostgreSQL mediante RESP2 con redis-cli o Node.js. Incluye autenticación, configuración del cliente, ejemplos ejecutables y limpieza.
section: RESP
permalink: /es/docs/resp.html
last_modified_at: "2026-10-04"
---

# Conéctate mediante RESP {#connect-over-resp}

Usa `MGET` para leer filas de PostgreSQL almacenadas en caché con un cliente RESP2. Inicia la [demo desechable](QUICKSTART.md) con su configuración RESP:

```bash
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml up --build --wait
```

Esto habilita RESP en `127.0.0.1:56379`. Recrear la demo descarta sus datos. El token siguiente es público y solo sirve para esta demo local.

Los workers RESP usan el rol de PostgreSQL configurado para todos los clientes. No heredan los permisos SQL, la transacción ni el snapshot del cliente que llama. Usa SQL si necesitas esas propiedades de sesión. El listener se vincula a loopback de forma predeterminada; una dirección IPv4 no local requiere `pg_local_cache.allow_plaintext_network=on`. La demo lo activa solo dentro de su red de contenedores. El listener no ofrece TLS: usa loopback o una red de confianza. `pg_local_cache.enabled` es un interruptor de emergencia SIGHUP. Cada worker RESP aplica la recarga de forma asíncrona en su siguiente límite entre comandos, después de que termine el comando que esté ejecutando. El campo `cache_enabled` de `local_cache.health()` muestra el valor que ve la sesión SQL que llama a la función; no confirma que todos los workers hayan aplicado el cambio. Para desactivar las lecturas de caché sin reiniciar, ejecuta `ALTER SYSTEM SET pg_local_cache.enabled = off;` y `SELECT pg_reload_conf();`; mientras esté desactivada, RESP lee directamente de la tabla de origen. Consulta la [referencia técnica](TECHNICAL.md).

## redis-cli {#redis-cli}

```bash
export REDISCLI_AUTH=DemoRespToken_0123456789abcdef0123456789
redis-cli -2 -p 56379 MGET 'CRUD:pglc_demo.public.items:{"id":42}'
```

La respuesta contiene la fila 42 como JSON. Las filas que no existen devuelven
`nil`. [redis-cli](https://redis.io/docs/latest/develop/tools/cli/) lee el
token de `REDISCLI_AUTH`.

## Node.js {#nodejs}

```bash
npm --prefix examples/node-postgres ci --ignore-scripts
npm --prefix examples/node-postgres run resp
```

El ejemplo usa el paquete oficial `@redis/client` y comprueba el orden, los
duplicados, las posiciones de entrada nulas y las filas ausentes. El helper
omite las claves nulas en el protocolo y restaura sus posiciones después de
decodificar. Para conectarte desde tu aplicación:

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

Configura `PGLC_RESP_TOKEN` con el token del servidor y mantén abierta esta
conexión entre solicitudes. Estos [ajustes del cliente](https://github.com/redis/node-redis/blob/master/docs/client-configuration.md)
seleccionan RESP2 y omiten los comandos de metadatos propios de Redis. Usa solo
autenticación por token, sin nombre de usuario ni número de base de datos Redis.

## Compara con SQL {#compare-with-sql}

El [benchmark común](BENCHMARKS.md#run-the-same-comparison-on-every-client) ejecuta RESP `MGET` y SQL preparado con las mismas claves y resultados descodificados tanto en Node.js como en Go. Las mediciones publicadas de SQL `mget` de 2.x son históricas; ese modo de prueba se eliminó en 3.0.0. Para una caché de aplicaciones más amplia, lee la [guía de caché aparte de PostgreSQL y Redis](postgresql-redis-cache.md).

## Detén la demo {#stop-the-demo}

```bash
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml down
```
