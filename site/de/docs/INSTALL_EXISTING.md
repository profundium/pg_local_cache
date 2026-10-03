---
layout: doc
lang: de
translation_key: INSTALL_EXISTING
title: pg_local_cache auf PostgreSQL 14–18 installieren
seo_title: pg_local_cache auf PostgreSQL 14–18 installieren
description: Installieren Sie die pg_local_cache-PostgreSQL-Erweiterung mit verifizierten Linux-Binärdateien oder PGXS, konfigurieren Sie Preload und Neustart, prüfen Sie die Installation und stellen Sie sie sicher wieder her.
section: Installation
permalink: /de/docs/INSTALL_EXISTING.html
last_modified_at: "2026-09-05"
---

# pg_local_cache auf einem bestehenden PostgreSQL-Server installieren {#install-pg_local_cache-on-an-existing-postgresql-server}

Installieren Sie die Erweiterung mit einem verifizierten Linux-Paket oder bauen
Sie sie mit PostgreSQLs PGXS-Toolchain. Beide Wege erfordern vor
`CREATE EXTENSION` einen kontrollierten PostgreSQL-Neustart.

> **Wartungsfenster einplanen:** Die erste Aktivierung ändert
> `shared_preload_libraries`. Bewahren Sie vorhandene Einträge und starten Sie
> den richtigen Cluster erst neu, wenn die Vorprüfungen erfolgreich sind.

## Installationsweg wählen {#choose-an-installation-path}

| Weg | Geeignet für | Zuständig für Neustart |
|---|---|---|
| Neueste verifizierte Binärdatei | Lokaler Linux-amd64-Cluster | `pg_ctl`-Bootstrap |
| Feste verifizierte Binärdatei | Produktion und verwaltete Abläufe | systemd, `pg_ctl` oder externer Operator |
| PGXS-Quellcode-Build | Nicht unterstützte Plattform oder benutzerdefinierte PostgreSQL-Installation | Ihr normaler Betriebsablauf |

Veröffentlichte Binärdateien unterstützen PostgreSQL 14–18 unter Linux amd64 mit
glibc oder musl. Die Beispiele mit fester Version verwenden pg_local_cache 2.0.1.

## Schnelle Binärinstallation {#fast-binary-install}

Für einen lokalen, durch `pg_ctl` kontrollierten Cluster:

```bash
curl -fsSL https://github.com/profundium/pg_local_cache/releases/latest/download/install-latest.sh | bash -s -- app
```

Ersetzen Sie `app` durch den Datenbanknamen. Dadurch wird der reine SQL-Modus
mit `pg_local_cache.port = 0` aktiviert.

Der Bootstrap löst einen Release-Tag auf, verifiziert `fetch-release.sh` gegen
die `SHA256SUMS` dieses Releases, wählt das passende PostgreSQL- und libc-Archiv,
verifiziert es, installiert es, startet neu, erstellt die Erweiterung und führt
`local_cache.health()` aus.

Wenn `curl | bash` außerhalb Ihrer Richtlinie liegt, prüfen Sie das Skript zuerst:

```bash
curl -fsSLO https://github.com/profundium/pg_local_cache/releases/latest/download/install-latest.sh
less install-latest.sh
bash install-latest.sh app
```

## Kontrollierte Binärinstallation {#controlled-binary-install}

Laden Sie ein festes Release mit seinem veröffentlichten Helfer herunter:

```bash
curl -fsSLO https://github.com/profundium/pg_local_cache/releases/download/v2.0.1/fetch-release.sh
bash fetch-release.sh --release-tag v2.0.1 --output-directory ./pg_local_cache-package
```

Führen Sie die Vorprüfung aus und wählen Sie den Zuständigen für den Neustart explizit:

```bash
sudo ./pg_local_cache-package/install.sh preflight --database app
sudo ./pg_local_cache-package/install.sh install \
  --database app \
  --restart-method systemd \
  --systemd-unit postgresql@16-main
```

