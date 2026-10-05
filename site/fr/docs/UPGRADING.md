---
layout: doc
lang: fr
translation_key: UPGRADING
title: Mettre pg_local_cache à niveau vers 3.1.0
seo_title: "Mettre pg_local_cache à niveau vers 3.1.0"
description: Mettre pg_local_cache à niveau depuis 3.0.0 ou 2.x, vérifier les paramètres de capacité et de workers de 3.1, puis contrôler l’extension après redémarrage.
section: Installation
permalink: /fr/docs/UPGRADING.html
last_modified_at: "2026-10-06"
---

# Mettre pg_local_cache à niveau vers 3.1.0 {#upgrade-pg_local_cache-from-2x-to-310}

La version 3.0.0 a supprimé la fonction SQL `local_cache.mget(regclass, anyarray)`.
Utilisez RESP `MGET` authentifié pour lire les lignes complètes mises en cache.
Les workers RESP utilisent le rôle PostgreSQL configuré ; ils n’héritent ni des
droits SQL, ni de la transaction, ni du snapshot de l’application. Gardez SQL
pour les projections, jointures, verrous de ligne et lectures nécessitant la
sémantique de la session applicative. Le [guide RESP](resp.md) décrit la
configuration client et l’encodage des clés.

## Mise à niveau depuis 3.0.0 {#upgrade-from-300}

La version 3.1.0 modifie la bibliothèque partagée et la disposition de la
mémoire partagée. Installez le paquet ou la bibliothèque 3.1.0 correspondant à
la version majeure de PostgreSQL, puis redémarrez PostgreSQL avant utilisation.
La migration SQL `3.0.0--3.1.0` ne change rien : objets SQL, associations de
tables et triggers restent identiques. Exécutez la mise à jour de l’extension
dans chaque base pour enregistrer la version `3.1.0`.

### Modifications des paramètres {#setting-changes}

| Paramètre | Comportement en 3.1.0 |
|---|---|
| `pg_local_cache.cache_entries` | Plage de `128` à `16777216` descripteurs. La valeur intégrée par défaut est `262144`, calculée avec le budget par défaut de 384 Mio en réservant au moins la moitié aux pages d’arène. La capacité réelle en octets dépend de l’arène et de la taille des lignes ; le démarrage échoue si le budget configuré est dépassé. |
| `pg_local_cache.lock_partitions` | Valeur par défaut `64` ; puissance de deux entre `16` et `256`. Les petits caches utilisent moins de partitions. |
| `pg_local_cache.dirty_marker_entries` | La valeur par défaut `-1` active le calcul automatique : `min(16384, max(1024, floor(cache_entries / 4)))`. Plage explicite : `128`–`1048576`. |
| `pg_local_cache.dirty_marker_memory_mb` | La valeur par défaut `-1` active le calcul automatique : `min(16, max(1, floor(memory_budget_mb / 25)))` Mio. Plage explicite : `1`–`1024` Mio. |
| `pg_local_cache.max_clients_per_worker` | Valeur par défaut `64` ; plage étendue à `1`–`4096`. `max_clients` ne peut dépasser `workers × max_clients_per_worker`. La limite souple `RLIMIT_NOFILE` de chaque worker doit être au moins `min(max_clients, max_clients_per_worker) + 33` ; augmentez la limite `nofile` du processus/conteneur si nécessaire. |
| `pg_local_cache.max_deferred_misses` | Nouveau paramètre. Valeur par défaut `8` ; plage `1`–`64` requêtes différées par worker quand la relation est verrouillée. |

Aucun paramètre de 3.0.0 n’a été supprimé ou renommé en 3.1.0. Tous ces
paramètres prennent effet après redémarrage.

`local_cache.stats()` ajoute `fast_path_hits`, `fast_path_fallbacks` et quatre
`fast_path_fallback_key_form`, `fast_path_fallback_mapping_shape`, `fast_path_fallback_multi_key` et `fast_path_fallback_cache_state` ; `cache_memory_capacity_bytes`,
`cache_memory_used_bytes`, `cache_fragmentation_bytes`,
`arena_admission_rejections_total` ; `dirty_marker_capacity`,
`dirty_marker_entries`, `dirty_marker_highwater`,
`dirty_marker_fallbacks_total`, `dirty_marker_entries_effective`,
`dirty_marker_memory_mb_effective`, `dirty_marker_memory_capacity_bytes`,
`dirty_key_limit_fallbacks`, ainsi que `lock_partitions`,
`max_clients_per_worker` et `client_slots`. RESP `STAT` ajoute les champs
locaux au worker `deferred_misses_total`, `deferred_misses_current`,
`deferred_timeouts_total` et `deferred_rejections_total`.

