---
layout: doc
lang: fr
translation_key: UPGRADING
title: Mettre à niveau pg_local_cache de 2.x vers 3.0.0
seo_title: "Mettre à niveau pg_local_cache de 2.x vers 3.0.0"
description: Migrez les applications de SQL mget vers RESP MGET, installez et mettez à niveau l'extension en sécurité, traitez les erreurs de dépendance et revenez à 2.0.4.
section: Installation
permalink: /fr/docs/UPGRADING.html
last_modified_at: "2026-10-04"
---

# Mettre à niveau pg_local_cache de 2.x vers 3.0.0 {#upgrade-pg_local_cache-from-2x-to-300}

La version 3.0.0 supprime la fonction SQL `local_cache.mget(regclass, anyarray)`.
Utilisez RESP `MGET` authentifié pour lire les lignes complètes mises en cache :

```text
MGET CRUD:app.public.items:{"id":42} CRUD:app.public.items:{"id":7}
```

Les workers RESP utilisent le rôle PostgreSQL configuré. Ils n'héritent ni des
droits SQL, ni de la transaction, ni du snapshot de l'application. Utilisez SQL
pour les projections, jointures, verrous de lignes et lectures qui nécessitent
la sémantique de la session applicative. Le [guide RESP](resp.md) décrit la
configuration du client et l'encodage des clés.

## Ordre de mise à niveau {#upgrade-order}

1. Modifiez les applications pour qu'elles n'appellent plus SQL `mget`. Vérifiez
   que RESP `MGET` utilise un rôle worker dédié et respecte le modèle
   d'autorisation requis.
2. Installez le paquet ou la bibliothèque 3.0.0 correspondant à la version
   majeure de PostgreSQL en cours d'exécution.
3. Si le listener 2.x utilisait une adresse de
   `pg_local_cache.bind_address` hors loopback, configurez TLS natif pour RESP
   et fournissez son certificat et sa clé avant le redémarrage. Réglez
   `pg_local_cache.tls_ca_file` pour exiger des certificats clients (mTLS).
   TLS RESP est indépendant des paramètres `ssl_*` de PostgreSQL. Si le trafic
   en clair est nécessaire, définissez explicitement
   `pg_local_cache.allow_plaintext_network = on`, uniquement sur un réseau de
   confiance. Sans TLS ni cet opt-in explicite pour le trafic en clair, les
   workers RESP refusent de démarrer.
4. Redémarrez PostgreSQL pour qu'il charge la nouvelle bibliothèque partagée.
5. Vérifiez le listener : `SELECT local_cache.health();` doit afficher
   `workers_running = workers_configured`. Envoyez RESP `PING` et attendez `PONG`.
6. Vérifiez la version de la bibliothèque chargée :

   ```sql
   SELECT current_setting('pg_local_cache.binary_version');
   ```

   Le résultat doit être `3.0.0`.
7. Dans chaque base où l'extension est installée, connectez-vous en tant que
   superutilisateur de base de données et exécutez :

   ```sql
   ALTER EXTENSION pg_local_cache UPDATE;
   ```

La migration supprime puis recrée `local_cache.metrics()`, car le type de
résultat tabulaire a changé avec le retrait des compteurs de l'API SQL de lecture.
Les droits personnalisés sur `local_cache.metrics()` sont conservés. PostgreSQL refuse de la supprimer tant qu'un objet utilisateur en dépend. La
migration n'utilise volontairement pas `CASCADE`. Supprimez ou modifiez les
vues et fonctions dépendantes vous-même, puis relancez la mise à niveau. Les
autres fonctions C conservées sont remplacées sur place ; leurs OID, droits et
triggers associés aux tables restent valides.

## Revenir à 2.0.4 {#rollback-to-204}

Il n'existe pas de script de rétrogradation. Pour revenir à 2.0.4, réinstallez
son paquet, redémarrez PostgreSQL pour qu'il charge l'ancienne bibliothèque,
détachez toutes les tables mappées, puis recréez l'extension dans chaque base
et rattachez les tables :

```sql
SELECT local_cache.detach_table('public.items'::regclass);
DROP EXTENSION pg_local_cache;
CREATE EXTENSION pg_local_cache VERSION '2.0.4';
SELECT local_cache.attach_table('public.items'::regclass);
```

Répétez `detach_table` et `attach_table` pour chaque table mappée. Avant le
retour arrière, enregistrez la liste des tables attachées et les éventuels
droits personnalisés accordés sur l'extension afin de pouvoir les restaurer.
Les objets utilisateur dépendant des fonctions de l'extension peuvent bloquer
sa suppression avec l'erreur de dépendance PostgreSQL habituelle ; traitez
explicitement ces dépendances.

## Chemin de recherche de bibliothèque {#library-lookup-path}

Le fichier de contrôle utilise le nom de bibliothèque simple `pg_local_cache`
dans `module_pathname`. PostgreSQL le résout via `dynamic_library_path`
(dont la valeur par défaut inclut `$libdir`). Si le serveur remplace ce
paramètre, ajoutez avant le redémarrage le répertoire où le paquet a installé
`pg_local_cache`.
