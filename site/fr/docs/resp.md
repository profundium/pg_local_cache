---
layout: doc
lang: fr
translation_key: resp
title: Se connecter via RESP
description: Lisez des lignes PostgreSQL via RESP2 avec redis-cli ou Node.js. Authentification, paramètres client, exemples exécutables et nettoyage inclus.
section: RESP
permalink: /fr/docs/resp.html
last_modified_at: "2026-10-04"
---

# Se connecter via RESP {#connect-over-resp}

Utilisez `MGET` pour lire les lignes PostgreSQL mises en cache avec un client
RESP2. Démarrez la [démo éphémère](QUICKSTART.md) avec sa configuration RESP :

```bash
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml up --build --wait
```

Cela active RESP sur `127.0.0.1:56379`. Recréer la démo supprime ses données.
Le token ci-dessous est public et réservé à cette démo locale.

Le listener RESP écoute par défaut sur `127.0.0.1`. Privilégiez TLS au-delà de
loopback ; TLS RESP utilise des paramètres propres, indépendants des paramètres
`ssl_*` de PostgreSQL. Lorsque TLS est désactivé, une écoute en clair hors
loopback exige l'opt-in explicite `pg_local_cache.allow_plaintext_network=on`, à
limiter aux réseaux de confiance. La démo ne l'active que dans son réseau de
conteneurs. Consultez [les clients TLS et mTLS](#tls-mtls-clients).
`pg_local_cache.enabled` est un coupe-circuit SIGHUP. Chaque worker RESP
applique le rechargement de manière asynchrone à sa prochaine limite entre
commandes, une fois terminée toute commande en cours. Le champ `cache_enabled`
de `local_cache.health()` indique la valeur vue par la session SQL qui appelle
la fonction ; il ne confirme pas que tous les workers l'ont appliquée. Pour
désactiver les lectures du cache sans redémarrer, exécutez `ALTER SYSTEM SET pg_local_cache.enabled = off;` puis `SELECT pg_reload_conf();` ; RESP lit alors
directement dans la table source.

## Clients TLS et mTLS {#tls-mtls-clients}

Privilégiez TLS pour accéder à RESP au-delà de loopback. La configuration TLS
native de RESP est indépendante des paramètres `ssl_*` de PostgreSQL. Ces
exemples utilisent RESP2 et mTLS : `cache.example` doit correspondre au
certificat serveur, `./ca.crt` doit lui faire confiance et le certificat client
doit être signé par la CA configurée dans `pg_local_cache.tls_ca_file`. Pour un
TLS avec authentification du serveur uniquement, omettez les options de
certificat et de clé client.

**redis-cli**

```bash
export REDISCLI_AUTH="$PGLC_RESP_TOKEN"
redis-cli -2 --tls --cacert ./ca.crt --cert ./client.crt --key ./client.key -h cache.example -p 6380 MGET 'CRUD:app.public.items:{"id":42}'
```

**go-redis**

```go
package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"log"
	"os"

	"github.com/redis/go-redis/v9"
)

func main() {
	caPEM, err := os.ReadFile("./ca.crt")
	if err != nil {
		log.Fatal(err)
	}
	roots := x509.NewCertPool()
	if ok := roots.AppendCertsFromPEM(caPEM); !ok {
		log.Fatal("no CA certificates found")
	}
	clientCert, err := tls.LoadX509KeyPair("./client.crt", "./client.key")
	if err != nil {
		log.Fatal(err)
	}

	client := redis.NewClient(&redis.Options{
		Addr:            "cache.example:6380",
		Password:        os.Getenv("PGLC_RESP_TOKEN"),
		Protocol:        2,
		DisableIdentity: true,
		TLSConfig: &tls.Config{
			RootCAs:      roots,
			MinVersion:   tls.VersionTLS12,
			ServerName:   "cache.example",
			Certificates: []tls.Certificate{clientCert},
		},
	})
	defer client.Close()

	rows, err := client.MGet(context.Background(), `CRUD:app.public.items:{"id":42}`).Result()
	if err != nil {
		log.Fatal(err)
	}
	log.Printf("%v", rows)
}
```

**node-redis**

```js
import { readFileSync } from 'node:fs';
import { createClient } from '@redis/client';

const client = createClient({
  socket: {
    host: 'cache.example',
    port: 6380,
    tls: true,
    servername: 'cache.example',
    ca: readFileSync('./ca.crt'),
    cert: readFileSync('./client.crt'),
    key: readFileSync('./client.key'),
  },
  password: process.env.PGLC_RESP_TOKEN,
  RESP: 2,
  disableClientInfo: true,
});
client.on('error', console.error);
await client.connect();

try {
  const values = await client.mGet(['CRUD:app.public.items:{"id":42}']);
  const rows = values.map(value => value === null ? null : JSON.parse(value));
  console.log(rows);
} finally {
  await client.close();
}
```

## redis-cli {#redis-cli}

```bash
export REDISCLI_AUTH=DemoRespToken_0123456789abcdef0123456789
redis-cli -2 -p 56379 MGET 'CRUD:pglc_demo.public.items:{"id":42}'
```

La réponse contient la ligne 42 au format JSON. Les lignes absentes renvoient
`nil`. [redis-cli](https://redis.io/docs/latest/develop/tools/cli/) lit le
jeton depuis `REDISCLI_AUTH`.

## Node.js {#nodejs}

```bash
npm --prefix examples/node-postgres ci --ignore-scripts
npm --prefix examples/node-postgres run resp
```

L’exemple utilise le paquet officiel `@redis/client` et vérifie l’ordre, les
doublons, les positions d’entrée nulles et les lignes absentes. Son helper
omet les clés nulles sur le réseau et rétablit leurs positions après décodage.
Pour vous connecter depuis votre application :

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

Définissez `PGLC_RESP_TOKEN` avec le jeton du serveur. Gardez cette connexion
ouverte entre les requêtes. Ces [paramètres client](https://github.com/redis/node-redis/blob/master/docs/client-configuration.md)
sélectionnent RESP2 et ignorent les commandes de métadonnées propres à Redis.
Utilisez une authentification par jeton sans nom d’utilisateur ni numéro de base
de données Redis.

## Comparer au SQL {#compare-with-sql}

Le [benchmark commun](BENCHMARKS.md#run-the-same-comparison-on-every-client) exécute RESP `MGET` et SQL préparé avec les mêmes clés et résultats décodés dans Node.js et Go. Les mesures publiées de SQL `mget` en 2.x sont historiques ; cette voie de test a été supprimée en 3.0.0. Pour une mise en cache applicative plus large, lisez le [guide cache-aside PostgreSQL et Redis](postgresql-redis-cache.md).

## Arrêter la démo {#stop-the-demo}

```bash
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml down
```
