---
layout: doc
lang: fr
translation_key: postgresql-caching
title: Guide de décision sur la mise en cache PostgreSQL
seo_title: "Guide de décision sur la mise en cache PostgreSQL : pages, lignes, vues ou Redis"
description: Choisissez la mise en cache des pages PostgreSQL, le SQL préparé, le cache de lignes complètes, les vues matérialisées ou un cache externe selon le travail à éviter.
section: Guides
permalink: /fr/docs/postgresql-caching.html
last_modified_at: "2026-09-16"
---

# Guide de décision sur la mise en cache PostgreSQL {#postgresql-caching-decision-guide}

« Ajouter un cache » décrit plusieurs changements différents. La mise en cache
des pages PostgreSQL, le SQL préparé, un cache de lignes complètes, une vue
matérialisée et Redis évitent des parties différentes d'une lecture. Choisissez
selon le travail répété dans votre requête, puis mesurez le chemin complet avec
le [guide des benchmarks](BENCHMARKS.md).

## Commencer par le travail répété {#start-with-the-work-you-repeat}

| Besoin | Première option | Ce que cela change |
|---|---|---|
| Garder les pages de table et d'index en mémoire | `shared_buffers` de PostgreSQL et le cache du système | Moins de lectures du stockage ; le SQL s'exécute toujours |
| Envoyer plusieurs fois la même instruction | Une instruction préparée | Moins de parsing et de planification répétés ; l'exécution a toujours lieu |
| Renvoyer des lignes complètes par clé primaire | `pg_local_cache` SQL `mget` | Réutilise des charges utiles de lignes complètes éligibles via une API explicite |
| Pré-calculer une jointure ou un agrégat | Une vue matérialisée | Lit des résultats persistés ; le rafraîchissement définit la fraîcheur |
| Partager des objets applicatifs entre services | Un cache externe comme Redis | Clés, TTL et invalidation gérés par l'application |

### Pages et SQL préparé {#pages-and-prepared-sql}

[`shared_buffers`](https://www.postgresql.org/docs/18/runtime-config-resource.html#GUC-SHARED-BUFFERS)
contient des pages de base de données, pas les résultats finaux des `SELECT`.
Une page chaude peut éviter une E/S de stockage, mais PostgreSQL doit toujours
planifier ou exécuter la requête, vérifier la visibilité et construire le
résultat. Une [instruction préparée](https://www.postgresql.org/docs/18/sql-prepare.html)
peut éviter le parsing et l'analyse répétés dans une session. Elle s'exécute
toujours sur l'état courant de la base, et son plan peut être générique ou
personnalisé.

La [comparaison du cache de lignes](row-cache-vs-shared-buffers.md) montre le
travail qui reste sur chaque chemin.

### Lignes complètes par clé primaire {#whole-rows-by-primary-key}

`pg_local_cache` stocke des lignes complètes sérialisées sous des clés
primaires complètes dans la mémoire partagée bornée de PostgreSQL. On y accède
via `local_cache.mget('public.items'::regclass, $1::bigint[])` ; un `SELECT`
ordinaire ne le consulte jamais. Les lectures propres et éligibles en
`READ COMMITTED` peuvent être des hits, tandis que les niveaux d'isolation plus
stricts, les écritures de la transaction, la récupération, l'exécution
parallèle ou les lignes trop volumineuses utilisent PostgreSQL. Les mappings de
tables non pris en charge sont rejetés lors de l'attachement. Il s'agit d'un
chemin de lecture précis, pas d'un cache arbitraire de résultats de requêtes.
Voir le [guide des recherches par lots](batch-primary-key-lookups.md), le
[contrat technique](TECHNICAL.md) et les [vérifications transactionnelles](cache-invalidation.md).

### Vues et caches externes {#views-and-external-caches}

Les [vues matérialisées PostgreSQL](https://www.postgresql.org/docs/18/rules-materializedviews.html)
conservent un résultat de requête dans une relation et le rafraîchissent à la
demande. Elles conviennent aux rapports répétables, agrégats et jointures
lorsqu'un calendrier de rafraîchissement est une limite de fraîcheur acceptable.
Elles ne remplacent pas un cache de lignes par clé.

Un cache externe comme Redis convient aux objets applicatifs partagés par
plusieurs processus ou services. L'application possède les clés, la
sérialisation, le TTL et l'invalidation. Consultez le [guide cache-aside Redis](postgresql-redis-cache.md).
Le [démarrage rapide](QUICKSTART.md) exécute `pg_local_cache` sur `public.items`.
