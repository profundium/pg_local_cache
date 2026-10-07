---
layout: doc
lang: de
translation_key: QUICKSTART
title: pg_local_cache lokal ausprobieren
seo_title: "Einen PostgreSQL-Zeilen-Cache lokal ausprobieren | pg_local_cache"
description: "Führen Sie pg_local_cache 3.1.0 in einem temporären PostgreSQL aus, lesen Sie Beispielzeilen über RESP, prüfen Sie Cache-Treffer, testen Sie Aktualisierungen und entfernen Sie die Demo, ohne eine bestehende Datenbank zu ändern."
section: Schnellstart
permalink: /de/docs/QUICKSTART.html
last_modified_at: "2026-09-16"
---

# pg_local_cache lokal ausprobieren {#try-pg_local_cache-locally}

Diese Demo erstellt pg_local_cache aus Ihrem Checkout in einem separaten PostgreSQL-16-Server. Sie installiert es nicht in einem vorhandenen PostgreSQL-Server. Das Lese-Beispiel verwendet den über das Compose-Overlay konfigurierten RESP-Listener.

Sie benötigen Git, Docker und Docker Compose mit Unterstützung für `up --wait`.
Das Image wird aus dem Quellcode gebaut.

## Datenbank starten {#start-the-database}

```bash
git clone https://github.com/profundium/pg_local_cache.git
cd pg_local_cache
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml up --build --wait
```

Die Demo bindet PostgreSQL und RESP an die Loopback-Ports `55432` und `56379`, hat kein dauerhaftes Volume und speichert Daten in containerlokalem tmpfs. Das RESP-Overlay bindet innerhalb des Container-Netzwerks und aktiviert seinen Klartext-Demo-Listener ausdrücklich. Beim Stoppen des Containers werden die Daten verworfen. `demo-only` und das öffentliche RESP-Token gelten nur für diese Loopback-Demo; verwenden Sie in Produktion eigene Zugangsdaten.

Wenn Port 55432 belegt ist, setzen Sie vor dem Start von Compose
`PGLC_DEMO_PORT` und lassen Sie es beim Ausführen des Node.js-Beispiels gesetzt:

```bash
export PGLC_DEMO_PORT=55433
```

## Über RESP lesen {#read-as-an-application-role}

Die Einrichtung erstellt 4.096 Zeilen in `public.items`. Nur diese Tabelle ist an den Cache angehängt. Die Rolle `demo` ist kein Superuser.

```bash
export REDISCLI_AUTH=DemoRespToken_0123456789abcdef0123456789
redis-cli -2 -p 56379 MGET \
  'CRUD:pglc_demo.public.items:{"id":42}' \
  'CRUD:pglc_demo.public.items:{"id":7}' \
  'CRUD:pglc_demo.public.items:{"id":42}' \
  'CRUD:pglc_demo.public.items:{"id":999999}'
```

Die Antwort behält Schlüsselreihenfolge und Duplikate bei. Die erste und dritte Position gehören zu Zeile 42; die letzte Position ist RESP null, da Schlüssel 999999 fehlt. Eine RESP-Anfrage lässt Null-Eingabeschlüssel aus; Client-Helfer können diese Positionen bei Bedarf wiederherstellen.

Prüfen Sie die Zähler als Datenbankadministrator:

```bash
docker compose -f examples/compose.yaml exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 -U postgres -d pglc_demo \
  -c 'SELECT local_cache.health();' \
  -c 'SELECT local_cache.stats();'
```

In dieser frischen Demo sollte `local_cache.health()` `ready: true` melden, und wiederholte Lesevorgänge sollten `cache_hits` erhöhen. Prüfen Sie bei Bedarf `cache_misses`, `database_reads` und `cache_enabled` in `local_cache.stats()` und `local_cache.health()`.

## Commit und Rollback prüfen {#check-commit-and-rollback}

Mit Node.js 20 oder neuer:

```bash
npm --prefix examples/node-postgres ci --ignore-scripts
npm --prefix examples/node-postgres run demo
```

Der Test verwendet RESP für Lesevorgänge und PostgreSQL für Schreibvorgänge. Er prüft einen warmen Treffer, Eingabereihenfolge, doppelte und fehlende Schlüssel, Cache-Invalidierung und Sichtbarkeit bestätigter Änderungen. RESP-Worker verwenden eine konfigurierte PostgreSQL-Rolle und teilen weder SQL-Transaktion noch Snapshot der Anwendung.

Siehe den [Leitfaden zur Cache-Invalidierung](cache-invalidation.md) oder die [Erklärung der Node.js-Abfrage](resp.md#nodejs).

## Ihre Anwendung verbinden {#connect-your-application}

- [Node.js](resp.md#nodejs): Verwenden Sie RESP für gecachte Lesevorgänge und `pg` für SQL-Schreibvorgänge.
- [Go](resp.md#go): Verwenden Sie RESP für gecachte Lesevorgänge und `pgx` für SQL-Schreibvorgänge.
- [RESP](resp.md): Verbinden Sie sich mit einem Redis-Client.

Als Nächstes [vergleichen Sie dieselbe vorbereitete SQL- und RESP-Arbeitslast](BENCHMARKS.md#mit-bench-reproduzieren). Bei Ergebnis- oder Einrichtungsproblemen öffnen Sie einen [Workload-Bericht](https://github.com/profundium/pg_local_cache/issues/new?template=workload.yml) mit Umgebung, Benchmark-JSON oder Fehlerprotokoll.

## Demo entfernen {#remove-the-demo}

```bash
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml down
```

Das lokal gebaute Docker-Image bleibt für einen weiteren Lauf verfügbar. Kein
hosteigener PostgreSQL-Dienst muss neu gestartet oder wiederhergestellt werden.

Für eine bestehende Datenbank folgen Sie dem [Installationsleitfaden](INSTALL_EXISTING.md).
Dieser Weg hat andere Berechtigungen, Konfigurationen und Neustartanforderungen.