Unterstützte Neustartmethoden sind `systemd`, `pg_ctl` und `none`. Verwenden Sie
`none` mit Patroni, einem Kubernetes-Operator oder einer anderen externen
Steuerung. Starten Sie über diese Steuerung neu und prüfen Sie anschließend:

```bash
sudo ./pg_local_cache-package/install.sh verify --database app
```

Das Installationsprogramm gibt ein Zustandsverzeichnis aus. Bewahren Sie es auf,
bis die Prüfung erfolgreich ist; es enthält das für `recover` erforderliche
Online-Backup.

## Aus dem Quellcode bauen {#build-from-source}

Verwenden Sie dasselbe `pg_config` wie der Ziel-PostgreSQL-Server. Installieren
Sie zuerst dessen Server-Entwicklungsheader, einen C-Compiler und GNU Make.

```bash
git clone --branch v2.0.1 --depth 1 https://github.com/profundium/pg_local_cache.git
cd pg_local_cache
make PG_CONFIG=/usr/lib/postgresql/16/bin/pg_config
sudo make install PG_CONFIG=/usr/lib/postgresql/16/bin/pg_config
```

Bauen Sie aus einem sauberen Checkout, damit die Binärdatei ihren Git-Commit
aufzeichnet. Die Quellinstallation kopiert nur Erweiterungsdateien. Fahren Sie
unten mit Preload-Konfiguration, Neustart und SQL-Initialisierung fort.

## Vor dem Neustart konfigurieren {#configure-before-restart}

Minimale SQL-only-Konfiguration mit Standardkapazität und Speicherbudget:

```conf
shared_preload_libraries = 'pg_local_cache'
pg_local_cache.database = 'app'
pg_local_cache.role = 'local_cache_worker'
pg_local_cache.cache_entries = 16384
pg_local_cache.memory_budget_mb = 384
pg_local_cache.port = 0
```

Behalten Sie vorhandene Einträge in `shared_preload_libraries` bei. Ersetzen Sie
hier und in den folgenden SQL-Grants `app` durch den tatsächlichen
Datenbanknamen. Das Speicherbudget gilt für die Erweiterung, nicht für den
gesamten PostgreSQL-Server.

Dimensionieren Sie `cache_entries`, Relationszustände, Clients, Worker und
`memory_budget_mb` gemeinsam. Die Vorprüfung des Binärinstallers weist
widersprüchliche Pläne zurück. Bei Quellcode-Builds ist dieselbe
Kapazitätsprüfung vor dem Neustart erforderlich; erhöhen Sie die Zahl der
Einträge nicht, ohne das Speicherbudget zu überprüfen.

## Eine Quellinstallation initialisieren {#initialize-a-source-installation}

Verbinden Sie sich nach dem Neustart als Datenbank-Superuser mit der
konfigurierten Datenbank. Führen Sie bei einer ersten manuellen Installation
Folgendes aus:

```sql
CREATE EXTENSION IF NOT EXISTS pg_local_cache;
CREATE ROLE local_cache_worker LOGIN NOINHERIT NOSUPERUSER
    NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT CONNECT ON DATABASE app TO local_cache_worker;
GRANT USAGE ON SCHEMA local_cache TO local_cache_worker;
GRANT SELECT ON TABLE local_cache.mapping TO local_cache_worker;
```

Der Binärinstaller erstellt diese Rolle und ihre Metadaten-Grants; überspringen
Sie diesen Block, wenn diese Einrichtung bereits erfolgt ist. Verwenden Sie bei
einem eigenen `pg_local_cache.role` diesen Namen konsequent. Verwenden Sie keine
Rolle wieder, der Anwendungstabellen gehören.

**Die Rolle ist auch bei `pg_local_cache.port = 0` erforderlich.** Das Anhängen
von Tabellen validiert sie auch im SQL-only-Modus. Sie muss vom Eigentümer der
Tabelle getrennt sein und die oben gezeigten Attribute und Metadaten-Grants
besitzen. `attach_table` verwaltet ihren Zugriff auf jede zugeordnete Tabelle.
Für den SQL-only-Betrieb sind weder Passwort noch Netzwerk-Listener erforderlich.

