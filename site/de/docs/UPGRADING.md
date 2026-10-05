---
layout: doc
lang: de
translation_key: UPGRADING
title: pg_local_cache auf 3.1.0 aktualisieren
seo_title: "pg_local_cache auf 3.1.0 aktualisieren"
description: pg_local_cache von 3.0.0 oder 2.x aktualisieren, Kapazitäts- und Worker-Einstellungen für 3.1 prüfen und die Erweiterung nach dem Neustart verifizieren.
section: Install
permalink: /de/docs/UPGRADING.html
last_modified_at: "2026-10-06"
---

# pg_local_cache auf 3.1.0 aktualisieren {#upgrade-pg_local_cache-from-2x-to-310}

Version 3.0.0 entfernte die SQL-Funktion `local_cache.mget(regclass, anyarray)`.
Verwenden Sie authentifiziertes RESP `MGET` für gecachte vollständige Zeilen.
RESP-Worker verwenden die konfigurierte PostgreSQL-Rolle und übernehmen weder
SQL-Rechte noch Transaktion oder Snapshot der Anwendung. Verwenden Sie SQL für
Projektionen, Joins, Zeilensperren und Lesevorgänge mit Semantik der
Anwendungssitzung. Client-Setup und Schlüsselcodierung beschreibt der
[RESP-Leitfaden](resp.md).

## Upgrade von 3.0.0 {#upgrade-from-300}

Version 3.1.0 ändert die Shared Library und das Shared-Memory-Layout. Installieren
Sie das Paket oder die Bibliothek 3.1.0 für die laufende PostgreSQL-Hauptversion
und starten Sie PostgreSQL neu, bevor Sie sie verwenden. Die SQL-Migration
`3.0.0--3.1.0` ist wirkungslos: SQL-Objekte, Tabellenzuordnungen und Trigger
bleiben unverändert. Führen Sie das Extension-Upgrade in jeder Datenbank aus,
damit Version `3.1.0` vermerkt wird.

### Einstellungsänderungen {#setting-changes}

| Einstellung | Verhalten in 3.1.0 |
|---|---|
| `pg_local_cache.cache_entries` | Bereich `128`–`16777216` Deskriptoren. Der eingebaute Standardwert `262144` wird anhand des Standardbudgets von 384 MiB berechnet und reserviert mindestens die Hälfte für Arenaseiten. Die tatsächliche Byte-Kapazität hängt von Arena und Zeilengröße ab; beim Überschreiten des Budgets schlägt der Start fehl. |
| `pg_local_cache.lock_partitions` | Standard `64`; Zweierpotenz von `16` bis `256`. Kleine Caches verwenden weniger Partitionen. |
| `pg_local_cache.dirty_marker_entries` | Standard `-1` aktiviert automatische Berechnung: `min(16384, max(1024, floor(cache_entries / 4)))`. Expliziter Bereich: `128`–`1048576`. |
| `pg_local_cache.dirty_marker_memory_mb` | Standard `-1` aktiviert automatische Berechnung: `min(16, max(1, floor(memory_budget_mb / 25)))` MiB. Expliziter Bereich: `1`–`1024` MiB. |
| `pg_local_cache.max_clients_per_worker` | Standard `64`; Bereich auf `1`–`4096` erweitert. `max_clients` darf `workers × max_clients_per_worker` nicht überschreiten. Das Softlimit `RLIMIT_NOFILE` jedes Workers muss mindestens `min(max_clients, max_clients_per_worker) + 33` betragen; erhöhen Sie bei Bedarf das `nofile`-Limit des Prozesses/Containers. |
| `pg_local_cache.max_deferred_misses` | Neue Einstellung. Standard `8`; Bereich `1`–`64` aufgeschobene, relationsgesperrte Anfragen je Worker. |

In 3.1.0 wurden keine Einstellungen aus 3.0.0 entfernt oder umbenannt. Alle
Einstellungen werden nach einem Neustart wirksam.

`local_cache.stats()` ergänzt `fast_path_hits`, `fast_path_fallbacks` und vier
`fast_path_fallback_key_form`, `fast_path_fallback_mapping_shape`, `fast_path_fallback_multi_key` und `fast_path_fallback_cache_state`; `cache_memory_capacity_bytes`,
`cache_memory_used_bytes`, `cache_fragmentation_bytes`,
`arena_admission_rejections_total`; `dirty_marker_capacity`,
`dirty_marker_entries`, `dirty_marker_highwater`,
`dirty_marker_fallbacks_total`, `dirty_marker_entries_effective`,
`dirty_marker_memory_mb_effective`, `dirty_marker_memory_capacity_bytes`,
`dirty_key_limit_fallbacks` sowie `lock_partitions`,
`max_clients_per_worker` und `client_slots`. RESP `STAT` ergänzt Worker-lokale
Felder `deferred_misses_total`, `deferred_misses_current`,
`deferred_timeouts_total` und `deferred_rejections_total`.

