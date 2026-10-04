---
layout: doc
lang: fr
translation_key: postgresql-redis-cache
title: Cache-aside PostgreSQL et Redis
seo_title: "PostgreSQL et Redis cache-aside : invalidation et conditions de course"
description: Comparez le cache-aside Redis géré par l’application aux lectures RESP de pg_local_cache, notamment les courses de remplissage obsolète et l’invalidation des écritures.
section: Guides
permalink: /fr/docs/postgresql-redis-cache.html
last_modified_at: "2026-10-04"
---

# PostgreSQL et Redis cache-aside {#postgresql-and-redis-cache-aside}

Ce guide présente le cache-aside Redis avec PostgreSQL comme source de vérité, sa course de remplissage obsolète et la façon dont `pg_local_cache` invalide les lignes attachées.

Lors d’un miss Redis, l’application lit la ligne PostgreSQL, la renvoie et la stocke sous une clé applicative. Lors d’une écriture, elle valide les données PostgreSQL puis supprime la clé Redis. Un TTL limite la durée de conservation ; il ne garantit pas la fraîcheur. Consultez le [guide Redis sur le cache-aside](https://redis.io/docs/latest/develop/use-cases/cache-aside/).

## La course d’invalidation {#the-invalidation-race}

Un lecteur peut charger une ancienne ligne PostgreSQL, s’interrompre, puis la stocker après qu’un écrivain a validé ses changements et supprimé la clé. Le lecteur suivant voit alors des données obsolètes jusqu’à l’expiration du TTL ou une autre suppression.

Pour atténuer cette course, on peut rejeter les remplissages portant une ancienne version de la base, sérialiser les chargements d’une même clé ou publier les changements validés via un outbox ou un consommateur CDC. Chaque méthode ajoute de la coordination. Consultez le [guide d’invalidation transactionnelle](cache-invalidation.md) pour la frontière équivalente des remplissages tardifs dans PostgreSQL.

## Où se place pg_local_cache {#where-pg_local_cache-fits}

`pg_local_cache` stocke des lignes complètes par clé primaire dans la mémoire partagée limitée de PostgreSQL. Les applications demandent ces lignes avec `MGET` authentifié via RESP2 ; le SQL ordinaire et les résultats arbitraires de requêtes n’utilisent pas ce cache. Les triggers des tables attachées établissent des barrières pour les écritures ; si les contrôles d’admissibilité échouent, la lecture passe par PostgreSQL.

Contrairement au cache-aside Redis, ce chemin utilise le processus d’écriture de la base pour invalider les données et n’utilise ni TTL ni structures de données Redis générales. Les workers RESP utilisent le rôle PostgreSQL configuré dans des transactions courtes indépendantes. Consultez [Clients RESP](resp.md) et la [référence technique](TECHNICAL.md) pour les détails de connexion et de sécurité.

Utilisez Redis pour les objets applicatifs partagés, une fraîcheur gérée par TTL ou les structures de données Redis. Utilisez `pg_local_cache` pour relire souvent des lignes complètes par clé primaire dans une seule base PostgreSQL. Si vous combinez ces couches, prévoyez des clés, une invalidation et une supervision distinctes.
