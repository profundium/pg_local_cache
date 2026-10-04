---
layout: post
lang: fr
translation_key: blog-ordered-batch-reads
title: "Lectures PostgreSQL par lots sans perdre l'ordre ni les clés absentes"
description: Remplacez les requêtes de clés primaires N+1 en conservant les ID dupliqués, l'ordre d'entrée, les positions NULL et les lignes absentes. Comparez ANY, WITH ORDINALITY et RESP MGET.
permalink: /fr/blog/ordered-batch-reads/
date: "2026-09-22"
last_modified_at: "2026-10-04"
topic: application
---

# Les lectures par lots ont besoin d'un contrat de résultat {#batch-reads-need-a-result-contract}

Remplacer une boucle de requêtes par clé primaire par une seule requête `ANY`
supprime des allers-retours. Cela peut aussi modifier la forme de la réponse.
Un appelant peut demander `[42, 7, 42, NULL, -1]` et attendre cinq positions de
résultat. La sémantique des ensembles SQL ne promet pas cet alignement.

> **Note du 04/10/2026 :** l’API SQL de cache de lignes local_cache.mget(regclass, anyarray) de la version 2.x a été supprimée en 3.0.0 ; utilisez RESP MGET pour lire les lignes complètes.

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
dans le SQL. Le [code source de l’assistant Node.js](https://github.com/profundium/pg_local_cache/blob/master/examples/node-postgres/queries.mjs)
montre l’alignement des résultats côté client.

## Comparer l'API de lignes complètes {#whole-row-api}

Pour lire des lignes complètes dans les tables attachées, utilisez RESP MGET. Encodez chaque clé primaire dans une clé de la forme CRUD:<db>.<schema>.<table>:<json pk> :

RESP MGET renvoie un tableau RESP2 dans l’ordre de la requête. Les clés dupliquées gardent leur position ; une ligne absente renvoie nil. Les clés RESP identifient des valeurs de clé primaire et ne portent pas de position SQL NULL dans un tableau. Si chaque entrée SQL, y compris NULL, doit produire un résultat aligné, utilisez la requête SQL avec WITH ORDINALITY présentée plus haut. Une commande accepte au plus 1 024 clés, chaque ligne JSON est limitée à 65 536 octets et la réponse encodée à 66 560 octets. RESP MGET ne remplace ni les projections, ni les jointures, ni les verrous de lignes, ni la mise en cache de résultats arbitraires.

```bash
REDISCLI_AUTH=DemoRespToken_0123456789abcdef0123456789 redis-cli -2 -p 56379 MGET 'CRUD:pglc_demo.public.items:{"id":42}' \
  'CRUD:pglc_demo.public.items:{"id":7}' \
  'CRUD:pglc_demo.public.items:{"id":42}' \
  'CRUD:pglc_demo.public.items:{"id":-1}'
```

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
