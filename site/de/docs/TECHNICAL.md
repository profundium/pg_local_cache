---
layout: doc
lang: de
translation_key: TECHNICAL
title: Technische Referenz für pg_local_cache
seo_title: pg_local_cache RESP-API, Konsistenz, Speicher und Konfiguration
description: Referenz zu RESP-Lesevorgängen, unterstützten Tabellen, Transaktionssperren, TLS, Shared Memory, Metriken und PostgreSQL-Einstellungen.
section: Technik
permalink: /de/docs/TECHNICAL.html
---

# Technische Referenz für pg_local_cache {#pg_local_cache-technical-reference}

Technische Referenz zum RESP2-Endpunkt, zur Cache-Konsistenz, zu Ressourcenlimits und zur Sicherheit. Einrichtungsschritte finden Sie im [Schnellstart](QUICKSTART.md) und in der [Installation](INSTALL_EXISTING.md).

## Unterstützte Tabellen und Schlüssel {#supported-tables-and-keys}

Hängen Sie dauerhafte Heap-Tabellen mit einem gültigen Primärschlüssel an. Nicht unterstützt werden partitionierte, vererbte, durch Zeilensicherheit (RLS) geschützte, temporäre und Fremdtabellen sowie Tabellen im Besitz einer Erweiterung. Unterstützte Primärschlüsseltypen sind `smallint`, `integer`, `bigint`, `text`, `varchar`, `char` mit deterministischen Kollationen sowie `uuid`; zusammengesetzte Schlüssel dürfen diese Typen verwenden und höchstens 16 Spalten umfassen.

