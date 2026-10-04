---
layout: doc
lang: de
translation_key: TECHNICAL
title: Technische Referenz für pg_local_cache
seo_title: pg_local_cache SQL-API, Konsistenz, Speicher und RESP2
description: "Technische Referenz zu pg_local_cache: RESP-MGET, transaktionsbewusste Invalidierung, begrenzter PostgreSQL-Shared-Memory, Monitoring und RESP2."
section: Technik
permalink: /de/docs/TECHNICAL.html
---

# Technische Referenz für pg_local_cache {#pg_local_cache-technical-reference}

`pg_local_cache` speichert vollständige Zeilen anhand des vollständigen Primärschlüssels im begrenzten PostgreSQL-Shared-Memory. Der RESP2-Endpunkt stellt `MGET`, `SET` und `DEL` bereit.

> **Gewöhnliches SQL bleibt gewöhnlich:** Die Erweiterung installiert keine
> Planner- oder Executor-Hooks. Ein normales `SELECT` verwendet immer PostgreSQL
> und liest niemals diesen Cache.

## Unterstützte Tabellen und Schlüssel {#supported-tables-and-keys}

Quelltabellen müssen dauerhafte Heap-Tabellen mit einem gültigen Primärschlüssel
sein und dürfen weder RLS, Partitionierung, Vererbung noch Erweiterungsbesitz
haben.

Unterstützte Schlüsseltypen:

- `smallint`, `integer` und `bigint`;
- `text`, `varchar` und `char` mit deterministischen Kollationen;
- `uuid`;
- zusammengesetzte Primärschlüssel, die ausschließlich aus diesen Typen bestehen.

Nicht unterstützte Relationen werden beim Anhängen abgelehnt, statt eine unsichere
unvollständige Zuordnung zu erzeugen.

## Tabellen anhängen, abgleichen und trennen {#attach-reconcile-and-detach-tables}

`local_cache.attach_table(regclass)` führt eine einmalig abgesicherte
Einrichtungssequenz aus:

1. Relation sperren und validieren;
2. Namespace, Relations-OID und geordnete Primärschlüsselspalten aufzeichnen;
3. von der Erweiterung verwaltete Statement-, Row- und Truncate-Trigger installieren;
4. Worker-Zuordnungen neu laden.

DDL-Event-Trigger invalidieren gecachte Zuordnungsmetadaten. Führen Sie nach
beabsichtigten Schemaänderungen `local_cache.reconcile_table(...)` oder
`local_cache.reconcile_all()` aus. `local_cache.detach_table(...)` entfernt die
Zuordnung und ihre Trigger.

## Lesepfad und sicherer Fallback {#read-path-and-safe-fallback}

Bei aktiviertem Cache wird für jeden RESP-`MGET`-Schlüssel zuerst der gemeinsame
Cache geprüft. Bei jedem Cache-Miss liest der Worker die Quellzeile in einer
eigenen kurzen Transaktion. Zeilen über dem Payload-Limit werden weiterhin von
PostgreSQL zurückgegeben, aber nicht gecacht.

## Transaktionskonsistenz {#transaction-consistency}

Trigger für Schreibvorgänge an zugeordneten Tabellen veröffentlichen in jeder
PostgreSQL-Sitzung vor Commit-Sichtbarkeit Dirty-Writer-Sperren pro Schlüssel
oder Relation und erhöhen Generationen. Betroffene Cache-Einträge werden bis zum
Ende des Schreibvorgangs umgangen; Generationsprüfungen verhindern die
Veröffentlichung veralteter laufender Lesevorgänge. Daher kann nach einem Commit
kein veralteter Cache-Treffer folgen.

RESP-Lesezugriffe verwenden `pg_local_cache.role`, nicht die PostgreSQL-Rolle des
Clients, und laufen in unabhängigen kurzen Transaktionen. Sie sehen keine
uncommitteten Client-Änderungen, teilen nicht dessen Snapshot und sind nicht Teil
seiner Transaktion. `pg_local_cache.enabled = off` umgeht den Cache.

