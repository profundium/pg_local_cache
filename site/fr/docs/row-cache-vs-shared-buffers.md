---
layout: doc
lang: fr
translation_key: row-cache-vs-shared-buffers
title: Cache de lignes PostgreSQL et shared_buffers
seo_title: "Cache de lignes PostgreSQL et shared_buffers | pg_local_cache"
description: "Comparez le cache de pages PostgreSQL au cache de lignes complètes de pg_local_cache : travail source évité, coûts du cache et charges qui doivent conserver le SQL ordinaire."
section: Chemins de lecture
permalink: /fr/docs/row-cache-vs-shared-buffers.html
last_modified_at: "2026-10-04"
---

# Cache de lignes PostgreSQL et shared_buffers {#postgresql-row-cache-vs-shared_buffers}

Ce guide compare le cache de pages PostgreSQL au cache de lignes complètes de `pg_local_cache` et montre ce que chaque chemin de lecture doit encore faire.

[`shared_buffers`](https://www.postgresql.org/docs/18/runtime-config-resource.html#GUC-SHARED-BUFFERS) conserve les pages de la base en mémoire. Une page chaude peut éviter des E/S de stockage, mais PostgreSQL vérifie toujours la visibilité des tuples, exécute la requête et construit le résultat. Un hit RESP `MGET` admissible peut renvoyer une charge utile de ligne complète stockée après validation de la clé, de la génération du fence et de la charge utile.

Consultez la [référence technique du chemin de lecture](TECHNICAL.md#read-path-and-safe-fallback).

## PostgreSQL met-il en cache les résultats de SELECT ? {#does-postgresql-cache-select-results}

Non. Le cache de pages de PostgreSQL stocke des pages, pas les résultats finaux des requêtes. Une [instruction préparée](https://www.postgresql.org/docs/18/sql-prepare.html) peut réutiliser le travail de parsing et de planification ; PostgreSQL exécute tout de même la requête. `pg_local_cache` expose le cache de lignes complètes via RESP `MGET`, et non un cache de `SELECT` arbitraires. Consultez [Clients RESP](resp.md).

## Comparer le travail, pas seulement le support de stockage {#compare-the-work-not-just-the-storage-medium}

| Chemin de lecture | Travail restant |
|---|---|
| SQL préparé par clé primaire sur des pages chaudes | Protocole, exécution de la requête, vérifications de visibilité et conversion du résultat |
| Hit d’un `MGET` RESP admissible | Protocole, conversion de la clé, synchronisation du cache, contrôles d’admissibilité et renvoi de la charge utile |
| Miss ou contournement RESP | Vérifications du cache et lecture de la table source ; les lignes admissibles peuvent remplir le cache |

Un hit évite de répéter l’exécution sur la table source et la sérialisation de la ligne complète. Il utilise toujours un worker PostgreSQL et la synchronisation du cache. Les requêtes RESP s’exécutent indépendamment de la transaction SQL de l’appelant.

## Coûts à inclure {#costs-to-include}

Les lignes et l’état des correspondances consomment de la mémoire partagée supplémentaire. Les écritures dans les tables attachées exécutent des triggers d’invalidation. Un jeu de travail plus grand que la capacité du cache peut augmenter les misses et les évictions.

Mesurez les deux chemins avec le même jeu de clés, la même forme de ligne, le même nombre de connexions et le même mélange de requêtes. Comparez séparément le SQL par lots et la lecture d’une ligne : le traitement par lots peut réduire les allers-retours sans cache.

## Quand laisser l’application telle quelle {#when-to-leave-the-application-alone}

Gardez le SQL ordinaire si sa latence de bout en bout est acceptable, si l’application n’a besoin que d’une projection ou si les jointures, plages et agrégations dominent. Utilisez SQL pour les verrous de lignes et les lectures qui doivent partager une transaction.

## Cache de lignes ou cache externe ? {#row-cache-or-an-external-cache}

Utilisez `pg_local_cache` si PostgreSQL reste la source faisant autorité et si des lignes complètes sont souvent lues par clé primaire. Un cache externe convient à l’état applicatif avec TTL, au pub/sub, à la coordination distribuée ou aux objets partagés entre services. La [référence technique](TECHNICAL.md) décrit la frontière de sécurité RESP.
