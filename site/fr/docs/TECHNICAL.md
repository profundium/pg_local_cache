---
layout: doc
lang: fr
translation_key: TECHNICAL
title: Référence technique de pg_local_cache
seo_title: "API RESP de pg_local_cache, cohérence, mémoire et configuration"
description: "Référence technique de pg_local_cache : lectures RESP, tables prises en charge, barrières transactionnelles, TLS, mémoire partagée, métriques et paramètres PostgreSQL."
section: Technique
permalink: /fr/docs/TECHNICAL.html
last_modified_at: "2026-10-04"
---

# Référence technique de pg_local_cache {#pg_local_cache-technical-reference}

Référence technique du point de terminaison RESP2, de la cohérence du cache, des limites de ressources et de la sécurité. Consultez le [guide de démarrage rapide](QUICKSTART.md) et le [guide d’installation](INSTALL_EXISTING.md) pour configurer l’extension.

## Tables et clés prises en charge {#supported-tables-and-keys}

Attachez des tables heap permanentes dotées d’une clé primaire valide. Les tables partitionnées, héritées, soumises à la sécurité au niveau des lignes, temporaires, étrangères ou appartenant à une extension ne sont pas prises en charge. Les types de clé primaire pris en charge sont `smallint`, `integer`, `bigint`, `text`, `varchar`, `char` avec des collations déterministes et `uuid`. Les clés composites peuvent utiliser ces types, sur 16 colonnes au maximum.

