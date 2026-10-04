---
layout: doc
lang: fr
translation_key: resp
title: Clients RESP
description: Connectez des clients compatibles avec Redis pour lire des lignes PostgreSQL via RESP2 MGET, avec authentification par jeton et TLS natif.
section: RESP
permalink: /fr/docs/resp.html
redirect_from:
  - /fr/docs/go.html
  - /fr/docs/node-postgres.html
last_modified_at: "2026-10-04"
---

# Clients RESP {#resp-clients}

Connectez des clients compatibles avec Redis pour lire des lignes PostgreSQL entières par clé primaire. Cette page décrit l’encodage des clés, l’authentification, les réponses, les erreurs et TLS.

<a id="connect-over-resp"></a>

## Contrat des clés et des réponses {#key-and-response-contract}

Les clés RESP identifient une table attachée et un objet de clé primaire :

```text
CRUD:<db>.<schema>.<table>:<json pk>
CRUD:app.public.items:{"id":42}
CRUD:app.public.orders:{"tenant_id":7,"id":42}
```

`MGET key [key ...]` renvoie un tableau RESP2 dans l’ordre de la requête. Les clés en double conservent leur position. Les lignes existantes sont des chaînes bulk JSON ; les lignes absentes sont des éléments nil.

| Limite | Comportement |
|---|---|
| 1 024 clés par commande | Les lots plus grands renvoient `ERR MGET accepts at most 1024 keys`. |
| 65 536 octets par ligne JSON | Une ligne plus grande ne peut pas être renvoyée via RESP. |
| 66 560 octets par réponse MGET encodée | Les réponses plus volumineuses renvoient `ERR response exceeds limit`. |

La taille d’un lot est limitée à la fois par le nombre de clés et par le nombre d’octets de la réponse. Scindez les lots si une ligne volumineuse risque d’approcher la limite de réponse.

## AUTH {#auth}

Envoyez `AUTH <token>` avant toute autre commande. Les clients peuvent aussi envoyer `AUTH <username> <token>` ; le nom d’utilisateur doit correspondre à `pg_local_cache.role`. Le jeton est partagé entre les clients RESP et ne sélectionne pas les privilèges PostgreSQL client par client. Stockez-le dans `pg_local_cache.auth_token_file` avec le mode `0400` ou `0600` ; réservez `auth_token` intégré au développement. Les listeners hors loopback exigent un jeton d’au moins 32 octets.

## TLS {#tls}

Le TLS RESP natif utilise les paramètres `pg_local_cache.tls_*` et est distinct du TLS SQL de PostgreSQL. Il nécessite une version de PostgreSQL avec OpenSSL, un certificat serveur et une clé privée. Le paramètre `tls_ca_file` rend également les certificats clients obligatoires et les vérifie (mTLS). Omettez les options de certificat client pour un TLS avec authentification du serveur uniquement. La version minimale par défaut est TLS 1.2.

Par défaut, le listener est lié à loopback. Si TLS est désactivé, un listener en clair hors loopback exige `pg_local_cache.allow_plaintext_network=on`. Consultez la [référence technique](TECHNICAL.md#optional-resp2-endpoint) pour les paramètres d’écoute et de sécurité.

## redis-cli {#redis-cli}

Définissez `PGLC_RESP_TOKEN` avec le jeton configuré. Le guide de démarrage local fournit aussi un jeton de démonstration.

```sh
export REDISCLI_AUTH="$PGLC_RESP_TOKEN"
redis-cli -2 -h 127.0.0.1 -p 56379 MGET 'CRUD:pglc_demo.public.items:{"id":42}'
redis-cli -2 --tls --cacert ./ca.crt --cert ./client.crt --key ./client.key -h cache.example -p 6380 MGET 'CRUD:app.public.items:{"id":42}'
```

## Go {#go}

Installez `github.com/redis/go-redis/v9`. Définissez `PGLC_RESP_TOKEN` ; la variante TLS utilise des fichiers CA et certificat client.

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

Variante TLS :

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

Installez avec `npm install redis@^4`. Redis v4 utilise RESP2 par défaut.

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

Variante TLS :

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

Installez avec `python -m pip install redis`.

```python
import os
import redis

client = redis.Redis(host="127.0.0.1", port=56379,
                     password=os.environ["PGLC_RESP_TOKEN"],
                     decode_responses=True, protocol=2)
print(client.execute_command('MGET', 'CRUD:pglc_demo.public.items:{"id":42}'))
client.close()
```

Variante TLS :

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

## Erreurs et limites {#errors}

| Réponse | Cause |
|---|---|
| `NOAUTH Authentication required` | Authentifiez d’abord la connexion. |
| `WRONGPASS invalid authentication token` | Le jeton ou le nom d’utilisateur facultatif est incorrect. |
| `ERR MGET accepts at most 1024 keys` | Scindez le lot. |
| `ERR response exceeds limit` | Réduisez la taille du lot ou la charge utile des lignes. |
| `ERR MGET deadline exceeded` | La lecture source et l’attente sur la même clé ont dépassé le délai de la commande. |
| `ERR KVik key targets a different database` | Utilisez la base configurée pour le point de terminaison RESP. |
| `ERR unknown KVik table mapping` | Vérifiez que la clé désigne un schéma et une table associés. |
| `ERR key must use CRUD:database.schema.table:{primary-key-json}` | Utilisez le format CRUD complet de la clé. |
| `ERR KVik key must end with a primary-key JSON object` | Terminez la portée de la table par un objet JSON de clé primaire. |
| `ERR invalid CRUD cache scope` | `INVALIDATE` uniquement : la portée fournie n’est pas une portée CRUD prise en charge. |

RESP est indépendant de la connexion SQL, du rôle, de la transaction et du snapshot de l’appelant. Pour les transactions SQL, utilisez directement PostgreSQL ; consultez le [guide d’invalidation](cache-invalidation.md).

## Nettoyage de la démonstration {#stop-the-demo}

```sh
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml down
```

La fonction SQL `local_cache.mget(regclass, anyarray)` de la version 2.x a été supprimée dans la version 3.0.0 ; consultez le [guide de mise à niveau](UPGRADING.md).
