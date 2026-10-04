---
layout: doc
lang: fr
translation_key: INSTALL_EXISTING
title: Installer pg_local_cache sur PostgreSQL 14–18
seo_title: Installer pg_local_cache sur PostgreSQL 14–18
description: Installez un paquet Debian ou RPM vérifié, utilisez PGXN ou PGXS, configurez preload, redémarrez PostgreSQL, puis initialisez, mettez à niveau ou désinstallez pg_local_cache.
section: Installation
permalink: /fr/docs/INSTALL_EXISTING.html
last_modified_at: "2026-10-04"
---

# Installer pg_local_cache sur un serveur PostgreSQL existant {#install-pg_local_cache-on-an-existing-postgresql-server}

Ce guide concerne Linux et PostgreSQL 14–18. L'installation et la configuration nécessitent les droits de superutilisateur de la base de données. Ajouter l'extension à <code>shared_preload_libraries</code> nécessite un redémarrage de PostgreSQL.

## 1. Prérequis {#choose-an-installation-path}

Pour compiler depuis les sources, utilisez le <code>pg_config</code> du serveur cible. Planifiez le redémarrage avec le service ou l'opérateur qui gère le cluster PostgreSQL.

## 2. Installer un paquet {#fast-binary-install}

Téléchargez le paquet et <code>SHA256SUMS</code> correspondant à la version majeure de PostgreSQL et à l'architecture depuis la même [publication GitHub](https://github.com/profundium/pg_local_cache/releases).

### Debian et Ubuntu {#controlled-binary-install}

Téléchargez <code>postgresql-&lt;major&gt;-pg-local-cache_&lt;version&gt;-1_&lt;arch&gt;.deb</code>. Les paquets sont construits sur Debian 12 et nécessitent glibc 2.36 ou ultérieure. Le manifeste contient tous les fichiers de la publication ; utilisez <code>--ignore-missing</code> si vous n'en téléchargez que certains.

Vérifiez la somme de contrôle et la provenance de la compilation, puis installez le paquet :

```bash
sha256sum -c --ignore-missing SHA256SUMS
gh attestation verify <file>.deb --repo profundium/pg_local_cache
sudo apt install ./<file>.deb
```

### RHEL, Rocky Linux et AlmaLinux 9

