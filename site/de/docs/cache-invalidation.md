---
layout: doc
lang: de
translation_key: cache-invalidation
title: Transaktionsbewusste Cache-Invalidierung in PostgreSQL
seo_title: "PostgreSQL-Cache-Invalidierung: Commit und Rollback | pg_local_cache"
description: So sichern PostgreSQL-Trigger RESP-Zeilen-Lesevorgänge über Commit, Rollback und gleichzeitige Cache-Befüllungen hinweg ab.
section: Cache-Invalidierung
permalink: /de/docs/cache-invalidation.html
last_modified_at: "2026-10-04"
---

# Transaktionsbewusste Cache-Invalidierung in PostgreSQL {#transaction-aware-cache-invalidation-in-postgresql}

Dieser Leitfaden erklärt, wie Trigger an angehängten Tabellen verhindern, dass auf einen bestätigten PostgreSQL-Schreibvorgang ein veralteter RESP-Cache-Treffer folgt.

![Schreibinvalidierung: Bestätigte Aktualisierungen veröffentlichen einen Fence; ein Rollback vor Fence-Veröffentlichung lässt den Eintrag gültig.](../../docs/diagrams/write-invalidation.svg)

Ein Trigger erfasst geänderte Schlüssel oder eine geänderte Relation innerhalb der Schreibtransaktion. Beim Commit veröffentlicht die Erweiterung Invalidation-Fences und erhöht Generationen. Eine Befüllung, die vor dem Fence begonnen hat, kann keine veralteten Daten veröffentlichen. Ein Rollback vor der Fence-Veröffentlichung verwirft den Änderungszustand der Transaktion; vorherige Cache-Einträge bleiben gültig. Wird die Transaktion nach der Veröffentlichung abgebrochen, wird die Invalidierung nicht rückgängig gemacht und betroffene Einträge bleiben ungültig.

Den vollständigen Vertrag des Lesewegs finden Sie in der [technischen Referenz zur Konsistenz](TECHNICAL.md#transaction-consistency).

## Invalidierung über SQL und RESP prüfen {#test-with-two-sessions}

Starten Sie die [lokale Demo](QUICKSTART.md) und lesen Sie denselben Schlüssel über RESP und PostgreSQL:

```text
MGET CRUD:pglc_demo.public.items:{"id":42}
```

Aktualisieren und bestätigen Sie den Eintrag in einer separaten SQL-Sitzung:

```sql
BEGIN;
UPDATE public.items SET revision = revision + 1 WHERE id = 42;
COMMIT;
```

Der nächste RESP-Befehl liefert die bestätigte Revision. Wird die Schreibtransaktion stattdessen zurückgerollt, liefert RESP weiterhin die letzte bestätigte Revision.

RESP verwendet die konfigurierte PostgreSQL-Rolle in einer unabhängigen kurzen Transaktion. Rolle, Transaktion und Snapshot der Anwendung werden nicht geteilt. Verwenden Sie für Read-your-writes und `SELECT ... FOR UPDATE` SQL innerhalb der Anwendungstransaktion.

## Fälle, in denen der Cache umgangen wird {#cases-that-deliberately-bypass-the-cache}

Ist `pg_local_cache.enabled` deaktiviert, überspringt RESP `MGET` Cache-Suche und Befüllung und liest aus der Quelltabelle. Ein aktiver Dirty Fence für einen Schlüssel, eine Relation oder global blockiert ebenfalls Cache-Treffer und Befüllungen; die Lesevorgänge verwenden dann die Quelltabelle. RESP-Worker starten erst nach Ende der Recovery; Recovery ist daher keine eigene Umgehungsbedingung. Eine Zeile, die nicht in einen Cache-Eintrag passt, kann trotzdem aus PostgreSQL zurückgegeben werden, wenn ihr JSON innerhalb des RESP-Wertlimits liegt, wird aber nicht gespeichert.

## Ursache eines Fehltreffers prüfen {#inspect-the-cause-of-a-miss}

Vergleichen Sie `local_cache.stats()` und `local_cache.health()` vor und nach einer kontrollierten Arbeitslast. Prüfen Sie Zähler für Bypass, Fehltreffer, Invalidierung und das Neuladen von Zuordnungen. Siehe die [technische Metrikliste](TECHNICAL.md#health-and-monitoring).
