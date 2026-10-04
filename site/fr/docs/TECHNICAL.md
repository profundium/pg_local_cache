---
layout: doc
lang: fr
translation_key: TECHNICAL
title: Référence technique de pg_local_cache
seo_title: API SQL, cohérence, mémoire et RESP2 de pg_local_cache
description: "Référence technique de pg_local_cache : MGET via RESP, invalidation tenant compte des transactions, mémoire partagée PostgreSQL bornée et supervision."
section: Technique
permalink: /fr/docs/TECHNICAL.html
---

# Référence technique de pg_local_cache {#pg_local_cache-technical-reference}

`pg_local_cache` met en cache les lignes complètes par clé primaire complète dans la mémoire partagée bornée de PostgreSQL. Son point de terminaison RESP2 expose `MGET`, `SET` et `DEL`.

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
3. créer les triggers de statement, de ligne et de truncate appartenant à l'extension ;
4. recharger les mappings des workers.

Les triggers d'événements DDL invalident les métadonnées de mapping du cache.
Exécutez `local_cache.reconcile_table(...)` ou
`local_cache.reconcile_all()` après des changements de schéma intentionnels.
`local_cache.detach_table(...)` supprime le mapping et ses triggers.

## Chemin de lecture et repli sûr {#read-path-and-safe-fallback}

Lorsque le cache est activé, chaque clé RESP `MGET` consulte d'abord le cache
partagé. Chaque échec entraîne la lecture de la ligne source dans sa propre
transaction courte. PostgreSQL renvoie toujours les lignes qui dépassent la limite
de charge utile du cache, mais elles ne sont pas mises en cache.

## Cohérence transactionnelle {#transaction-consistency}

Les triggers d'écriture sur les tables mappées publient, dans chaque session
PostgreSQL avant la visibilité du commit, des barrières dirty-writer par clé ou
relation et font avancer les générations. Les lectures contournent les entrées
protégées jusqu'à la fin de l'écriture ; les contrôles de génération empêchent la
publication de lectures en cours devenues obsolètes. Un hit périmé ne peut donc
pas suivre une écriture validée.

Les lectures RESP utilisent `pg_local_cache.role`, et non le rôle PostgreSQL du
client, dans des transactions courtes indépendantes. Elles ne voient pas les
modifications non validées du client, ne partagent pas son snapshot et ne font
pas partie de sa transaction. `pg_local_cache.enabled = off` contourne le cache.

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
| `pg_local_cache.port` | `6380` | port RESP ; `0` réservé aux tests de régression et au diagnostic, aucune lecture servie |
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
| `pg_local_cache.enabled` | `on` | interrupteur d’urgence SIGHUP du cache ; sur `off`, RESP lit directement dans la table source |
| `pg_local_cache.tls` | `off` | activer TLS sur le listener RESP ; PostgreSQL doit prendre en charge OpenSSL |
| `pg_local_cache.tls_cert_file` | vide | certificat/chaîne serveur PEM ; requis si TLS est activé |
| `pg_local_cache.tls_key_file` | vide | clé privée serveur PEM ; requise si TLS est activé |
| `pg_local_cache.tls_ca_file` | vide | CA cliente de confiance ; son réglage active mTLS |
| `pg_local_cache.tls_min_protocol_version` | `TLSv1.2` | version TLS minimale (`TLSv1.2` ou `TLSv1.3`) |
| `pg_local_cache.allow_plaintext_network` | `off` | option postmaster pour les listeners en clair hors du loopback IPv4 |
| `pg_local_cache.allow_superuser` | `off` | dérogation de rôle réservée au développement |

Ce sont des paramètres du postmaster. Dimensionnez-les avant le redémarrage.
Consultez le [guide d'installation](INSTALL_EXISTING.md) pour installer le paquet
et suivre les étapes de redémarrage.

## Point de terminaison RESP2 {#optional-resp2-endpoint}

RESP2 utilise les mêmes mappings et le même cache partagé. Les clés filaires
ont cette forme :

```text
CRUD:database.schema.table:{"pk_column":<json-scalar>,...}
```

Les commandes prises en charge sont `MGET`, `SET`, `DEL` et l'invalidation
ciblée, toutes authentifiées et bornées. Les workers RESP utilisent un rôle
PostgreSQL configuré unique ; ils n'héritent pas des ACL de base de données de
chaque client réseau.

TLS pour RESP utilise des paramètres `pg_local_cache.tls_*` propres,
indépendants des paramètres `ssl_*` de PostgreSQL. TLS PostgreSQL sur le port
SQL ne protège pas RESP et TLS RESP ne modifie pas le listener SQL. Activez
`pg_local_cache.tls` et fournissez un certificat et une clé serveur. Le réglage
de `pg_local_cache.tls_ca_file` vérifie les certificats clients et active mTLS.
La version minimale du protocole est `TLSv1.2` par défaut et peut être relevée à
`TLSv1.3`. Les chiffrements par défaut du système OpenSSL s'appliquent. La clé
privée suit la [règle PostgreSQL sur les fichiers de clé
serveur](https://www.postgresql.org/docs/current/ssl-tcp.html). Privilégiez TLS
au-delà de loopback. Lorsque TLS est désactivé, une écoute en clair hors
loopback exige l'opt-in explicite `pg_local_cache.allow_plaintext_network = on`,
à limiter aux réseaux de confiance. Un listener hors loopback exige toujours un
jeton d'au moins 32 octets ; préférez un fichier de jeton aux droits restreints
à un jeton intégré.

Le paramètre opérationnel `pg_local_cache.enabled` est un paramètre SIGHUP qui sert d’interrupteur d’arrêt d’urgence. Pour désactiver le service de cache :

```sql
ALTER SYSTEM SET pg_local_cache.enabled = off;
SELECT pg_reload_conf();
```

Chaque worker RESP applique le rechargement de manière asynchrone à sa prochaine limite entre commandes, une fois terminée toute commande en cours. Le champ `cache_enabled` de `local_cache.health()` indique la valeur vue par la session SQL qui appelle la fonction ; il ne confirme pas que tous les workers l'ont appliquée. Pour le réactiver, exécutez aussi :

```sql
ALTER SYSTEM SET pg_local_cache.enabled = on;
SELECT pg_reload_conf();
```

## Santé et supervision {#health-and-monitoring}

`local_cache.health()` indique la disponibilité et la convergence du mapping.
`local_cache.stats()` renvoie des compteurs JSON. `local_cache.metrics()` expose
la ligne de métriques typées utilisée par l'exporteur.

Les compteurs RESP de `stats()` et `metrics()` comprennent :

- `sql_gets`
- `sql_meta`
- `sql_sets`
- `sql_dels`
- `sql_result_reuses`
- `tls_handshakes_total`
- `tls_handshake_failures_total`

Les lectures de base de données, invalidations, rejets d'admission, replis dus
aux clés modifiées, singleflight, workers et compteurs RESP restent séparés.

Ensuite : utilisez le [guide d'installation](INSTALL_EXISTING.md) pour vérifier
les paquets Debian et RPM, compiler avec PGXS, configurer, redémarrer, mettre à
jour et désinstaller.
