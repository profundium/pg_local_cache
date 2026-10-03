---
layout: doc
lang: fr
translation_key: TECHNICAL
title: Référence technique de pg_local_cache
seo_title: API SQL, cohérence, mémoire et RESP2 de pg_local_cache
description: "Référence technique de pg_local_cache : mget SQL, invalidation tenant compte des transactions, mémoire partagée PostgreSQL bornée, supervision et RESP2 optionnel."
section: Technique
permalink: /fr/docs/TECHNICAL.html
---

# Référence technique de pg_local_cache {#pg_local_cache-technical-reference}

`pg_local_cache` met en cache les lignes complètes par clé primaire complète
dans la mémoire partagée bornée de PostgreSQL. Il expose une fonction SQL
explicite `local_cache.mget` et un point de terminaison RESP2 optionnel.

> **Le SQL ordinaire reste ordinaire :** l'extension n'installe aucun hook du
> planificateur ou de l'exécuteur. Un `SELECT` normal utilise toujours
> PostgreSQL et ne lit jamais ce cache.

## Tables et clés prises en charge {#supported-tables-and-keys}

Les tables sources doivent être des tables heap permanentes avec une clé
primaire valide, sans RLS, partitionnement, héritage ni propriété d'extension.

Types de clés pris en charge :

- `smallint`, `integer` et `bigint` ;
- `text`, `varchar` et `char` avec des collations déterministes ;
- `uuid` ;
- clés primaires composites constituées uniquement de ces types.

Les relations non prises en charge sont rejetées lors de l'attachement au lieu
de produire un mapping partiel non sûr.

## Attacher, réconcilier et détacher des tables {#attach-reconcile-and-detach-tables}

`local_cache.attach_table(regclass)` exécute une séquence de configuration
protégée :

1. verrouiller et valider la relation ;
2. enregistrer son namespace, son OID de relation et les colonnes de clé primaire dans l'ordre ;
3. installer les triggers de statement, de ligne et de truncate appartenant à l'extension ;
4. recharger les mappings des workers.

Les triggers d'événements DDL invalident les métadonnées de mapping du cache.
Exécutez `local_cache.reconcile_table(...)` ou
`local_cache.reconcile_all()` après des changements de schéma intentionnels.
`local_cache.detach_table(...)` supprime le mapping et ses triggers.

## API SQL mget {#sql-mget-api}

Signature :

```sql
local_cache.mget(relation regclass, key_values anyarray) RETURNS text[]
```

Les clés à une colonne utilisent leur type de tableau natif. Les clés
composites utilisent `text[][]` rectangulaire, avec une clé par ligne et un
composant par colonne de clé primaire.

Contrat :

- au maximum 1 024 clés par appel ;
- l'ordre d'entrée et les doublons sont conservés ;
- l'entrée `NULL` et les lignes absentes produisent des résultats `NULL` alignés ;
- les composants d'une clé composite ne peuvent pas être `NULL` ;
- chaque composant est analysé par la fonction d'entrée de son type PostgreSQL ;
- le lot composite complet est validé avant la première recherche ;
- les appelants doivent avoir `SELECT` sur la table source ;
- la fonction est `SECURITY INVOKER`.

Une requête source préparée est mise en cache par instance de fonction,
utilisateur, relation et génération du mapping.

## Chemin de lecture et repli sûr {#read-path-and-safe-fallback}

Chaque clé demandée suit le même chemin :

1. canoniser la clé primaire complète ;
2. utiliser le cache partagé uniquement dans une transaction `READ COMMITTED` propre sur le primaire inscriptible ;
3. valider la somme de contrôle de la charge utile, le descripteur de ligne, le `xmin` source et la visibilité du snapshot ;
4. sinon exécuter la requête indexée de la table source via SPI ;
5. publier une entrée positive ou négative uniquement après la preuve d'un snapshot récent.

`REPEATABLE READ`, `SERIALIZABLE`, la récupération, l'exécution parallèle et une
transaction ayant écrit des données mappées contournent le cache. Les lignes
plus grandes que la limite de charge utile du cache sont tout de même renvoyées
par PostgreSQL mais ne sont pas mises en cache.

## Cohérence transactionnelle {#transaction-consistency}

Avant qu'une écriture mappée puisse être validée, les triggers mettent en place une barrière pour la
clé ou la relation concernée. Un remplissage porte les générations du mapping,
globale, de la relation, de la clé et du loader ; un loader obsolète ne peut
donc pas publier après une invalidation ou une éviction.