Les modifications DDL nécessitent une réconciliation des correspondances. Consultez le [guide d’installation](INSTALL_EXISTING.md#attach-a-table).

## Chemin de lecture et repli sûr {#read-path-and-safe-fallback}

![Chemin de lecture RESP MGET : cache hit, remplissage depuis la source avec barrière et contournement par le coupe-circuit.](../../docs/diagrams/read-path.svg)

Chaque clé RESP `MGET` est validée et normalisée avant la recherche. En cas de cache hit admissible, le JSON de la ligne entière est renvoyé. En cas de miss, le worker lit la table source dans une transaction courte, puis publie le résultat dans le cache uniquement si sa barrière de lecture est toujours valide. Les lignes absentes renvoient nil. Les lignes dont la charge utile ne tient pas dans le cache partagé peuvent tout de même être renvoyées par PostgreSQL si leur JSON respecte la limite de taille des valeurs RESP.

## Cohérence transactionnelle {#transaction-consistency}

![Invalidation à l’écriture : les barrières avant commit protègent les écritures validées ; un rollback avant publication de la barrière préserve les anciennes entrées.](../../docs/diagrams/write-invalidation.svg)

Les triggers de ligne et d’instruction des tables mappées collectent les clés modifiées ou la relation dans l’état local de la transaction. Le callback pré-commit publie des barrières d’invalidation avant que l’écriture devienne visible. Après le commit, les lecteurs ne peuvent plus utiliser une ancienne entrée ; les remplissages en cours dont la génération est périmée échouent à la vérification. Un rollback avant la publication de la barrière abandonne l’état modifié et laisse l’ancienne entrée valide.

Les lectures RESP utilisent `pg_local_cache.role` dans des transactions courtes indépendantes. Elles ne partagent ni le rôle SQL du client, ni sa transaction, ses écritures non validées ou son snapshot.

## Mémoire partagée et configuration {#shared-memory-and-configuration}

Au démarrage de PostgreSQL, l’extension préalloue une mémoire partagée limitée pour le cache, les correspondances et l’état des workers et des clients. `memory_budget_mb` limite l’allocation déterministe de l’extension. Les refus d’admission et les évictions ne dépassent pas la capacité configurée ; les lectures se replient sur PostgreSQL.

| Paramètre | Valeur par défaut | Plage | Application |
|---|---:|---|---|
| `pg_local_cache.enabled` | `on` | `on` / `off` | SIGHUP |
| `pg_local_cache.allow_plaintext_network` | `off` | `on` / `off` | Redémarrage |
| `pg_local_cache.tls` | `off` | `on` / `off` | Redémarrage |
| `pg_local_cache.tls_cert_file` | vide | chemin vers un fichier PEM | Redémarrage |
| `pg_local_cache.tls_key_file` | vide | chemin vers un fichier PEM | Redémarrage |
| `pg_local_cache.tls_ca_file` | vide | chemin vers un fichier CA PEM | Redémarrage |
| `pg_local_cache.tls_min_protocol_version` | `TLSv1.2` | `TLSv1.2` / `TLSv1.3` | Redémarrage |
| `pg_local_cache.port` | `6380` | `0`–`65535` ; `0` désactive RESP | Redémarrage |
| `pg_local_cache.workers` | `4` | `1`–`32` | Redémarrage |
| `pg_local_cache.cache_entries` | `16384` | `128`–`65536` | Redémarrage |
| `pg_local_cache.relation_states` | `1024` | `128`–`8192` | Redémarrage |
| `pg_local_cache.max_clients` | `256` | `1`–`4096` ; au plus le nombre de slots workers | Redémarrage |
| `pg_local_cache.max_clients_per_worker` | `64` | `1`–`128` | Redémarrage |
| `pg_local_cache.memory_budget_mb` | `384` | `64`–`8192` Mo | Redémarrage |
| `pg_local_cache.idle_timeout_ms` | `300000` | `1000`–`86400000` | Redémarrage |
| `pg_local_cache.statement_timeout_ms` | `2000` | `100`–`60000` | Redémarrage |
| `pg_local_cache.lock_timeout_ms` | `250` | `10`–`60000` | Redémarrage |
| `pg_local_cache.singleflight_wait_ms` | `25` | `0`–`1000` | Redémarrage |
| `pg_local_cache.max_pipeline_commands` | `256` | `1`–`4096` | Redémarrage |
| `pg_local_cache.max_dirty_keys` | `4096` | `128`–`16384` | Redémarrage |
| `pg_local_cache.bind_address` | `127.0.0.1` | adresse IPv4 | Redémarrage |
| `pg_local_cache.database` | `postgres` | nom de la base de données | Redémarrage |
| `pg_local_cache.role` | `local_cache_worker` | rôle PostgreSQL avec l’attribut LOGIN | Redémarrage |
| `pg_local_cache.auth_token_file` | vide | fichier appartenant à l’utilisateur système PostgreSQL, mode `0400` ou `0600` | Redémarrage |
| `pg_local_cache.auth_token` | vide | jeton intégré ; développement uniquement | Redémarrage |
| `pg_local_cache.allow_superuser` | `off` | `on` / `off` ; développement uniquement | Redémarrage |

Tous les paramètres, sauf `enabled`, sont des paramètres postmaster et nécessitent un redémarrage. Le nombre de slots clients doit respecter `max_clients <= workers × max_clients_per_worker`.

## Point de terminaison RESP2 {#optional-resp2-endpoint}

Le point de terminaison accepte RESP2. Les clés ont la forme `CRUD:<db>.<schema>.<table>:<json pk>`. `MGET` conserve l’ordre des requêtes et les doublons ; une ligne absente donne un élément nil. Chaque requête accepte au plus 1 024 clés, chaque ligne JSON est limitée à 65 536 octets et la réponse encodée à 66 560 octets.

Les commandes de données prises en charge sont `MGET`, `SET` et `DEL` ; `AUTH` est obligatoire. Le point de terminaison prend aussi en charge `PING`, `ECHO`, `INFO`, `STAT`/`STATS`, `INVALIDATE` avec une portée définie, `HELLO 2`, `QUIT`, `CLIENT SETINFO`/`SETNAME`/`GETNAME`/`ID`, `COMMAND` et `SELECT 0`. Les commandes non prises en charge renvoient une erreur. Les clients RESP utilisent la base 0 ; la base et la table sont indiquées par chaque clé de cache.

## TLS et modèle de sécurité {#security-model}

Par défaut, le listener est lié à l’adresse IPv4 loopback. Le TLS RESP utilise les paramètres propres à l’extension, et non les paramètres PostgreSQL `ssl_*`. Il nécessite une version de PostgreSQL compilée avec OpenSSL, un certificat serveur et une clé au format PEM, ainsi qu’un redémarrage. Le paramètre `tls_ca_file` active la vérification obligatoire des certificats clients (mTLS) ; la version TLS minimale est 1.2 par défaut.

Sans TLS, les connexions en clair hors loopback exigent `allow_plaintext_network=on` et un réseau de confiance. Un listener hors loopback exige un jeton d’au moins 32 octets. Privilégiez un fichier de jeton dont les permissions sont restreintes. Tous les clients RESP partagent un rôle PostgreSQL configuré avec l’attribut LOGIN ; les droits PostgreSQL ne sont pas évalués séparément pour chaque client réseau. Par défaut, les workers n’utilisent pas un rôle superutilisateur ; ce mode est réservé au développement.

## Coupe-circuit du cache {#cache-kill-switch}

`pg_local_cache.enabled` est le coupe-circuit du cache, appliqué via SIGHUP. Lorsqu’il est désactivé, les lectures RESP contournent le cache partagé et lisent les tables sources ; `SET` et `DEL` continuent d’écrire via PostgreSQL. Les workers appliquent le rechargement de façon asynchrone aux frontières entre les commandes. `local_cache.health()` indique la valeur du paramètre visible par la session SQL appelante, et non une confirmation de chaque worker. Lors de la réactivation, l’époque du cache avance avant la reprise des lectures depuis le cache.

## Métriques et état de santé {#health-and-monitoring}

`local_cache.health()` indique l’état de préparation, l’état du cache et la convergence des correspondances. `local_cache.stats()` renvoie des compteurs JSON ; `local_cache.metrics()` renvoie la ligne de métriques typée destinée à l’exporteur.

Les métriques couvrent les hits, misses et hits négatifs du cache ; les lectures et écritures dans la source ; les invalidations et évictions ; les leaders, les waiters, les réutilisations et les expirations de single-flight ; le nombre actuel et maximal de clients ; les rejets dus aux limites de connexion ; les erreurs d’authentification et de protocole ; la contre-pression à l’envoi et les déconnexions de clients lents ; les démarrages de workers ; les repliements dus aux clés modifiées ; les échecs et nouvelles tentatives de rechargement des correspondances ; les handshakes TLS et leurs échecs. Les jauges comprennent les capacités d’entrées et de relations, les nombres de clients et de workers, la convergence des correspondances, la mémoire partagée, celle des workers, la mémoire estimée et le budget configuré.

Suite : [guide de démarrage rapide](QUICKSTART.md), [installation](INSTALL_EXISTING.md) et [mise à niveau](UPGRADING.md).