Upgrade-Reihenfolge:

1. Installieren Sie das Paket oder die Bibliothek 3.1.0 für die laufende
   PostgreSQL-Hauptversion.
2. Prüfen Sie die obigen Einstellungen. Erhöhen Sie bei mehr Client-Slots das
   Softlimit `nofile` des Prozesses/Containers gemäß der Formel.
3. Starten Sie PostgreSQL neu, damit es die Bibliothek lädt und das neue
   Shared-Memory-Layout anlegt.
4. Verbinden Sie sich in jeder Datenbank mit installierter Erweiterung als
   Datenbank-Superuser und führen Sie Folgendes aus:

   ```sql
   ALTER EXTENSION pg_local_cache UPDATE;
   ```

5. Prüfen Sie Bibliothek und Worker:

   ```sql
   SELECT current_setting('pg_local_cache.binary_version');
   SELECT local_cache.health();
   ```

   Die Bibliotheksversion muss `3.1.0` sein; health muss
   `workers_running = workers_configured` anzeigen.

## Upgrade von 2.x {#upgrade-from-2x}

Stellen Sie Anwendungen vor dem Upgrade von SQL `mget` auf RESP `MGET` um und
verwenden Sie eine eigene Worker-Rolle mit dem erforderlichen
Autorisierungsmodell. Wenn der 2.x-Listener eine Nicht-Loopback-Adresse nutzt,
konfigurieren Sie vor dem Neustart natives RESP-TLS mit Zertifikat und Schlüssel.
`pg_local_cache.tls_ca_file` erzwingt Clientzertifikate (mTLS). RESP-TLS ist
unabhängig von PostgreSQL-`ssl_*`. Falls Klartext erforderlich ist, setzen Sie
`pg_local_cache.allow_plaintext_network = on` ausdrücklich und nur in einem
vertrauenswürdigen Netzwerk. Ohne TLS oder diese Freigabe starten Nicht-Loopback-
RESP-Worker nicht.

Die SQL-Migration von 2.x auf 3.0 entfernte die SQL-Lese-API und ihre Zähler,
wodurch sich der Ergebnistyp von `local_cache.metrics()` änderte. Eigene
Berechtigungen bleiben erhalten. Ein abhängiges Benutzerobjekt kann die
Migration blockieren; sie verwendet kein `CASCADE`. Ändern oder entfernen Sie
solche Abhängigkeiten und wiederholen Sie das Upgrade. Diese SQL-Änderungen
gehören zur früheren 3.0-Migration, nicht zur wirkungslosen SQL-Migration 3.1. Verwenden Sie danach die obigen Schritte zum Installieren, Neustarten, Aktualisieren der Extension und Prüfen. PostgreSQL wendet zuerst die frühere Migration von 2.x auf 3.0 und dann die wirkungslose Migration von 3.0.0 auf 3.1.0 an.

## Rollback auf 2.0.4 {#rollback-to-204}

Es gibt kein Downgrade-Skript. Installieren Sie für die Rückkehr zu 2.0.4 das
entsprechende Paket erneut, starten Sie PostgreSQL neu, trennen Sie alle
zugeordneten Tabellen, erstellen Sie die Erweiterung in jeder Datenbank neu und
hängen Sie die Tabellen wieder an:

```sql
SELECT local_cache.detach_table('public.items'::regclass);
DROP EXTENSION pg_local_cache;
CREATE EXTENSION pg_local_cache VERSION '2.0.4';
SELECT local_cache.attach_table('public.items'::regclass);
```

Wiederholen Sie `detach_table` und `attach_table` für jede Tabelle. Sichern Sie
vor dem Rollback die Tabellenliste und eigene Extension-Berechtigungen.
Benutzerobjekte, die von Erweiterungsfunktionen abhängen, können das Entfernen
verhindern; behandeln Sie solche Abhängigkeiten ausdrücklich.

## Bibliothekssuchpfad {#library-lookup-path}

Die Control-Datei verwendet `pg_local_cache` als einfachen Namen in
`module_pathname`. PostgreSQL löst ihn über `dynamic_library_path` auf (der
Standard enthält `$libdir`). Wenn der Server die Einstellung überschreibt,
nehmen Sie vor dem Neustart das Installationsverzeichnis der Bibliothek auf.

Dokumentation für 2.x: https://github.com/profundium/pg_local_cache/tree/v2.0.4/docs