## Eine Tabelle anhängen {#attach-a-table}

Verwenden Sie eine vorhandene dauerhafte Tabelle mit einem unterstützten
Primärschlüssel. Als Datenbank-Superuser in der konfigurierten Datenbank:

```sql
SELECT local_cache.attach_table('public.items'::regclass);
SELECT local_cache.health();
```

Gewähren Sie einer vorhandenen Anwendungsrolle nur die nötigen Rechte:

```sql
GRANT SELECT ON public.items TO app_user;
GRANT USAGE ON SCHEMA local_cache TO app_user;
GRANT EXECUTE ON FUNCTION local_cache.mget(regclass, anyarray) TO app_user;
```

Gewöhnliche `SELECT`-Abfragen werden von der Erweiterung nicht umgeschrieben.

## Kaltes Füllen und warmen Treffer prüfen {#verify-cold-fill-and-warm-hit}

```sql
SELECT local_cache.invalidate('public.items');
SELECT local_cache.mget('public.items'::regclass, ARRAY[1]::bigint[]);
SELECT local_cache.mget('public.items'::regclass, ARRAY[1]::bigint[]);
SELECT local_cache.stats();
```

Bestätigen Sie, dass `local_cache.health()` bereit ist, die Zuordnung konvergiert
ist und sich die SQL-Cache-Zähler wie erwartet bewegen.

## Optionales RESP2 aktivieren {#enable-optional-resp2}

RESP2 fügt einen Listener, Worker-Prozesse und ein gemeinsames Token hinzu. Es
verwendet dieselbe dedizierte PostgreSQL-Rolle, die für das Anhängen von Tabellen
erforderlich ist:

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

Halten Sie den Listener auf `127.0.0.1` oder hinter authentifiziertem TLS. RESP-
Clients teilen sich die konfigurierte Worker-Rolle und erhalten keinen
PostgreSQL-ACL-Kontext pro Client.

## Fehlgeschlagene Binärinstallation wiederherstellen {#recover-a-failed-binary-install}

Verwenden Sie das vom Installer ausgegebene Zustandsverzeichnis:

```bash
sudo ./pg_local_cache-package/install.sh recover \
  --state-directory /path/printed/by/install
```

Stellen Sie nicht wieder her, nachdem ein neuer Postmaster Datenverkehr
akzeptiert hat, bevor Sie den aufgezeichneten Zustand und die betrieblichen
Auswirkungen geprüft haben.

## Fehlerbehebung {#troubleshooting}

- **Preload-Fehler:** Prüfen Sie die Konfiguration des Zielclusters und starten
  Sie den richtigen Postmaster neu.
- **Worker-Rolle fehlt oder wird abgelehnt:** Schließen Sie die obige SQL-
  Initialisierung einschließlich Rollenattributen und Metadaten-Grants ab, auch
  im SQL-only-Modus.
- **Tabelle abgelehnt:** Verwenden Sie eine dauerhafte, nicht partitionierte,
  nicht von RLS geschützte Tabelle mit einem unterstützten Primärschlüssel.
- **Berechtigungsfehler bei `mget`:** Gewähren Sie `SELECT` auf der Quelltabelle,
  `USAGE` auf dem Schema und `EXECUTE` auf der Funktion.
- **Cache-Bypasses:** Prüfen Sie Isolationsstufe, Schreibvorgänge der aktuellen
  Transaktion, Recovery-Zustand, Zeilengröße und Metriken.
- **Veraltete Zuordnung nach DDL:** Führen Sie
  `local_cache.reconcile_table('public.items'::regclass)` aus.

Als Nächstes lesen Sie die [technische Referenz](TECHNICAL.md) zu SQL-Verträgen,
Konsistenz, Speichergrößen, Monitoring und RESP-Sicherheit.
