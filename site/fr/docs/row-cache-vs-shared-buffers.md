---
layout: doc
lang: fr
translation_key: row-cache-vs-shared-buffers
title: Cache de lignes PostgreSQL contre shared_buffers
seo_title: "Cache de lignes PostgreSQL contre shared_buffers | pg_local_cache"
description: Comparez la mise en cache des pages PostgreSQL au cache de lignes complètes pg_local_cache 2.0. Voyez ce qu'évite un hit, ce qu'il coûte encore et quand ne pas ajouter un autre cache.
section: Chemins de lecture
permalink: /fr/docs/row-cache-vs-shared-buffers.html
last_modified_at: "2026-09-16"
---

# Cache de lignes PostgreSQL contre shared_buffers {#postgresql-row-cache-vs-shared_buffers}

Les [`shared_buffers`](https://www.postgresql.org/docs/16/runtime-config-resource.html#GUC-SHARED-BUFFERS)
de PostgreSQL contiennent des pages de base de données. pg_local_cache stocke
séparément des charges utiles de lignes complètes sérialisées sous leurs clés
primaires complètes. Une page déjà en mémoire peut éviter une lecture du
stockage, mais une requête doit toujours produire un résultat à partir des
tuples de la base. Un hit du cache de lignes peut renvoyer la charge utile après
les vérifications d'éligibilité et de snapshot.

Le système d'exploitation peut aussi mettre en cache le contenu des fichiers.
Utilisez une base chaude comme référence.

{% include diagrams/read-path.html id="buffers-read" %}

## PostgreSQL met-il en cache les résultats de SELECT ? {#does-postgresql-cache-select-results}

`shared_buffers` met en cache les pages utilisées par une requête, plutôt que
son jeu de résultats final. Une [instruction préparée](https://www.postgresql.org/docs/18/sql-prepare.html)
réutilise le travail de parsing et peut réutiliser un plan, mais PostgreSQL
l'exécute toujours. pg_local_cache ajoute la mise en cache de lignes complètes
via des appels explicites à `mget` ; il ne met pas en cache les résultats de
`SELECT` arbitraires et ne réécrit pas les requêtes existantes. [L'exemple Node.js](node-postgres.md) montre les deux API de lecture côte à côte.

## Comparer le travail, pas seulement le support de stockage {#compare-the-work-not-just-the-storage-medium}

| Lecture | Travail restant |
|---|---|
| SQL préparé par clé primaire sur des pages chaudes | Gestion du protocole, exécution du plan, vérifications de visibilité des lignes et conversion du résultat |
| Hit éligible du cache SQL mget | Gestion du protocole, exécution de la fonction SQL, conversion de la clé, synchronisation du cache, vérifications du snapshot et renvoi de la charge utile stockée |
| Miss ou contournement SQL mget | Vérifications de la fonction et requête de la table source ; un remplissage éligible réussi peut alimenter le cache |

Un hit du cache de lignes évite l'exécution répétée de la table source et la
sérialisation de la ligne complète. Les vérifications et la synchronisation du
cache consomment aussi du CPU, et un hit utilise toujours une connexion et un
backend PostgreSQL. Cette API SQL ne supprime ni les limites de connexions ni
la mise en file d'attente du pool.

## Coûts à inclure {#costs-to-include}

Une ligne mise en cache utilise davantage de mémoire partagée même si sa page
source est déjà en mémoire. L'extension maintient aussi l'état des mappings et
de l'invalidation. Les mises à jour des tables attachées exécutent ses
triggers. Lorsque l'ensemble de travail dépasse la capacité, une optimisation
apparente des lectures peut devenir surtout un coût de miss et d'éviction.

La démo par défaut compare volontairement un ensemble chaud de 128 lignes avec
1 024 emplacements de cache, puis un premier passage sur 4 096 lignes. Le
[guide des benchmarks](BENCHMARKS.md) explique les deux cas et mesure
séparément les écritures sur les tables attachées.

## Quand laisser l'application tranquille {#when-to-leave-the-application-alone}

Conservez la requête existante lorsque sa latence de bout en bout est déjà
acceptable, lorsque l'application n'a besoin que d'une petite projection d'une
grande ligne, ou lorsque les jointures, plages et agrégations dominent.
Comparez d'abord une requête ordinaire par lots aux appels actuels par clé de
l'application. Un gain dû au regroupement ne prouve pas un gain dû à la mise en
cache.

pg_local_cache 2.0 exige des appels `mget` explicites, l'installation de
l'extension et un preload au démarrage. Il rejette les tables RLS,
partitionnées et héritées.

## Cache de lignes ou cache externe ? {#row-cache-or-an-external-cache}

Pour les données dont PostgreSQL reste la source d'autorité, cette conception
garde l'invalidation sur le chemin d'écriture de la base et évite de maintenir
un protocole cache-aside applicatif. Elle ne fournit pas la sémantique Redis
générale. Le point de terminaison RESP2 optionnel possède un ensemble limité de
commandes et un modèle de sécurité séparé.

Un cache de lignes PostgreSQL ne peut pas remplacer un état applicatif fondé
sur le TTL, pub/sub ou la coordination distribuée. Consultez le [contrat technique](TECHNICAL.md) et les [exemples transactionnels](cache-invalidation.md).
