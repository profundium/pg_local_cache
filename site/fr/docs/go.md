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

La démo présente une requête SQL préparée ordinaire comme solution de repli :

```sql
SELECT id::text AS key, row_to_json(items)::text AS row
FROM public.items
WHERE id = ANY($1::bigint[]);
```

L’exemple demande `42, 7, 42, NULL, 999999`, rétablit l’ordre d’entrée en Go et affiche null pour l’entrée nulle et la clé absente. Pour les lectures en cache, utilisez RESP `MGET` ; le point de terminaison utilise le rôle configuré pour les workers et ne fait pas partie de la transaction SQL de l’application.

## Comparer les lignes, pas seulement les allers-retours {#compare-rows-not-just-round-trips}

Une requête ordinaire `WHERE id = ANY($1::bigint[])` ne préserve pas les positions demandées. Rétablissez l’ordre, les doublons et les lignes absentes pour les résultats SQL classiques. Pour les lectures de lignes complètes en cache, utilisez RESP `MGET` ; le [guide des recherches par lots](batch-primary-key-lookups.md) compare les contrats.

Le benchmark utilise des connexions persistantes et des instructions préparées. Préparer le SQL ne met pas en cache ses lignes de résultat : voir le [guide de cache PostgreSQL](postgresql-caching.md). Le [benchmark commun](BENCHMARKS.md#run-the-same-comparison-on-every-client) teste Go et Node.js avec les mêmes clés, tailles de lots, connexions et durées via SQL préparé et RESP `MGET`. RESP ne partage pas la transaction SQL de l’appelant.

Consultez le [démarrage rapide](QUICKSTART.md) pour la configuration et les
[vérifications transactionnelles](cache-invalidation.md) avant d'adapter le
chemin de lecture.
