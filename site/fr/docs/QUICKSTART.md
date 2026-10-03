---
layout: doc
lang: fr
translation_key: QUICKSTART
title: Essayer pg_local_cache localement
seo_title: "Essayer un cache de lignes PostgreSQL localement | pg_local_cache"
description: Exécutez pg_local_cache 2.0 dans un PostgreSQL éphémère, lisez des lignes d'exemple, inspectez les hits du cache, testez les mises à jour et supprimez la démo sans modifier une base existante.
section: Démarrage rapide
permalink: /fr/docs/QUICKSTART.html
last_modified_at: "2026-09-16"
---

# Essayer pg_local_cache localement {#try-pg_local_cache-locally}

Cette démo compile pg_local_cache depuis votre checkout dans un serveur
PostgreSQL 16 séparé. Elle ne l'installe pas dans un serveur PostgreSQL
existant.

Vous avez besoin de Git, Docker et Docker Compose avec la prise en charge de
`up --wait`. L'image est compilée depuis les sources.

## Démarrer la base de données {#start-the-database}

```bash
git clone https://github.com/profundium/pg_local_cache.git
cd pg_local_cache
docker compose -f examples/compose.yaml up --build --wait
```

La démo lie PostgreSQL à `127.0.0.1:55432`, n'a pas d'écouteur RESP ni de
volume persistant et stocke les données dans un tmpfs local au conteneur.
L'arrêt du conteneur supprime ses données. `demo-only` est prévu pour cette
démo sur loopback ; utilisez vos propres identifiants en production.

Si le port 55432 est occupé, définissez `PGLC_DEMO_PORT` avant de démarrer
Compose et gardez-le défini pour l'exemple Node.js :

```bash
export PGLC_DEMO_PORT=55433
```

## Lire avec un rôle applicatif {#read-as-an-application-role}

La configuration crée 4 096 lignes dans `public.items`. Seule cette table est
attachée au cache. Le rôle `demo` n'est pas un superutilisateur.

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

Les deux appels renvoient les mêmes lignes dans le même ordre. La première et
la troisième position désignent la ligne 42. Les deux dernières positions
sont des `NULL` SQL : une entrée est nulle et la clé 999999 n'existe pas. Dans
psql, les valeurs nulles SQL apparaissent vides par défaut.

La fonction renvoie **`text[]`**. `unnest` ci-dessus affiche une entrée du
tableau par ligne.

Inspectez les compteurs en tant qu'administrateur de base :

```bash
docker compose -f examples/compose.yaml exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 -U postgres -d pglc_demo \
  -c 'SELECT local_cache.health();' \
  -c 'SELECT local_cache.stats();'
```

Dans cette démo fraîche, `local_cache.health()` doit indiquer `ready: true`,
et la répétition des lectures doit augmenter `sql_cache_hits`.
Si les hits restent à zéro, inspectez `sql_cache_misses`,
`sql_cache_fills` et `sql_cache_bypasses` avec le [guide d'invalidation](cache-invalidation.md#inspect-the-cause-of-a-miss).

## Vérifier commit et rollback {#check-commit-and-rollback}

Avec Node.js 20 ou ultérieur :

```bash
npm --prefix examples/node-postgres ci --ignore-scripts
npm --prefix examples/node-postgres run demo
```

Le test ouvre des connexions de lecture et d'écriture séparées. Il vérifie un
hit chaud, l'ordre d'entrée, les doublons et les clés absentes, une mise à jour
non validée, la lecture de ses propres écritures, le rollback et une mise à
jour validée. Il se termine avec un code non nul en cas d'assertion échouée.

Consultez le [parcours SQL à deux sessions](cache-invalidation.md) ou
[l'explication des requêtes Node.js](node-postgres.md).

## Connecter votre application {#connect-your-application}

- [Node.js](node-postgres.md) : utilisez votre connexion ou pool `pg` existant.
- [Go](go.md) : connectez-vous avec `pgx` et décodez les lignes renvoyées.
- [RESP](resp.md) : activez le point de terminaison optionnel et connectez-vous avec un client Redis.

Ensuite, [comparez la même charge SQL et RESP](BENCHMARKS.md#run-the-same-comparison-on-every-client).
Pour les résultats ou les problèmes de configuration, ouvrez un
[rapport de charge](https://github.com/profundium/pg_local_cache/issues/new?template=workload.yml)
avec votre environnement et le JSON du benchmark ou le journal d'erreur.

## Supprimer la démo {#remove-the-demo}

```bash
docker compose -f examples/compose.yaml down
```

L'image Docker compilée localement reste disponible pour une autre exécution.
Aucun service PostgreSQL de l'hôte ne doit être redémarré ni restauré.

Pour une base existante, suivez le [guide d'installation](INSTALL_EXISTING.md).
Ce chemin a des privilèges, une configuration et des exigences de redémarrage
différents.
