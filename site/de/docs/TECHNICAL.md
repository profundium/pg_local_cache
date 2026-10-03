---
layout: doc
lang: de
translation_key: TECHNICAL
title: Technische Referenz für pg_local_cache
seo_title: pg_local_cache SQL-API, Konsistenz, Speicher und RESP2
description: Technische Referenz für SQL-mget, transaktionsbewusste Invalidation, begrenzten PostgreSQL-Shared-Memory, Monitoring und optionales RESP2 von pg_local_cache.
section: Technik
permalink: /de/docs/TECHNICAL.html
---

# Technische Referenz für pg_local_cache {#pg_local_cache-technical-reference}

`pg_local_cache` cached vollständige Zeilen per vollständigem Primärschlüssel im
begrenzten PostgreSQL-Shared-Memory. Es stellt eine explizite SQL-Funktion
`local_cache.mget` und einen optionalen RESP2-Endpunkt bereit.

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

## SQL-mget-API {#sql-mget-api}

Signatur:

```sql
local_cache.mget(relation regclass, key_values anyarray) RETURNS text[]
```

Einspaltige Schlüssel verwenden ihren nativen Array-Typ. Zusammengesetzte
Schlüssel verwenden rechteckige `text[][]`-Arrays, mit einem Schlüssel pro Zeile
und einer Komponente pro Primärschlüsselspalte.

Vertrag:

- höchstens 1.024 Schlüssel pro Aufruf;
- Eingabereihenfolge und Duplikate bleiben erhalten;
- Eingabe-`NULL` und fehlende Zeilen erzeugen ausgerichtete `NULL`-Ergebnisse;
- Komponenten zusammengesetzter Schlüssel dürfen nicht `NULL` sein;
- jede Komponente wird von der PostgreSQL-Typ-Eingabefunktion geparst;
- der vollständige zusammengesetzte Batch wird vor der ersten Abfrage validiert;
- Aufrufer benötigen `SELECT` auf der Quelltabelle;
- die Funktion ist `SECURITY INVOKER`.

Eine vorbereitete Quellabfrage wird pro Funktionsinstanz, Benutzer, Relation und
Zuordnungsgeneration gecached.

## Lesepfad und sicherer Fallback {#read-path-and-safe-fallback}

Jeder angeforderte Schlüssel folgt demselben Pfad:

1. vollständigen Primärschlüssel kanonisieren;
2. den Shared Cache nur in einer sauberen `READ COMMITTED`-Transaktion auf dem
   beschreibbaren primären Server verwenden;
3. Payload-Prüfsumme, Zeilendeskriptor, Quell-`xmin` und Snapshot-Sichtbarkeit
   validieren;
4. andernfalls die indizierte Quelltabellenabfrage über SPI ausführen;
5. einen positiven oder negativen Eintrag erst nach einem Beweis des neuesten
   Snapshots veröffentlichen.

`REPEATABLE READ`, `SERIALIZABLE`, Recovery, parallele Ausführung und eine
Transaktion, die zugeordnete Daten geschrieben hat, umgehen den Cache. Zeilen,
die größer als das Payload-Limit des Caches sind, werden weiterhin aus
PostgreSQL zurückgegeben, aber nicht gecached.

## Transaktionskonsistenz {#transaction-consistency}

Bevor eine zugeordnete Änderung committen kann, setzen Trigger eine Sperre für
den betroffenen Schlüssel oder die Relation. Ein Cache-Fill trägt Generationen
für Zuordnung, globalen Zustand, Relation, Schlüssel und Loader; dadurch kann
ein veralteter Loader nach einer Invalidation oder Verdrängung nichts mehr
veröffentlichen.

Positive Einträge speichern `xmin` des Quell-Tupels und einen
FullXID-Beobachtungshorizont. Snapshot-ungeeignete Einträge fallen auf
PostgreSQL zurück. Negative Einträge sind für einen älteren aktiven Snapshot
niemals maßgeblich.

Rollback entfernt transaktionslokale Dirty-Zustände, ohne neue Daten zu
veröffentlichen. Read-your-writes kommt daher aus PostgreSQL und nicht aus
spekulativem Cache-Inhalt.

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
| `pg_local_cache.port` | `6380` | RESP-Port; `0` deaktiviert RESP |
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
| `pg_local_cache.allow_superuser` | `off` | Rollenüberschreibung, nur für Entwicklung |

Dies sind Postmaster-Einstellungen. Dimensionieren Sie sie vor dem Neustart; die
Vorprüfung des Binärinstallers prüft den kombinierten Plan.

## Optionaler RESP2-Endpunkt {#optional-resp2-endpoint}

RESP2 verwendet dieselben Zuordnungen und denselben Shared Cache. Wire-Schlüssel
verwenden diese Form:

```text
CRUD:database.schema.table:{"pk_column":<json-scalar>,...}
```

Unterstützte Befehle sind authentifiziertes und begrenztes `MGET`, `SET`, `DEL`
und bereichsbezogene Invalidation. RESP-Worker verwenden eine konfigurierte
PostgreSQL-Rolle; sie übernehmen nicht die Datenbank-ACLs der einzelnen
Netzwerkclients.

Der Endpunkt hat kein TLS. Binden Sie ihn an Loopback oder setzen Sie ihn hinter
einen authentifizierten TLS-Proxy. Bevorzugen Sie eine Token-Datei mit restriktiven
Dateirechten gegenüber einem Token inline.

## Gesundheit und Monitoring {#health-and-monitoring}

`local_cache.health()` meldet Bereitschaft und Konvergenz der Zuordnungen.
`local_cache.stats()` gibt JSON-Zähler zurück. `local_cache.metrics()` stellt die
typisierte Metrikzeile für den Exporter bereit.

SQL-Cache-Zähler beschreiben nur explizite `mget`-Aufrufe:

- `sql_cache_hits`
- `sql_cache_misses`
- `sql_cache_fills`
- `sql_cache_bypasses`

Datenbanklesevorgänge, Invalidationen, abgelehnte Aufnahmen, Dirty-Key-Fallback,
Singleflight-, Worker- und RESP-Zähler bleiben getrennt.

Als Nächstes verwenden Sie den [Installationsleitfaden](INSTALL_EXISTING.md) für
verifizierte Binärdateien, PGXS-Quellcode-Builds, kontrollierte Neustarts,
Prüfung und Wiederherstellung.
