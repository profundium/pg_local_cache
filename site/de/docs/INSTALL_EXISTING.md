---
layout: doc
lang: de
translation_key: INSTALL_EXISTING
title: pg_local_cache auf PostgreSQL 14–18 installieren
seo_title: pg_local_cache auf PostgreSQL 14–18 installieren
description: Installieren Sie ein geprüftes Debian- oder RPM-Paket, verwenden Sie PGXN oder PGXS, konfigurieren Sie Preload, starten Sie PostgreSQL neu und initialisieren, aktualisieren oder entfernen Sie pg_local_cache.
section: Installation
permalink: /de/docs/INSTALL_EXISTING.html
last_modified_at: "2026-10-04"
---

# pg_local_cache auf einem bestehenden PostgreSQL-Server installieren {#install-pg_local_cache-on-an-existing-postgresql-server}

Diese Anleitung gilt für Linux und PostgreSQL 14–18. Installation und Konfiguration erfordern Datenbank-Superuserrechte. Das Eintragen der Erweiterung in <code>shared_preload_libraries</code> erfordert einen Neustart von PostgreSQL.

## 1. Voraussetzungen {#choose-an-installation-path}

Verwenden Sie beim Bauen aus dem Quellcode das <code>pg_config</code> des Zielservers. Planen Sie den Neustart mit dem Dienst oder Operator, der den PostgreSQL-Cluster verwaltet.

## 2. Paket installieren {#fast-binary-install}

