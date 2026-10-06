---
layout: doc
lang: es
translation_key: resp
title: Clientes RESP
description: Conecte clientes compatibles con Redis a pg_local_cache mediante RESP2, MGET, autenticación por token y TLS nativo.
section: RESP
permalink: /es/docs/resp.html
redirect_from:
  - /es/docs/go.html
  - /es/docs/node-postgres.html
last_modified_at: "2026-10-04"
---

# Clientes RESP {#resp-clients}

Conecte clientes compatibles con Redis para leer filas completas de PostgreSQL por clave primaria. Esta página trata la codificación de claves, la autenticación, el comportamiento de las respuestas, los errores y TLS.

<a id="connect-over-resp"></a>

## Contrato de claves y respuestas {#key-and-response-contract}

Las claves RESP identifican una tabla adjunta y un objeto de clave primaria:

```text
CRUD:<db>.<schema>.<table>:<json pk>
CRUD:app.public.items:{"id":42}
CRUD:app.public.orders:{"tenant_id":7,"id":42}
```

`MGET key [key ...]` devuelve un array RESP2 en el orden de la solicitud. Los duplicados conservan su posición. Las filas existentes son cadenas bulk JSON; las filas inexistentes son elementos nil.

| Límite | Comportamiento |
|---|---|
| Hasta 1.024 claves por comando | Dentro del límite de bytes, un comando con 1.025 claves devuelve `ERR MGET accepts at most 1024 keys`; con 1.026 o más, el parser devuelve `ERR invalid argument count`. |
| 65.536 bytes por solicitud codificada | Si el búfer de entrada se llena antes de analizar una solicitud completa, el servidor cierra la conexión. |
| 65.536 bytes por fila JSON | No se puede devolver por RESP una fila mayor. |
| 66.560 bytes por respuesta MGET codificada | Las respuestas agregadas mayores devuelven `ERR response exceeds limit`. |

El tamaño del lote está limitado por el número de claves y por los bytes de solicitud y respuesta. Mantenga cada solicitud codificada dentro de 65.536 bytes; divida los lotes si las claves o filas grandes se acercan a cualquiera de esos límites.

## AUTH {#auth}

Envíe `AUTH <token>` antes de otros comandos. Los clientes también pueden enviar `AUTH <username> <token>`; el nombre de usuario debe coincidir con `pg_local_cache.role`. El token se comparte entre todos los clientes RESP y no asigna privilegios de PostgreSQL por cliente. Guárdelo en `pg_local_cache.auth_token_file` con modo `0400` o `0600`; reserve `auth_token` en línea para desarrollo. Los listeners fuera de loopback requieren un token de al menos 32 bytes.

## TLS {#tls}

TLS nativo de RESP utiliza los ajustes `pg_local_cache.tls_*` y es independiente del TLS SQL de PostgreSQL. Requiere una compilación de PostgreSQL con OpenSSL, un certificado de servidor y una clave privada. Si configura `tls_ca_file`, también se exigen y verifican certificados de cliente (mTLS). Omita las opciones de certificado de cliente para TLS con autenticación solo del servidor. La versión mínima predeterminada es TLS 1.2.

