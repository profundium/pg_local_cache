---
layout: doc
lang: de
translation_key: resp
title: Über RESP verbinden
description: Lesen Sie PostgreSQL-Zeilen über RESP2 mit redis-cli oder Node.js. Enthält Authentifizierung, Client-Einstellungen, ausführbare Beispiele und Aufräumen.
section: RESP
permalink: /de/docs/resp.html
last_modified_at: "2026-10-04"
---

# Über RESP verbinden {#connect-over-resp}

Verwenden Sie `MGET`, um gecachte PostgreSQL-Zeilen mit einem RESP2-Client zu lesen.
Starten Sie die [verworfene Demo](QUICKSTART.md) mit ihrer RESP-Konfiguration:

```bash
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml up --build --wait
```

Damit wird RESP unter `127.0.0.1:56379` aktiviert. Beim erneuten Erstellen der
Demo werden ihre Daten verworfen. Das folgende Token ist öffentlich und nur für
diese lokale Demo bestimmt.

Der RESP-Listener bindet standardmäßig an `127.0.0.1`. Für eine Netzwerkadresse ist `pg_local_cache.allow_plaintext_network=on` erforderlich; das Demo-Container-Setup aktiviert dies nur für sein isoliertes Beispielnetz. Der Endpunkt bietet selbst kein TLS: Verwenden Sie Loopback oder einen authentifizierten TLS-Proxy und schützen Sie die Token-Datei. `pg_local_cache.enabled` ist ein SIGHUP-Kill-Switch. Jeder RESP-Worker übernimmt einen Reload asynchron an seiner nächsten Befehlsgrenze, nachdem der gerade ausgeführte Befehl beendet ist. Das Feld `cache_enabled` in `local_cache.health()` zeigt die Einstellung der aufrufenden SQL-Sitzung; es bestätigt nicht, dass alle Worker sie übernommen haben. Zum Abschalten der Cache-Abfragen ohne Neustart führen Sie `ALTER SYSTEM SET pg_local_cache.enabled = off;` und `SELECT pg_reload_conf();` aus; RESP liest dann direkt aus der Quelltabelle.

## redis-cli {#redis-cli}

```bash
export REDISCLI_AUTH=DemoRespToken_0123456789abcdef0123456789
redis-cli -2 -p 56379 MGET 'CRUD:pglc_demo.public.items:{"id":42}'
```

Die Antwort enthält Zeile 42 als JSON. Fehlende Zeilen geben `nil` zurück.
[redis-cli](https://redis.io/docs/latest/develop/tools/cli/) liest das Token
aus `REDISCLI_AUTH`.

## Node.js {#nodejs}

```bash
npm --prefix examples/node-postgres ci --ignore-scripts
npm --prefix examples/node-postgres run resp
```

Das Beispiel verwendet das offizielle Paket `@redis/client` und prüft
Reihenfolge, Duplikate, Null-Eingabepositionen und fehlende Zeilen. Der Helper
lässt Null-Schlüssel auf der Leitung aus und stellt ihre Positionen nach dem
Dekodieren wieder her. Für die Verbindung aus einer Anwendung:

```js
import { createClient } from '@redis/client';

const client = createClient({
  url: 'redis://127.0.0.1:56379',
  password: process.env.PGLC_RESP_TOKEN,
  RESP: 2,
  disableClientInfo: true,
});
client.on('error', console.error);
await client.connect();

try {
  const values = await client.mGet(['CRUD:pglc_demo.public.items:{"id":42}']);
  const rows = values.map(value => value === null ? null : JSON.parse(value));
  console.log(rows);
} finally {
  await client.close();
}
```

Setzen Sie `PGLC_RESP_TOKEN` auf das Server-Token. Halten Sie die Verbindung
über mehrere Anfragen offen. Diese [Client-Einstellungen](https://github.com/redis/node-redis/blob/master/docs/client-configuration.md)
wählen RESP2 und überspringen Redis-spezifische Metadatenbefehle. Verwenden Sie
Token-Authentifizierung ohne Benutzernamen oder Redis-Datenbanknummer.

## Mit SQL vergleichen {#compare-with-sql}

Der [gemeinsame Benchmark](BENCHMARKS.md#run-the-same-comparison-on-every-client) führt RESP `MGET` und vorbereitetes SQL mit denselben Schlüsseln und dekodierten Ergebnissen in Node.js und Go aus. Veröffentlichte SQL-`mget`-Messungen aus 2.x sind historische Werte; dieser Benchmark-Pfad wurde in 3.0.0 entfernt. Für breiteres Anwendungs-Caching lesen Sie den [Leitfaden zu PostgreSQL und Redis Cache-aside](postgresql-redis-cache.md).

## Demo stoppen {#stop-the-demo}

```bash
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml down
```
