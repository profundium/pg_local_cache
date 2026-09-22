---
layout: doc
lang: fr
translation_key: node-postgres
title: Recherches de lignes par lots avec node-postgres
seo_title: "Recherches de lignes PostgreSQL par lots avec node-postgres"
description: Utilisez pg_local_cache 2.0 depuis Node.js avec un tableau bigint paramétré et un transport JSON. Conservez l'ordre et les valeurs nulles, puis comparez avec une requête ANY préparée.
section: Node.js
permalink: /fr/docs/node-postgres.html
last_modified_at: "2026-09-16"
---

# Recherches de lignes par lots avec node-postgres {#batch-row-lookups-with-node-postgres}

Lisez les lignes par clé primaire avec votre connexion ou votre pool
node-postgres existant.

Démarrez la [démo](QUICKSTART.md), installez les dépendances et exécutez ses
assertions d'intégration :

```bash
npm --prefix examples/node-postgres ci --ignore-scripts
npm --prefix examples/node-postgres run demo
```

## Envoyer une requête paramétrée {#send-one-parameterized-query}

Avec un client ou un pool node-postgres connecté :

```js
const result = await client.query({
  name: 'items-mget',
  text: "SELECT array_to_json(local_cache.mget('public.items'::regclass, $1::bigint[])) AS rows",
  values: [[42, 7, 42, null, 999999]],
});
const rows = result.rows[0].rows.map(row =>
  row === null ? null : JSON.parse(row)
);
```

`mget` renvoie `text[]`. `array_to_json` envoie le tableau externe en JSON,
donc node-postgres applique son décodeur JSON. Chaque élément non nul est une
ligne sérialisée qui nécessite `JSON.parse` ; les positions correspondent aux
positions d'entrée, et les clés absentes ou les entrées nulles produisent
`null`.

Gardez le nom de table fixe dans le code applicatif. Passez les ID comme
paramètres de requête, et non dans du SQL assemblé à partir de chaînes.
Consultez la documentation node-postgres sur [les paramètres et les instructions préparées nommées](https://node-postgres.com/features/queries).

Le helper exécutable rejette les lots de plus de 1 024 clés et renvoie `[]`
sans requête pour un lot vide. Il utilise des ID de démo entiers sûrs. Les
champs PostgreSQL `bigint` et `numeric` dans le JSON peuvent dépasser la plage
numérique exacte de JavaScript ; utilisez un parseur JSON sans perte ou un
contrat de sérialisation explicite pour ces valeurs.

## Comparer avec la requête par lots existante {#compare-with-the-existing-batch-query}

La référence utilise :

```sql
SELECT id::text AS key, row_to_json(i)::text AS row
FROM public.items AS i
WHERE id = ANY($1::bigint[]);
```

`ANY` ne conserve ni l'ordre d'entrée ni les positions demandées en double.
L'exemple les restaure côté client et fournit une valeur nulle pour les lignes
absentes avant de comparer les résultats.

L'implémentation exécutable se trouve dans
[examples/node-postgres](https://github.com/profundium/pg_local_cache/tree/master/examples/node-postgres).
Le helper reçoit un client existant au lieu de créer un pool par appel.

## Transactions et limites applicatives {#transactions-and-application-boundaries}

Utilisez un même client acquis pendant toute une transaction. Les lectures
après des écritures dans la même transaction utilisent le chemin de la table
source PostgreSQL. La démo le vérifie avec des connexions de lecture et
d'écriture séparées ; consultez [l'invalidation du cache](cache-invalidation.md).

## Instructions préparées et cache des résultats {#prepared-statements-and-result-caching}

Une requête node-postgres nommée réutilise une instruction préparée sur chaque
connexion. Elle ne met pas en cache les lignes renvoyées. `local_cache.mget`
ajoute un cache partagé séparé de lignes complètes dans PostgreSQL ; le client
envoie toujours une requête et décode son résultat. Consultez le [guide de décision sur la mise en cache](postgresql-caching.md) pour comparer les
couches et le [guide des recherches par lots](batch-primary-key-lookups.md)
pour une alternative SQL uniquement qui conserve les positions demandées.

Pour RESP2, utilisez [l'exemple RESP Node.js](resp.md#nodejs).
[Les résultats Node.js enregistrés](benchmarks-node.md) incluent des lectures
par lots et des mises à jour concurrentes. Le [benchmark commun](BENCHMARKS.md#run-the-same-comparison-on-every-client)
exécute Node.js et Go dans les mêmes scénarios SQL et RESP.
