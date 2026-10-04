---
layout: doc
lang: fr
translation_key: postgresql-caching
title: Guide de décision sur la mise en cache PostgreSQL
seo_title: "Mise en cache PostgreSQL : pages, lignes, vues ou Redis"
description: Comparez le cache de pages PostgreSQL, le SQL préparé, le cache de lignes complètes, les vues matérialisées et Redis selon le travail évité par chaque chemin de lecture.
section: Guides
permalink: /fr/docs/postgresql-caching.html
last_modified_at: "2026-10-04"
---

# Guide de décision sur la mise en cache PostgreSQL {#postgresql-caching-decision-guide}

Ce guide compare le cache de pages PostgreSQL, le SQL préparé, le cache de lignes complètes, les vues matérialisées et Redis selon le travail évité par chaque option.

## Commencer par le travail répété {#start-with-the-work-you-repeat}

| Besoin | Option | Travail évité |
|---|---|---|
| Garder en mémoire les pages de table et d’index | `shared_buffers` PostgreSQL et cache du système d’exploitation | Lectures du stockage ; le SQL est toujours exécuté |
| Répéter une instruction dans une session | Instruction préparée | Analyse et parsing répétés |
| Lire des lignes complètes par clé primaire | `MGET` RESP authentifié avec `pg_local_cache` | Lectures admissibles de lignes complètes depuis la source |
| Réutiliser des jointures ou des agrégats | Vue matérialisée | Recalcul du résultat stocké jusqu’à son rafraîchissement |
| Partager des objets entre services | Redis cache-aside | Lectures de la source gérées par l’application |

### Pages et SQL préparé {#pages-and-prepared-sql}

[`shared_buffers`](https://www.postgresql.org/docs/18/runtime-config-resource.html#GUC-SHARED-BUFFERS) met en cache des pages de base de données, pas les résultats finaux de `SELECT`. PostgreSQL vérifie toujours la visibilité, exécute la requête et construit chaque résultat. Une [instruction préparée](https://www.postgresql.org/docs/18/sql-prepare.html) réduit le parsing répété ; elle est tout de même exécutée sur l’état courant de la base.

Consultez [Cache de lignes ou shared_buffers](row-cache-vs-shared-buffers.md) pour voir le travail restant sur chaque chemin.

### Lignes complètes par clé primaire {#whole-rows-by-primary-key}

`pg_local_cache` stocke des lignes complètes dans la mémoire partagée limitée de PostgreSQL. Un `MGET` RESP authentifié peut renvoyer une ligne mise en cache qui remplit les critères ; le SQL ordinaire n’utilise jamais ce cache. Les misses et lectures non admissibles utilisent la table source. Le point de terminaison utilise un seul rôle de base configuré et ne partage pas la transaction SQL ni le snapshot de l’appelant. Consultez le [guide des lectures par lots](batch-primary-key-lookups.md), la [référence technique](TECHNICAL.md) et le [guide d’invalidation](cache-invalidation.md).

### Vues et caches externes {#views-and-external-caches}

Une [vue matérialisée PostgreSQL](https://www.postgresql.org/docs/18/rules-materializedviews.html) stocke le résultat d’une requête et est rafraîchie à la demande. Elle convient aux rapports et agrégats dont la fraîcheur dépend du moment du rafraîchissement.

Redis convient aux objets applicatifs partagés entre processus. L’application gère les clés, la sérialisation, les TTL et l’invalidation. Consultez [PostgreSQL et Redis cache-aside](postgresql-redis-cache.md). Lancez le [guide de démarrage rapide](QUICKSTART.md) pour essayer le cache de lignes.