Les entrées positives enregistrent le `xmin` du tuple source et un horizon
d'observation FullXID. Les entrées inéligibles pour le snapshot utilisent
PostgreSQL. Les entrées négatives ne sont jamais une autorité pour un snapshot
actif plus ancien.

Le rollback supprime l'état transactionnel local sans publier de nouvelles
données. La lecture de ses propres écritures vient donc de PostgreSQL, et non
du contenu spéculatif du cache.

## Mémoire partagée et configuration {#shared-memory-and-configuration}

Les entrées de cache, états de relations, compteurs, générations de workers et
emplacements de clients RESP sont alloués au démarrage du postmaster. La
capacité est bornée. L'éviction échantillonne un ensemble tournant borné et
privilégie les entrées obsolètes ; un échec d'admission revient à la table
source au lieu d'allouer une mémoire illimitée.

| Paramètre | Valeur par défaut | Signification |
|---|---:|---|
| `pg_local_cache.database` | `postgres` | base servie par l'extension |
| `pg_local_cache.cache_entries` | `16384` | capacité de lignes partagée |
| `pg_local_cache.relation_states` | `1024` | capacité d'état de mapping partagé |
| `pg_local_cache.memory_budget_mb` | `384` | budget de démarrage de l'extension |
| `pg_local_cache.port` | `6380` | port RESP ; `0` désactive RESP |
| `pg_local_cache.bind_address` | `127.0.0.1` | adresse d'écoute RESP |
| `pg_local_cache.workers` | `4` | workers RESP |
| `pg_local_cache.role` | `local_cache_worker` | rôle PostgreSQL RESP |
| `pg_local_cache.max_clients` | `256` | limite globale de clients RESP |
| `pg_local_cache.max_clients_per_worker` | `64` | emplacements par worker |
| `pg_local_cache.idle_timeout_ms` | `300000` | délai d'inactivité et de client lent |
| `pg_local_cache.statement_timeout_ms` | `2000` | délai des instructions worker |
| `pg_local_cache.lock_timeout_ms` | `250` | délai des verrous worker |
| `pg_local_cache.singleflight_wait_ms` | `25` | attente d'un suiveur pour la même clé |
| `pg_local_cache.max_pipeline_commands` | `256` | commandes par tour de boucle d'événements |
| `pg_local_cache.max_dirty_keys` | `4096` | nombre maximal de clés protégées par transaction |
| `pg_local_cache.auth_token_file` | vide | identifiant RESP recommandé |
| `pg_local_cache.auth_token` | vide | token inline réservé au développement |
| `pg_local_cache.allow_superuser` | `off` | dérogation de rôle réservée au développement |

Ce sont des paramètres du postmaster. Dimensionnez-les avant le redémarrage ;
la vérification préalable de l'installateur contrôle le plan combiné.

## Point de terminaison RESP2 optionnel {#optional-resp2-endpoint}

RESP2 utilise les mêmes mappings et le même cache partagé. Les clés filaires
ont cette forme :

```text
CRUD:database.schema.table:{"pk_column":<json-scalar>,...}
```

Les commandes prises en charge sont `MGET`, `SET`, `DEL` et l'invalidation
ciblée, toutes authentifiées et bornées. Les workers RESP utilisent un rôle
PostgreSQL configuré unique ; ils n'héritent pas des ACL de base de données de
chaque client réseau.

Le point de terminaison n'a pas de TLS. Liez-le à loopback ou placez-le
derrière un proxy TLS authentifié. Préférez un fichier de token dont les droits
sont restreints à un token inline.

## Santé et supervision {#health-and-monitoring}

`local_cache.health()` indique la disponibilité et la convergence du mapping.
`local_cache.stats()` renvoie des compteurs JSON. `local_cache.metrics()` expose
la ligne de métriques typées utilisée par l'exporteur.

Les compteurs du cache SQL décrivent uniquement les appels explicites à `mget` :

- `sql_cache_hits`
- `sql_cache_misses`
- `sql_cache_fills`
- `sql_cache_bypasses`

Les lectures de base de données, invalidations, rejets d'admission, replis dus
aux clés modifiées, singleflight, workers et compteurs RESP restent séparés.

Ensuite : utilisez le [guide d'installation](INSTALL_EXISTING.md) pour les
binaires vérifiés, les compilations source PGXS, les redémarrages contrôlés, la
vérification et la récupération.
