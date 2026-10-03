---
layout: doc
lang: fr
translation_key: postgresql-redis-cache
title: Cache-aside PostgreSQL et Redis
seo_title: "Cache-aside PostgreSQL et Redis : invalidation et courses"
description: Utilisez PostgreSQL comme source de vérité avec un chemin cache-aside Redis, comprenez les courses de lectures obsolètes et voyez où se place pg_local_cache.
section: Guides
permalink: /fr/docs/postgresql-redis-cache.html
last_modified_at: "2026-09-16"
---

# Cache-aside PostgreSQL et Redis {#postgresql-and-redis-cache-aside}

Le cache-aside Redis place l'application entre une lecture et son stockage
faisant autorité. En cas de miss, lisez PostgreSQL, renvoyez cette valeur et
écrivez-la dans Redis ; lors d'une écriture, mettez PostgreSQL à jour et
invalidez la clé Redis correspondante. Le [guide du pattern Redis](https://redis.io/docs/latest/develop/use-cases/cache-aside/)
décrit ce flux, mais il ne rend pas un cache applicatif cohérent
transactionnellement avec PostgreSQL.

Pour une ligne indexée par `public.items.id`, le schéma est :

```text
GET item:42
miss -> SELECT * FROM public.items WHERE id = $1
     -> SET item:42 <serialized row> EX <ttl>
write -> UPDATE public.items ...
      -> COMMIT
      -> DEL item:42
```

Utilisez du SQL paramétré et un namespace de clés. Un TTL limite la durée
pendant laquelle une valeur stockée reste éligible dans Redis ; il ne prouve
pas sa fraîcheur par rapport à un commit PostgreSQL. La suppression explicite
gère les écritures ordinaires, mais ne supprime pas toutes les courses.

## La course d'invalidation {#the-invalidation-race}

Considérez deux requêtes. Le lecteur R1 rate Redis et lit l'ancienne ligne dans
PostgreSQL. L'écrivain W valide une nouvelle ligne et supprime `item:42`. R1
reprend ensuite et stocke son ancienne valeur dans Redis. Le lecteur suivant
voit une valeur obsolète jusqu'à l'expiration de la clé ou une autre écriture
qui la supprime.

Les mesures possibles incluent une nouvelle suppression après la fin du
chargeur, le stockage d'une version de la base et le rejet des valeurs plus
anciennes, la sérialisation des chargements par clé ou la publication des
changements validés via un outbox ou un consommateur CDC. Chacune ajoute de la
coordination et des cas d'échec. Le [guide d'invalidation du cache](cache-invalidation.md)
montre le problème analogue de remplissage tardif dans PostgreSQL.

## Où se place pg_local_cache {#where-pg_local_cache-fits}

`pg_local_cache` est une option PostgreSQL locale plus étroite pour des lignes
complètes indexées par clé primaire. `local_cache.mget` est explicite ; un
`SELECT` normal et une forme de requête arbitraire ne lisent jamais le cache.
Les triggers des tables attachées mettent en place une barrière pour les clés ou relations concernées
sur le chemin d'écriture de la base, et les lectures éligibles peuvent revenir
à PostgreSQL lorsque les règles de transaction ou de snapshot interdisent un
hit. Commencez par le [guide des recherches par lots](batch-primary-key-lookups.md)
et le [contrat technique](TECHNICAL.md).

Cette extension ne fournit ni compatibilité Redis générale, ni TTL Redis, ni
protocole de cache applicatif distribué. Son point de terminaison RESP2
optionnel expose un ensemble limité de commandes authentifiées sur les mêmes
mappings et possède son propre modèle de sécurité ; il n'a pas de TLS.
Utilisez-le lorsque le problème est la lecture transactionnelle de lignes
complètes locale à PostgreSQL. Utilisez Redis lorsque plusieurs instances
applicatives ont besoin d'objets partagés, d'une fraîcheur fondée sur le TTL ou
de structures de données Redis. Combiner les deux exige des clés,
invalidations et métriques séparées pour chaque couche.

Lancez le [démarrage rapide](QUICKSTART.md), comparez avec la requête client
ordinaire dans [l'exemple node-postgres](node-postgres.md) et inspectez les
compteurs SQL et RESP séparés. Le [guide de décision sur la mise en cache](postgresql-caching.md)
liste les autres options PostgreSQL.
