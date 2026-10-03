---
layout: doc
lang: fr
translation_key: INSTALL_EXISTING
title: Installer pg_local_cache sur PostgreSQL 14-18
seo_title: Installer pg_local_cache sur PostgreSQL 14-18
description: Installez l'extension PostgreSQL pg_local_cache avec des binaires Linux vérifiés ou PGXS, puis configurez le preload, redémarrez, vérifiez et récupérez-la en toute sécurité.
section: Installation
permalink: /fr/docs/INSTALL_EXISTING.html
last_modified_at: "2026-09-05"
---

# Installer pg_local_cache sur un serveur PostgreSQL existant {#install-pg_local_cache-on-an-existing-postgresql-server}

Installez l'extension avec un paquet Linux vérifié ou compilez-la avec la
chaîne d'outils PGXS de PostgreSQL. Les deux chemins nécessitent un seul
redémarrage contrôlé de PostgreSQL avant `CREATE EXTENSION`.

> **Planifiez une fenêtre de maintenance :** la première activation modifie
> `shared_preload_libraries`. Conservez ses entrées existantes et ne redémarrez
> le bon cluster qu'après la réussite des vérifications préalables.

## Choisir un chemin d'installation {#choose-an-installation-path}

| Chemin | Adapté à | Responsable du redémarrage |
|---|---|---|
| Dernier binaire vérifié | Cluster Linux amd64 local | bootstrap `pg_ctl` |
| Binaire vérifié figé | Production et opérations managées | systemd, `pg_ctl` ou opérateur externe |
| Compilation source PGXS | Plateforme non prise en charge ou installation PostgreSQL personnalisée | Votre procédure d'exploitation habituelle |

Les binaires publiés prennent en charge PostgreSQL 14-18 sous Linux amd64 avec
glibc ou musl. Les exemples de version figée ci-dessous utilisent
pg_local_cache 2.0.1.

## Installation rapide d'un binaire {#fast-binary-install}

Pour un cluster local contrôlé par `pg_ctl` :

```bash
curl -fsSL https://github.com/profundium/pg_local_cache/releases/latest/download/install-latest.sh | bash -s -- app
```

Remplacez `app` par le nom de la base de données. Cela active le mode SQL
uniquement avec `pg_local_cache.port = 0`.

Le bootstrap résout un seul tag de version, vérifie `fetch-release.sh` par
rapport au `SHA256SUMS` de cette version, sélectionne l'archive PostgreSQL et
libc correspondante, la vérifie, l'installe, redémarre, crée l'extension et
exécute `local_cache.health()`.

Si `curl | bash` n'est pas autorisé par votre politique, inspectez d'abord le
script :

```bash
curl -fsSLO https://github.com/profundium/pg_local_cache/releases/latest/download/install-latest.sh
less install-latest.sh
bash install-latest.sh app
```

## Installation contrôlée d'un binaire {#controlled-binary-install}

Téléchargez une version figée avec son helper publié :

```bash
curl -fsSLO https://github.com/profundium/pg_local_cache/releases/download/v2.0.1/fetch-release.sh
bash fetch-release.sh --release-tag v2.0.1 --output-directory ./pg_local_cache-package
```

Exécutez les vérifications préalables, puis choisissez explicitement le
responsable du redémarrage :

```bash
sudo ./pg_local_cache-package/install.sh preflight --database app
sudo ./pg_local_cache-package/install.sh install \
  --database app \
  --restart-method systemd \
  --systemd-unit postgresql@16-main
```

Les méthodes de redémarrage prises en charge sont `systemd`, `pg_ctl` et
`none`. Utilisez `none` avec Patroni, un opérateur Kubernetes ou un autre
contrôleur externe. Redémarrez via ce contrôleur, puis vérifiez :

```bash
sudo ./pg_local_cache-package/install.sh verify --database app
```

L'installateur affiche un répertoire d'état. Conservez-le jusqu'à la fin de la
vérification ; il contient la sauvegarde en ligne nécessaire à `recover`.

## Compiler depuis les sources {#build-from-source}

Utilisez le même `pg_config` que le serveur PostgreSQL cible. Installez d'abord
les en-têtes de développement du serveur, un compilateur C et GNU Make.

```bash
git clone --branch v2.0.1 --depth 1 https://github.com/profundium/pg_local_cache.git
cd pg_local_cache
make PG_CONFIG=/usr/lib/postgresql/16/bin/pg_config
sudo make install PG_CONFIG=/usr/lib/postgresql/16/bin/pg_config
```

Compilez depuis un checkout propre afin que le binaire enregistre son commit
Git. L'installation source ne copie que les fichiers de l'extension.
Poursuivez avec la configuration du preload, le redémarrage et
l'initialisation SQL ci-dessous.

## Configurer avant le redémarrage {#configure-before-restart}

Configuration SQL uniquement minimale avec la capacité et le budget mémoire
par défaut :

```conf
shared_preload_libraries = 'pg_local_cache'
pg_local_cache.database = 'app'
pg_local_cache.role = 'local_cache_worker'
pg_local_cache.cache_entries = 16384
pg_local_cache.memory_budget_mb = 384
pg_local_cache.port = 0
```

