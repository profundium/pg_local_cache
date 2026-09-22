---
layout: doc
lang: de
translation_key: resp
title: Über RESP verbinden
description: Lesen Sie PostgreSQL-Zeilen über RESP2 mit redis-cli oder Node.js. Enthält Authentifizierung, Client-Einstellungen, ausführbare Beispiele und Aufräumen.
section: RESP
permalink: /de/docs/resp.html
last_modified_at: "2026-09-16"
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

## redis-cli {#redis-cli}

```bash
export REDISCLI_AUTH=DemoRespToken_0123456789abcdef0123456789
redis-cli -2 -p 56379 MGET 'CRUD:pglc_demo.public.items:{"id":42}'
```

Die Antwort enthält Zeile 42 als JSON. Fehlende Zeilen geben `nil` zurück.
[redis-cli](https://redis.io/docs/latest/develop/tools/cli/) liest das Token aus
`REDISCLI_AUTH`.

## Node.js {#nodejs}

```bash
npm --prefix examples/node-postgres ci --ignore-scripts
npm --prefix examples/node-postgres run resp
```

Das Beispiel verwendet das offizielle Paket `@redis/client` und prüft Reihenfolge,
Duplikate, Null-Eingabepositionen und fehlende Zeilen. Sein Helfer lässt
Null-Schlüssel auf der Leitung weg und stellt ihre Positionen nach dem Dekodieren
wieder her.
Für die Verbindung aus Ihrer Anwendung:

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

Setzen Sie `PGLC_RESP_TOKEN` auf das Token Ihres Servers. Halten Sie diese
Verbindung über mehrere Anfragen offen. Diese [Client-Einstellungen](https://github.com/redis/node-redis/blob/master/docs/client-configuration.md)
wählen RESP2 und überspringen Redis-spezifische Client-Metadatenbefehle.
Verwenden Sie ausschließlich Token-Authentifizierung, ohne Benutzernamen oder
Redis-Datenbanknummer.

RESP-Worker verwenden für alle Clients eine konfigurierte PostgreSQL-Rolle. Für
Lesevorgänge innerhalb einer SQL-Transaktion verwenden Sie [Node.js-SQL](node-postgres.md)
oder [Go-SQL](go.md). Die [RESP-Referenz](TECHNICAL.md#optional-resp2-endpoint)
beschreibt Befehle und Limits.

## Mit SQL vergleichen {#compare-with-sql}

Der [gemeinsame Benchmark](BENCHMARKS.md#run-the-same-comparison-on-every-client)
führt RESP `MGET`, SQL `mget` und vorbereitetes SQL mit denselben Schlüsseln und
dekodierten Ergebnissen sowohl in Node.js als auch in Go aus.
Für breiteres Anwendungs-Caching lesen Sie den [Leitfaden zu PostgreSQL und Redis Cache-aside](postgresql-redis-cache.md).

## Demo stoppen {#stop-the-demo}

```bash
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml down
```
