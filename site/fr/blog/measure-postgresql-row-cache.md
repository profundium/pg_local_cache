---
layout: post
lang: fr
translation_key: blog-measure-postgresql-row-cache
title: "Quand un cache de lignes PostgreSQL aide : mesurer toute la lecture"
description: Concevez une comparaison équitable du cache de lignes PostgreSQL avec SQL préparé et RESP MGET. Distinguez lectures chaudes, misses, tailles de lots, écritures et coûts clients.
permalink: /fr/blog/measure-postgresql-row-cache/
date: "2026-09-22"
last_modified_at: "2026-10-04"
topic: performance
---

# Quand un cache de lignes PostgreSQL aide {#when-a-postgresql-row-cache-helps}

Une base de données peut servir chaque page depuis la mémoire tout en passant
du temps à exécuter les requêtes, vérifier la visibilité et construire les
résultats. Un cache de lignes essaie d'éviter une partie de ce travail répété.
Il ajoute aussi la gestion des clés, les vérifications du cache et les coûts de
sérialisation. La bonne question est de savoir si la requête applicative
complète devient moins coûteuse pour votre charge.

> **Note de version du 04/10/2026 :** L’API SQL `local_cache.mget(regclass, anyarray)` de 2.x a été supprimée en 3.0.0 ; utilisez RESP `MGET` pour lire les lignes.

`pg_local_cache` propose un endpoint RESP `MGET` explicite pour lire des lignes complètes. Les requêtes `SELECT` ordinaires conservent leur chemin d’exécution PostgreSQL habituel. Un cache `shared_buffers` chaud et un cache de lignes chaud sont donc des conditions expérimentales différentes.

## Écrire d'abord le contrat de résultat {#result-contract}

Comparez les mêmes clés, colonnes et formes de sortie. Si l'application n'a
besoin que de deux colonnes, comparer cette projection SQL à des lignes
complètes sérialisées mesure des travaux différents. Si les appelants attendent
des doublons, l'ordre d'entrée et un résultat nul pour chaque clé absente,
incluez ce travail d'alignement dans chaque client.

Le [code source conservé de l’exemple Node.js](https://github.com/profundium/pg_local_cache/blob/master/examples/node-postgres/queries.mjs)
contient la référence préparée `ANY` et une fonction qui restaure les positions
d’entrée. L’[exemple ordonné `WITH ORDINALITY`](ordered-batch-reads.md#explicit-positions)
montre la gestion des positions en SQL. Aucun des deux n’exige l’extension.

## Modifier une seule dimension de charge à la fois {#workload-dimensions}

| Expérience | Ce qui reste fixe | Ce que cela révèle |
|---|---|---|
| Lectures chaudes répétées | Clés, forme du résultat, connexions | Réutilisation d'entrées déjà remplies |
| Clés froides ou absentes | Distribution des requêtes et taille du lot | Coûts de la table source et des résultats négatifs |
| Lots plus grands | Nombre total de clés demandées et forme de la charge utile | Économies d'allers-retours contre travail par clé |
| Écritures concurrentes | Mélange lecture/écriture et limites transactionnelles | Coûts d'invalidation, de remplissage et de visibilité |
| Lignes plus larges | Distribution des clés et placement du client | Coûts de sérialisation, de transport et contournement lié à la taille |

Une lecture inéligible peut légitimement utiliser la table source. Inspectez les
variations de compteurs autour de chaque expérience ; un faible taux de hits ne
permet pas à lui seul de diagnostiquer une installation défaillante. Gardez les
compteurs SQL et RESP séparés. La [référence technique](../docs/TECHNICAL.md#health-and-monitoring)
décrit `local_cache.stats()` et `local_cache.health()`.

## Utiliser le runner partagé, puis inspecter les preuves {#shared-runner}

Après le [démarrage rapide](../docs/QUICKSTART.md), exécutez la comparaison du
dépôt :

```bash
./examples/benchmark.sh all > comparison.json
python3 scripts/benchmark_report.py comparison.json
```

Le [guide des benchmarks](../docs/BENCHMARKS.md) liste les prérequis, les
contrôles de charge et les métriques. Conservez le JSON brut. Enregistrez les
révisions de l'extension et du harnais, la version PostgreSQL, la machine, le
nombre de connexions et le placement du client. Comparez les exécutions
répétées, les distributions de latence et les ressources serveur avec le débit.
Un court smoke test de correction n'est pas un résultat de vitesse publiable.

## Décider depuis la limite applicative {#application-boundary}

SQL préparé et RESP `MGET` utilisent des chemins de lecture et un traitement des résultats différents. Un gain pour l’un ne prouve pas un gain pour l’autre. Mesurez chaque chemin avec le profil réel des requêtes de votre application ; un résultat ne prédit pas les performances de toutes les charges.

Gardez le SQL ordinaire lorsque des jointures, projections, verrous ou formes de tables non prises en charge sont nécessaires, ou si le cache n’apporte aucun bénéfice mesuré. Pour des lectures répétées de lignes complètes par clé primaire, testez RESP `MGET` avec le même travail client que celui effectué par votre application. Consultez le [guide de décision sur la mise en cache](../docs/postgresql-caching.md) et [l’expérience d’invalidation](../docs/cache-invalidation.md).
