---
layout: post
lang: de
translation_key: blog-cache-aside-late-fill
title: "PostgreSQL-Cache-Invalidation: die Race Condition beim späten Fill"
description: Ein Cache-aside-Rennen nachvollziehen, bei dem ein alter Lesevorgang nach dem Commit einen gelöschten Schlüssel erneut füllt. Veröffentlichungs-Sperren, Snapshots, Rollback und anfragebezogene Caches verstehen.
permalink: /de/blog/cache-aside-late-fill/
date: "2026-09-22"
last_modified_at: "2026-09-22"
topic: correctness
---

# Cache-Invalidation und die Race Condition beim späten Fill {#cache-invalidation-and-the-late-fill-race}

„Den Cache-Schlüssel nach dem Datenbank-Update löschen“ lässt ein Timingproblem
offen: Eine andere Anfrage lädt möglicherweise bereits den alten Wert. Das
Löschen entfernt den jetzt vorhandenen Eintrag; es bricht kein Ergebnis ab, das
noch unterwegs ist.

## Die zwei Anfragen verfolgen {#two-requests}

Nehmen wir an, PostgreSQL hält Revision 0 und eine Anwendung verwendet
Cache-aside-Lesevorgänge. Diese Sequenz kann auch dann auftreten, wenn der
Schreiber nach dem Commit invalidiert:

| Schritt | Leser A | Schreiber B |
|---|---|---|
| 1 | Findet den Cache nicht und liest Revision 0 | |
| 2 | Pausiert vor dem Speichern des Ergebnisses | Aktualisiert die Zeile auf Revision 1 |
| 3 | | Führt Commit aus und löscht den Cache-Schlüssel |
| 4 | Veröffentlicht seine zuvor gelesene Revision 0 | |
| 5 | Eine spätere Anfrage liest den veralteten Cache-Wert | |

Eine TTL kann begrenzen, wie lange dieser Wert verwendet werden darf. Sie macht
Schritt 4 jedoch nicht korrekt. Das Löschen vor dem Commit erzeugt ein anderes
Zeitfenster, in dem ein Leser den alten bestätigten Datenbankzustand erneut
eintragen kann. Siehe den [Leitfaden zu PostgreSQL und Redis](../docs/postgresql-redis-cache.md)
für die Grenze zwischen Cache-aside auf Anwendungsebene und lokalem
Datenbank-Zeilen-Cache.

## Veröffentlichung ebenso wie Lookup validieren {#publication}

Ein Cache-Fill benötigt den Nachweis, dass sein Ergebnis bei der Veröffentlichung
weiterhin geeignet ist. In `pg_local_cache` 2.0 setzen Schreibvorgänge Sperren
für betroffene Schlüssel oder Relationen; Fills tragen Generationsinformationen,
und positive Cache-Einträge tragen Tupel-Sichtbarkeitsinformationen. Eine
geänderte Generation kann einen alten Fill ablehnen. Ein Lesevorgang, der einen
Eintrag nicht sicher verwenden kann, fällt auf PostgreSQL zurück.

Diese Prüfungen gehören zu einem bestimmten PostgreSQL-Lesepfad. Sie machen den
optionalen RESP-Endpunkt weder zu einem allgemeinen Redis-Server, noch
invalidieren sie Werte, die eine Anwendung bereits an anderer Stelle kopiert hat.

## Rollback und Commit testen {#test-transactions}

Verwenden Sie den [Test mit zwei Sitzungen](../docs/cache-invalidation.md#test-with-two-sessions)
in einer verworfenen Datenbank. Wärmen Sie die Zeile in einer Sitzung auf. In
einer anderen aktualisieren Sie die Zeile und lassen die Transaktion offen.
Prüfen Sie drei Beobachtungen:

1. Der Schreiber kann seine eigene Änderung über den Quelltabellenpfad lesen.
2. Die andere Sitzung sieht weiterhin den bestätigten Wert, während der
   Schreibvorgang offen ist.
3. Nach Rollback bleibt der ursprüngliche Wert erhalten; nach Commit sieht eine
   neue Anweisung unter `READ COMMITTED` die neue Revision.

Die dritte Bedingung bezieht sich auf eine neue Anweisung. Eine früher gestartete
Anweisung muss während ihrer Ausführung keinen neueren Snapshot übernehmen. Der
[Transaktionsvertrag](../docs/TECHNICAL.md#transaction-consistency) beschreibt
auch die Modi, die den Cache umgehen. Verwenden Sie gewöhnliches SQL für
Zeilensperren.

## Den nächsten Cache in der Anwendung prüfen {#application-cache}

Ein anfragebezogener DataLoader kann weiterhin einen Wert halten, den er vor
einer Mutation geladen hat. Datenbank-Invalidation kann dieses JavaScript-Objekt
nicht entfernen. Löschen oder ersetzen Sie den betroffenen Loader-Eintrag nach
einer Mutation gemäß Autorisierung und Ergebnisvertrag der Anwendung. Halten Sie
Loader auf Anfragen begrenzt.

Der [Batching-Leitfaden](../docs/batch-primary-key-lookups.md#graphql-dataloader-and-n1-reads)
trennt Memoization der Anfrage vom gemeinsamen Zeilen-Cache. Wenn Sie eine
veraltete Antwort untersuchen, verfolgen Sie jeden Speicherpunkt vom
Datenbank-Snapshot bis zum Antwortobjekt. Korrektheit an einer Grenze leert die
anderen nicht.
