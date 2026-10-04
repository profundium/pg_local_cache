---
layout: doc
lang: fr
translation_key: cache-invalidation
title: Invalidation du cache PostgreSQL tenant compte des transactions
seo_title: "Invalidation du cache PostgreSQL : commit et rollback | pg_local_cache"
description: Découvrez comment les triggers PostgreSQL établissent des barrières pour les lectures de lignes RESP lors des commits, rollbacks et remplissages concurrents du cache.
section: Invalidation du cache
permalink: /fr/docs/cache-invalidation.html
last_modified_at: "2026-10-04"
---

# Invalidation du cache PostgreSQL tenant compte des transactions {#transaction-aware-cache-invalidation-in-postgresql}

Ce guide explique comment les triggers des tables attachées empêchent un hit RESP obsolète après la validation d’une écriture PostgreSQL.

![Invalidation à l’écriture : les mises à jour validées publient une barrière ; un rollback avant sa publication préserve la validité de l’entrée.](../../docs/diagrams/write-invalidation.svg)

Un trigger enregistre les clés ou la relation modifiées dans la transaction d’écriture. Au commit, l’extension publie des barrières d’invalidation et avance les générations. Un remplissage commencé avant la barrière ne peut pas publier de données obsolètes. Un rollback avant la publication de la barrière abandonne l’état modifié de la transaction et préserve les anciennes entrées. Si la transaction est annulée après la publication, l’invalidation n’est pas annulée et les entrées concernées restent invalides.

Consultez la [référence technique de cohérence](TECHNICAL.md#health-and-monitoring) pour le contrat complet du chemin de lecture.

## Vérifier l’invalidation entre SQL et RESP {#test-with-two-sessions}

Démarrez la [démo locale](QUICKSTART.md), puis lisez la même clé via RESP et PostgreSQL :

```text
MGET CRUD:pglc_demo.public.items:{"id":42}
```

Dans une session SQL séparée, mettez à jour la ligne et validez la transaction :

```sql
BEGIN;
UPDATE public.items SET revision = revision + 1 WHERE id = 42;
COMMIT;
```

La commande RESP suivante renvoie la version validée. Si l’écriture est annulée par un rollback, RESP continue de renvoyer la dernière version validée.

RESP utilise le rôle PostgreSQL configuré dans une transaction courte indépendante. Il ne partage ni le rôle, ni la transaction, ni le snapshot de l’application. Utilisez SQL dans la transaction de l’application pour lire vos propres écritures et exécuter `SELECT ... FOR UPDATE`.

## Cas qui contournent le cache {#cases-that-deliberately-bypass-the-cache}

Quand `pg_local_cache.enabled` est désactivé, RESP `MGET` ignore la recherche et le remplissage du cache et lit la table source. Une barrière d’écriture active pour une clé, une relation ou globalement bloque aussi les lectures depuis le cache et les nouveaux remplissages ; la lecture passe alors par la table source. Les workers RESP démarrent après la fin de la récupération ; celle-ci ne constitue donc pas une condition distincte de contournement. Une ligne trop volumineuse pour une entrée du cache peut quand même être renvoyée par PostgreSQL si son JSON respecte la limite RESP, sans être stockée.

## Examiner la cause d’un miss {#inspect-the-cause-of-a-miss}

Comparez `local_cache.stats()` et `local_cache.health()` avant et après une charge contrôlée. Vérifiez les compteurs de contournement, de miss, d’invalidation et de rechargement des correspondances. Consultez la [liste des métriques techniques](TECHNICAL.md#health-and-monitoring).
