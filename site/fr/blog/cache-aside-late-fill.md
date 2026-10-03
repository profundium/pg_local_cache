---
layout: post
lang: fr
translation_key: blog-cache-aside-late-fill
title: "Invalidation du cache PostgreSQL : la course au remplissage tardif"
description: Parcourez une course cache-aside où une ancienne lecture remplit une clé supprimée après le commit. Comprenez la validation des remplissages, les snapshots, le rollback et les caches locaux aux requêtes.
permalink: /fr/blog/cache-aside-late-fill/
date: "2026-09-22"
last_modified_at: "2026-09-22"
topic: correctness
---

# Invalidation du cache et course au remplissage tardif {#cache-invalidation-and-the-late-fill-race}

« Supprimer la clé du cache après la mise à jour de la base » laisse un
problème de timing : une autre requête peut déjà être en train de charger
l'ancienne valeur. La suppression retire l'entrée qui existe maintenant ; elle
n'annule pas un résultat encore en cours.

## Suivre les deux requêtes {#two-requests}

Supposons que PostgreSQL contienne la révision 0 et qu'une application utilise
des lectures cache-aside. Cette séquence peut arriver même lorsque l'écrivain
invalide après le commit :

| Étape | Lecteur A | Écrivain B |
|---|---|---|
| 1 | Rate le cache et lit la révision 0 | |
| 2 | Se met en pause avant de stocker le résultat | Met la ligne à jour vers la révision 1 |
| 3 | | Valide et supprime la clé du cache |
| 4 | Publie la révision 0 précédemment lue | |
| 5 | Une requête ultérieure lit la valeur obsolète du cache | |

Un TTL peut limiter la durée pendant laquelle cette valeur reste éligible. Il
ne rend pas l'étape 4 correcte. Déplacer la suppression avant le commit crée
un autre intervalle pendant lequel un lecteur peut repeupler le cache depuis
l'ancien état validé de la base. Consultez le [guide PostgreSQL et Redis](../docs/postgresql-redis-cache.md)
pour la limite entre le cache-aside applicatif et le cache de lignes local à la
base.

## Valider la publication comme la recherche {#publication}

Un remplissage du cache a besoin de preuves que son résultat est encore
éligible au moment de sa publication. Dans `pg_local_cache` 2.0, les écritures
mettent en place une barrière pour les clés ou relations concernées ; les remplissages portent des
informations de génération et les entrées positives du cache portent des
informations de visibilité du tuple. Une génération modifiée peut rejeter un
ancien remplissage. Une lecture qui ne peut pas utiliser une entrée de manière
sûre revient à PostgreSQL.

Ces vérifications appartiennent à un chemin de lecture PostgreSQL précis. Elles
ne transforment pas le point de terminaison RESP optionnel en serveur Redis
généraliste et n'invalident pas les valeurs qu'une application a déjà copiées
ailleurs.

## Tester rollback et commit {#test-transactions}

Utilisez le [test à deux sessions](../docs/cache-invalidation.md#test-with-two-sessions)
sur une base éphémère. Réchauffez la ligne dans une session. Dans une autre,
mettez la ligne à jour en gardant la transaction ouverte. Vérifiez trois
observations :

1. L'écrivain peut lire sa propre modification via le chemin de la table source.
2. L'autre session voit toujours la valeur validée pendant que l'écriture est ouverte.
3. Après le rollback, la valeur originale reste présente ; après le commit, une nouvelle instruction sous `READ COMMITTED` voit la nouvelle révision.

La troisième condition concerne une nouvelle instruction. Une instruction
commencée plus tôt n'a pas à adopter un snapshot plus récent en cours
d'exécution. Le [contrat transactionnel](../docs/TECHNICAL.md#transaction-consistency)
décrit aussi les modes qui contournent le cache. Utilisez le SQL ordinaire
pour verrouiller les lignes.

## Vérifier le cache suivant dans l'application {#application-cache}

Un DataLoader local à la requête peut encore contenir une valeur chargée avant
une mutation. L'invalidation de la base ne peut pas retirer cet objet
JavaScript. Supprimez ou remplacez l'entrée concernée après une mutation selon
les règles d'autorisation et le contrat de résultat de l'application. Gardez
les loaders limités aux requêtes.

Le [guide des lots](../docs/batch-primary-key-lookups.md#graphql-dataloader-and-n1-reads)
sépare la mémoïsation de requête du cache de lignes partagé. Pour diagnostiquer
une réponse obsolète, suivez chaque point de stockage entre le snapshot de la
base et l'objet de réponse. La correction à une limite ne vide pas les autres.
