---
layout: doc
lang: fr
translation_key: batch-primary-key-lookups
title: Recherches PostgreSQL par clé primaire en lots
seo_title: "Recherches PostgreSQL par clé primaire en lots avec ANY et mget"
description: Remplacez les lectures de clés primaires N+1 par une requête PostgreSQL paramétrée, conservez les positions d'entrée si nécessaire et comparez le chemin mget explicite de pg_local_cache.
section: Guides
permalink: /fr/docs/batch-primary-key-lookups.html
last_modified_at: "2026-09-16"
---

# Recherches PostgreSQL par clé primaire en lots {#batch-postgresql-primary-key-lookups}

Si le code applicatif envoie une requête par ID, les allers-retours réseau et
le coût des requêtes peuvent dominer une petite lecture de ligne. Commencez
par essayer une instruction paramétrée :

```sql
SELECT id, value, revision
FROM public.items
WHERE id = ANY($1::bigint[]);
```

Passez les ID comme paramètre de tableau. Gardez la table et les colonnes fixes
dans l'instruction ; ne construisez pas le SQL à partir de chaînes d'ID.
PostgreSQL évalue `ANY` en comparant l'expression de gauche aux éléments du
tableau, comme l'expliquent ses [documents sur les comparaisons de lignes et de tableaux](https://www.postgresql.org/docs/18/functions-comparisons.html#FUNCTIONS-COMPARISONS-ANY-SOME).

## Connaître le contrat de résultat {#know-the-result-contract}

La requête ci-dessus renvoie un ensemble. Elle ne promet pas l'ordre d'entrée,
et un ID dupliqué correspond normalement une seule fois à la ligne de la
table. Les ID absents ne produisent aucune ligne. Une entrée `NULL` ne
correspond pas à une clé primaire non nulle ; un tableau nul ou des éléments
nuls suivent aussi les règles `ANY` à trois valeurs de PostgreSQL. Un tableau
vide ne renvoie aucune ligne.

Si l'appelant a besoin d'un résultat pour chaque position demandée, conservez
explicitement les positions :

```sql
WITH requested AS (
  SELECT key, position
  FROM unnest($1::bigint[]) WITH ORDINALITY AS input(key, position)
)
SELECT requested.position,
       requested.key,
       CASE WHEN items.id IS NULL THEN NULL
            ELSE row_to_json(items)::text END AS row
FROM requested
LEFT JOIN public.items AS items ON items.id = requested.key
ORDER BY requested.position;
```

`WITH ORDINALITY` conserve les doublons et les positions `NULL` ; la jointure
externe gauche renvoie une ligne `row` nulle pour une clé absente. C'est une
bonne référence pour un client qui a besoin d'un alignement explicite.
Consultez [l'exemple node-postgres](node-postgres.md) pour restaurer le même
contrat côté client.

## Quand `mget` est la bonne alternative {#when-mget-is-the-right-alternative}

Pour des lignes complètes par clé primaire, `pg_local_cache` offre une API de
lots explicite et bornée :

```sql
SELECT local_cache.mget(
  'public.items'::regclass,
  $1::bigint[]
) AS rows;
```

Le tableau `text[]` renvoyé conserve l'ordre d'entrée et les doublons. Les
entrées `NULL` et les lignes absentes produisent des éléments `NULL` alignés.
Les appels acceptent au plus 1 024 clés, et la fonction peut contourner ou
manquer le cache selon les règles de transaction, de snapshot, de mapping et
de taille de ligne ; elle revient à PostgreSQL sans changer le contrat de
résultat. Elle renvoie des lignes complètes sérialisées ; utilisez donc `ANY`
ou la requête avec ordinality si vous avez besoin d'une projection, de
jointures, de filtres au-delà de la clé ou d'un lot non borné.

## GraphQL, DataLoader et lectures N+1 {#graphql-dataloader-and-n1-reads}

[DataLoader](https://github.com/graphql/dataloader#batching) regroupe les
chargements individuels en un lot. Sa fonction de lot doit renvoyer une valeur
par clé d'entrée dans le même ordre ; la restauration ci-dessus fournit cette
forme même pour les lignes absentes.

La [mémoïsation par requête de DataLoader](https://github.com/graphql/dataloader#caching-per-request)
est séparée du cache de lignes partagé de PostgreSQL. Créez des loaders pour
chaque requête et supprimez les entrées concernées après les mutations de cette
requête. L'invalidation PostgreSQL ne peut pas vider les valeurs déjà stockées
dans un loader JavaScript. Conservez les contrôles d'autorisation applicatifs ;
`pg_local_cache` ne prend pas en charge les tables RLS.

Lancez le [démarrage rapide](QUICKSTART.md), puis comparez les deux chemins de
lecture dans les [benchmarks](BENCHMARKS.md). La [référence technique](TECHNICAL.md#sql-mget-api)
définit l'API ; le [guide des transactions](cache-invalidation.md) couvre les
écritures.
