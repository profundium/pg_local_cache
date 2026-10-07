---
layout: doc
lang: de
translation_key: resp
title: RESP-Clients
description: "Redis-kompatible Clients über RESP2 mit pg_local_cache verbinden: MGET, Token-Authentifizierung und natives TLS."
section: RESP
permalink: /de/docs/resp.html
redirect_from:
  - /de/docs/go.html
  - /de/docs/node-postgres.html
last_modified_at: "2026-10-04"
---

# RESP-Clients {#resp-clients}

Verbinden Sie Redis-kompatible Clients, um vollständige PostgreSQL-Zeilen anhand des Primärschlüssels zu lesen. Diese Seite behandelt Schlüsselcodierung, Authentifizierung, Antwortverhalten, Fehler und TLS.

<a id="connect-over-resp"></a>

## Schlüssel- und Antwortvertrag {#key-and-response-contract}

RESP-Schlüssel identifizieren eine angehängte Tabelle und ein Primärschlüsselobjekt:

```text
CRUD:<db>.<schema>.<table>:<json pk>
CRUD:app.public.items:{"id":42}
CRUD:app.public.orders:{"tenant_id":7,"id":42}
```

`MGET key [key ...]` liefert ein RESP2-Array in Anfrage-Reihenfolge. Duplikate behalten ihre Position. Vorhandene Zeilen werden als JSON-Bulk-Strings zurückgegeben, fehlende Zeilen als `nil`-Elemente.

| Limit | Verhalten |
|---|---|
| Höchstens 1.024 Schlüssel pro Befehl | Innerhalb des Byte-Limits liefert ein Befehl mit 1.025 Schlüsseln `ERR MGET accepts at most 1024 keys`; ab 1.026 Schlüsseln liefert der Parser `ERR invalid argument count`. |
| 65.536 Byte pro codierter Anfrage | Ist der Eingabepuffer voll, bevor eine vollständige Anfrage geparst wurde, schließt der Server die Verbindung. |
| 65.536 Byte pro JSON-Zeile | Eine größere Zeile kann nicht über RESP zurückgegeben werden. |
| 66.560 Byte pro codierter MGET-Antwort | Größere Gesamtantworten liefern `ERR response exceeds limit`. |

Die Batch-Größe ist durch Schlüsselanzahl, codierte Anfragebytes und Antwortbytes begrenzt. Halten Sie jede codierte Anfrage innerhalb des 65.536-Byte-Limits; teilen Sie Batches auf, wenn große Schlüssel oder Zeilen sich einem Byte-Limit nähern.

## AUTH {#auth}

Senden Sie vor allen anderen Befehlen `AUTH <token>`. Clients können auch `AUTH <username> <token>` senden; der Benutzername muss `pg_local_cache.role` entsprechen. Das Token gilt für alle RESP-Clients gemeinsam und legt keine PostgreSQL-Berechtigungen pro Client fest. Speichern Sie es in `pg_local_cache.auth_token_file` mit Modus `0400` oder `0600`; reservieren Sie das Inline-`auth_token` für die Entwicklung. Listener außerhalb von Loopback benötigen ein Token mit mindestens 32 Byte.

## TLS {#tls}

Für natives RESP-TLS gelten die Einstellungen `pg_local_cache.tls_*`; es ist getrennt vom SQL-TLS von PostgreSQL. Erforderlich sind ein PostgreSQL-Build mit OpenSSL, ein Serverzertifikat und ein privater Schlüssel. Mit `tls_ca_file` werden außerdem Clientzertifikate verlangt und geprüft (mTLS). Bei TLS mit alleiniger Serverauthentifizierung lassen Sie die Clientzertifikat-Optionen weg. Die minimale Version ist standardmäßig TLS 1.2.

