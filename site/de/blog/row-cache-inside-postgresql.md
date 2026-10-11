---
layout: post
lang: de
translation_key: blog-row-cache-inside-postgresql
title: "Cache-aside ohne Race Condition: Zeilen-Cache in PostgreSQL im Messvergleich"
description: pg_local_cache 3.1.0 hält heiße Zeilen im PostgreSQL Shared Memory und liefert sie per RESP. Vergleich mit Valkey und Prepared SQL ohne veraltete Zeilen.
permalink: /de/blog/row-cache-inside-postgresql/
date: "2026-10-12"
last_modified_at: "2026-10-12"
topic: performance
---

# Cache-aside ohne Race Condition {#cache-aside-without-the-race}

Wenn Lesezugriffe per Primärschlüssel häufiger werden, stellen viele Redis oder
Valkey neben PostgreSQL und verwenden Cache-aside: erst den Cache lesen, bei
einem Fehltreffer PostgreSQL abfragen und den Cache füllen, bei jedem
Schreibvorgang den Schlüssel löschen. Das funktioniert, bis sich Lesen und Schreiben
überschneiden. Ein Leser, der den Schlüssel nicht gefunden hat, kann die Zeile
kurz vor dem Commit des Schreibers abrufen. Dessen `DEL` trifft zuerst ein, das
Füllen des Lesers danach; so bleibt die alte Zeile im Cache, bis sie anderweitig
entfernt wird. Diese [Race Condition beim späten Füllen](cache-aside-late-fill.md)
haben wir bereits beschrieben.

pg_local_cache verlagert den Cache direkt in PostgreSQL. Cache und die
Schreibvorgänge, denen er folgen muss, liegen damit im selben
Transaktionssystem.

## So bleibt der Cache korrekt {#how-it-stays-correct}

- Zeilen einer eingebundenen Tabelle liegen in einem begrenzten Shared Memory,
  sind nach Primärschlüssel indiziert und werden von Hintergrundprozessen über
  RESP2 bereitgestellt. Jeder Redis-Client kann sie per `MGET` lesen; der
  Schlüssel lautet `CRUD:<db>.<schema>.<table>:<json primary key>`.
- Zeilentrigger veröffentlichen vor Sichtbarkeit der Transaktion eine
  Invalidierungsmarke für jeden geschriebenen Schlüssel. Änderungen an der
  ganzen Relation wie `TRUNCATE` veröffentlichen eine Marke für die gesamte
  Tabelle.
- Bei einem Fehltreffer nimmt der Leser zuerst ein Lesetoken. Das Ergebnis wird
  nur gespeichert, wenn zwischenzeitlich keine Marke für diesen Schlüssel, die
  Tabelle oder den gesamten Cache veröffentlicht wurde. Ein Fill, der mit
  einem Schreibvorgang kollidiert, wird verworfen.
- Gleichzeitige Fehltreffer für denselben Schlüssel werden gebündelt: Ein Leser
  lädt die Zeile, die anderen verwenden sein Ergebnis.
- PostgreSQL bleibt die maßgebliche Datenquelle. Der Cache ist flüchtig und
  kann jederzeit verworfen werden.

## Kosten und Nutzen {#results}

Die folgenden Zahlen stammen aus Version 3.1.0 auf zwei VMs mit je 16 vCPUs
(Xeon Gold 6354, 10 GbE), PostgreSQL 16 und einem Go/pgx-Client. Die
[Benchmark-Seite](../docs/BENCHMARKS.md) enthält alle Tabellen, die JSONL-Rohdaten
und die Schritte zur Wiederholung.

**Lesen während des Schreibens.** 64 Leser, ein Schlüssel pro Lesevorgang,
gleichmäßig über 60k Zeilen verteilt, 30,000 `UPDATE`/s:

| Stack | Lesevorgänge/s | Trefferquote | Veraltete Einträge |
| --- | --- | --- | --- |
| pg_local_cache | 161,919 | 0.837 | 0 |
| Valkey Cache-aside | 42,294 | 0.577 | 0 |
| Vorbereitetes SQL | 106,949 | — | — |

Bei unbegrenzt vielen Schreibern endete Valkey Cache-aside mit veralteten
Einträgen (1 im gemischten Lauf; 2 und 1 in den 120-Sekunden-Prüfungen).
pg_local_cache beendete jeden Lauf ohne veraltete Einträge, auch einen einstündigen
Soak-Test mit 252M `MGET` und 72M `UPDATE`.

**Schreibaufwand.** Gegenüber einfachen Tabellenschreibvorgängen lag der
Durchsatz bei `UPDATE` über pg_local_cache um 3.6–8.2% niedriger, bei `UPDATE`
mit anschließendem Valkey-`DEL` um 28.9–63.7%.

**Lese-Durchsatz.** Bei Beschränkung des Servers auf ein vCPU-Paar lieferte
pg_local_cache 210k Lesevorgänge/s gegenüber 180k bei Single-Thread-Valkey; mit
zwei Paaren waren es 337k gegenüber 266k bei Valkey mit I/O-Threads. Bei vier
Paaren erreichten beide einen clientbedingten Höchstwert von etwa 360k; dabei
nutzte pg_local_cache 5.2 Server-vCPUs, Valkey 8.0. Single-Thread-Valkey hatte
den geringsten CPU-Aufwand pro Anfrage. Vorbereitetes SQL lag in den
Einzelschlüsseltests weit zurück.

## Wann es das falsche Werkzeug ist {#when-not-to-use-it}

- Kein Ersatz für Redis: keine TTLs, kein pub/sub und keine Datenstrukturen,
  sondern nur das Lesen ganzer Zeilen per Primärschlüssel.
- Der heiße Datenbestand muss in den konfigurierten Speicher passen; eine Zeile
  ist auf 64 KiB JSON über RESP begrenzt.
- Es bedient eine Datenbank auf einem beschreibbaren Primärserver.
- `shared_preload_libraries` und ein Neustart sind erforderlich.

## Ausprobieren {#try-it}

pg_local_cache unterstützt PostgreSQL 14–18 und steht unter der MIT-Lizenz.
Installieren Sie es über [GitHub Releases](https://github.com/profundium/pg_local_cache/releases)
(deb und rpm), [PGXN](https://pgxn.org/dist/pg_local_cache/) oder die Pigsty-
Repositories und folgen Sie anschließend dem [Schnellstart](../docs/QUICKSTART.md).