El listener se vincula a loopback de forma predeterminada. Con TLS desactivado, un listener de texto claro fuera de loopback requiere `pg_local_cache.allow_plaintext_network=on`. Consulte la [referencia técnica](TECHNICAL.md#optional-resp2-endpoint) para ver los ajustes del listener y de seguridad.

## redis-cli {#redis-cli}

Defina `PGLC_RESP_TOKEN` con el token configurado. El inicio rápido local también incluye un token de demostración.

```sh
export REDISCLI_AUTH="$PGLC_RESP_TOKEN"
redis-cli -2 -h 127.0.0.1 -p 56379 MGET 'CRUD:pglc_demo.public.items:{"id":42}'
redis-cli -2 --tls --cacert ./ca.crt --cert ./client.crt --key ./client.key -h cache.example -p 6380 MGET 'CRUD:app.public.items:{"id":42}'
```

## Go {#go}

Instale `github.com/redis/go-redis/v9`. Defina `PGLC_RESP_TOKEN`; la variante TLS usa archivos de CA y de certificado de cliente.

```go
package main

import (
	"context"
	"fmt"
	"os"

	"github.com/redis/go-redis/v9"
)

func main() {
	client := redis.NewClient(&redis.Options{
		Addr: "127.0.0.1:56379", Password: os.Getenv("PGLC_RESP_TOKEN"), Protocol: 2,
	})
	defer client.Close()
	rows, err := client.MGet(context.Background(), `CRUD:pglc_demo.public.items:{"id":42}`).Result()
	if err != nil {
		panic(err)
	}
	fmt.Printf("%v\n", rows)
}
```

Variante TLS:

```go
package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"os"

	"github.com/redis/go-redis/v9"
)

func main() {
	ca, err := os.ReadFile("./ca.crt")
	if err != nil { panic(err) }
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(ca) { panic("invalid CA") }
	cert, err := tls.LoadX509KeyPair("./client.crt", "./client.key")
	if err != nil { panic(err) }
	client := redis.NewClient(&redis.Options{
		Addr: "cache.example:6380", Password: os.Getenv("PGLC_RESP_TOKEN"), Protocol: 2,
		TLSConfig: &tls.Config{MinVersion: tls.VersionTLS12, ServerName: "cache.example", RootCAs: roots, Certificates: []tls.Certificate{cert}},
	})
	defer client.Close()
	rows, err := client.MGet(context.Background(), `CRUD:app.public.items:{"id":42}`).Result()
	if err != nil { panic(err) }
	fmt.Printf("%v\n", rows)
}
```

## Node.js (redis v4) {#nodejs}

Instale con `npm install redis@^4`. Redis v4 usa RESP2 de forma predeterminada.

```js
import { createClient } from 'redis';

const client = createClient({
  socket: { host: '127.0.0.1', port: 56379 },
  password: process.env.PGLC_RESP_TOKEN,
});
client.on('error', console.error);
await client.connect();
try {
  console.log(await client.mGet(['CRUD:pglc_demo.public.items:{"id":42}']));
} finally {
  await client.quit();
}
```

Variante TLS:

```js
import { readFileSync } from 'node:fs';
import { createClient } from 'redis';

const client = createClient({
  socket: {
    host: 'cache.example', port: 6380, tls: true, servername: 'cache.example',
    ca: [readFileSync('./ca.crt')],
    cert: readFileSync('./client.crt'), key: readFileSync('./client.key'),
  },
  password: process.env.PGLC_RESP_TOKEN,
});
client.on('error', console.error);
await client.connect();
try {
  console.log(await client.mGet(['CRUD:app.public.items:{"id":42}']));
} finally {
  await client.quit();
}
```

## Python (redis-py) {#python}

Instale con `python -m pip install redis`.

```python
import os
import redis

client = redis.Redis(host="127.0.0.1", port=56379,
                     password=os.environ["PGLC_RESP_TOKEN"],
                     decode_responses=True, protocol=2)
print(client.execute_command('MGET', 'CRUD:pglc_demo.public.items:{"id":42}'))
client.close()
```

Variante TLS:

```python
import os
import redis

client = redis.Redis(host="cache.example", port=6380,
                     password=os.environ["PGLC_RESP_TOKEN"],
                     decode_responses=True, protocol=2, ssl=True,
                     ssl_ca_certs="./ca.crt", ssl_certfile="./client.crt",
                     ssl_keyfile="./client.key", ssl_cert_reqs="required",
                     ssl_check_hostname=True)
print(client.execute_command('MGET', 'CRUD:app.public.items:{"id":42}'))
client.close()
```

## Errores y límites {#errors}

| Respuesta | Causa |
|---|---|
| `NOAUTH Authentication required` | Autentique primero la conexión. |
| `WRONGPASS invalid authentication token` | El token o el nombre de usuario opcional es incorrecto. |
| `ERR MGET accepts at most 1024 keys` | El comando tiene 1.025 claves; divida el lote. |
| `ERR invalid argument count` | El parser rechazó demasiados argumentos; un MGET con 1.026 claves o más supera el límite de argumentos. |
| `ERR busy: relation locked, retry` | La cola de fallos aplazados está llena mientras la relación sigue bloqueada; reintente la solicitud. |
| `ERR response exceeds limit` | Reduzca el tamaño del lote o de la carga de la fila. |
| `ERR PostgreSQL: …` | Error de PostgreSQL o SPI, incluidos los tiempos de espera o la cancelación de sentencias o bloqueos de PostgreSQL. |
| `ERR MGET deadline exceeded` | Venció el plazo agregado explícito de MGET, también mientras esperaba en la cola por un bloqueo de relación; es distinto de los tiempos de espera de sentencias o bloqueos de PostgreSQL. |
| `ERR KVik key targets a different database` | Usa la base de datos configurada para el endpoint RESP. |
| `ERR unknown KVik table mapping` | Comprueba que la clave nombre un esquema y una tabla asociados. |
| `ERR key must use CRUD:database.schema.table:{primary-key-json}` | Usa el formato completo de clave CRUD. |
| `ERR KVik key must end with a primary-key JSON object` | Termina el ámbito de tabla con un objeto JSON de clave primaria. |
| `ERR invalid CRUD cache scope` | Solo para `INVALIDATE`: el ámbito indicado no es un ámbito CRUD admitido. |

RESP usa una conexión independiente y no forma parte de la transacción SQL del cliente. Para transacciones SQL, use PostgreSQL directamente; consulte la [guía de invalidación](cache-invalidation.md).

## Limpiar la demostración {#stop-the-demo}

```sh
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml down
```

`local_cache.mget(regclass, anyarray)` de 2.x se eliminó en la versión 3.0.0; consulte la [guía de actualización](UPGRADING.md).