Schemaänderungen erfordern einen Abgleich der Zuordnungen. Siehe [Installation](INSTALL_EXISTING.md#attach-a-table).

## Leseweg und sicherer Fallback {#read-path-and-safe-fallback}

![RESP-MGET-Leseweg: Cache-Treffer, abgesicherte Befüllung aus der Quelle und Umgehung per Notausschalter.](../../docs/diagrams/read-path.svg)

Jeder RESP-`MGET`-Schlüssel wird vor der Suche validiert und kanonisiert. Ein zulässiger Treffer liefert die vollständige Zeile als JSON. Bei einem Fehltreffer liest der Worker die Quelltabelle in einer kurzen Transaktion und veröffentlicht einen Cache-Eintrag nur, wenn sein Lesefence noch aktuell ist. Fehlende Zeilen liefern `nil`. Zeilen, deren Payload nicht in den gemeinsamen Cache passt, können trotzdem aus PostgreSQL zurückgegeben werden, sofern ihr JSON innerhalb des RESP-Wertlimits liegt.

## Transaktionskonsistenz {#transaction-consistency}

![Schreibinvalidierung: Fences vor dem Commit schützen bestätigte Schreibvorgänge; ein Rollback vor Veröffentlichung des Fence erhält vorherige Einträge.](../../docs/diagrams/write-invalidation.svg)

Zeilen- und Statement-Trigger an zugeordneten Tabellen sammeln geänderte Schlüssel oder eine betroffene Relation im transaktionslokalen Zustand. Der Pre-Commit-Callback veröffentlicht Invalidation-Fences und erhöht Generationen. Eine Befüllung, die vor dem Fence begonnen hat, kann keine veralteten Daten veröffentlichen. Ein Rollback vor Veröffentlichung des Fence verwirft den Änderungszustand; vorherige Cache-Einträge bleiben gültig. Wird die Transaktion nach der Veröffentlichung abgebrochen, bleibt die Invalidierung bestehen und betroffene Einträge bleiben ungültig.

RESP-Lesevorgänge verwenden `pg_local_cache.role` in unabhängigen kurzen Transaktionen. Sie teilen weder die SQL-Rolle noch Transaktion, uncommittete Änderungen oder Snapshot eines Clients.

## Speicherbedarf und Einstellungen {#shared-memory-and-configuration}

Die Erweiterung allokiert beim PostgreSQL-Start begrenzten gemeinsamen Cache-, Zuordnungs- und Worker-/Client-Zustand vorab. `memory_budget_mb` begrenzt die deterministische Speicherallokation der Erweiterung. Fehlgeschlagene Aufnahmen und Verdrängungen überschreiten die konfigurierte Kapazität nicht; Lesevorgänge greifen auf PostgreSQL zurück.

| Einstellung | Standard | Bereich | Neuladen |
|---|---:|---|---|
| `pg_local_cache.enabled` | `on` | `on` / `off` | SIGHUP |
| `pg_local_cache.allow_plaintext_network` | `off` | `on` / `off` | Neustart |
| `pg_local_cache.tls` | `off` | `on` / `off` | Neustart |
| `pg_local_cache.tls_cert_file` | leer | PEM-Dateipfad | Neustart |
| `pg_local_cache.tls_key_file` | leer | PEM-Dateipfad | Neustart |
| `pg_local_cache.tls_ca_file` | leer | CA-PEM-Dateipfad | Neustart |
| `pg_local_cache.tls_min_protocol_version` | `TLSv1.2` | `TLSv1.2` / `TLSv1.3` | Neustart |
| `pg_local_cache.port` | `6380` | `0`–`65535`; `0` deaktiviert RESP | Neustart |
| `pg_local_cache.workers` | `4` | `1`–`32` | Neustart |
| `pg_local_cache.cache_entries` | `16384` | `128`–`65536` | Neustart |
| `pg_local_cache.relation_states` | `1024` | `128`–`8192` | Neustart |
| `pg_local_cache.max_clients` | `256` | `1`–`4096`; höchstens so viele wie Worker-Slots | Neustart |
| `pg_local_cache.max_clients_per_worker` | `64` | `1`–`128` | Neustart |
| `pg_local_cache.memory_budget_mb` | `384` | `64`–`8192` MB | Neustart |
| `pg_local_cache.idle_timeout_ms` | `300000` | `1000`–`86400000` | Neustart |
| `pg_local_cache.statement_timeout_ms` | `2000` | `100`–`60000` | Neustart |
| `pg_local_cache.lock_timeout_ms` | `250` | `10`–`60000` | Neustart |
| `pg_local_cache.singleflight_wait_ms` | `25` | `0`–`1000` | Neustart |
| `pg_local_cache.max_pipeline_commands` | `256` | `1`–`4096` | Neustart |
| `pg_local_cache.max_dirty_keys` | `4096` | `128`–`16384` | Neustart |
| `pg_local_cache.bind_address` | `127.0.0.1` | IPv4-Adresse | Neustart |
| `pg_local_cache.database` | `postgres` | Datenbankname | Neustart |
| `pg_local_cache.role` | `local_cache_worker` | PostgreSQL-LOGIN-Rolle | Neustart |
| `pg_local_cache.auth_token_file` | leer | Datei im Besitz des PostgreSQL-Betriebssystembenutzers, Modus `0400` oder `0600` | Neustart |
| `pg_local_cache.auth_token` | leer | Inline-Token; nur für Entwicklung | Neustart |
| `pg_local_cache.allow_superuser` | `off` | `on` / `off`; nur für Entwicklung | Neustart |

Alle Einstellungen außer `enabled` sind Postmaster-Einstellungen und erfordern einen Neustart. Für Client-Slots muss `max_clients <= workers × max_clients_per_worker` gelten.

## RESP2-Endpunkt {#optional-resp2-endpoint}

Der Endpunkt akzeptiert RESP2. Schlüssel verwenden das Format `CRUD:<db>.<schema>.<table>:<json pk>`. `MGET` erhält Reihenfolge und Duplikate der Anfrage; eine fehlende Zeile wird als `nil`-Element zurückgegeben. Pro Anfrage sind höchstens 1.024 Schlüssel zulässig, jede JSON-Zeile darf höchstens 65.536 Byte groß sein und die codierte Antwort höchstens 66.560 Byte.

Unterstützte Datenbefehle sind `MGET`, `SET` und `DEL`; `AUTH` ist erforderlich. Außerdem unterstützt der Endpunkt `PING`, `ECHO`, `INFO`, `STAT`/`STATS`, bereichsbezogenes `INVALIDATE`, `HELLO 2`, `QUIT`, `CLIENT SETINFO`/`SETNAME`/`GETNAME`/`ID`, `COMMAND` und `SELECT 0`. Nicht unterstützte Befehle liefern einen Fehler. RESP-Clients verwenden Datenbank 0; Datenbank- und Tabellenbereich stammen aus jedem Cache-Schlüssel.

## TLS- und Sicherheitsmodell {#security-model}

Der Listener bindet standardmäßig an IPv4-Loopback. Für RESP-TLS gelten erweiterungsspezifische Einstellungen, nicht PostgreSQL-`ssl_*`. Erforderlich sind ein PostgreSQL-Build mit OpenSSL, ein PEM-Serverzertifikat samt Schlüssel und ein Neustart. Wenn `tls_ca_file` gesetzt ist, werden Clientzertifikate zwingend geprüft (mTLS); die minimale TLS-Version ist standardmäßig 1.2.

Ist TLS ausgeschaltet, erfordert ein Klartext-Listener außerhalb von Loopback `allow_plaintext_network=on` und ein vertrauenswürdiges Netzwerk. Listener außerhalb von Loopback benötigen ein Token mit mindestens 32 Byte. Bevorzugen Sie eine Token-Datei mit eingeschränkten Rechten. Alle RESP-Clients teilen sich eine konfigurierte PostgreSQL-LOGIN-Rolle; PostgreSQL-Berechtigungen werden nicht für jeden Netzwerk-Client separat ausgewertet. Superuser-Worker sind standardmäßig deaktiviert und nur für Entwicklung vorgesehen.

## Cache-Notausschalter {#cache-kill-switch}

`pg_local_cache.enabled` ist ein SIGHUP-Notausschalter für den Cache. Ist er ausgeschaltet, umgehen RESP-Lesevorgänge den gemeinsamen Cache und lesen die Quelltabelle; `SET` und `DEL` schreiben weiterhin über PostgreSQL. Worker übernehmen Reloads asynchron an Befehlsgrenzen. `local_cache.health()` meldet die Einstellung der aufrufenden SQL-Sitzung, nicht die Bestätigung jedes Workers. Beim erneuten Aktivieren wird die Cache-Epoche erhöht, bevor Worker wieder Cache-Lesevorgänge ausführen.

## Metriken und Zustand {#health-and-monitoring}

`local_cache.health()` meldet Bereitschaft, Cache-Zustand und Konvergenz der Zuordnungen. `local_cache.stats()` liefert JSON-Zähler; `local_cache.metrics()` liefert die typisierte Exporter-Zeile.

Zu den Metriken gehören Cache-Treffer, Fehltreffer und negative Treffer; Lese- und Schreibvorgänge an der Quelle; Invalidierungen und Verdrängungen; Singleflight-Anführer, wartende Clients, Wiederverwendung und Timeouts; aktive und maximale Clientzahl; Ablehnungen wegen Verbindungslimits; Authentifizierungs- und Protokollfehler; Ausgabedrosselung und Abbrüche langsamer Clients; Worker-Starts; Fallbacks wegen geänderter Schlüssel; Fehler und Wiederholungen beim Neuladen von Zuordnungen; TLS-Handshakes und -Fehler. Gauges umfassen Eintrags- und Relationskapazitäten, Client- und Worker-Zahlen, Konvergenz der Zuordnungen, gemeinsamen/Worker-/geschätzten Speicher sowie das konfigurierte Budget.

Weiter geht es mit [Schnellstart](QUICKSTART.md), [Installation](INSTALL_EXISTING.md) und [Upgrade](UPGRADING.md).