Conservez toutes les entrées existantes de `shared_preload_libraries`.
Remplacez `app` par le vrai nom de la base ici et dans les GRANT SQL
ci-dessous. Le budget mémoire concerne l'extension, pas l'ensemble du serveur
PostgreSQL.

Dimensionnez ensemble `cache_entries`, les états de relations, les clients, les
workers et `memory_budget_mb`. La vérification préalable de l'installateur
binaire rejette les plans incohérents. Les compilations source nécessitent la
même revue de capacité avant le redémarrage ; n'augmentez pas le nombre
d'entrées sans revoir le budget mémoire.

## Initialiser une installation source {#initialize-a-source-installation}

Après le redémarrage, connectez-vous à la base configurée en tant que
superutilisateur de base de données. Pour une première installation manuelle,
exécutez :

```sql
CREATE EXTENSION IF NOT EXISTS pg_local_cache;
CREATE ROLE local_cache_worker LOGIN NOINHERIT NOSUPERUSER
    NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT CONNECT ON DATABASE app TO local_cache_worker;
GRANT USAGE ON SCHEMA local_cache TO local_cache_worker;
GRANT SELECT ON TABLE local_cache.mapping TO local_cache_worker;
```

Le binaire installe ce rôle et ses privilèges de métadonnées ; ignorez ce bloc
lorsque cette configuration est déjà terminée. Pour un
`pg_local_cache.role` personnalisé, utilisez ce nom partout. Ne réutilisez pas
un rôle propriétaire de tables applicatives.

**Le rôle est requis même avec `pg_local_cache.port = 0`.** L'attachement de
table le valide aussi en mode SQL uniquement. Il doit être distinct du
propriétaire de la table et posséder les attributs et privilèges de métadonnées
ci-dessus. `attach_table` gère son accès à chaque table mappée. Aucun mot de
passe ni écouteur réseau n'est nécessaire pour le mode SQL uniquement.

## Attacher une table {#attach-a-table}

Utilisez une table permanente existante avec une clé primaire prise en charge.
En tant que superutilisateur de base dans la base configurée :

```sql
SELECT local_cache.attach_table('public.items'::regclass);
SELECT local_cache.health();
```

N'accordez à un rôle applicatif existant que ce dont il a besoin :

```sql
GRANT SELECT ON public.items TO app_user;
GRANT USAGE ON SCHEMA local_cache TO app_user;
GRANT EXECUTE ON FUNCTION local_cache.mget(regclass, anyarray) TO app_user;
```

Les `SELECT` ordinaires ne sont pas réécrits par l'extension.

## Vérifier le remplissage à froid et le hit à chaud {#verify-cold-fill-and-warm-hit}

```sql
SELECT local_cache.invalidate('public.items');
SELECT local_cache.mget('public.items'::regclass, ARRAY[1]::bigint[]);
SELECT local_cache.mget('public.items'::regclass, ARRAY[1]::bigint[]);
SELECT local_cache.stats();
```

Confirmez que `local_cache.health()` est prêt, que le mapping a convergé et que
les compteurs du cache SQL évoluent comme prévu.

## Activer RESP2 en option {#enable-optional-resp2}

RESP2 ajoute un écouteur, des processus workers et un token partagé. Il utilise
le même rôle PostgreSQL dédié requis pour l'attachement des tables :

```bash
sudo ./pg_local_cache-package/install.sh preflight \
  --database app \
  --mode resp \
  --token-file /secure/path/token

sudo ./pg_local_cache-package/install.sh install \
  --database app \
  --mode resp \
  --token-file /secure/path/token \
  --restart-method systemd \
  --systemd-unit postgresql@16-main
```

Gardez l'écouteur sur `127.0.0.1` ou derrière un TLS authentifié. Les clients
RESP partagent le rôle worker configuré et ne reçoivent pas le contexte ACL
PostgreSQL propre au client SQL.

## Récupérer une installation binaire échouée {#recover-a-failed-binary-install}

Utilisez le répertoire d'état affiché par l'installateur :

```bash
sudo ./pg_local_cache-package/install.sh recover \
  --state-directory /path/printed/by/install
```

Ne récupérez pas après qu'un nouveau postmaster a accepté du trafic sans avoir
revu l'état enregistré et l'impact opérationnel.

## Dépannage {#troubleshooting}

- **Erreur de preload :** vérifiez la configuration du cluster cible et redémarrez le bon postmaster.
- **Rôle worker absent ou rejeté :** terminez l'initialisation SQL ci-dessus, y compris ses attributs et privilèges de métadonnées, même en mode SQL uniquement.
- **Table rejetée :** utilisez une table permanente, non partitionnée et sans RLS avec une clé primaire prise en charge.
- **Erreur de permission `mget` :** accordez `SELECT` sur la table source, `USAGE` sur le schéma et `EXECUTE` sur la fonction.
- **Contournements du cache :** inspectez le niveau d'isolation, les écritures de la transaction courante, l'état de récupération, la taille des lignes et les métriques.
- **Mapping obsolète après DDL :** exécutez `local_cache.reconcile_table('public.items'::regclass)`.

Ensuite : lisez la [référence technique](TECHNICAL.md) pour les contrats SQL,
la cohérence, le dimensionnement mémoire, la supervision et la sécurité RESP.
