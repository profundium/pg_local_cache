---
layout: post
lang: fr
translation_key: blog-ordered-batch-reads
title: "Lectures PostgreSQL par lots sans perdre l'ordre ni les clés absentes"
description: Remplacez les requêtes de clés primaires N+1 en conservant les ID dupliqués, l'ordre d'entrée, les positions NULL et les lignes absentes. Comparez ANY, WITH ORDINALITY et SQL mget.
permalink: /fr/blog/ordered-batch-reads/
date: "2026-09-22"
last_modified_at: "2026-09-22"
topic: application
---

# Les lectures par lots ont besoin d'un contrat de résultat {#batch-reads-need-a-result-contract}

Remplacer une boucle de requêtes par clé primaire par une seule requête `ANY`
supprime des allers-retours. Cela peut aussi modifier la forme de la réponse.
Un appelant peut demander `[42, 7, 42, NULL, -1]` et attendre cinq positions de
résultat. La sémantique des ensembles SQL ne promet pas cet alignement.

## Un ensemble de lignes n'est pas une liste de réponses {#set-versus-list}

Avec `WHERE id = ANY($1::bigint[])`, un ID dupliqué correspond normalement une
seule fois à sa ligne de table. Un ID absent ne contribue à aucune ligne. Une
entrée `NULL` ne correspond pas à une clé primaire non nulle, et le résultat ne
garantit pas l'ordre d'entrée. Ajouter `ORDER BY id` trie par clé ; cela ne
reproduit toujours pas les positions demandées.

Si les consommateurs ont besoin d'un ensemble, cela convient. S'ils ont besoin
d'un résultat pour chaque entrée, faites des positions une partie de la requête
ou restaurez-les dans l'application.

## Garder les positions explicites en SQL {#explicit-positions}

Démarrez la [démo locale](../docs/QUICKSTART.md) et exécutez ceci dans sa
session `psql` :

```sql
WITH requested AS (
  SELECT key, position
  FROM unnest(ARRAY[42, 7, 42, NULL, -1]::bigint[])
       WITH ORDINALITY AS input(key, position)
)
SELECT requested.position,
       requested.key,
       CASE WHEN items.id IS NULL THEN NULL
            ELSE row_to_json(items)::text END AS row
FROM requested
LEFT JOIN public.items AS items ON items.id = requested.key
ORDER BY requested.position;
```

La colonne d'ordinalité distingue les deux occurrences de 42. La jointure
externe gauche conserve les cinq positions, y compris l'entrée nulle et toute
clé absente. Pour une clé manquante, `row` vaut `NULL` SQL. Dans le code
applicatif, passez le tableau comme paramètre plutôt que de concaténer les ID
dans le SQL. [L'exemple Node.js](../docs/node-postgres.md) montre l'alignement
côté client.

## Comparer l'API de lignes complètes {#whole-row-api}

Sur une table attachée, l'appel de cache explicite correspondant est :

```sql
SELECT local_cache.mget(
  'public.items'::regclass,
  ARRAY[42, 7, 42, NULL, -1]::bigint[]
) AS rows;
```

Il renvoie `text[]`, en conservant l'ordre d'entrée et les doublons. Les clés
absentes et les entrées nulles produisent des éléments `NULL` SQL alignés ;
chaque élément présent est une ligne complète sérialisée. Un miss ou un
contournement du cache lit PostgreSQL. L'API accepte au plus 1 024 clés par
appel. Elle ne remplace ni les projections, ni les jointures, ni les verrous de
lignes, ni la mise en cache arbitraire des résultats de requêtes.

## Garder les lots bornés et observables {#bounded-batches}

Au-delà de 1 024 clés, divisez explicitement les requêtes ou gardez une requête
SQL ordinaire. Découper entre plusieurs instructions peut observer des
snapshots `READ COMMITTED` différents ; choisissez délibérément la sémantique
transactionnelle. Les lots plus grands augmentent aussi la taille de la
réponse et le travail de décodage du client ; « moins de requêtes » ne prouve
pas à lui seul une requête plus rapide.

Pour GraphQL, une fonction de lot DataLoader doit renvoyer une réponse par clé
d'entrée dans le même ordre. La mémoïsation locale à la requête et le cache
partagé de PostgreSQL sont des couches séparées ; supprimez les entrées
concernées après les mutations. Consultez le [guide complet des lots](../docs/batch-primary-key-lookups.md)
et comparez la latence, les charges utiles et le débit avec le [runner de benchmarks](../docs/BENCHMARKS.md).
