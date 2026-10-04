---
layout: doc
lang: de
translation_key: UPGRADING
title: pg_local_cache von 2.x auf 3.0.0 aktualisieren
seo_title: "pg_local_cache von 2.x auf 3.0.0 aktualisieren"
description: Stellen Sie Anwendungen von SQL mget auf RESP MGET um, aktualisieren Sie die Erweiterung sicher und erfahren Sie, wie Sie Abhängigkeitsfehler behandeln und auf 2.0.4 zurückrollen.
section: Install
permalink: /de/docs/UPGRADING.html
last_modified_at: "2026-10-04"
---

# pg_local_cache von 2.x auf 3.0.0 aktualisieren {#upgrade-pg_local_cache-from-2x-to-300}

Version 3.0.0 entfernt die SQL-Funktion `local_cache.mget(regclass, anyarray)`.
Verwenden Sie authentifiziertes RESP `MGET` für gecachte vollständige Zeilen:

```text
MGET CRUD:app.public.items:{"id":42} CRUD:app.public.items:{"id":7}
```

RESP-Worker verwenden die konfigurierte PostgreSQL-Rolle. Sie übernehmen weder
SQL-Rechte noch Transaktion oder Snapshot der Anwendung. Verwenden Sie SQL für
Projektionen, Joins, Zeilensperren und Lesevorgänge, die Semantik der
Anwendungssitzung benötigen. Der [RESP-Leitfaden](resp.md) beschreibt
Client-Setup und Schlüsselcodierung.

## Upgrade-Reihenfolge {#upgrade-order}

1. Stellen Sie Anwendungen so um, dass sie SQL `mget` nicht mehr aufrufen. Prüfen
   Sie, dass RESP `MGET` eine eigene Worker-Rolle und das erforderliche
   Autorisierungsmodell verwendet.
2. Installieren Sie das Paket oder die Bibliothek 3.0.0 für die laufende
   PostgreSQL-Hauptversion.
3. Wenn der 2.x-Listener eine Nicht-Loopback-Adresse in
   `pg_local_cache.bind_address` verwendete, binden Sie ihn vor dem Neustart an
   Loopback und verwenden Sie für entfernte Clients einen lokalen Proxy oder
   Sidecar, oder setzen Sie `pg_local_cache.allow_plaintext_network = on`
   ausdrücklich, wenn Klartext in diesem Netzwerk beabsichtigt ist. Native TLS
   kommt in einer späteren Version. Andernfalls starten die RESP-Worker nicht.
4. Starten Sie PostgreSQL neu, damit es die neue Shared Library lädt.
5. Prüfen Sie den Listener: `SELECT local_cache.health();` muss
   `workers_running = workers_configured` anzeigen. Senden Sie RESP `PING` und
   erwarten Sie `PONG`.
6. Prüfen Sie die geladene Bibliotheksversion:

   ```sql
   SELECT current_setting('pg_local_cache.binary_version');
   ```

   Das Ergebnis muss `3.0.0` sein.
7. Verbinden Sie sich in jeder Datenbank mit installierter Erweiterung als
   Datenbank-Superuser und führen Sie Folgendes aus:

   ```sql
   ALTER EXTENSION pg_local_cache UPDATE;
   ```

Die Migration entfernt und erstellt `local_cache.metrics()` neu, weil sich
sich der Tabellenergebnistyp durch den Wegfall der SQL-Lese-API-Zähler geändert hat.
Benutzerdefinierte Berechtigungen auf `local_cache.metrics()` bleiben erhalten. PostgreSQL verweigert das Entfernen, solange ein Benutzerobjekt davon abhängt.
Die Migration verwendet absichtlich kein `CASCADE`. Entfernen oder ändern Sie
abhängige Sichten und Funktionen selbst und wiederholen Sie dann das
Extension-Upgrade. Andere erhaltene C-Funktionen werden an Ort und Stelle
ersetzt, sodass ihre OIDs, Berechtigungen und Trigger angehängter Tabellen
gültig bleiben.

## Rollback auf 2.0.4 {#rollback-to-204}

Es gibt kein Downgrade-Skript. Installieren Sie für die Rückkehr zu 2.0.4 das
entsprechende Paket erneut, starten Sie PostgreSQL neu, damit es die alte
Bibliothek lädt, trennen Sie dann alle zugeordneten Tabellen, erstellen Sie
die Erweiterung in jeder Datenbank neu und hängen Sie die Tabellen wieder an:

```sql
SELECT local_cache.detach_table('public.items'::regclass);
DROP EXTENSION pg_local_cache;
CREATE EXTENSION pg_local_cache VERSION '2.0.4';
SELECT local_cache.attach_table('public.items'::regclass);
```

Wiederholen Sie `detach_table` und `attach_table` für jede zugeordnete
Tabelle. Sichern Sie vor dem Rollback die Liste der angehängten Tabellen sowie
eigene Berechtigungen für die Erweiterung, damit Sie sie wiederherstellen
können. Benutzerobjekte, die von Erweiterungsfunktionen abhängen, können das
Entfernen mit dem normalen PostgreSQL-Abhängigkeitsfehler verhindern; behandeln
Sie solche Abhängigkeiten ausdrücklich.

## Bibliothekssuchpfad {#library-lookup-path}

Die Control-Datei verwendet den einfachen Bibliotheksnamen `pg_local_cache` in
`module_pathname`. PostgreSQL löst ihn über `dynamic_library_path` auf
(der Standard enthält `$libdir`). Wenn der Server diese Einstellung
überschreibt, nehmen Sie vor dem Neustart das Verzeichnis auf, in dem das Paket
`pg_local_cache` installiert hat.
