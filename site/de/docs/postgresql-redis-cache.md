---
layout: doc
lang: de
translation_key: postgresql-redis-cache
title: Cache-aside mit PostgreSQL und Redis
seo_title: "PostgreSQL und Redis mit Cache-aside: Invalidierung und Race Conditions"
description: Vergleichen Sie anwendungsverwaltetes Redis-Cache-aside mit RESP-Lesevorgängen von pg_local_cache, einschließlich veralteter Cache-Befüllungen und Schreibinvalidierung.
section: Leitfäden
permalink: /de/docs/postgresql-redis-cache.html
last_modified_at: "2026-10-04"
---

# Cache-aside mit PostgreSQL und Redis {#postgresql-and-redis-cache-aside}

Dieser Leitfaden erklärt Redis-Cache-aside mit PostgreSQL als maßgeblicher Datenquelle, die Race Condition durch veraltete Cache-Befüllungen und die Invalidierung angehängter Zeilen in `pg_local_cache`.

Bei einem Redis-Fehltreffer liest die Anwendung eine Zeile aus PostgreSQL, gibt sie zurück und speichert sie unter einem Anwendungsschlüssel. Bei einem Schreibvorgang bestätigt sie zunächst die PostgreSQL-Daten und löscht dann den Redis-Schlüssel. Eine TTL begrenzt die Aufbewahrungsdauer, beweist aber nicht, dass der Wert aktuell ist. Siehe den [Redis-Leitfaden zu Cache-aside](https://redis.io/docs/latest/develop/use-cases/cache-aside/).

## Die Race Condition bei der Invalidierung {#the-invalidation-race}

Ein Leser kann eine alte PostgreSQL-Zeile laden, pausieren und sie erst speichern, nachdem ein Schreiber den Commit ausgeführt und den Schlüssel gelöscht hat. Der nächste Leser erhält veraltete Daten, bis der Eintrag abläuft oder erneut gelöscht wird.

Zu den Gegenmaßnahmen gehören, Befüllungen mit einer veralteten Datenbankversion abzulehnen, Ladevorgänge pro Schlüssel zu serialisieren oder bestätigte Änderungen über einen Outbox- oder CDC-Consumer zu veröffentlichen. Jede Maßnahme erfordert zusätzliche Koordination. Der [Leitfaden zur transaktionsbewussten Invalidierung](cache-invalidation.md) beschreibt die entsprechende Grenze für verspätete Befüllungen innerhalb von PostgreSQL.

## Wo pg_local_cache passt {#where-pg_local_cache-fits}

`pg_local_cache` speichert vollständige Zeilen anhand des Primärschlüssels im begrenzten Shared Memory von PostgreSQL. Anwendungen fordern Zeilen mit authentifiziertem RESP2-`MGET` an; gewöhnliches SQL und beliebige Abfrageergebnisse verwenden diesen Cache nicht. Trigger angehängter Tabellen sichern Schreibvorgänge mit Fences ab; wenn Eignungsprüfungen einen Cache-Treffer ausschließen, verwenden Lesevorgänge PostgreSQL.

Anders als Redis-Cache-aside nutzt dieser Weg den Datenbank-Schreibpfad für die Invalidierung und verwendet weder TTLs noch allgemeine Redis-Datenstrukturen. RESP-Worker verwenden die konfigurierte PostgreSQL-Rolle in unabhängigen kurzen Transaktionen. Verbindungs- und Sicherheitsdetails finden Sie unter [RESP-Clients](resp.md) und in der [technischen Referenz](TECHNICAL.md).

Verwenden Sie Redis für gemeinsam genutzte Anwendungsobjekte, TTL-gesteuerte Aktualität oder Redis-Datenstrukturen. Verwenden Sie `pg_local_cache` für wiederholte Lesevorgänge vollständiger Zeilen aus einer PostgreSQL-Datenbank. Wenn Sie beide Ebenen kombinieren, brauchen Sie getrennte Schlüssel, Invalidierung und Überwachung.