Laden Sie das Paket und <code>SHA256SUMS</code> für PostgreSQL-Hauptversion und Architektur aus demselben [GitHub-Release](https://github.com/profundium/pg_local_cache/releases) herunter.

### Debian und Ubuntu {#controlled-binary-install}

Laden Sie <code>postgresql-&lt;major&gt;-pg-local-cache_&lt;version&gt;-1_&lt;arch&gt;.deb</code> herunter. Die Pakete werden auf Debian 12 gebaut und benötigen glibc 2.36 oder neuer. Das Manifest enthält alle Release-Dateien; bei einer Teilauswahl benötigen Sie <code>--ignore-missing</code>.

Prüfen Sie Prüfsumme und Build-Provenienz, installieren Sie anschließend das Paket:

```bash
sha256sum -c --ignore-missing SHA256SUMS
gh attestation verify <file>.deb --repo profundium/pg_local_cache
sudo apt install ./<file>.deb
```

### RHEL, Rocky Linux und AlmaLinux 9

Laden Sie das passende <code>.rpm</code> für PostgreSQL-Hauptversion und Architektur herunter. Prüfen und installieren Sie es:

```bash
sha256sum -c --ignore-missing SHA256SUMS
gh attestation verify <file>.rpm --repo profundium/pg_local_cache
sudo dnf install ./<file>.rpm
```

### PGXN

```bash
pgxn install pg_local_cache
```

### Aus dem Quellcode bauen {#build-from-source}

Installieren Sie die Entwicklungs-Header der Zielversion von PostgreSQL, einen C-Compiler und GNU Make. Bauen und installieren Sie die Erweiterung mit dem <code>pg_config</code> dieser Version:

```bash
make PG_CONFIG=/path/to/pg_config && \
  sudo make PG_CONFIG=/path/to/pg_config install
```

## 3. <code>postgresql.conf</code> konfigurieren {#configure-before-restart}

Behalten Sie vorhandene Einträge in <code>shared_preload_libraries</code> bei und ergänzen Sie <code>pg_local_cache</code>. Ersetzen Sie <code>app</code> durch den Namen der Datenbank, die die Erweiterung bedient. Minimale SQL-only-Konfiguration:

```conf
shared_preload_libraries = 'pg_local_cache'
pg_local_cache.database = 'app'
pg_local_cache.role = 'local_cache_worker'
pg_local_cache.cache_entries = 16384
pg_local_cache.memory_budget_mb = 384
pg_local_cache.port = 0
```

Für RESP2 sollte der Listener nur auf Loopback oder hinter einem authentifizierten TLS-Proxy erreichbar sein. Verwenden Sie eine geschützte Token-Datei:

### RESP2-Einstellungen {#enable-optional-resp2}

```conf
shared_preload_libraries = 'pg_local_cache'
pg_local_cache.database = 'app'
pg_local_cache.role = 'local_cache_worker'
pg_local_cache.port = 6380
pg_local_cache.bind_address = '127.0.0.1'
pg_local_cache.auth_token_file = '/secure/path/token'
```

Stimmen Sie Cache-Einträge, Relationszustände, Worker, Clients und <code>memory_budget_mb</code> gemeinsam ab. Hinweise zu Kapazität und Speicher finden Sie in der [technischen Referenz](TECHNICAL.md#shared-memory-and-configuration).

## 4. PostgreSQL neu starten

Verwenden Sie den Dienst oder Operator, der den Cluster verwaltet. Mit systemd:

```bash
# Debian and Ubuntu
sudo systemctl restart postgresql@<major>-main
# RHEL, Rocky Linux, and AlmaLinux
sudo systemctl restart postgresql-<major>
```

Ändern Sie bei Patroni die Clusterkonfiguration und starten Sie den Cluster über Patroni neu:

```bash
patronictl edit-config <cluster>
patronictl restart <cluster>
```

Erstellen Sie für Kubernetes ein eigenes PostgreSQL-Image mit dem passenden Paket und rollen Sie es über Ihren Operator aus. CloudNativePG-Erweiterungsimages sind geplant.

## 5. Initialisieren {#initialize-a-source-installation}

Verbinden Sie sich als Datenbank-Superuser mit der konfigurierten Datenbank. Erstellen Sie die Erweiterung und eine eigene Worker-Rolle und gewähren Sie ihr Zugriff auf die Metadaten:

```sql
CREATE EXTENSION IF NOT EXISTS pg_local_cache;
CREATE ROLE local_cache_worker LOGIN NOINHERIT NOSUPERUSER
    NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT CONNECT ON DATABASE app TO local_cache_worker;
GRANT USAGE ON SCHEMA local_cache TO local_cache_worker;
GRANT SELECT ON TABLE local_cache.mapping TO local_cache_worker;
```

Die Worker-Rolle wird auch bei <code>pg_local_cache.port = 0</code> benötigt. Verwenden Sie nicht die Rolle, der Anwendungstabellen gehören. Binden Sie jede permanente Tabelle mit einem unterstützten Primärschlüssel an:

### Tabelle anbinden {#attach-a-table}

```sql
SELECT local_cache.attach_table('public.items'::regclass);
```

### Bereitschaft prüfen {#verify-cold-fill-and-warm-hit}

```sql
SELECT local_cache.health();
```

Prüfen Sie, ob die Erweiterung bereit ist und die Zuordnungen aktuell sind.

## 6. Aktualisieren {#recover-a-failed-binary-install}

Installieren Sie das neue Paket und starten Sie PostgreSQL über den zuständigen Dienst oder Operator neu. Aktualisieren Sie danach die Erweiterung als Datenbank-Superuser:

```sql
ALTER EXTENSION pg_local_cache UPDATE;
```

## 7. Deinstallieren {#troubleshooting}

Trennen Sie alle angebundenen Tabellen und entfernen Sie die Erweiterung als Datenbank-Superuser. Entfernen Sie <code>pg_local_cache</code> aus <code>shared_preload_libraries</code>, starten Sie PostgreSQL neu und entfernen Sie das Paket mit dem passenden Paketmanager:

```sql
SELECT local_cache.detach_table('public.items'::regclass);
DROP EXTENSION pg_local_cache;
```

```bash
sudo apt remove postgresql-<major>-pg-local-cache
sudo dnf remove pg_local_cache_<major>
```

Weiter: Lesen Sie in der [technischen Referenz](TECHNICAL.md) mehr über SQL, Speicherbedarf, Monitoring und RESP-Sicherheit.