Ces RPM nécessitent PostgreSQL de PGDG dans la même version majeure. Configurez
le [dépôt Yum PGDG](https://www.postgresql.org/download/linux/redhat/) pour
votre version d'EL et la version majeure de PostgreSQL, puis installez
`postgresql<major>-server` depuis PGDG avant l'extension. Le paquet de
l'extension s'installe sous `/usr/pgsql-<major>`. Les paquets PostgreSQL fournis
par la distribution utilisent d'autres noms de paquets, chemins et noms de
services ; pour ces serveurs, suivez les
[instructions de compilation depuis les sources](#build-from-source).

Téléchargez le <code>.rpm</code> correspondant à la version majeure de PostgreSQL et à l'architecture. Vérifiez-le puis installez-le :

```bash
sha256sum -c --ignore-missing SHA256SUMS
gh attestation verify <file>.rpm --repo profundium/pg_local_cache
sudo dnf install ./<file>.rpm
```

### PGXN

Installez le client PGXN, les en-têtes de développement du serveur PostgreSQL
cible, un compilateur C et GNU Make. Indiquez le <code>pg_config</code> du
serveur cible ; sinon PGXN utilise le premier trouvé dans <code>PATH</code>.
L'installation des fichiers système nécessite les droits root. Consultez les
[options de la commande PGXN install](https://pgxn.github.io/pgxnclient/usage.html#pgxn-install).

```bash
pgxn install --pg_config /path/to/pg_config --sudo sudo pg_local_cache
```

### Compiler depuis les sources {#build-from-source}

Installez les en-têtes de développement de la version cible de PostgreSQL, un compilateur C et GNU Make. Compilez puis installez l'extension avec le <code>pg_config</code> de cette version :

```bash
make PG_CONFIG=/path/to/pg_config && \
  sudo make PG_CONFIG=/path/to/pg_config install
```

## 3. Configurer <code>postgresql.conf</code> {#configure-before-restart}

Conservez les entrées existantes de `shared_preload_libraries` et ajoutez
`pg_local_cache`. Remplacez `app` par le nom de la base servie par l'extension.

### Configurer le listener RESP {#enable-optional-resp2}

Configurez le listener RESP avec un rôle worker dédié et un fichier de jeton
protégé :

```conf
shared_preload_libraries = 'pg_local_cache'
pg_local_cache.database = 'app'
pg_local_cache.role = 'local_cache_worker'
pg_local_cache.port = 6380
pg_local_cache.bind_address = '127.0.0.1'
pg_local_cache.auth_token_file = '/secure/path/token'
pg_local_cache.cache_entries = 16384
pg_local_cache.memory_budget_mb = 384
```

Par défaut, le listener n'accepte que les connexions loopback. Pour les clients
distants, utilisez un proxy ou sidecar local, ou autorisez explicitement le
trafic en clair sur un réseau de confiance avec
`pg_local_cache.allow_plaintext_network = on` ; TLS natif n'est pas disponible
dans la version 3.0.0.

Dimensionnez ensemble les entrées de cache, les états de relation, les workers,
les clients et `memory_budget_mb`. Consultez les conseils de capacité et de
mémoire dans la [référence technique](TECHNICAL.md#shared-memory-and-configuration).

## 4. Redémarrer PostgreSQL

Utilisez le service ou l'opérateur qui gère le cluster. Avec systemd :

```bash
# Debian and Ubuntu
sudo systemctl restart postgresql@<major>-main
# RHEL, Rocky Linux, and AlmaLinux
sudo systemctl restart postgresql-<major>
```

Avec Patroni, modifiez la configuration du cluster puis redémarrez-le avec Patroni :

```bash
patronictl edit-config <cluster>
patronictl restart <cluster>
```

Pour Kubernetes, créez une image PostgreSQL personnalisée contenant le paquet correspondant, puis déployez-la via votre opérateur. Les images d'extension CloudNativePG sont prévues.

## 5. Initialiser {#initialize-a-source-installation}

Connectez-vous à la base configurée en tant que superutilisateur de base de données. Créez l'extension et un rôle worker dédié, puis accordez-lui l'accès aux métadonnées :

```sql
CREATE EXTENSION IF NOT EXISTS pg_local_cache;
CREATE ROLE local_cache_worker LOGIN NOINHERIT NOSUPERUSER
    NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT CONNECT ON DATABASE app TO local_cache_worker;
GRANT USAGE ON SCHEMA local_cache TO local_cache_worker;
GRANT SELECT ON TABLE local_cache.mapping TO local_cache_worker;
```

Séparez le rôle worker des rôles propriétaires des tables applicatives. Attachez chaque table permanente dotée d'une clé primaire prise en charge :

### Attacher une table {#attach-a-table}

```sql
SELECT local_cache.attach_table('public.items'::regclass);
```

### Vérifier l'état {#verify-cold-fill-and-warm-hit}

```sql
SELECT local_cache.health();
```

Vérifiez que l'extension est prête et que les associations sont à jour.

## 6. Mettre à niveau {#recover-a-failed-binary-install}

Pour passer de 2.x à 3.0, suivez d'abord le [guide de mise à niveau](UPGRADING.md).
Migrez les applications de SQL `mget` vers RESP `MGET`, installez le
paquet 3.0.0 correspondant, redémarrez PostgreSQL et mettez à niveau l'extension
dans chaque base où elle est installée :

```sql
ALTER EXTENSION pg_local_cache UPDATE;
```

## 7. Désinstaller {#troubleshooting}

Détachez toutes les tables associées et supprimez l'extension en tant que superutilisateur de la base. Retirez <code>pg_local_cache</code> de <code>shared_preload_libraries</code>, redémarrez PostgreSQL puis supprimez le paquet avec le gestionnaire approprié :

```sql
SELECT local_cache.detach_table('public.items'::regclass);
DROP EXTENSION pg_local_cache;
```

```bash
sudo apt remove postgresql-<major>-pg-local-cache
sudo dnf remove pg_local_cache_<major>
```

À suivre : consultez la [référence technique](TECHNICAL.md) pour SQL, le dimensionnement mémoire, la supervision et la sécurité RESP.
