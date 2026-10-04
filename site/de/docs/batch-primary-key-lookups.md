---
layout: doc
lang: de
translation_key: batch-primary-key-lookups
title: Batch-Abfragen von PostgreSQL per Primärschlüssel
seo_title: "PostgreSQL-Zeilen gebündelt mit RESP MGET lesen"
description: "Erfahren Sie, wie Sie N+1-Lesezugriffe per Primärschlüssel vermeiden und vollständige Zeilen mit authentifiziertem RESP MGET gebündelt lesen."
section: Leitfäden
permalink: /de/docs/batch-primary-key-lookups.html
last_modified_at: "2026-10-04"
---

# Gebündelte PostgreSQL-Abfragen per Primärschlüssel {#batch-postgresql-primary-key-lookups}

Dieser Leitfaden zeigt, wie RESP-`MGET`-Batches einen Datenbankaufruf pro Schlüssel vermeiden und die Ergebnispositionen beibehalten.

## N+1-Lesezugriffe vermeiden {#graphql-dataloader-and-n1-reads}

Wenn eine Anwendung pro ID eine Zeile abruft, folgen auf die erste Abfrage N Lesezugriffe auf die Quelle. Fassen Sie die bekannten Primärschlüssel in einem `MGET` zusammen, um eine begrenzte Batch-Anfrage zu senden. Das eignet sich für wiederholte Abrufe vollständiger Zeilen; beliebige SQL-Abfragen werden dadurch nicht gecacht, und Joins oder Projektionen werden nicht ersetzt.

## Ergebnisvertrag {#know-the-result-contract}

`MGET key [key ...]` liefert ein Array-Element je Eingabeschlüssel und erhält dabei die Reihenfolge. Duplikate bleiben erhalten. Für fehlende Zeilen liefert die Antwort `nil`. Ein Schlüssel hat das Format `CRUD:<db>.<schema>.<table>:<json pk>`; Kodierung und ausführbare Beispiele stehen unter [RESP-Clients](resp.md#key-and-response-contract).

## Wann MGET passt {#when-mget-is-the-right-alternative}

Ein Befehl akzeptiert bis zu 1.024 Schlüssel. Die codierte Antwort ist auf 66.560 Byte begrenzt. Bei großen Zeilen kann daher auch ein Batch mit wenigen Schlüsseln zu groß sein. Teilen Sie Anfragen nach Schlüsselzahl und erwarteter Nutzlast auf. Eine zu große Antwort führt zu einem Fehler statt zu einem unvollständigen Array.

Für Filter, Joins, Zeilensperren, Projektionen und Lesezugriffe innerhalb derselben Transaktion eignet sich gewöhnliches SQL besser. Den Vergleich von Quellabfrage und RESP beschreibt der [Leitfaden zur Cache-Invalidierung](cache-invalidation.md) sowie die [technische Referenz](TECHNICAL.md#transaction-consistency).

Die SQL-Funktion `local_cache.mget(regclass, anyarray)` aus 2.x wurde in 3.0.0 entfernt; siehe den [Upgrade-Leitfaden](UPGRADING.md).