## Shared Memory und Konfiguration {#shared-memory-and-configuration}

Cache-Einträge, Relationszustände, Zähler, Worker-Generationen und RESP-
Client-Slots werden beim Start des Postmasters allokiert. Die Kapazität ist
begrenzt. Die Verdrängung prüft eine begrenzte rotierende Menge und bevorzugt
veraltete Einträge; wenn die Aufnahme scheitert, geht der Vorgang zur Quelltabelle
zurück, statt unbegrenzt Speicher zu allokieren.

| Einstellung | Standard | Bedeutung |
|---|---:|---|
| `pg_local_cache.database` | `postgres` | Von der Erweiterung bediente Datenbank |
| `pg_local_cache.cache_entries` | `16384` | Gemeinsame Zeilenkapazität |
| `pg_local_cache.relation_states` | `1024` | Kapazität des gemeinsamen Zuordnungszustands |
| `pg_local_cache.memory_budget_mb` | `384` | Erweiterungsbudget beim Start |
| `pg_local_cache.port` | `6380` | RESP-Port; `0` nur für Regressionstests und Diagnose, keine Lesezugriffe |
| `pg_local_cache.bind_address` | `127.0.0.1` | RESP-Bind-Adresse |
| `pg_local_cache.workers` | `4` | RESP-Worker |
| `pg_local_cache.role` | `local_cache_worker` | PostgreSQL-Rolle für RESP |
| `pg_local_cache.max_clients` | `256` | Globales RESP-Client-Limit |
| `pg_local_cache.max_clients_per_worker` | `64` | Slots pro Worker |
| `pg_local_cache.idle_timeout_ms` | `300000` | Frist für Inaktivität und langsame Clients |
| `pg_local_cache.statement_timeout_ms` | `2000` | Statement-Frist des Workers |
| `pg_local_cache.lock_timeout_ms` | `250` | Sperrfrist des Workers |
| `pg_local_cache.singleflight_wait_ms` | `25` | Wartezeit eines Followers auf denselben Schlüssel |
| `pg_local_cache.max_pipeline_commands` | `256` | Befehle pro Event-Loop-Durchlauf |
| `pg_local_cache.max_dirty_keys` | `4096` | Begrenzung der Schlüssel-Sperren pro Transaktion |
| `pg_local_cache.auth_token_file` | leer | Bevorzugtes RESP-Zugangsdokument |
| `pg_local_cache.auth_token` | leer | Token inline, nur für Entwicklung |
| `pg_local_cache.enabled` | `on` | SIGHUP-Notausschalter für den Cache; bei `off` liest RESP direkt aus der Quelltabelle |
| `pg_local_cache.tls` | `off` | TLS für den RESP-Listener aktivieren; PostgreSQL muss mit OpenSSL gebaut sein |
| `pg_local_cache.tls_cert_file` | leer | PEM-Serverzertifikat/-kette; bei aktiviertem TLS erforderlich |
| `pg_local_cache.tls_key_file` | leer | PEM-Server-Privatschlüssel; bei aktiviertem TLS erforderlich |
| `pg_local_cache.tls_ca_file` | leer | Vertrauenswürdige Client-CA; aktiviert mTLS |
| `pg_local_cache.tls_min_protocol_version` | `TLSv1.2` | Minimale TLS-Version (`TLSv1.2` oder `TLSv1.3`) |
| `pg_local_cache.allow_plaintext_network` | `off` | Postmaster-Opt-in für Klartext-Listener außerhalb von IPv4-Loopback |
| `pg_local_cache.allow_superuser` | `off` | Rollenüberschreibung, nur für Entwicklung |

Dies sind Postmaster-Einstellungen. Dimensionieren Sie sie vor dem Neustart.
Der [Installationsleitfaden](INSTALL_EXISTING.md) beschreibt Pakete und Neustarts.

