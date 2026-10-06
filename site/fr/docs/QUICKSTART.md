---
layout: doc
lang: fr
translation_key: QUICKSTART
title: Essayer pg_local_cache localement
seo_title: "Essayer un cache de lignes PostgreSQL localement | pg_local_cache"
description: "Exécutez pg_local_cache 3.1.0 dans un PostgreSQL éphémère, lisez des lignes d’exemple via RESP, observez les hits du cache, testez les mises à jour et supprimez la démo sans modifier une base existante."
section: Démarrage rapide
permalink: /fr/docs/QUICKSTART.html
last_modified_at: "2026-09-16"
---

# Essayer pg_local_cache localement {#try-pg_local_cache-locally}

Cette démo construit pg_local_cache depuis votre copie dans un serveur PostgreSQL 16 séparé. Elle ne l’installe pas sur un serveur PostgreSQL existant. L'exemple de lecture utilise le listener RESP configuré par la surcouche Compose.

Vous avez besoin de Git, Docker et Docker Compose avec la prise en charge de
`up --wait`. L'image est compilée depuis les sources.

## Démarrer la base de données {#start-the-database}

```bash
git clone https://github.com/profundium/pg_local_cache.git
cd pg_local_cache
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml up --build --wait
```

La démo lie PostgreSQL et RESP aux ports loopback `55432` et `56379`, sans volume persistant, et stocke les données dans un tmpfs local au conteneur. La surcouche RESP se lie dans le réseau du conteneur et active explicitement son écoute en clair de démonstration. L’arrêt du conteneur supprime les données. `demo-only` et le jeton RESP public sont réservés à cette démo loopback ; utilisez vos propres identifiants en production.

Si le port 55432 est occupé, définissez `PGLC_DEMO_PORT` avant de démarrer
Compose et gardez-le défini pour l'exemple Node.js :

```bash
export PGLC_DEMO_PORT=55433
```

## Lire via RESP {#read-as-an-application-role}

La configuration crée 4 096 lignes dans `public.items`. Seule cette table est attachée au cache. Le rôle `demo` n’est pas superutilisateur.

```bash
export REDISCLI_AUTH=DemoRespToken_0123456789abcdef0123456789
redis-cli -2 -p 56379 MGET \
  'CRUD:pglc_demo.public.items:{"id":42}' \
  'CRUD:pglc_demo.public.items:{"id":7}' \
  'CRUD:pglc_demo.public.items:{"id":42}' \
  'CRUD:pglc_demo.public.items:{"id":999999}'
```

La réponse conserve l’ordre des clés et les doublons. Les première et troisième positions correspondent à la ligne 42 ; la dernière est RESP null, car la clé 999999 n’existe pas. Une requête RESP omet les clés d’entrée nulles ; les helpers client peuvent rétablir ces positions si nécessaire.

Inspectez les compteurs en tant qu’administrateur de base :

```bash
docker compose -f examples/compose.yaml exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 -U postgres -d pglc_demo \
  -c 'SELECT local_cache.health();' \
  -c 'SELECT local_cache.stats();'
```

Dans cette démo fraîche, `local_cache.health()` doit indiquer `ready: true`, et la répétition des lectures doit augmenter `cache_hits`. Si besoin, consultez `cache_misses`, `database_reads` et `cache_enabled` dans `local_cache.stats()` et `local_cache.health()`.

## Vérifier commit et rollback {#check-commit-and-rollback}

Avec Node.js 20 ou ultérieur :

```bash
npm --prefix examples/node-postgres ci --ignore-scripts
npm --prefix examples/node-postgres run demo
```

Le test utilise RESP pour les lectures et PostgreSQL pour les écritures. Il vérifie un hit chaud, l’ordre d’entrée, les doublons et clés absentes, l’invalidation du cache et la visibilité des mises à jour validées. Les workers RESP utilisent un rôle PostgreSQL configuré et ne partagent ni la transaction SQL ni le snapshot de l’application.

Consultez le [guide d’invalidation du cache](cache-invalidation.md) ou [l’explication des requêtes Node.js](resp.md#nodejs).

## Connecter votre application {#connect-your-application}

- [Node.js](resp.md#nodejs) : utilisez RESP pour les lectures mises en cache et `pg` pour les écritures SQL.
- [Go](resp.md#go) : utilisez RESP pour les lectures mises en cache et `pgx` pour les écritures SQL.
- [RESP](resp.md) : connectez-vous avec un client Redis.

Ensuite, [comparez la même charge en SQL préparé et RESP](BENCHMARKS.md#run-the-same-comparison-on-every-client). Pour signaler des résultats ou un problème de configuration, ouvrez un [rapport de charge](https://github.com/profundium/pg_local_cache/issues/new?template=workload.yml) avec l’environnement, le JSON du benchmark ou le journal d’erreur.

## Supprimer la démo {#remove-the-demo}

```bash
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml down
```

L'image Docker compilée localement reste disponible pour une autre exécution.
Aucun service PostgreSQL de l'hôte ne doit être redémarré ni restauré.

Pour une base existante, suivez le [guide d'installation](INSTALL_EXISTING.md).
Ce chemin a des privilèges, une configuration et des exigences de redémarrage
différents.
