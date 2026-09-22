---
layout: doc
lang: fr
translation_key: go
title: Recherches de lignes par lots avec Go et pgx
seo_title: "Recherches de lignes PostgreSQL par lots avec Go et pgx"
description: Utilisez pg_local_cache depuis Go avec pgx, des clés paramétrées et des lignes JSON décodées.
section: Go
permalink: /fr/docs/go.html
last_modified_at: "2026-09-16"
---

# Recherches de lignes par lots avec Go et pgx {#batch-row-lookups-with-go-and-pgx}

Commencez par la [base éphémère](QUICKSTART.md), puis exécutez :

```bash
go -C examples/go-pgx run ./demo
```

Elle utilise `127.0.0.1:55432`, la base `pglc_demo` et les identifiants
`demo` / `demo-only`. Définissez `PGLC_DEMO_PORT` si le démarrage rapide
utilise un autre port.

La démo envoie les clés comme paramètre de requête :

```sql
SELECT local_cache.mget('public.items'::regclass, $1::bigint[]);
```

`mget` renvoie `text[]` ; chaque élément non nul est une ligne JSON. L'exemple
demande `42, 7, 42, NULL, 999999` et affiche les lignes dans l'ordre d'entrée.
Le doublon `42` reste aux deux positions ; l'entrée nulle et `999999` absent
produisent des éléments nuls.

## Comparer les lignes, pas seulement les allers-retours {#compare-rows-not-just-round-trips}

Une requête ordinaire `WHERE id = ANY($1::bigint[])` ne conserve pas les
positions demandées. Restaurez l'ordre d'entrée, les doublons et les lignes
absentes avant de la comparer à `mget` ; le [guide des recherches par lots](batch-primary-key-lookups.md)
montre les approches côté client et SQL.

Le benchmark utilise des connexions persistantes et des instructions
préparées. Préparer le SQL ne met pas ses lignes de résultat en cache :
consultez le [guide de mise en cache PostgreSQL](postgresql-caching.md). Le
[benchmark commun](BENCHMARKS.md#run-the-same-comparison-on-every-client) teste
Go et Node.js avec les mêmes clés, tailles de lots, nombres de connexions et
durée via SQL et RESP. RESP ne partage pas la transaction SQL de l'appelant.

Consultez le [démarrage rapide](QUICKSTART.md) pour la configuration et les
[vérifications transactionnelles](cache-invalidation.md) avant d'adapter le
chemin de lecture.