## RESP2-Endpunkt {#optional-resp2-endpoint}

RESP2 verwendet dieselben Zuordnungen und denselben Shared Cache. Wire-Schlüssel
verwenden diese Form:

```text
CRUD:database.schema.table:{"pk_column":<json-scalar>,...}
```

Unterstützte Befehle sind authentifiziertes und begrenztes `MGET`, `SET`, `DEL`
und bereichsbezogene Invalidation. RESP-Worker verwenden eine konfigurierte
PostgreSQL-Rolle; sie übernehmen nicht die Datenbank-ACLs der einzelnen
Netzwerkclients.

TLS für RESP nutzt eigene `pg_local_cache.tls_*`-Einstellungen und ist
unabhängig von PostgreSQL-`ssl_*`-Einstellungen. PostgreSQL-TLS auf dem SQL-Port
schützt RESP nicht; RESP-TLS ändert den SQL-Listener nicht. Aktivieren Sie
`pg_local_cache.tls` und konfigurieren Sie Serverzertifikat und Schlüssel.
`pg_local_cache.tls_ca_file` prüft Clientzertifikate und aktiviert mTLS. Die
minimale Protokollversion ist standardmäßig `TLSv1.2` und kann auf `TLSv1.3`
erhöht werden. Es gelten die OpenSSL-Systemvorgaben für Cipher. Für den privaten
Schlüssel gilt [PostgreSQLs Regel für
Server-Schlüsseldateien](https://www.postgresql.org/docs/current/ssl-tcp.html).
Außerhalb von Loopback wird TLS empfohlen. Ist TLS ausgeschaltet, erfordert
Klartext auf einem Nicht-Loopback-Listener das explizite Opt-in
`pg_local_cache.allow_plaintext_network = on`, beschränkt auf vertrauenswürdige
Netzwerke. Ein Nicht-Loopback-Listener benötigt weiterhin ein Token mit
mindestens 32 Byte; bevorzugen Sie eine Token-Datei mit eingeschränkten Rechten
statt eines Inline-Tokens.

Der Betriebsparameter `pg_local_cache.enabled` ist ein SIGHUP-Parameter und dient als operativer Notausschalter für den Cache-Dienst. Zum Deaktivieren:

```sql
ALTER SYSTEM SET pg_local_cache.enabled = off;
SELECT pg_reload_conf();
```

Jeder RESP-Worker übernimmt den Reload asynchron an seiner nächsten Befehlsgrenze, nachdem der gerade ausgeführte Befehl beendet ist. Das Feld `cache_enabled` in `local_cache.health()` zeigt die Einstellung der aufrufenden SQL-Sitzung; es bestätigt nicht, dass alle Worker sie übernommen haben. Zum erneuten Aktivieren:

```sql
ALTER SYSTEM SET pg_local_cache.enabled = on;
SELECT pg_reload_conf();
```

## Gesundheit und Monitoring {#health-and-monitoring}

`local_cache.health()` meldet Bereitschaft und Konvergenz der Zuordnungen.
`local_cache.stats()` gibt JSON-Zähler zurück. `local_cache.metrics()` stellt die
typisierte Metrikzeile für den Exporter bereit.

Die RESP-Zähler von `stats()` und `metrics()` umfassen:

- `sql_gets`
- `sql_meta`
- `sql_sets`
- `sql_dels`
- `sql_result_reuses`
- `tls_handshakes_total`
- `tls_handshake_failures_total`

Datenbanklesevorgänge, Invalidationen, abgelehnte Aufnahmen, Dirty-Key-Fallback,
Singleflight-, Worker- und RESP-Zähler bleiben getrennt.

Als Nächstes verwenden Sie den [Installationsleitfaden](INSTALL_EXISTING.md) für
Debian- und RPM-Paketprüfung, PGXS-Quellcode-Builds, Konfiguration, Neustarts,
Upgrades und Deinstallation.
