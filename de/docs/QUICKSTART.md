---
layout: doc
lang: de
translation_key: QUICKSTART
title: pg_local_cache lokal ausprobieren
seo_title: "Einen PostgreSQL-Zeilen-Cache lokal ausprobieren | pg_local_cache"
description: Führen Sie pg_local_cache 2.0 in einem verworfenen PostgreSQL aus, lesen Sie Beispielzeilen, prüfen Sie Cache-Treffer, testen Sie Updates und entfernen Sie die Demo, ohne eine bestehende Datenbank zu ändern.
section: Schnellstart
permalink: /de/docs/QUICKSTART.html
last_modified_at: "2026-09-16"
---

# pg_local_cache lokal ausprobieren {#try-pg_local_cache-locally}

Diese Demo baut pg_local_cache aus Ihrem Checkout in einem separaten PostgreSQL-
16-Server. Sie installiert nicht in einem bestehenden PostgreSQL-Server.

Sie benötigen Git, Docker und Docker Compose mit Unterstützung für `up --wait`.
Das Image wird aus dem Quellcode gebaut.

## Datenbank starten {#start-the-database}

```bash
git clone https://github.com/profundium/pg_local_cache.git
cd pg_local_cache
docker compose -f examples/compose.yaml up --build --wait
```

Die Demo bindet PostgreSQL an `127.0.0.1:55432`, hat keinen RESP-Listener und
kein dauerhaftes Volume und speichert Daten in containerlokalem tmpfs. Beim
Stoppen des Containers werden die Daten verworfen. `demo-only` ist für diese
Loopback-Demo gedacht; verwenden Sie in Produktion eigene Zugangsdaten.

Wenn Port 55432 belegt ist, setzen Sie vor dem Start von Compose
`PGLC_DEMO_PORT` und lassen Sie es beim Ausführen des Node.js-Beispiels gesetzt:

```bash
export PGLC_DEMO_PORT=55433
```

## Als Anwendungsrolle lesen {#read-as-an-application-role}

Die Einrichtung erstellt 4.096 Zeilen in `public.items`. Nur diese Tabelle ist
an den Cache angehängt. Die Rolle `demo` ist kein Superuser.

```bash
docker compose -f examples/compose.yaml exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 -U demo -d pglc_demo <<'SQL'
SELECT unnest(local_cache.mget(
  'public.items'::regclass,
  ARRAY[42, 7, 42, NULL, 999999]::bigint[]
));
SELECT unnest(local_cache.mget(
  'public.items'::regclass,
  ARRAY[42, 7, 42, NULL, 999999]::bigint[]
));
SQL
```

Beide Aufrufe geben dieselben geordneten Zeilen zurück. Die erste und dritte
Position beziehen sich auf Zeile 42. Die letzten beiden Positionen sind SQL-
`NULL`: Eine Eingabe ist null, und der Schlüssel 999999 existiert nicht. In
psql erscheinen SQL-Nullwerte standardmäßig leer.

Die Funktion gibt **`text[]`** zurück. `unnest` zeigt oben einen Array-Eintrag pro
Zeile an.

Prüfen Sie die Zähler als Datenbankadministrator:

```bash
docker compose -f examples/compose.yaml exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 -U postgres -d pglc_demo \
  -c 'SELECT local_cache.health();' \
  -c 'SELECT local_cache.stats();'
```

In dieser frischen Demo sollte `local_cache.health()` `ready: true` melden, und
wiederholte Lesevorgänge sollten `sql_cache_hits` erhöhen. Wenn die Trefferzahl
bei null bleibt, prüfen Sie `sql_cache_misses`, `sql_cache_fills` und
`sql_cache_bypasses` mit dem [Invalidation-Leitfaden](cache-invalidation.md#inspect-the-cause-of-a-miss).

## Commit und Rollback prüfen {#check-commit-and-rollback}

Mit Node.js 20 oder neuer:

```bash
npm --prefix examples/node-postgres ci --ignore-scripts
npm --prefix examples/node-postgres run demo
```

Der Test öffnet getrennte Lese- und Schreibverbindungen. Er prüft einen warmen
Treffer, Eingabereihenfolge, doppelte und fehlende Schlüssel, ein nicht
bestätigtes Update, Read-your-writes, Rollback und ein bestätigtes Update. Bei
einer fehlgeschlagenen Assertion endet er mit einem Fehlercode.

Siehe den [SQL-Rundgang mit zwei Sitzungen](cache-invalidation.md) oder die
[Erklärung der Node.js-Abfrage](node-postgres.md).

## Ihre Anwendung verbinden {#connect-your-application}

- [Node.js](node-postgres.md): Verwenden Sie Ihre bestehende `pg`-Verbindung oder Ihren Pool.
- [Go](go.md): Verbinden Sie sich mit `pgx` und dekodieren Sie die zurückgegebenen Zeilen.
- [RESP](resp.md): Aktivieren Sie den optionalen Endpunkt und verbinden Sie sich mit einem Redis-Client.

Als Nächstes [vergleichen Sie dieselbe SQL- und RESP-Arbeitslast](BENCHMARKS.md#run-the-same-comparison-on-every-client).
Bei Ergebnis- oder Einrichtungsproblemen öffnen Sie einen
[Workload-Bericht](https://github.com/profundium/pg_local_cache/issues/new?template=workload.yml)
mit Ihrer Umgebung sowie Benchmark-JSON oder Fehlerprotokoll.

## Demo entfernen {#remove-the-demo}

```bash
docker compose -f examples/compose.yaml down
```

Das lokal gebaute Docker-Image bleibt für einen weiteren Lauf verfügbar. Kein
hosteigener PostgreSQL-Dienst muss neu gestartet oder wiederhergestellt werden.

Für eine bestehende Datenbank folgen Sie dem [Installationsleitfaden](INSTALL_EXISTING.md).
Dieser Weg hat andere Berechtigungen, Konfigurationen und Neustartanforderungen.