Der Listener bindet standardmäßig an Loopback. Wenn TLS ausgeschaltet ist, erfordert ein Klartext-Listener außerhalb von Loopback `pg_local_cache.allow_plaintext_network=on`. Listener- und Sicherheitseinstellungen finden Sie in der [technischen Referenz](TECHNICAL.md#optional-resp2-endpoint).

## redis-cli {#redis-cli}

Setzen Sie `PGLC_RESP_TOKEN` auf das konfigurierte Token. Der lokale Schnellstart stellt außerdem ein Demo-Token bereit.

```sh
export REDISCLI_AUTH="$PGLC_RESP_TOKEN"
redis-cli -2 -h 127.0.0.1 -p 56379 MGET 'CRUD:pglc_demo.public.items:{"id":42}'
redis-cli -2 --tls --cacert ./ca.crt --cert ./client.crt --key ./client.key -h cache.example -p 6380 MGET 'CRUD:app.public.items:{"id":42}'
```

## Go {#go}

Installieren Sie `github.com/redis/go-redis/v9`. Setzen Sie `PGLC_RESP_TOKEN`; die TLS-Variante verwendet CA- und Clientzertifikatdateien.

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

TLS-Variante:

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

Installieren Sie mit `npm install redis@^4`. Redis v4 verwendet standardmäßig RESP2.

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

TLS-Variante:

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

Installieren Sie mit `python -m pip install redis`.

```python
import os
import redis

client = redis.Redis(host="127.0.0.1", port=56379,
                     password=os.environ["PGLC_RESP_TOKEN"],
                     decode_responses=True, protocol=2)
print(client.execute_command('MGET', 'CRUD:pglc_demo.public.items:{"id":42}'))
client.close()
```

TLS-Variante:

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

## Fehler und Grenzen {#errors}

| Antwort | Ursache |
|---|---|
| `NOAUTH Authentication required` | Authentifizieren Sie die Verbindung zuerst. |
| `WRONGPASS invalid authentication token` | Token oder optionaler Benutzername ist falsch. |
| `ERR MGET accepts at most 1024 keys` | Der Befehl enthält 1.025 Schlüssel; teilen Sie ihn auf. |
| `ERR invalid argument count` | Der Parser hat zu viele Argumente abgelehnt; ein MGET mit mindestens 1.026 Schlüsseln überschreitet sein Argumentlimit. |
| `ERR busy: relation locked, retry` | Die Deferred-Miss-Queue ist bei gesperrter Relation voll; wiederholen Sie die Anfrage. |
| `ERR response exceeds limit` | Verringern Sie Batch-Größe oder Zeilen-Payload. |
| `ERR PostgreSQL: …` | PostgreSQL- oder SPI-Fehler, einschließlich PostgreSQL-Statement- oder Lock-Timeout bzw. Abbruch. |
| `ERR MGET deadline exceeded` | Die ausdrückliche gemeinsame MGET-Frist lief ab, auch während der Warteschlange auf eine Relationssperre; sie unterscheidet sich von PostgreSQL-Statement-/Lock-Timeouts. |
| `ERR KVik key targets a different database` | Verwenden Sie die für den RESP-Endpunkt konfigurierte Datenbank. |
| `ERR unknown KVik table mapping` | Prüfen Sie, ob Schlüssel-Schema und -Tabelle zugeordnet sind. |
| `ERR key must use CRUD:database.schema.table:{primary-key-json}` | Verwenden Sie das vollständige CRUD-Schlüsselformat. |
| `ERR KVik key must end with a primary-key JSON object` | Beenden Sie den Tabellenbereich mit einem JSON-Primärschlüsselobjekt. |
| `ERR invalid CRUD cache scope` | Nur für `INVALIDATE`: Der angegebene Bereich ist kein unterstützter CRUD-Bereich. |

RESP verwendet eine separate Verbindung und ist nicht Teil der SQL-Transaktion des Aufrufers. Verwenden Sie für SQL-Transaktionen direkt PostgreSQL; siehe den [Leitfaden zur Invalidierung](cache-invalidation.md).

## Demo bereinigen {#stop-the-demo}

```sh
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml down
```

`local_cache.mget(regclass, anyarray)` aus 2.x wurde in Version 3.0.0 entfernt; siehe den [Upgrade-Leitfaden](UPGRADING.md).