Étapes de mise à niveau :

1. Installez le paquet ou la bibliothèque 3.1.0 adapté à la version majeure de
   PostgreSQL.
2. Vérifiez les paramètres ci-dessus. Si vous augmentez les slots clients,
   relevez la limite souple `nofile` du processus/conteneur selon la formule.
3. Redémarrez PostgreSQL pour charger la bibliothèque et allouer la nouvelle
   disposition de mémoire partagée.
4. Dans chaque base où l’extension est installée, connectez-vous comme
   superutilisateur de la base et exécutez :

   ```sql
   ALTER EXTENSION pg_local_cache UPDATE;
   ```

5. Vérifiez la bibliothèque et les workers :

   ```sql
   SELECT current_setting('pg_local_cache.binary_version');
   SELECT local_cache.health();
   ```

   La version doit être `3.1.0` ; health doit indiquer
   `workers_running = workers_configured`.

## Mise à niveau depuis 2.x {#upgrade-from-2x}

Avant la mise à niveau, faites abandonner SQL `mget` par les applications au
profit de RESP `MGET`, avec un rôle worker dédié et le modèle d’autorisation
requis. Si le listener 2.x utilisait une adresse hors loopback, configurez TLS
RESP natif avec certificat et clé avant le redémarrage. `pg_local_cache.tls_ca_file`
impose les certificats clients (mTLS). TLS RESP est indépendant de PostgreSQL
`ssl_*`. Si le clair est nécessaire, définissez explicitement
`pg_local_cache.allow_plaintext_network = on` et limitez-le à un réseau de
confiance. Sans TLS ni cette autorisation, les workers RESP hors loopback ne
démarrent pas.

La migration SQL de 2.x vers 3.0 a supprimé l’API SQL de lecture et ses
compteurs, ce qui a modifié le type de résultat de `local_cache.metrics()`.
Les droits personnalisés sont conservés. Un objet dépendant peut bloquer la
migration ; elle n’utilise pas `CASCADE`. Modifiez ou supprimez ces dépendances,
puis réessayez. Ces changements SQL relèvent de l’ancienne migration 3.0, pas
de la migration 3.1 sans effet. Suivez ensuite les étapes ci-dessus d’installation, de redémarrage, de mise à jour de l’extension et de vérification. PostgreSQL appliquera d’abord l’ancienne migration de 2.x vers 3.0, puis la migration sans effet de 3.0.0 vers 3.1.0.

## Revenir à 2.0.4 {#rollback-to-204}

Aucun script de downgrade n’existe. Pour revenir à 2.0.4, réinstallez son
paquet, redémarrez PostgreSQL pour charger l’ancienne bibliothèque, détachez
toutes les tables mappées, recréez l’extension dans chaque base et rattachez
les tables :

```sql
SELECT local_cache.detach_table('public.items'::regclass);
DROP EXTENSION pg_local_cache;
CREATE EXTENSION pg_local_cache VERSION '2.0.4';
SELECT local_cache.attach_table('public.items'::regclass);
```

Répétez `detach_table` et `attach_table` pour chaque table. Avant le rollback,
sauvegardez la liste des tables attachées et les droits personnalisés sur
l’extension. Des objets dépendant de fonctions de l’extension peuvent bloquer
la suppression ; traitez explicitement ces dépendances.

## Chemin de recherche de la bibliothèque {#library-lookup-path}

Le fichier de contrôle indique le nom nu `pg_local_cache` dans `module_pathname`.
PostgreSQL le résout via `dynamic_library_path` (qui inclut `$libdir` par
défaut). Si le serveur remplace ce paramètre, ajoutez avant redémarrage le
répertoire d’installation de `pg_local_cache`.

Documentation 2.x : https://github.com/profundium/pg_local_cache/tree/v2.0.4/docs
